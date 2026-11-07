# -*- coding: utf-8 -*-
"""MCP 服务桥：机器人本体作为 MCP 客户端直连 MCP 服务器。

设计（2026-11 用户拍板：内置不做插件，复用 ZCode 已装服务）：
  - 配置两个来源：
      1) ZCode 已装服务（~/.zcode/cli/config.json 的 mcp.servers）——零重复
         安装：http 型直连同一在跑实例；stdio 型按同 command/args 自己拉起
         一份进程（stdio 管道是父进程私有的，跨进程无法附加，只能复用安装）
      2) 本应用自定义 http 服务（DATA_DIR/mcp.json 的 custom 段）
  - 传输支持：stdio（换行分隔 JSON-RPC）+ http（Streamable HTTP）。
    sse 旧传输 v1 不支持（UI 标注；服务方可升级 http）。
  - 安全默认：总开关关；每服务独立开关默认关；仅主人可触发（agent 侧闸门）。
    ZCode 配置里的鉴权头（Bearer 等）只引用不复制存储、不在 UI 展示。

MCP 本质是 JSON-RPC 2.0：initialize → initialized → tools/list → tools/call。
零第三方依赖（subprocess + http.client + queue 均为标准库）。
"""
import http.client
import itertools
import json
import os
import pathlib
import queue
import re
import subprocess
import threading
import time
import urllib.parse

import qq_common as C

MCP_JSON = os.path.join(C.DATA_DIR, "mcp.json")
ZCODE_CONFIG = os.path.join(os.path.expanduser("~"), ".zcode", "cli", "config.json")
_PROTO = "2025-03-26"          # MCP 协议版本（服务器会回自己支持的版本，兼容即可）
_INIT_TIMEOUT = 20             # 握手/列表超时（秒）
_CALL_TIMEOUT = 60             # 单次工具调用超时（秒）
_ids = itertools.count(1)

_CLIENTS = {}                  # name -> McpClient（agent 进程内常驻，懒启动）
_cfg_cache = {"ts": 0, "data": None}


# ---------------- 配置 ----------------

def mcp_cfg():
    """mcp.json（mtime 签名缓存：agent 每条主人消息都会查一次闸门）。"""
    import os as _os
    try:
        st = _os.stat(MCP_JSON)
        if _cfg_cache["data"] is not None and _cfg_cache["ts"] == (st.st_mtime, st.st_size):
            return _cfg_cache["data"]
        d = json.loads(pathlib.Path(MCP_JSON).read_text(encoding="utf-8"))
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    d.setdefault("enabled", False)
    d.setdefault("servers", {})      # {name: {on:bool, allow_group:bool}}
    d.setdefault("custom", {})       # {name: {type:"http", url, headers}}
    _cfg_cache["data"], _cfg_cache["ts"] = d, None
    try:
        st = os.stat(MCP_JSON)
        _cfg_cache["ts"] = (st.st_mtime, st.st_size)
    except Exception:
        pass
    return d


