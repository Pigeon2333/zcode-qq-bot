# -*- coding: utf-8 -*-
"""冒烟：qq_mcp / qq_mcp_server 离线回归。
覆盖：总开关闸门（mcp_allowed/tool_index/call_tool）、SSE POST 鉴权头、
收件箱目录三级解析、发送预检+投递回执（假 daemon，端口 0）、凭据打码。
不连 ws、不碰真实端口（39763/4/5）、不读写 ~/.zcode-qq-bot（QQBOT_DATA_DIR
在 import 前就指向临时目录）。"""
import ast
import http.client
import http.server
import json
import os
import queue
import shutil
import socket
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(BASE, "plugins", "qq-bot", "scripts")
TMP = tempfile.mkdtemp(prefix="smoke_mcp_")
# 必须 import 前设置：qq_mcp_server / qq_common 都在 import 时读它
os.environ["QQBOT_DATA_DIR"] = TMP
sys.path.insert(0, SCRIPTS)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import qq_mcp as M          # noqa: E402
import qq_mcp_server as S   # noqa: E402
S.DATA_DIR = TMP            # 双保险：模块属性也指向临时目录

TOK = uuid.uuid4().hex[:12]          # 运行时生成的假令牌（不写凭据字面量）
OLD_INBOX_ENV = os.environ.get("QQBOT_TASK_INBOX")
fails = []
passed = 0


def chk(name, cond):
    global passed
    print(("PASS " if cond else "FAIL ") + name)
    if cond:
        passed += 1
    else:
        fails.append(name)


def set_mcp_json(cfg):
    """把 mcp.json 指到临时文件并清掉进程内缓存。"""
    M.MCP_JSON = os.path.join(TMP, "mcp.json")
    Path(M.MCP_JSON).write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    M._cfg_cache["data"] = None
    M._cfg_cache["ts"] = 0


def set_app_cfg(cfg):
    Path(os.path.join(TMP, "app_config.json")).write_text(
        json.dumps(cfg, ensure_ascii=False), encoding="utf-8")


# ============ A. qq_mcp 总开关闸门 ============
chk("A1 mcp_allowed 纯函数: enabled=false+服务on → False",
    M.mcp_allowed({"enabled": False, "servers": {"a": {"on": True}}}) is False)
chk("A2 mcp_allowed 纯函数: enabled=true+全部off → False",
    M.mcp_allowed({"enabled": True, "servers": {"a": {"on": False}}}) is False)
chk("A3 mcp_allowed 纯函数: enabled=true+某服务on → True",
    M.mcp_allowed({"enabled": True, "servers": {"a": {"on": True}}}) is True)
chk("A4 mcp_allowed 纯函数: 缺 servers 键 → False",
    M.mcp_allowed({"enabled": True}) is False)

set_mcp_json({"enabled": False, "servers": {"a": {"on": True}}})
chk("A5 enabled=false+某服务on: mcp_allowed()=False", M.mcp_allowed() is False)
rows, errors = M.tool_index()
chk("A6 enabled=false+某服务on: tool_index 返回空结果",
    rows == [] and errors == [])
try:
    M.call_tool("a", "t", {})
    rejected = False
except M.McpError:
    rejected = True
chk("A7 enabled=false+某服务on: call_tool 抛 McpError 拒绝", rejected)

set_mcp_json({"enabled": True, "servers": {}})
chk("A8 enabled=true+无服务on: mcp_allowed()=False", M.mcp_allowed() is False)
rows2, errors2 = M.tool_index()
chk("A9 enabled=true+无服务on: tool_index 返回空结果",
    rows2 == [] and errors2 == [])
try:
    M.call_tool("a", "t", {})
    rejected2 = False
except M.McpError:
    rejected2 = True
chk("A10 enabled=true+无服务on: call_tool 抛 McpError 拒绝", rejected2)

set_mcp_json({"enabled": True, "servers": {"a": {"on": True}}})
chk("A11 enabled=true+某服务on: mcp_allowed()=True", M.mcp_allowed() is True)


