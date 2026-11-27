#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qq_tools_builtin.py — 内置工具登记处（A2a：web_search、react；
A2b：send_group、draw、cron_add、cron_list）

每个工具是一层薄包装：调用既有函数（统一搜索入口 search_web、daemon 的
set_msg_reaction、发群的解析+校验+发送、画图执行、定时的存入/读取），
不重新实现任何既有防护。执行函数由调用方传入（register_builtin 的各个
*_fn 参数），所以本模块不 import qq_bot_agent；import 无副作用——真正
登记只在调用方启动时（qq_bot_agent.main）做，可重复调用，已登记的名字
跳过（qq_tools.register 同名会抛 ValueError）。没传对应执行函数的工具
不登记。

模型侧约定见 qq_tools.py：回复里一行一个「[TOOL] 名字 {json参数}」。
"""
import re

import qq_tools


def _clip(text, limit):
    text = str(text or "")
    return text if len(text) <= limit else text[:limit]


def register_builtin(search_web_fn, send_group_fn=None, draw_fn=None,
                     cron_add_fn=None, cron_list_fn=None):
    """把内置工具登记进 qq_tools。幂等：已登记的名字原样跳过，不抛异常、
    也不覆盖已登记的同名工具。四个带对外副作用的工具（发到群/画图/定时）
    只登记调用方传了对应执行函数的那些，没传的不登记。"""
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