def save_mcp_cfg(d):
    pathlib.Path(MCP_JSON).write_text(
        json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    _cfg_cache["data"], _cfg_cache["ts"] = d, 0


def load_zcode_servers():
    """读 ZCode 的 mcp.servers；任何异常都静默返回空（没装 ZCode = 无此来源）。"""
    try:
        d = json.loads(pathlib.Path(ZCODE_CONFIG).read_text(encoding="utf-8"))
        sv = ((d.get("mcp") or {}).get("servers") or {})
        return sv if isinstance(sv, dict) else {}
    except Exception:
        return {}


def all_servers():
    """合并两个来源 → [{name, source, type, spec}]（type: stdio/http/sse）。"""
    out = []
    for name, spec in load_zcode_servers().items():
        if isinstance(spec, dict) and spec.get("type"):
            out.append({"name": str(name), "source": "zcode",
                        "type": str(spec.get("type")), "spec": spec})
    cfg = mcp_cfg()
    for name, spec in (cfg.get("custom") or {}).items():
        if isinstance(spec, dict) and spec.get("url"):
            out.append({"name": str(name), "source": "custom",
                        "type": "http", "spec": spec})
    return out


def server_on(name):
    return bool((mcp_cfg()["servers"].get(name) or {}).get("on"))


def ready():
    """agent 闸门：总开关开 + 至少一个已启用的可传输服务（stdio/http）。"""
    if not mcp_cfg().get("enabled"):
        return False
    return any(server_on(s["name"]) and s["type"] in ("stdio", "http")
               for s in all_servers())


def enabled_tool_servers():
    """已启用且传输支持的 (name, spec) 列表。"""
    return [(s["name"], s["spec"]) for s in all_servers()
            if server_on(s["name"]) and s["type"] in ("stdio", "http")]


# ---------------- 客户端 ----------------

class McpError(Exception):
    pass


class McpClient:
    """单个 MCP 服务器的客户端（stdio 常驻进程 / http 按请求连接）。"""

    def __init__(self, name, spec):
        self.name = str(name)
        self.kind = str(spec.get("type") or "")
        self.spec = spec
        self._proc = None
        self._q = None
        self._wr = threading.Lock()
        self._sid = ""            # http 会话 id（服务器 Mcp-Session-Id 响应头）
        self._tools = None
        self._bad = ""            # 不可用原因（上次失败）

    # ---- 底层收发 ----
    def _next_id(self):
        return next(_ids)

    def _stdio_proc(self):
        if self._proc and self._proc.poll() is None:
            return self._proc
        cmd = [str(self.spec.get("command") or "")]
        if not cmd[0]:
            raise McpError("stdio 服务器缺 command")
        cmd += [str(a) for a in (self.spec.get("args") or [])]
        env = dict(os.environ)
        for k, v in (self.spec.get("env") or {}).items():
            env[str(k)] = str(v)
        try:
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                errors="replace", env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            raise McpError("启动失败：%s" % e)
        q = queue.Queue()
        self._q = q

        def _reader(p, box):
            try:
                for line in p.stdout:
                    box.put(line)
            except Exception:
                pass
        threading.Thread(target=_reader, args=(self._proc, q), daemon=True).start()
        return self._proc

    def _stdio_rw(self, payload, timeout, want_id=None):
        p = self._stdio_proc()
        with self._wr:
            try:
                p.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                p.stdin.flush()
            except Exception as e:
                raise McpError("写管道失败（进程可能已退出）：%s" % e)
        if want_id is None:          # 通知：不等待响应
            return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                line = self._q.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                break
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("id") == want_id:
                return obj
        raise McpError("响应超时（%ds）" % timeout)

    def _http_post(self, payload, timeout, want_id=None):
        u = urllib.parse.urlsplit(str(self.spec.get("url") or ""))
        if u.scheme not in ("http", "https"):
            raise McpError("url 非法")
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        for k, v in (self.spec.get("headers") or {}).items():
            headers[str(k)] = str(v)
        if self._sid:
            headers["Mcp-Session-Id"] = self._sid
        conn_cls = http.client.HTTPSConnection if u.scheme == "https" \
            else http.client.HTTPConnection
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "https" else 80)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        conn = conn_cls(host, port, timeout=timeout)
        try:
            conn.request("POST", path, body=body, headers=headers)
            r = conn.getresponse()
            sid = r.getheader("Mcp-Session-Id")
            if sid:
                self._sid = sid
            raw = r.read().decode("utf-8", "replace")
            if r.status >= 400:
                raise McpError("HTTP %d：%.160s" % (r.status, raw))
        finally:
            conn.close()
        if want_id is None:
            return None             # 通知：2xx 即可
        ctype = r.getheader("Content-Type") or ""
        if "text/event-stream" in ctype:
            # Streamable HTTP 的 SSE 响应：扫 data: 行找同 id 的 JSON-RPC 响应
            for ln in raw.splitlines():
                ln = ln.strip()
                if ln.startswith("data:"):
                    try:
                        obj = json.loads(ln[5:].strip())
                    except ValueError:
                        continue
                    if obj.get("id") == want_id:
                        return obj
            raise McpError("SSE 响应里没有找到结果")
        try:
            obj = json.loads(raw)
        except ValueError:
            raise McpError("响应不是 JSON：%.120s" % raw)
        if isinstance(obj, list):   # 批量响应：找同 id
            for o in obj:
                if isinstance(o, dict) and o.get("id") == want_id:
                    return o
            raise McpError("批量响应里没有找到结果")
        return obj

    def _rpc(self, method, params, timeout=_INIT_TIMEOUT):
        rid = self._next_id()
        payload = {"jsonrpc": "2.0", "id": rid, "method": method,
                   "params": params or {}}
        if self.kind == "stdio":
            obj = self._stdio_rw(payload, timeout, want_id=rid)
        elif self.kind == "http":
            obj = self._http_post(payload, timeout, want_id=rid)
        else:
            raise McpError("不支持的传输类型：%s" % self.kind)
        if obj is None:
            raise McpError("空响应")
        if obj.get("error"):
            raise McpError("%.200s" % (obj["error"].get("message") or obj["error"]))
        return obj.get("result") or {}

    def _notify(self, method):
        payload = {"jsonrpc": "2.0", "method": method}
        if self.kind == "stdio":
            self._stdio_rw(payload, 5, want_id=None)
        elif self.kind == "http":
            try:
                self._http_post(payload, 8, want_id=None)
            except McpError:
                pass      # 部分服务器对通知返回非 JSON，忽略

    # ---- 生命周期 ----
    def ensure(self):
        """initialize + tools/list（工具清单缓存）。失败标记 _bad 供 UI 展示。"""
        if self._tools is not None:
            return self
        res = self._rpc("initialize", {
            "protocolVersion": _PROTO, "capabilities": {},
            "clientInfo": {"name": "zcode-qq-bot", "version": str(C.APP_VERSION)}})
        self._notify("notifications/initialized")
        tools = (self._rpc("tools/list", {}) or {}).get("tools") or []
        self._tools = [t for t in tools if isinstance(t, dict) and t.get("name")]
        return self

    def tools(self):
        self.ensure()
        return self._tools or []

    def call(self, tool, args, timeout=_CALL_TIMEOUT):
        """tools/call → 拼接 text 内容（isError 的结果如实返回文本）。"""
        self.ensure()
        res = self._rpc("tools/call", {"name": str(tool), "arguments": args or {}},
                        timeout=timeout)
        parts = []
        for c in (res.get("content") or []):
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(str(c.get("text") or ""))
        txt = "\n".join(parts).strip()
        if res.get("isError"):
            txt = ("工具报错：" + txt) if txt else "工具报错（无文字结果）"
        return txt or ("（无文字结果；结构化字段 %d 个）"
                       % len([c for c in (res.get("content") or []) if isinstance(c, dict)]))

    def close(self):
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass
        self._proc = None


