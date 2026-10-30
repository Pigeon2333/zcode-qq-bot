# -*- coding: utf-8 -*-
"""每日热点速览插件：每 6 小时（首个 tick 立即）抓一次当日热点写 hot_brief.json，
当天才注入聊天上下文。自 agent 内联 _hot_brief_refresh/_hot_brief_line 迁入。"""

import json
import os
import re
import time

DATA_DIR = os.environ.get("QQBOT_DATA_DIR") or os.path.join(
    os.path.expanduser("~"), ".zcode-qq-bot")
BRIEF_PATH = os.path.join(DATA_DIR, "hot_brief.json")
_MAX = 400


def register(api):
    api.on_tick(api.every(360, lambda d: _refresh(api)))
    api.inject_context(_line)


def _refresh(api):
    try:
        import qq_bot_agent as A
        res, _backend = A.search_web("今日热点新闻 头条")
        text = str(res or "").strip()
        if text:
            pathlib = __import__("pathlib")
            pathlib.Path(BRIEF_PATH).write_text(json.dumps(
                {"date": time.strftime("%Y-%m-%d"),
                 "text": text[:_MAX + 200], "ts": time.time()},
                ensure_ascii=False), encoding="utf-8")
            api.log("brief ok %d chars" % len(text))
    except Exception as e:
        api.log("REFRESH_FAIL %r" % e)


def _line(chat_type, chat_id):
    try:
        with open(BRIEF_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return ""
    if not isinstance(d, dict) or str(d.get("date") or "") != time.strftime("%Y-%m-%d"):
        return ""
    text = str(d.get("text") or "").strip()
    if not text:
        return ""
    return ("\n\n## 今日热点速览（今天 %s 的新闻/热搜摘要，聊到相关话题时可自然引用，"
            "不确定的细节不要编）\n- %s" % (d.get("date"), text[:_MAX]))
