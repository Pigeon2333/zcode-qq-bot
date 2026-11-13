# -*- coding: utf-8 -*-
"""对话群管冒烟：教学仅主人注入+标记解析全op+越权拒绝+daemon拦截器+成员解析。"""
import os
import sys
import time
from collections import deque

sys.path.insert(0, os.path.join("plugins", "qq-bot", "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import qq_daemon as D  # noqa: E402
import qq_bot_agent as A  # noqa: E402

fails = []


def chk(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (" | " + str(extra)[:80] if extra else ""))
    if not cond:
        fails.append(name)


# ---------- ① 开关与教学注入 ----------
os.environ["QQBOT_DATA_DIR"] = os.path.expanduser("~/.zcode-qq-bot")
A.load_json_const.cache_clear() if hasattr(A.load_json_const, "cache_clear") else None
import json as _j
import pathlib as _pl
_pl.Path(os.path.expanduser("~/.zcode-qq-bot/group_admin.json")).write_text(
    '{"chat_enabled": true, "groups": {}}', encoding="utf-8")
chk("1a 开关读True", A._gadmin_chat_enabled() is True)


class FakeDaemon:
    def group_names(self):
        return {"602323545": "鸽子窝", "1061684987": "方舟摄影"}


msg_owner = {"chat_type": "private", "sender_id": 3349525227}
msg_other = {"chat_type": "group", "sender_id": 123456}
r1 = A._gadmin_rule(FakeDaemon(), msg_owner)
r2 = A._gadmin_rule(FakeDaemon(), msg_other)
chk("1b 主人拿到教学（含标记+群表）",
    "[GADMIN]" in r1 and "鸽子窝(602323545)" in r1 and "边界铁律" in r1, r1[:60])
chk("1c 非主人零教学", r2 == "")

# ---------- ② 标记解析（各 op） ----------
cases = [
    ("[GADMIN] 禁言|群=602323545|人=张三|分钟=10", "ban", {"group_id": "602323545", "who": "张三", "minutes": "10"}),
    ("[GADMIN] 解禁|群=602323545|人=张三", "unban", {"group_id": "602323545", "who": "张三"}),
    ("[GADMIN] 全体禁言|群=602323545|开=0", "whole", {"group_id": "602323545", "on": False}),
    ("[GADMIN] 公告|群=602323545|内容=今晚维护", "notice", {"group_id": "602323545", "content": "今晚维护"}),
    ("[GADMIN] 撤回|群=602323545|人=张三|条数=3", "recall", {"group_id": "602323545", "who": "张三", "count": "3"}),
]
for mark, want_op, want in cases:
    m = A._GADMIN_MARK_RE.search(mark)
    okk = m and m.group(1) and (want["group_id"] in mark)
    chk("2 标记解析 %s" % want_op, bool(m), m.groups() if m else None)

# ---------- ③ daemon gadmin_exec（mock 动作）+ 成员解析 ----------
d = D.Daemon(ws_url="", token="", cfg={}, ctl_port=0)
d.login = {"user_id": 3126940548}
calls = []
d._call_action = lambda action, params, timeout=None: (
    calls.append((action, params)) or
    ([{"user_id": 999, "card": "张三", "nickname": "zs"}]
     if action == "get_group_member_list" else {}))
r = d.gadmin_exec({"op": "ban", "group_id": "602323545", "who": "张三", "minutes": 10})
acts = [c for c in calls if c[0] == "set_group_ban"]
chk("3a 禁言（人名解析→QQ999+600s）", r.get("ok") and acts
    and acts[0][1]["user_id"] == 999 and acts[0][1]["duration"] == 600,
    r.get("detail"))
chk("3b 人话回执", "已把 张三 禁言 10 分钟" == r.get("detail"), r.get("detail"))

calls.clear()
r = d.gadmin_exec({"op": "recall", "group_id": "602323545", "who": "张三", "count": 2})
chk("3c 撤回无近期消息→人话失败", not r.get("ok") and "最近没有能撤回" in r.get("detail", ""), r.get("detail"))

# 历史里补两条张三消息 → 撤回两条
key = "group_602323545"
d.mem_history[key].append({"ts": time.time() - 60, "message_id": "m1",
                           "sender_id": 999, "is_bot": False, "text": "广告1"})
d.mem_history[key].append({"ts": time.time() - 30, "message_id": "m2",
                           "sender_id": 999, "is_bot": False, "text": "广告2"})
calls.clear()
r = d.gadmin_exec({"op": "recall", "group_id": "602323545", "who": "张三", "count": 2})
dels = [c for c in calls if c[0] == "delete_msg"]
chk("3d 批量撤回2条", r.get("ok") and len(dels) == 2
    and {dels[0][1]["message_id"], dels[1][1]["message_id"]} == {"m1", "m2"},
    r.get("detail"))

r = d.gadmin_exec({"op": "notice", "group_id": "602323545", "content": "测试公告"})
chk("3e 公告执行", r.get("ok") and any(c[0] == "_send_group_notice" for c in calls))

r = d.gadmin_exec({"op": "ban", "group_id": "602323545", "who": "不存在的人"})
chk("3f 解析不到人→人话失败", not r.get("ok") and "没找到" in r.get("detail", ""))

# ---------- ④ 出站拦截 ----------
d2 = D.Daemon(ws_url="", token="", cfg={}, ctl_port=0)
s = d2._strip_control_marks("好嘞已办[GADMIN] 禁言|群=1|人=x|分钟=5")
chk("4 拦截器剥GADMIN残留", "好嘞已办" == s, s)

print()
print("失败 %d 项: %s" % (len(fails), fails) if fails else "全部通过 ✓")
sys.exit(1 if fails else 0)