class _StubClient:
    def tools(self):
        return [{"name": "do_thing", "description": "  does  stuff ",
                 "inputSchema": {"required": ["a"]}}]


_ets, _gc = M.enabled_tool_servers, M.get_client
M.enabled_tool_servers = lambda: [("fake", {})]
M.get_client = lambda name: _StubClient()
try:
    rows3, errors3 = M.tool_index()
finally:
    M.enabled_tool_servers, M.get_client = _ets, _gc
chk("A12 enabled=true+某服务on: tool_index 照常出索引行",
    len(rows3) == 1 and "fake.do_thing" in rows3[0] and "必填参数：a" in rows3[0]
    and errors3 == [])

# ============ B. SSE 传输 POST 带配置鉴权头 ============
class _CapConn:
    captured = {}

    def __init__(self, host, port, timeout=None):
        _CapConn.captured = {"host": host, "port": port}

    def request(self, method, path, body=None, headers=None):
        _CapConn.captured.update({"method": method, "path": path,
                                  "headers": dict(headers or {})})

    def getresponse(self):
        return type("R", (), {"read": lambda s, *a: b"",
                              "getheader": lambda s, k, d=None: None})()

    def close(self):
        pass


cli = M.McpClient("sse1", {"type": "sse", "url": "http://127.0.0.1:9/sse",
                           "headers": {"Authorization": "Bearer " + TOK,
                                       "X-Tag": "t1"}})
cli._sse_alive = True
cli._sse_post = "http://127.0.0.1:9/message?sid=abc"
cli._sse_q = queue.Queue()
cli._sse_q.put({"id": 1, "result": {}})
_orig_conn = http.client.HTTPConnection
http.client.HTTPConnection = _CapConn
try:
    cli._sse_rw({"jsonrpc": "2.0", "id": 1, "method": "x"}, 2, want_id=1)
finally:
    http.client.HTTPConnection = _orig_conn
cap = _CapConn.captured
chk("B1 SSE POST 方法与路径正确",
    cap.get("method") == "POST" and cap.get("path") == "/message?sid=abc")
chk("B2 SSE POST 头带配置的鉴权头 Authorization",
    cap.get("headers", {}).get("Authorization") == "Bearer " + TOK)
chk("B3 SSE POST 头带配置的全部 headers（X-Tag）+ Content-Type/Length",
    cap.get("headers", {}).get("X-Tag") == "t1"
    and cap.get("headers", {}).get("Content-Type") == "application/json"
    and "Content-Length" in cap.get("headers", {}))

# ============ C. 收件箱目录解析 ============
inbox_a = os.path.join(TMP, "inbox_env")
inbox_b = os.path.join(TMP, "inbox_cfg")
try:
    os.environ["QQBOT_TASK_INBOX"] = inbox_a
    chk("C1 QQBOT_TASK_INBOX 非空 → 用环境变量", S._inbox_dir() == inbox_a)
finally:
    os.environ.pop("QQBOT_TASK_INBOX", None)
set_app_cfg({"task_inbox_dir": inbox_b})
chk("C2 无环境变量、app_config.json 有 task_inbox_dir → 用配置",
    S._inbox_dir() == inbox_b)
set_app_cfg({})
chk("C3 两者都没有 → DATA_DIR/tasks/zcode-tasks",
    S._inbox_dir() == os.path.join(S.DATA_DIR, "tasks", "zcode-tasks"))
src_server = Path(os.path.join(SCRIPTS, "qq_mcp_server.py")).read_text(
    encoding="utf-8")
chk("C4 源码不再有开发机硬编码路径 qq-bot\\zcode-tasks",
    "D:\\ZCode\\workspaces\\qq-bot" not in src_server)

