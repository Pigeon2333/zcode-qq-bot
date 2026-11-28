#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qq_tools_builtin.py — 内置工具登记处（A2a：web_search、react；
A2b：send_group、draw、cron_add、cron_list；
B2a：pc_find/pc_read/pc_list/pc_search/pc_apps/pc_send/pc_open/pc_close；
B3：mcp_call——MCP 外部工具并入工具表的通用入口；
C7：gadmin_ban/gadmin_unban/gadmin_whole_ban/gadmin_whole_unban/
gadmin_notice/gadmin_recall——群管动作并入工具表；
D 批：web_fetch——网页正文读取（qq_webfetch.fetch 的薄包装）；
chat_search——聊天记录关键词检索（执行函数由 agent 侧整段提供））

每个工具是一层薄包装：调用既有函数（统一搜索入口 search_web、daemon 的
set_msg_reaction、发群的解析+校验+发送、画图执行、定时的存入/读取、
qq_bot_agent._devctrl_step 的八个设备动作），不重新实现任何既有防护——
设备操控的保护目录/敏感文件/open 白名单/系统进程黑名单四道防护全部在
_devctrl_step 内部，pc_* 只许调它。执行函数由调用方传入（register_builtin
的各个 *_fn 参数），所以本模块不 import qq_bot_agent；import 无副作用——
真正登记只在调用方启动时（qq_bot_agent.main）做，可重复调用，已登记的名字
跳过（qq_tools.register 同名会抛 ValueError）。没传对应执行函数的工具
不登记。

