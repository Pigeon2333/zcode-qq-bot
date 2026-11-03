# -*- coding: utf-8 -*-
"""每日一句：hitokoto.cn 免费接口，每 2 小时取一句存数据文件，当天注入。"""
import json, os, time, http.client, pathlib

DATA_DIR = os.environ.get("QQBOT_DATA_DIR") or os.path.join(
    os.path.expanduser("~"), ".zcode-qq-bot")
PATH = os.path.join(DATA_DIR, "daily_word.json")


def register(api):
    api.on_tick(api.every(120, lambda d: _fetch(api)))
    api.inject_context(_line)


def _fetch(api):
    try:
        c = http.client.HTTPSConnection("v1.hitokoto.cn", timeout=10)
        c.request("GET", "/?c=i&encode=json")
        d = json.loads(c.getresponse().read().decode("utf-8", "replace"))
        c.close()
        text = (str(d.get("hitokoto") or "").strip() + "——" + str(d.get("from") or "")).strip("-—")
        if text:
            pathlib.Path(PATH).write_text(json.dumps(
                {"date": time.strftime("%Y-%m-%d"), "text": text[:120]},
                ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        api.log("FETCH_FAIL %r" % e)


def _line(ct, cid):
    try:
        with open(PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        if d.get("date") == time.strftime("%Y-%m-%d") and d.get("text"):
            return ("\n\n## 今日一句（今天看到的话；聊天中偶尔可自然引用一句类似气质的话，"
                    "不必提及来源）\n- " + str(d["text"]))
    except Exception:
        pass
    return ""