# ============ D. 发送预检 + 投递回执（假 daemon，端口 0） ============
class _FakeDaemon(http.server.BaseHTTPRequestHandler):
    state = {"sent": 100, "inc": True, "broken_status": False}
    errors = []

    def log_message(self, *a):
        pass

    def handle_error(self, request, client_address):
        _FakeDaemon.errors.append("handler error")

    def _reply(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")   # 不留 keep-alive 悬连
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/status":
            if _FakeDaemon.state["broken_status"]:
                body = b"<html>not json</html>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._reply({"ok": True, "ws_connected": True,
                             "stats": {"sent": _FakeDaemon.state["sent"]}})
        else:
            self._reply({"ok": False, "error": "no such path"})

    def do_POST(self):
        if self.path == "/send":
            if _FakeDaemon.state["inc"]:
                _FakeDaemon.state["sent"] += 1
            self._reply({"ok": True, "queued": 1})
        else:
            self._reply({"ok": False, "error": "no such path"})


_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeDaemon)
_srv.daemon_threads = True
FAKE_PORT = _srv.server_address[1]
_thd = threading.Thread(target=_srv.serve_forever, daemon=True)
_thd.start()
# 一个确定没有监听的端口（bind 后立刻关掉）
_s = socket.socket()
_s.bind(("127.0.0.1", 0))
DEAD_PORT = _s.getsockname()[1]
_s.close()

try:
    # D1: daemon 连不上 → ok=false 中文原因，不去调 /send
    set_app_cfg({"daemon_port": DEAD_PORT})
    r = S.t_qq_send({"chat_type": "group", "chat_id": "123", "text": "hi"})
    chk("D1 daemon 连不上: ok=false 且报「机器人服务没有运行」，无 queued",
        r.get("ok") is False and "机器人服务没有运行" in str(r.get("error"))
        and "queued" not in r)

    # D2: queued=1 且 stats.sent 增加 → delivered=true，原字段保留
    _FakeDaemon.state.update(sent=100, inc=True, broken_status=False)
    set_app_cfg({"daemon_port": FAKE_PORT})
    r2 = S.t_qq_send({"chat_type": "group", "chat_id": "123", "text": "hi"})
    chk("D2 sent 增加: delivered=true 且 ok/queued 原字段含义不变",
        r2.get("delivered") is True and r2.get("ok") is True
        and r2.get("queued") == 1 and isinstance(r2.get("waited_sec"), float)
        and r2["waited_sec"] <= 2)

    # D3: queued=1 但 sent 一直不变 → delivered=false，waited_sec 约 8
    _FakeDaemon.state.update(sent=100, inc=False)
    r3 = S.t_qq_send({"chat_type": "private", "chat_id": "1", "text": "x"})
    chk("D3 sent 不变: delivered=false 且 waited_sec 约 8",
        r3.get("delivered") is False and 7.0 <= r3.get("waited_sec", 0) <= 10.0)

    # D4: 调用前拿不到 stats.sent（/status 返回非 JSON）→ delivered="unknown"
    _FakeDaemon.state.update(sent=100, inc=True, broken_status=True)
    r4 = S.t_qq_send({"chat_type": "group", "chat_id": "123", "text": "hi"})
    chk("D4 拿不到基线: delivered='unknown' 且不空等（waited_sec≈0）",
        r4.get("delivered") == "unknown" and r4.get("waited_sec", 9) <= 1.0
        and r4.get("queued") == 1)

    # D5: qq_notify 预检：daemon 连不上 → ok=false，不调 /send
    set_app_cfg({"daemon_port": DEAD_PORT, "zcode_bridge_enabled": True,
                 "zcode_notify_enabled": True, "owner_qq": "10001"})
    _zi = S._zcode_installed
    S._zcode_installed = lambda: True
    try:
        r5 = S.t_qq_notify({"text": "n"})
    finally:
        S._zcode_installed = _zi
    chk("D5 qq_notify daemon 连不上: ok=false 报「机器人服务没有运行」",
        r5.get("ok") is False and "机器人服务没有运行" in str(r5.get("error")))

    # ============ E. 凭据打码 ============
    set_app_cfg({"daemon_port": FAKE_PORT})
    m1 = S._mask_secrets({"api_token": TOK, "name": "x",
                          "note": "token=" + TOK + " tail"})
    chk("E1 键名含 token → ***；普通键不变；值里 token=xxx 打码",
        m1["api_token"] == "***" and m1["name"] == "x"
        and m1["note"] == "token=*** tail")
    m2 = S._mask_secrets({"api_key": ""})
    chk("E2 空串凭据字段保持空串", m2["api_key"] == "")
    m3 = S._mask_secrets({"AccessToken": TOK, "items": [{"Secret": TOK}],
                          "auth_header": "Bearer " + TOK})
    chk("E3 大小写不敏感 + 嵌套 list + auth 键",
        m3["AccessToken"] == "***" and m3["items"][0]["Secret"] == "***"
        and m3["auth_header"] == "***")
    m4 = S._mask_secrets({"cfg": '"token": "' + TOK + '", ok'})
    chk("E4 字符串里 \"token\": \"xxx\" 片段打码",
        m4["cfg"] == '"token": "***", ok')
    src_obj = {"api_token": TOK}
    S._mask_secrets(src_obj)
    chk("E5 纯函数：不改输入", src_obj["api_token"] == TOK)

    # t_qq_logs：打桩数据源，检查每行都过打码
    _req_orig = S._req
    S._req = lambda *a, **k: {"ok": True, "lines": [
        {"text": "Authorization: Bearer " + TOK}, {"text": "plain log line"}]}
    try:
        lg = S.t_qq_logs({"n": 40})
    finally:
        S._req = _req_orig
    chk("E6 qq_logs 每行都打码（Bearer 值 → ***，普通行不动）",
        lg["lines"][0] == "Authorization: Bearer ***"
        and lg["lines"][1] == "plain log line")

    # t_qq_config_get app 分支：既有 key 过滤保留，token/password 打码
    _lc = S._load_cfg
    S._load_cfg = lambda: {"api_token": TOK, "password": TOK,
                           "nickname": "x", "db_key": TOK}
    try:
        cg = S.t_qq_config_get({"kind": "app"})
    finally:
        S._load_cfg = _lc
    chk("E7 config_get(app): token/password 打码、db_key 仍被既有过滤丢掉、普通键保留",
        cg["config"].get("api_token") == "***"
        and cg["config"].get("password") == "***"
        and cg["config"].get("nickname") == "x"
        and "db_key" not in cg["config"])

    # t_qq_config_get reply 分支：整包过打码
    S._req = lambda *a, **k: {"ok": True, "tmpl": "Authorization: Bearer " + TOK,
                              "name": "y"}
    try:
        cg2 = S.t_qq_config_get({"kind": "reply"})
    finally:
        S._req = _req_orig
    chk("E8 config_get(reply): 值里 Bearer 片段打码、普通键保留",
        cg2["config"].get("tmpl") == "Authorization: Bearer ***"
        and cg2["config"].get("name") == "y" and cg2.get("ok") is True)