模型侧约定见 qq_tools.py：回复里一行一个「[TOOL] 名字 {json参数}」。
"""
import json
import re

import qq_tools


def _clip(text, limit):
    text = str(text or "")
    return text if len(text) <= limit else text[:limit]


def register_builtin(search_web_fn, send_group_fn=None, draw_fn=None,
                     cron_add_fn=None, cron_list_fn=None,
                     devctrl_step_fn=None, devctrl_enabled_fn=None,
                     mcp_call_fn=None, mcp_allowed_fn=None,
                     gadmin_exec_fn=None, gadmin_enabled_fn=None,
                     web_fetch_fn=None, web_fetch_enabled_fn=None,
                     chat_search_fn=None):
    """把内置工具登记进 qq_tools。幂等：已登记的名字原样跳过，不抛异常、
    也不覆盖已登记的同名工具。四个带对外副作用的工具（发到群/画图/定时）、
    八个设备工具（B2a 的 pc_*，两个 devctrl 函数都传了才登记）、MCP 通用
    入口（B3 的 mcp_call，mcp_call_fn/mcp_allowed_fn 两个都传了才登记）、
    六个群管工具（C7 的 gadmin_*，gadmin_exec_fn/gadmin_enabled_fn 两个
    都传了才登记）、网页读取（D 批的 web_fetch，web_fetch_fn/
    web_fetch_enabled_fn 两个都传了才登记）与聊天记录检索（D 批的
    chat_search，chat_search_fn 传了才登记——它需要 msg，执行函数由
    agent 侧整段提供）只登记调用方传了对应执行函数的那些，没传的不登记。"""
    if qq_tools.get_tool("web_search") is None:
        qq_tools.register({
            "name": "web_search",
            "desc": "联网搜索：按关键词搜一下，拿回最新的公开资料",
            "params": [{"name": "query", "type": "str", "required": True,
                        "desc": "要搜的关键词"}],
            "risk": "read", "scope": "all",
            "returns": "搜索结果的前几条",
            "run": _web_search_run(search_web_fn),
        })
    if qq_tools.get_tool("react") is None:
        qq_tools.register({
            "name": "react",
            "desc": "给刚收到的那条消息挂一个 QQ 表情回应",
            "params": [{"name": "emoji_id", "type": "str", "required": True,
                        "desc": "QQ 系统表情编号（如 129=赞）"}],
            "risk": "write", "scope": "owner",
            "returns": "已给那条消息挂了表情",
            "run": _react_run,
        })
    if send_group_fn is not None and qq_tools.get_tool("send_group") is None:
        qq_tools.register({
            "name": "send_group",
            "desc": "替主人把一句话发到指定的群（只能发主人白名单里的群），"
                    "发完回读核对",
            "params": [
                {"name": "target", "type": "str", "required": True,
                 "desc": "群号（优先）或完整群名"},
                {"name": "text", "type": "str", "required": True,
                 "desc": "要发到群里的话"},
            ],
            "risk": "write", "scope": "owner",
            "returns": "发没发出去；发出去时带回读核对结论",
            "run": _send_group_run(send_group_fn),
            "verify": _send_group_verify,
        })
    if draw_fn is not None and qq_tools.get_tool("draw") is None:
        qq_tools.register({
            "name": "draw",
            "desc": "按描述画一张图，画好自动发到当前会话",
            "params": [{"name": "prompt", "type": "str", "required": True,
                        "desc": "画什么（画面描述）"}],
            "risk": "write", "scope": "owner",
            "returns": "已接下画图任务，画好后自动发图",
            "run": _draw_run(draw_fn),
        })
    if cron_add_fn is not None and qq_tools.get_tool("cron_add") is None:
        qq_tools.register({
            "name": "cron_add",
            "desc": "加一条每天定时任务：到点自动执行，结果私聊发给主人",
            "params": [
                {"name": "time", "type": "str", "required": True,
                 "desc": "每天的触发时间，形如 8:30 或 08:30"},
                {"name": "content", "type": "str", "required": True,
                 "desc": "到点要做的任务描述"},
            ],
            "risk": "write", "scope": "owner",
            "returns": "定时存好了（带编号）",
            "run": _cron_add_run(cron_add_fn),
        })
    if cron_list_fn is not None and qq_tools.get_tool("cron_list") is None:
        qq_tools.register({
            "name": "cron_list",
            "desc": "列出现在所有的每天定时任务",
            "params": [],
            "risk": "read", "scope": "owner",
            "returns": "定时任务清单文字",
            "run": _cron_list_run(cron_list_fn),
        })
    if (devctrl_step_fn is not None and devctrl_enabled_fn is not None
            and qq_tools.get_tool("pc_find") is None):
        for _spec in _devctrl_specs(devctrl_step_fn, devctrl_enabled_fn):
            if qq_tools.get_tool(_spec["name"]) is None:
                qq_tools.register(_spec)
    if (mcp_call_fn is not None and mcp_allowed_fn is not None
            and qq_tools.get_tool("mcp_call") is None):
        qq_tools.register({
            "name": "mcp_call",
            "desc": "调用 MCP 外部工具（服务器名和工具名照工具清单写）",
            "params": [
                {"name": "server", "type": "str", "required": True,
                 "desc": "MCP 服务器名（照工具清单写）"},
                {"name": "tool", "type": "str", "required": True,
                 "desc": "工具名（照工具清单写）"},
                {"name": "args", "type": "str", "required": False,
                 "desc": "JSON 对象的文本（如 {\"key\": \"值\"}），"
                         "没有参数就不填"},
            ],
            "risk": "write", "scope": "owner",
            "returns": "外部工具的返回文本",
            "run": _mcp_call_run(mcp_call_fn, mcp_allowed_fn),
        })
    if (gadmin_exec_fn is not None and gadmin_enabled_fn is not None
            and qq_tools.get_tool("gadmin_ban") is None):
        for _spec in _gadmin_specs(gadmin_exec_fn, gadmin_enabled_fn):
            if qq_tools.get_tool(_spec["name"]) is None:
                qq_tools.register(_spec)
    if (web_fetch_fn is not None and web_fetch_enabled_fn is not None
            and qq_tools.get_tool("web_fetch") is None):
        qq_tools.register({
            "name": "web_fetch",
            "desc": "读取一个网页的正文（只访问公网网址，内网/本机地址不碰）",
            "params": [{"name": "url", "type": "str", "required": True,
                        "desc": "要读的网页地址（http:// 或 https://）"}],
            "risk": "read", "scope": "owner",
            "returns": "网页标题、最终地址与正文（正文走内容通道回喂）",
            "run": _web_fetch_run(web_fetch_fn, web_fetch_enabled_fn),
        })
    if chat_search_fn is not None and qq_tools.get_tool("chat_search") is None:
        qq_tools.register({
            "name": "chat_search",
            "desc": "在聊天记录里按关键词找消息（默认当前会话；查别的群要"
                    "主人开开关）",
            "params": [
                {"name": "query", "type": "str", "required": True,
                 "desc": "要找的关键词，可以多个（每个词都出现才算命中）"},
                {"name": "chat", "type": "str", "required": False,
                 "desc": "群号或群名；不填就查当前会话"},
                {"name": "limit", "type": "int", "required": False,
                 "desc": "往前翻多少条，不填默认 100，最多 200"}],
            "risk": "read", "scope": "owner",
            "returns": "命中的消息（何时、谁、说了什么；最多 20 条）",
            "run": _chat_search_run(chat_search_fn),
        })


def _web_search_run(search_web_fn):
    def run(daemon, msg, args):
        try:
            res = search_web_fn(args["query"])
        except Exception as exc:
            return qq_tools.make_result(False, "搜索出错：%s" % exc)
        text = res[0] if isinstance(res, tuple) else res
        text = _clip(text, 800).strip()
        if not text:
            return qq_tools.make_result(False, "没搜到结果")
        return qq_tools.make_result(True, text)
    return run


def _react_run(daemon, msg, args):
    ok, detail = daemon.set_msg_reaction((msg or {}).get("message_id"),
                                         args["emoji_id"])
    if ok:
        return qq_tools.make_result(True, "已给那条消息挂了表情")
    return qq_tools.make_result(False, "没做成：%s" % detail)


# ---- A2b：send_group / draw / cron_add / cron_list ----

def _stats_sent(daemon):
    """daemon 的发送计数（status().stats.sent）；读不到返回 None（回读里
    就不算计数这一项），不抛异常。"""
    try:
        sent = ((daemon.status() or {}).get("stats") or {}).get("sent")
        return int(sent) if sent is not None else None
    except Exception:
        return None


def _send_group_run(send_group_fn):
    def run(daemon, msg, args):
        sent_before = _stats_sent(daemon)   # 发送前计数（回读写 +N 用）
        try:
            ok, note, gid = send_group_fn(daemon, msg, args["target"],
                                          args["text"])
        except Exception as exc:
            return qq_tools.make_result(False, "发群出错：%s" % exc)
        data = {"gid": gid} if gid else {}
        if sent_before is not None:
            data["sent_before"] = sent_before
        return qq_tools.make_result(bool(ok), note, data=data)
    return run


def _send_group_verify(daemon, msg, args, result):
    """发成功后的回读核对（只读）：发送计数涨没涨、群最近 5 条历史里
    有没有这条文字，结论写成给模型看的一两句直白话。群号取不到就不
    回读、evidence 留空；任何读取失败都不外抛。"""
    gid = (result.get("data") or {}).get("gid")
    if not gid:
        return ""
    first_line = ""
    for p in str(args.get("text") or "").split("\n"):
        p = p.strip()
        if p:
            first_line = p
            break
    parts = []
    before = (result.get("data") or {}).get("sent_before")
    after = _stats_sent(daemon)
    if before is not None and after is not None and after >= before:
        parts.append("发送计数 +%d" % (after - before))
    try:
        hist = daemon.history("group", gid, 5) or []
    except Exception:
        hist = []
    hit = bool(first_line) and any(
        first_line in str((h or {}).get("text") or "") for h in hist
        if isinstance(h, dict))
    if hit:
        parts.append("群 %s 最近 5 条历史里有这条消息，确认发出去了" % gid)
    else:
        parts.append("群 %s 最近 5 条历史里没看到这条消息" % gid)
    return "，".join(parts)


def _draw_run(draw_fn):
    def run(daemon, msg, args):
        try:
            draw_fn(daemon, msg, args["prompt"])
        except Exception as exc:
            return qq_tools.make_result(False, "画图接单出错：%s" % exc)
        return qq_tools.make_result(True, "已接下画图任务，画好后自动发图")
    return run


_CRON_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")


def _cron_add_run(cron_add_fn):
    def run(daemon, msg, args):
        t = str(args.get("time") or "").strip()
        content = str(args.get("content") or "").strip()
        m = _CRON_TIME_RE.fullmatch(t)
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            return qq_tools.make_result(
                False, "时间格式不对：要 HH:MM（00:00–23:59），"
                       "比如 8:30 或 08:30")
        if not content:
            return qq_tools.make_result(False, "任务内容不能为空：到点要做"
                                               "什么，写清楚一点")
        try:
            note = cron_add_fn(t, content)
        except Exception as exc:
            return qq_tools.make_result(False, "定时没存上：%s" % exc)
        note = str(note or "").strip() or (
            "已设定时：每天 %s 执行「%s…」" % (t, content[:30]))
        return qq_tools.make_result(True, note)
    return run


def _cron_list_run(cron_list_fn):
    def run(daemon, msg, args):
        try:
            text = cron_list_fn()
        except Exception as exc:
            return qq_tools.make_result(False, "定时列表没取到：%s" % exc)
        text = str(text or "").strip()
        if not text:
            return qq_tools.make_result(False, "定时列表是空的")
        return qq_tools.make_result(True, text)
    return run


# ---- B2a：设备操控八个动作（pc_*）----
# 全部只调 _devctrl_step（qq_bot_agent）：保护目录、敏感文件、open 白名单、
# 系统进程黑名单四道防护都在它内部，这里不重新实现任何防护、也不改它。

# _devctrl_step 返回文本里「失败」的固定开头（从 qq_bot_agent._devctrl_step
# 八个动作各自的返回文案归纳）：保护目录/敏感文件/动作执行未开启等的拒绝、
# read/send/list/open/close 的失败、发送失败、close 只发得出请求的部分成功、
# 不支持的动作。其余文案（找到 0 条、搜索无结果、「当前没有在运行」这类
# 已经是目标状态的结果）都算完成。
_DEVCTRL_FAIL_PREFIXES = (
    "read 拒绝", "read 失败",
    "send 拒绝", "send 失败", "send 发送失败",
    "list 拒绝", "list 失败",
    "open 拒绝", "open 失败",
    "close 拒绝", "close 失败", "close 部分成功",
    "不支持的动作",
)


def _devctrl_ok(text):
    """_devctrl_step 结果文本的成败判定：以上面归纳的失败开头为准，命中即
    未做成；其余一律算完成。"""
    return not str(text or "").startswith(_DEVCTRL_FAIL_PREFIXES)


def _devctrl_run(step_fn, enabled_fn, action, arg_of):
    """八个设备工具共用的薄包装体。第一行先查设备操控开关（关着时任何路径
    都执行不了设备动作）；然后只调 _devctrl_step。reply_to 按现有构造方式取
    主人 QQ：工具闸 scope=owner 保证走到这里的 msg 一定是主人的消息
    （is_owner 的判定就是 sender_id == owner_qq()，二者一致），所以取
    msg["sender_id"]，send 动作由此把文件发到主人私聊。_devctrl_step 的返回
    文本不抛异常，成败按 _devctrl_ok 归纳。"""
    def run(daemon, msg, args):
        if not enabled_fn():
            return qq_tools.make_result(
                False, "电脑操控没有开启（管理台 → 设备操控 里打开）")
        reply_to = ("private", str((msg or {}).get("sender_id") or ""))
        text = step_fn(daemon, reply_to, action, arg_of(args))
        return qq_tools.make_result(_devctrl_ok(text), text)
    return run


def _devctrl_specs(step_fn, enabled_fn):
    """八个设备工具的说明书（B2a 定稿表）：scope 全部 owner；pc_open/pc_close
    是 danger 级，由工具循环的授权闸管（B2b 接索码预检）。"""
    return (
        {"name": "pc_find",
         "desc": "在这台电脑上按文件名找文件，拿回匹配的完整路径",
         "params": [{"name": "query", "type": "str", "required": True,
                     "desc": "文件名关键词"}],
         "risk": "read", "scope": "owner",
         "returns": "在这台电脑上按名字找文件的结果",
         "run": _devctrl_run(step_fn, enabled_fn, "find",
                             lambda a: a["query"])},
        {"name": "pc_read",
         "desc": "读这台电脑上某个文件的开头部分；受保护的目录和敏感文件读不了",
         "params": [{"name": "path", "type": "str", "required": True,
                     "desc": "文件的完整路径"}],
         "risk": "read", "scope": "owner",
         "returns": "文件的前 2000 字",
         "run": _devctrl_run(step_fn, enabled_fn, "read",
                             lambda a: a["path"])},
        {"name": "pc_list",
         "desc": "看这台电脑上某个文件夹里有什么（不给路径就看主目录）",
         "params": [{"name": "path", "type": "str", "required": False,
                     "desc": "文件夹路径，不填就看主目录"}],
         "risk": "read", "scope": "owner",
         "returns": "某个目录下的内容",
         "run": _devctrl_run(step_fn, enabled_fn, "list",
                             lambda a: a.get("path", ""))},
        {"name": "pc_search",
         "desc": "在这台电脑上联网搜索，拿回最新的公开资料",
         "params": [{"name": "query", "type": "str", "required": True,
                     "desc": "要搜的关键词"}],
         "risk": "read", "scope": "owner",
         "returns": "联网搜索的结果",
         "run": _devctrl_run(step_fn, enabled_fn, "searchweb",
                             lambda a: a["query"])},
        {"name": "pc_apps",
         "desc": "列出这台电脑上装了哪些程序（开始菜单里能看到的）",
         "params": [],
         "risk": "read", "scope": "owner",
         "returns": "这台电脑上常用的程序",
         "run": _devctrl_run(step_fn, enabled_fn, "apps", lambda a: "")},
        {"name": "pc_send",
         "desc": "把这台电脑上的一个文件发给主人（私聊收货）",
         "params": [{"name": "path", "type": "str", "required": True,
                     "desc": "文件的完整路径"}],
         "risk": "write", "scope": "owner",
         "returns": "把这个文件发到主人的私聊",
         "run": _devctrl_run(step_fn, enabled_fn, "send",
                             lambda a: a["path"])},
        {"name": "pc_open",
         "desc": "在这台电脑上打开一个文件、程序或网页",
         "params": [{"name": "target", "type": "str", "required": True,
                     "desc": "程序名、文件路径或网址"}],
         "risk": "danger", "scope": "owner",
         "returns": "打开一个文件、程序或网址",
         "run": _devctrl_run(step_fn, enabled_fn, "open",
                             lambda a: a["target"])},
        {"name": "pc_close",
         "desc": "结束这台电脑上正在运行的一个程序（系统关键进程关不了）",
         "params": [{"name": "name", "type": "str", "required": True,
                     "desc": "程序名（可带 .exe）"}],
         "risk": "danger", "scope": "owner",
         "returns": "结束一个程序",
         "run": _devctrl_run(step_fn, enabled_fn, "close",
                             lambda a: a["name"])},
    )


# ---- B3：MCP 并入工具表（mcp_call）----
# MCP 的服务器维护、总开关、连接与超时都在 qq_mcp 里，这里不重新实现任何
# 一道防护——执行函数 mcp_call_fn / 开关函数 mcp_allowed_fn 由调用方传入
# （qq_bot_agent.main 接 _mcp_tool_call / _mcp_tool_allowed），本模块不
# import qq_mcp。write 级的复述确认、scope=owner 的权限闸都由 qq_tools
# 的统一工具闸管（I4）。

_MCP_RESULT_CAP = 1500


def _mcp_call_run(mcp_call_fn, mcp_allowed_fn):
    """mcp_call 的薄包装体。第一行查 MCP 自己的总开关（工具循环开关与它
    无关——MCP 能不能用由这个总开关决定，I1）；args 必须是 JSON 对象的
    文本（解析失败、数组、标量都不行）；调 mcp_call_fn，返回文本截 1500
    字（截断处加一句说明；qq_tools.make_result 对 summary 另有自己的上限，
    见该模块）；调用抛异常（McpError 或别的）一律 ok=False 带原因（截
    200 字），不外抛。"""
    def run(daemon, msg, args):
        if not mcp_allowed_fn():
            return qq_tools.make_result(
                False, "MCP 没有启用（管理台 → MCP 服务 里打开）")
        raw = str(args.get("args") or "").strip() or "{}"
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if not isinstance(parsed, dict):
            return qq_tools.make_result(
                False, "args 要是 JSON 对象的文本，例如 {\"key\": \"值\"}")
        try:
            text = mcp_call_fn(str(args.get("server") or "").strip(),
                               str(args.get("tool") or "").strip(), parsed)
        except Exception as exc:
            return qq_tools.make_result(
                False, "调用失败：%s" % str(exc)[:200])
        text = str(text or "")
        if len(text) > _MCP_RESULT_CAP:
            text = text[:_MCP_RESULT_CAP] + "\n（结果过长，已截断）"
        return qq_tools.make_result(True, text)
    return run


# ---- C7：群管动作并入工具表（gadmin_* 六个）----
# 全部只调 daemon.gadmin_exec（执行函数 gadmin_exec_fn 由调用方传入，
# qq_bot_agent.main 接 _gadmin_tool_call）：找人的三级解析、参数钳制、
# 失败的人话都在它内部，这里不重新实现任何一套。开关（群管对话总开关
# chat_enabled）与旧 [GADMIN] 标记教学/执行是同一个，由 gadmin_enabled_fn
# 传入。write 级的复述确认、danger 级的授权码、scope=owner 的权限闸都由
# qq_tools 的统一工具闸管（复用，不另写）。

def _gadmin_run(exec_fn, enabled_fn, op, fixed):
    """六个群管工具共用的薄包装体（C7，照 _devctrl_run 的先例）：第一行先
    查群管对话总开关（关着时任何路径都执行不了）；然后只调
    exec_fn(daemon, op, 参数)——它内部转 daemon.gadmin_exec。返回值是
    gadmin_exec 的 {"ok": bool, "detail": str}，按 ok 如实回喂：detail 是
    daemon 拟好的人话，原样进 summary（失败的原因就在 detail 里；工具循环
    的回喂行自己会标「没做成：」，这里不再叠一层前缀）。fixed 是各工具
    固定带入的参数键（whole_ban/whole_unban 的 on=True/False——开与解除
    拆成两个工具后，on 不再是模型可填的参数）。"""
    def run(daemon, msg, args):
        if not enabled_fn():
            return qq_tools.make_result(
                False, "对话群管没有开启（管理台 → 群管 里打开）")
        payload = dict(args) if isinstance(args, dict) else {}
        payload.update(fixed)
        try:
            r = exec_fn(daemon, op, payload)
        except Exception as exc:
            return qq_tools.make_result(
                False, "群管执行出错：%s" % str(exc)[:200])
        r = r if isinstance(r, dict) else {}
        detail = str(r.get("detail") or "").strip()
        if r.get("ok"):
            return qq_tools.make_result(True, detail or "办好了")
        return qq_tools.make_result(False, detail or "执行失败")
    return run


def _gadmin_specs(exec_fn, enabled_fn):
    """六个群管工具的说明书（C7 定稿表）：scope 全部 owner（现状只有主人
    能触发，不扩大）；gadmin_whole_ban / gadmin_recall 是 danger 级（撤回
    不可逆、全体禁言影响全群），由工具循环的授权闸管；gadmin_ban /
    gadmin_notice 是 write 级（受复述确认开关保护）；解禁两个是 soft 级
    （会改状态但随时可以反悔的恢复性动作——qq_tools 自 C7 起有 soft 这
    一级，闸上与 read 同一支直接执行，不受复述与授权管）。
    minutes / count 没填时的缺省（10 / 1）与越界钳制都在 exec_fn /
    daemon.gadmin_exec 那一侧（与旧分发处的缺省一致），这里不另写一套。"""
    return (
        {"name": "gadmin_ban",
         "desc": "禁言群里的某个成员一段时间（默认 10 分钟）",
         "params": [
             {"name": "group", "type": "str", "required": True,
              "desc": "群号（优先）或完整群名"},
             {"name": "member", "type": "str", "required": True,
              "desc": "要禁言的人：群名片/昵称或QQ号"},
             {"name": "minutes", "type": "int", "required": False,
              "desc": "禁言分钟数，不填默认 10"}],
         "risk": "write", "scope": "owner",
         "returns": "禁言办没办成和原因",
         "run": _gadmin_run(exec_fn, enabled_fn, "ban", {})},
        {"name": "gadmin_unban",
         "desc": "解除群里某个成员的禁言",
         "params": [
             {"name": "group", "type": "str", "required": True,
              "desc": "群号（优先）或完整群名"},
             {"name": "member", "type": "str", "required": True,
              "desc": "要解禁的人：群名片/昵称或QQ号"}],
         "risk": "soft", "scope": "owner",
         "returns": "解禁办没办成和原因",
         "run": _gadmin_run(exec_fn, enabled_fn, "unban", {})},
        {"name": "gadmin_whole_ban",
         "desc": "开启整个群的全体禁言（除了管理员大家都不能发言）",
         "params": [
             {"name": "group", "type": "str", "required": True,
              "desc": "群号（优先）或完整群名"}],
         "risk": "danger", "scope": "owner",
         "returns": "全体禁言办没办成和原因",
         "run": _gadmin_run(exec_fn, enabled_fn, "whole", {"on": True})},
        {"name": "gadmin_whole_unban",
         "desc": "解除整个群的全体禁言（恢复大家发言）",
         "params": [
             {"name": "group", "type": "str", "required": True,
              "desc": "群号（优先）或完整群名"}],
         "risk": "soft", "scope": "owner",
         "returns": "解除全体禁言办没办成和原因",
         "run": _gadmin_run(exec_fn, enabled_fn, "whole", {"on": False})},
        {"name": "gadmin_notice",
         "desc": "在群里发一条全员公告",
         "params": [
             {"name": "group", "type": "str", "required": True,
              "desc": "群号（优先）或完整群名"},
             {"name": "text", "type": "str", "required": True,
              "desc": "公告全文（不超过 2000 字）"}],
         "risk": "write", "scope": "owner",
         "returns": "公告发没发成和原因",
         "run": _gadmin_run(exec_fn, enabled_fn, "notice", {})},
        {"name": "gadmin_recall",
         "desc": "撤回群里某个成员最近发的消息（1–10 条）",
         "params": [
             {"name": "group", "type": "str", "required": True,
              "desc": "群号（优先）或完整群名"},
             {"name": "member", "type": "str", "required": True,
              "desc": "要撤回其消息的人：群名片/昵称或QQ号"},
             {"name": "count", "type": "int", "required": False,
              "desc": "撤回条数，不填默认 1"}],
         "risk": "danger", "scope": "owner",
         "returns": "撤回办没办成和原因",
         "run": _gadmin_run(exec_fn, enabled_fn, "recall", {})},
    )


# ---- D 批：web_fetch / chat_search ----
# web_fetch 只调 qq_webfetch.fetch（scheme/内网/回环/DNS 重绑定/体积/超时等
# 防护全在它内部，这里不重新实现任何一道）；chat_search 的取数与匹配由
# agent 侧的执行函数整段提供（它需要 msg 来定位当前会话），这里只做参数
# 拆包。两个工具都是 read/owner，由 qq_tools 的统一工具闸管。

def _web_fetch_run(fetch_fn, enabled_fn):
    """web_fetch 的薄包装体：第一行查开关（关着时任何路径都读不了网页）；
    调 fetch_fn，结果照 qq_webfetch.fetch 的返回形状（{ok, final_url,
    status, title, text, error}）转成统一结果——正文放 data["content"]
    （长内容通道，见 qq_tools._feedback），摘要「《标题》 最终地址」；标题
    或正文为空时摘要如实说明；ok=False 或执行函数抛异常 → ok=False 带原因
    （截 200 字），不外抛。"""
    def run(daemon, msg, args):
        if not enabled_fn():
            return qq_tools.make_result(
                False, "读取网页没有开启（管理台 → 工具循环 里打开）")
        url = str(args.get("url") or "").strip()
        try:
            res = fetch_fn(url)
        except Exception as exc:
            return qq_tools.make_result(
                False, "读取网页出错：%s" % str(exc)[:200])
        res = res if isinstance(res, dict) else {}
        if not res.get("ok"):
            return qq_tools.make_result(
                False, str(res.get("error") or "没读到网页"))
        title = str(res.get("title") or "").strip()
        text = str(res.get("text") or "")
        final_url = str(res.get("final_url") or url).strip()
        summary = ("《%s》 %s" % (title, final_url)) if title \
            else "（没抓到标题） %s" % final_url
        if not text.strip():
            summary += "（正文是空的）"
        return qq_tools.make_result(True, summary, data={"content": text})
    return run


def _chat_search_run(chat_search_fn):
    """chat_search 的薄包装体：把校验过的参数从 args 拆出来传给 agent 侧的
    执行函数（它需要 msg 定位当前会话，所以由 agent 侧整段提供）；取数、
    匹配、隐私开关判断全在那边。"""
    def run(daemon, msg, args):
        return chat_search_fn(daemon, msg, str(args.get("query") or ""),
                              str(args.get("chat") or "").strip(),
                              args.get("limit"))
    return run
