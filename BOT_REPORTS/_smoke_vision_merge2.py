# -*- coding: utf-8 -*-
"""冒烟：2026-11-18 识图三修（回捞不吞缓存 / 历史回捞 / TTL 180s）。
复现用户实测痛点：发图后连发"继续""重试"，机器人对着明晃晃的图说没看到图。
不连 ws、不真发消息——离线实例化 Daemon 跑完整 _handle_message_event 路径。"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join("..", "plugins", "qq-bot", "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import qq_daemon as D  # noqa: E402

d = D.Daemon(ws_url="", token="", cfg={}, ctl_port=0)
GID = "602323545"          # 与 _smoke_fwd 同群（本机群白名单已知放行）
OWNER = 3349525227
BOT_SELF = 1234567
IMG = "https://example.com/pic.jpg"
fails = []


def chk(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


_mid = iter(range(1000, 9999))


def ev_of(segs, uid=OWNER, ts=None):
    return {"message_type": "group", "group_id": GID, "self_id": BOT_SELF,
            "time": ts or time.time(), "message_id": "m%d" % next(_mid),
            "sender": {"user_id": uid, "nickname": "鸽纸"},
            "message": segs}


def txt_ev(t, uid=OWNER, ts=None):
    return ev_of([{"type": "text", "data": {"text": t}}], uid, ts)


def at_txt_ev(t):
    return ev_of([{"type": "at", "data": {"qq": str(BOT_SELF)}},
                  {"type": "text", "data": {"text": t}}])


def last_inbox():
    return d.inbox[-1] if d.inbox else None


# ===== 场景 1：图 → "继续"(未触发, 旧逻辑会吞缓存) → "@bot 重试"(触发) =====
d.inbox.clear()
d._nearby_img.clear()
t0 = time.time()
d._handle_message_event(ev_of([{"type": "image", "data": {"url": IMG}}], ts=t0))
chk("S1 图条入库带图", bool(last_inbox() and last_inbox().get("image_urls")))

d._handle_message_event(txt_ev("继续", ts=t0 + 3))
cont = last_inbox()
chk("S1 '继续'(未触发)并图不吞缓存", bool(
    cont and cont.get("image_urls") and cont.get("merged_nearby_image")))
ck = "group_%s_%s" % (GID, OWNER)
chk("S1 缓存仍在（未被'继续'消费）", ck in d._nearby_img)

d._handle_message_event(at_txt_ev("重试"))
retry = last_inbox()
chk("S1 '重试'触发并上图", bool(
    retry and retry.get("image_urls") and retry.get("merged_nearby_image")))
chk("S1 '重试'确实触发", bool(retry and retry.get("triggered")))

# ===== 场景 2：历史回捞——缓存已过期，图只在历史条目里 =====
d.inbox.clear()
d._nearby_img.clear()
hk = "group_%s" % GID
d.mem_history[hk] = []
d._handle_message_event(ev_of([{"type": "image", "data": {"url": IMG}}],
                              uid=999, ts=time.time() - 30))
chk("S2 他人图条入史", bool(
    d.mem_history[hk] and d.mem_history[hk][-1].get("image_urls")))
d._handle_message_event(at_txt_ev("继续"))
cont2 = last_inbox()
chk("S2 '继续'(无缓存)历史回捞并图", bool(
    cont2 and cont2.get("image_urls") and cont2.get("merged_nearby_image")))

# ===== 场景 3：历史回捞不含指代/续接词 → 不回捞（防误配）=====
# 注：@无图文字条自身会走 2.5s 延迟入箱（_fwd_defer，等随后配图的设计行为），
# 立即断言会读到上一条——等它的延迟线程落地后再查，也避免它稍后抢走 S5 的缓存。
d.inbox.clear()
d.mem_history[hk] = []
d._handle_message_event(ev_of([{"type": "image", "data": {"url": IMG}},
                               {"type": "text", "data": {"text": "无关图"}}],
                              uid=999, ts=time.time() - 30))
d._handle_message_event(at_txt_ev("今天天气真不错"))
time.sleep(3.2)
noweather = last_inbox()
chk("S3 无指代词不回捞", bool(
    noweather and noweather.get("text") == "今天天气真不错"
    and not noweather.get("image_urls")))

# ===== 场景 4：缓存消费上限 n>=3（防 TTL 内反复带旧图）=====
d.inbox.clear()
d._nearby_img[ck] = {"ts": time.time(), "urls": [IMG], "n": 2}
d._handle_message_event(txt_ev("继续"))
chk("S4 第3次并图后缓存清空", ck not in d._nearby_img)
d.inbox.clear()
d._handle_message_event(txt_ev("然后呢"))
chk("S4 第4次不再并图", not (last_inbox() or {}).get("image_urls"))

# ===== 场景 5：_fwd_deferred_push 兼容新缓存结构（含 n 字段）=====
d.inbox.clear()
d._nearby_img[ck] = {"ts": t0 + 1.2, "urls": [IMG], "n": 0}
item = {"id": 77, "ts": t0, "chat_type": "group", "chat_id": GID,
        "sender_id": OWNER, "text": "看下这个", "image_urls": []}
d._fwd_pending[ck] = t0
d.acked_max = 100  # 已越过 id=77：验证重编号防跳过
th = threading.Thread(target=d._fwd_deferred_push, args=(item, ck, t0),
                      daemon=True)
th.start(); th.join(4)
got = item.get("image_urls")
chk("S5 延迟并图成功(图后到)", bool(got and IMG in got))
chk("S5 重编号越过 acked_max", item["id"] > 100)

print("\n== 结果: %s ==" % ("全部通过" if not fails else "失败 %d 项" % len(fails)))
sys.exit(1 if fails else 0)