finally:
    _srv.shutdown()
    _srv.server_close()
    _thd.join(2)

# ============ F. 不变量 ============
# 用 AST 判定（注释里写着「不 import qq_common」的原因说明，不能拿子串搜）
_tree = ast.parse(src_server)
_imports_qc = [n for n in ast.walk(_tree)
               if (isinstance(n, ast.Import)
                   and any(a.name == "qq_common" for a in n.names))
               or (isinstance(n, ast.ImportFrom) and n.module == "qq_common")]
chk("F1 qq_mcp_server.py 不 import qq_common", not _imports_qc)
defs = {t["name"]: t["description"] for t in S.TOOL_DEFS}
chk("F2 TOOL_DEFS qq_send 描述带投递语义说明",
    "ok=true 只表示已入队" in defs.get("qq_send", "")
    and "delivered=true 才表示已发出" in defs.get("qq_send", ""))
chk("F3 假 daemon 线程已收尾", not _thd.is_alive())
chk("F4 假 daemon 服务端无处理错误", not _FakeDaemon.errors)

print()
print("%d 项通过，%d 项失败" % (passed, len(fails)))
if OLD_INBOX_ENV is not None:
    os.environ["QQBOT_TASK_INBOX"] = OLD_INBOX_ENV
else:
    os.environ.pop("QQBOT_TASK_INBOX", None)
shutil.rmtree(TMP, ignore_errors=True)   # 清理本脚本自建的临时目录（精确路径）
sys.exit(1 if fails else 0)
