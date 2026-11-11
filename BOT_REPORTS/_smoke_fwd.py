# -*- coding: utf-8 -*-
"""冒烟：复现 2026-11-11 20:46 鸽子窝时序，验证三个修复。
不连 ws、不真发消息——离线实例化 Daemon 跑触发/合并逻辑。"""
import os
import sys
import time

sys.path.insert(0, os.path.join("plugins", "qq-bot", "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import qq_daemon as D  # noqa: E402

d = D.Daemon(ws_url="", token="", cfg={}, ctl_port=0)
GID = "602323545"
OWNER = 3349525227
fails = []


def chk(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def info(**kw):
    base = {"text": "", "at_me": False, "has_reply": False, "reply_id": "",
            "at_users": [], "image_urls": [], "face_count": 0,
            "sender_id": OWNER, "sender_name": "鸽纸", "record": False}
    base.update(kw)
    return base


# --- Fix 0: 配置透传 ---
rc = d._reply_config()
chk("Fix0 vision_enabled 透传=True", rc.get("vision_enabled") is True)
chk("Fix0 vision_mode 透传=all_prob", rc.get("vision_mode") == "all_prob")

# --- Fix 0: 纯图消息绕过（同 20:46 的 id=8 场景）---
t, r = d._decide_trigger("group", info(image_urls=["http://x/1.jpg"]), GID)
chk("Fix0 纯图触发 (True,chat_random) 实得(%s,%s)" % (t, r), t is True and r == "chat_random")

# --- Fix 2: burst_wait 带图豁免 ---
key = "group_%s" % GID
now = time.time()
d.mem_history[key].append({"ts": now - 1, "sender_id": OWNER, "is_bot": False})
d.mem_history[key].append({"ts": now - 0.5, "sender_id": OWNER, "is_bot": False})
t, r = d._decide_trigger("group", info(text="不限老用户"), GID)
chk("Fix2 连发中纯文字仍 burst_wait 实得(%s,%s)" % (t, r), (t, r) == (False, "burst_wait"))
t, r = d._decide_trigger(
    "group", info(text="不限老用户", image_urls=["http://x/2.jpg"]), GID)
chk("Fix2 连发中带图豁免→绕过 实得(%s,%s)" % (t, r), t is True and r == "chat_random")

# --- Fix 1: 向后捞合并（_fwd_deferred_push 真跑一遍，含 2.5s 等待）---
ck = "group_%s_%s" % (GID, OWNER)
t0 = time.time()
item = {"id": 7, "ts": t0, "chat_type": "group", "chat_id": GID,
        "sender_id": OWNER, "text": "腾讯云搞活动了", "image_urls": []}
d._fwd_pending[ck] = t0
d._nearby_img[ck] = {"ts": t0 + 1.2, "urls": ["http://x/tencent.jpg"]}
th = __import__("threading").Thread(
    target=d._fwd_deferred_push, args=(item, ck, t0), daemon=True)
th.start()
th.join(6)
got = d.inbox[-1] if d.inbox else None
chk("Fix1 延迟入箱后图已并入", bool(got and got.get("image_urls") == ["http://x/tencent.jpg"]))
chk("Fix1 合并标记 merged_nearby_image", bool(got and got.get("merged_nearby_image")))
chk("Fix1 缓存一次性消费(_nearby_img 已弹)", ck not in d._nearby_img)
chk("Fix1 待定标记已清(_fwd_pending 已弹)", ck not in d._fwd_pending)

# --- Fix 1: 无后续图 → 原样入箱，不炸 ---
item2 = {"id": 8, "ts": time.time(), "chat_type": "group", "chat_id": GID,
         "sender_id": OWNER, "text": "没有配图的普通问题", "image_urls": []}
ck2 = ck + "_2"
th2 = __import__("threading").Thread(
    target=d._fwd_deferred_push, args=(item2, ck2, item2["ts"]), daemon=True)
th2.start()
th2.join(6)
got2 = d.inbox[-1]
chk("Fix1 无后续图原样入箱", got2.get("text") == "没有配图的普通问题"
    and got2.get("image_urls") == [])

# --- Fix 1: 重编号防丢（acked_max 已越过本条 id）---
item3 = {"id": 7, "ts": time.time(), "chat_type": "group", "chat_id": GID,
         "sender_id": OWNER, "text": "迟到的条目", "image_urls": []}
d.acked_max = 99
th3 = __import__("threading").Thread(
    target=d._fwd_deferred_push, args=(item3, ck, item3["ts"]), daemon=True)
th3.start()
th3.join(6)
got3 = d.inbox[-1]
chk("Fix1 重编号(acked_max=99 时 id 应 >99) 实得 id=%s" % got3.get("id"),
    got3.get("id", 0) > 99)

print()
print("失败 %d 项" % len(fails) if fails else "全部通过 ✓")
sys.exit(1 if fails else 0)