def get_client(name):
    """常驻客户端（agent 进程内缓存；toggle 关闭时由 reset 清掉）。"""
    if name in _CLIENTS:
        return _CLIENTS[name]
    for s in all_servers():
        if s["name"] == name and s["type"] in ("stdio", "http"):
            c = McpClient(name, s["spec"])
            _CLIENTS[name] = c
            return c
    return None


def reset_client(name):
    c = _CLIENTS.pop(name, None)
    if c:
        c.close()


# ---------------- 高层入口（agent / UI 共用） ----------------

def call_tool(server, tool, args, timeout=_CALL_TIMEOUT):
    c = get_client(server)
    if c is None:
        raise McpError("服务器不存在或未启用：%s" % server)
    return c.call(tool, args, timeout=timeout)


def tool_index(limit_per_server=20, doc_cap=4000):
    """agent 提示词用工具索引：[(server, tool, desc, 必填参数)]。
    单服务超时/失败就跳过（日志见返回的 errors）。总长度受 doc_cap 约束。"""
    rows, errors, used = [], [], 0
    for name, _spec in enabled_tool_servers():
        try:
            c = get_client(name)
            if c is None:
                continue
            tools = c.tools()
        except McpError as e:
            errors.append("%s: %s" % (name, e))
            reset_client(name)
            continue
        for t in tools[:limit_per_server]:
            desc = re.sub(r"\s+", " ", str(t.get("description") or ""))[:80]
            req = ",".join(str(x) for x in
                           ((t.get("inputSchema") or {}).get("required") or []))[:60]
            row = "- %s.%s — %s%s" % (name, t.get("name"), desc,
                                      ("（必填参数：%s）" % req) if req else "")
            if used + len(row) > doc_cap:
                return rows, errors
            rows.append(row)
            used += len(row) + 1
    return rows, errors


def test_server(name, timeout=_INIT_TIMEOUT):
    """UI 连接测试：独立临时客户端（测完即关，不污染常驻缓存）。"""
    spec = None
    for s in all_servers():
        if s["name"] == name:
            spec = s["spec"]
            if s["type"] not in ("stdio", "http"):
                return {"ok": False, "error": "暂不支持 %s 传输（SSE 旧传输 v1 未接）"
                        % s["type"].upper()}
    if spec is None:
        return {"ok": False, "error": "服务器不存在"}
    c = McpClient(name, spec)
    try:
        tools = c.tools()
        return {"ok": True, "count": len(tools),
                "tools": [{"name": str(t.get("name")),
                           "desc": re.sub(r"\s+", " ", str(t.get("description") or ""))[:100]}
                          for t in tools[:50]]}
    except McpError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": "%r" % e}
    finally:
        c.close()
