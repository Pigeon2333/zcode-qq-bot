#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qq_tools_builtin.py — 内置工具登记处（A2a 第一批：web_search、react）

每个工具是一层薄包装：调用既有函数（统一搜索入口 search_web、daemon 的
set_msg_reaction），不重新实现任何既有防护。执行函数由调用方传入
（register_builtin(search_web_fn)），所以本模块不 import qq_bot_agent；
import 无副作用——真正登记只在调用方启动时（qq_bot_agent.main）做，
可重复调用，已登记的名字跳过（qq_tools.register 同名会抛 ValueError）。

模型侧约定见 qq_tools.py：回复里一行一个「[TOOL] 名字 {json参数}」。
"""
import qq_tools


def _clip(text, limit):
    text = str(text or "")
    return text if len(text) <= limit else text[:limit]


def register_builtin(search_web_fn):
    """把第一批内置工具登记进 qq_tools。幂等：已登记的名字原样跳过，
    不抛异常、也不覆盖已登记的同名工具。"""
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
