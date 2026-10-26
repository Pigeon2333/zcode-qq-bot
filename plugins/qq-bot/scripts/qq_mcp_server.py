#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qq_mcp_server.py — ZCode 插件的 MCP 服务器（stdio / JSON-RPC 2.0）

让 ZCode 会话里的 AI 以结构化工具操控 QQ 机器人：
  qq_status / qq_logs / qq_send / qq_notify / qq_inject
  qq_config_get / qq_config_set / qq_memory_add
  qq_persona_list / qq_persona_switch / qq_task_done / qq_sticker_add

安全模型：
  - 仅与本机回环的 daemon/agent/管理台通信，token 来自 ~/.zcode-qq-bot/token
  - 通知（qq_notify）受 总开关+zcode_notify_enabled 门控
  - 素材入库受 zcode_sticker_enabled 门控；其余为只读或已有自身权限体系
"""

import base64
import http.client
import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qq_common as C  # noqa: E402

DATA_DIR = C.DATA_DIR
TOOLS = []


def _token():
    try:
        return open(os.path.join(DATA_DIR, "token"), "r", encoding="utf-8").read().strip()
    except OSError:
        return ""


def _ports():
    cfg = C.load_app_config()
    return int(cfg.get("daemon_port") or 39763), int(cfg.get("agent_admin_port") or 39764), \
        int(cfg.get("ui_port") or 39765)


def _req(port, method, path, body=None, timeout=30):
    tok = _token()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if tok:
        headers["X-QQBot-Token"] = tok
    conn.request(method, path, body=payload, headers=headers)
    raw = conn.getresponse().read().decode("utf-8", "replace")
    conn.close()
    try:
        return json.loads(raw)
    except ValueError:
        return {"ok": False, "error": raw[:200]}


def _zcode_on(key):
    cfg = C.load_app_config()
    return bool(cfg.get("zcode_bridge_enabled")) and C.zcode_installed() and bool(cfg.get(key))


def _owner():
    return str(C.load_app_config().get("owner_qq") or "").strip()


def _inbox_dir():
    return os.path.join(r"D:\ZCode\workspaces\qq-bot\zcode-tasks") \
        if os.path.isdir(r"D:\ZCode\workspaces\qq-bot") \
        else os.path.join(DATA_DIR, "tasks", "zcode-tasks")


# ---------------- 工具实现 ----------------

def t_qq_status(a):
    _d, ag, _u = _ports()
    d = _req(_d, "GET", "/status")
    g = _req(ag, "GET", "/admin/status")
    return {"ok": True, "daemon": {
        "running": d.get("ok"), "ws_connected": d.get("ws_connected"),
        "login": d.get("login"), "stats": d.get("stats")},
        "agent": {"paused": g.get("paused"), "current_model": g.get("current_model"),
                  "processed": g.get("processed"), "errors": g.get("errors"),
                  "cooldowns": g.get("cooldowns")}}


def t_qq_logs(a):
    n = max(5, min(200, int(a.get("n") or 40)))
    _d, ag, ui = _ports()
    r = _req(ui, "GET", "/api/runlog?src=all")
    lines = (r.get("lines") or [])[-n:]
    return {"ok": True, "lines": [str(x.get("text", x))[:200] for x in lines]}


def t_qq_send(a):
    _d, _ag, _u = _ports()
    r = _req(_d, "POST", "/send", {
        "chat_type": "group" if a.get("chat_type") == "group" else "private",
        "chat_id": str(a.get("chat_id") or ""), "texts": [str(a.get("text") or "")[:2000]]})
    return r


def t_qq_notify(a):
    if not _zcode_on("zcode_notify_enabled"):
        return {"ok": False, "error": "机器人「ZCode 联动 → 通知」未开启（控制台 ZCode 联动页）"}
    owner = _owner()
    if not owner:
        return {"ok": False, "error": "未设置主人 QQ"}
    _d, _ag, _u = _ports()
    return _req(_d, "POST", "/send", {
        "chat_type": "private", "chat_id": owner,
        "texts": ["[ZCode] " + str(a.get("text") or "")[:1500]]})


def t_qq_inject(a):
    _d, _ag, _u = _ports()
    ct = "group" if a.get("chat_type") == "group" else "private"
    ev = {"post_type": "message", "message_type": ct, "time": int(time.time()),
          "self_id": int(C.load_app_config().get("bot_qq") or 0),
          "message_id": "mcp" + uuid.uuid4().hex[:8],
          "sender": {"user_id": int(a.get("user_id") or _owner() or 0),
                     "nickname": a.get("name") or "MCP测试"},
          "message": [{"type": "text", "data": {"text": str(a.get("text") or "")[:1000]}}]}
    if ct == "group":
        ev["group_id"] = int(a.get("chat_id") or 0)
        ev["raw_message"] = ev["message"][0]["data"]["text"]
    else:
        ev["user_id"] = int(a.get("chat_id") or _owner() or 0)
        ev["raw_message"] = ev["message"][0]["data"]["text"]
    return _req(_d, "POST", "/test/inject", {"event": ev})


def t_qq_config_get(a):
    _d, _ag, ui = _ports()
    if a.get("kind") == "app":
        cfg = C.load_app_config()
        return {"ok": True, "config": {k: v for k, v in cfg.items() if "key" not in k.lower()}}
    r = _req(ui, "GET", "/api/reply-config")
    r.pop("ok", None)
    return {"ok": True, "config": r}


def t_qq_config_set(a):
    _d, _ag, ui = _ports()
    return _req(ui, "POST", "/api/reply-config",
                {str(a.get("key")): a.get("value"), "_from": "mcp"})


def t_qq_memory_add(a):
    _d, _ag, ui = _ports()
    return _req(ui, "POST", "/api/memory/add", {
        "scope": "contact" if a.get("scope") == "contact" else "group",
        "id": str(a.get("id") or ""), "text": str(a.get("text") or "")[:300]})


def t_qq_persona_list(a):
    _d, _ag, ui = _ports()
    return _req(ui, "GET", "/api/personas")


def t_qq_persona_switch(a):
    _d, _ag, ui = _ports()
    return _req(ui, "POST", "/api/personas/switch", {"name": str(a.get("name") or "")})


def t_qq_task_done(a):
    """收件箱任务完成回执：写 .result.md 并把原任务改 .done.md（机器人会自动转发主人）。"""
    f = str(a.get("task_file") or "").strip()
    if not f:
        # 未指定则取最早的未完成任务
        d = _inbox_dir()
        try:
            pend = sorted(x for x in os.listdir(d) if x.endswith(".md")
                          and not x.endswith((".done.md", ".result.md")))
            f = pend[0] if pend else ""
        except OSError:
            f = ""
        if not f:
            return {"ok": False, "error": "收件箱没有待回执任务"}
    d = _inbox_dir()
    base = os.path.basename(f)
    stem = base[:-3] if base.endswith(".md") else base
    if not os.path.isfile(os.path.join(d, base)):
        return {"ok": False, "error": "任务文件不存在: %s" % base}
    import pathlib
    try:
        pathlib.Path(os.path.join(d, stem + ".result.md")).write_text(
            str(a.get("result") or "（已完成）")[:4000], encoding="utf-8")
        os.replace(os.path.join(d, base), os.path.join(d, stem + ".done.md"))
        return {"ok": True, "task": stem,
                "note": "已标记完成；机器人会在一分钟内把结果转发到主人 QQ"}
    except OSError as e:
        return {"ok": False, "error": repr(e)}


def t_qq_sticker_add(a):
    """表情包入库：复制图片到 stickers/_zcode 池并登记（视觉描述由机器人自动补）。"""
    if not _zcode_on("zcode_sticker_enabled"):
        return {"ok": False, "error": "机器人「ZCode 联动 → 表情包入库」未开启"}
    src = str(a.get("path") or "").strip()
    if not os.path.isfile(src):
        return {"ok": False, "error": "文件不存在: %s" % src}
    if os.path.getsize(src) > 8 * 1024 * 1024:
        return {"ok": False, "error": "图片超过 8MB"}
    import shutil
    pool_dir = os.path.join(DATA_DIR, "stickers", "_zcode")
    os.makedirs(pool_dir, exist_ok=True)
    name = "zc_" + time.strftime("%m%d%H%M%S") + "_" + uuid.uuid4().hex[:6] \
        + os.path.splitext(src)[1].lower()
    dst = os.path.join(pool_dir, name)
    shutil.copyfile(src, dst)
    # 登记 stickers.json（_zcode 池，desc 留空 → 机器人启动时自动视觉描述）
    sp = os.path.join(DATA_DIR, "stickers.json")
    try:
        data = json.loads(open(sp, "r", encoding="utf-8").read()
                          or "{}") if os.path.isfile(sp) else {}
    except ValueError:
        data = {}
    pool = data.setdefault("_zcode", {"desc": "ZCode 生产素材", "items": []})
    if "items" not in pool or not isinstance(pool["items"], list):
        pool["items"] = []
    pool["items"].append({"file": "_zcode/" + name, "desc": str(a.get("desc") or ""),
                          "tags": ["ZCode"], "uses": 0, "added": int(time.time())})
    import pathlib
    tmp = sp + ".mcp.tmp"
    pathlib.Path(tmp).write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    os.replace(tmp, sp)
    return {"ok": True, "file": "_zcode/" + name,
            "note": "已入库 _zcode 池；视觉描述稍后自动生成"}


TOOL_DEFS = [
    {"name": "qq_status", "description": "查看 QQ 机器人运行状态（daemon/agent/登录/统计）",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "qq_logs", "description": "看机器人运行日志（最近 n 条，默认40）",
     "inputSchema": {"type": "object", "properties": {
         "n": {"type": "number", "description": "条数 5-200"}}}},
    {"name": "qq_send", "description": "让机器人发一条 QQ 消息（群或私聊）",
     "inputSchema": {"type": "object", "required": ["chat_type", "chat_id", "text"],
         "properties": {"chat_type": {"type": "string", "enum": ["group", "private"]},
                        "chat_id": {"type": "string", "description": "群号或QQ号"},
                        "text": {"type": "string"}}}},
    {"name": "qq_notify", "description": "向机器人主人 QQ 发一条通知（长任务完成/重要事件）",
     "inputSchema": {"type": "object", "required": ["text"],
         "properties": {"text": {"type": "string"}}}},
    {"name": "qq_inject", "description": "注入测试消息（不经过真实 QQ，调试人设/触发用）",
     "inputSchema": {"type": "object", "required": ["chat_type", "chat_id", "text"],
         "properties": {"chat_type": {"type": "string", "enum": ["group", "private"]},
                        "chat_id": {"type": "string"},
                        "user_id": {"type": "number", "description": "发送者QQ（默认主人）"},
                        "name": {"type": "string", "description": "发送者昵称"},
                        "text": {"type": "string"}}}},
    {"name": "qq_config_get", "description": "读机器人回复配置（kind=app 读应用配置）",
     "inputSchema": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": ["reply", "app"]}}}},
    {"name": "qq_config_set", "description": "改机器人回复配置单项（key/value）",
     "inputSchema": {"type": "object", "required": ["key", "value"],
         "properties": {"key": {"type": "string"}, "value": {}}}},
    {"name": "qq_memory_add", "description": "给机器人加一条长期记忆",
     "inputSchema": {"type": "object", "required": ["scope", "id", "text"],
         "properties": {"scope": {"type": "string", "enum": ["group", "contact"]},
                        "id": {"type": "string"}, "text": {"type": "string"}}}},
    {"name": "qq_persona_list", "description": "列出机器人的人设与当前激活项",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "qq_persona_switch", "description": "切换机器人人设",
     "inputSchema": {"type": "object", "required": ["name"],
         "properties": {"name": {"type": "string"}}}},
    {"name": "qq_task_done", "description": "回执 ZCode 收件箱任务完成（结果会自动转发主人QQ）；不传 task_file 自动取最早待办",
     "inputSchema": {"type": "object", "properties": {
         "task_file": {"type": "string", "description": "zcode-tasks 里的文件名"},
         "result": {"type": "string", "description": "结果说明/成果摘要"}}}},
    {"name": "qq_sticker_add", "description": "把本地图片入库为机器人表情包（_zcode 池，自动生成描述）",
     "inputSchema": {"type": "object", "required": ["path"],
         "properties": {"path": {"type": "string", "description": "图片绝对路径"},
                        "desc": {"type": "string", "description": "可选备注（视觉描述会自动补）"}}}},
]
TOOL_MAP = {t["name"]: fn for t, fn in zip(
    TOOL_DEFS, [t_qq_status, t_qq_logs, t_qq_send, t_qq_notify, t_qq_inject,
                t_qq_config_get, t_qq_config_set, t_qq_memory_add,
                t_qq_persona_list, t_qq_persona_switch, t_qq_task_done,
                t_qq_sticker_add])}


# ---------------- MCP stdio 协议 ----------------

def handle(req):
    method = req.get("method")
    rid = req.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "qq-bot", "version": "0.1.0"}}}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None  # 通知无响应
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOL_DEFS}}
    if method == "tools/call":
        name = (req.get("params") or {}).get("name")
        args = (req.get("params") or {}).get("arguments") or {}
        fn = TOOL_MAP.get(name)
        if not fn:
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": "未知工具: %s" % name}],
                "isError": True}}
        try:
            r = fn(args)
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": json.dumps(r, ensure_ascii=False)[:4000]}],
                "isError": bool(r.get("ok") is False)}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": "工具执行异常: %r" % e}],
                "isError": True}}
    if rid is not None:
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": "method not found: %s" % method}}
    return None


def main():
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        try:
            resp = handle(req)
        except Exception as e:  # 协议层兜底：绝不让服务器进程崩溃
            resp = {"jsonrpc": "2.0", "id": req.get("id"),
                    "error": {"code": -32603, "message": repr(e)}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
