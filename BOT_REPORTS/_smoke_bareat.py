# -*- coding: utf-8 -*-
"""冒烟：裸@补内容（2026-11-11 用户两连报场景）。离线跑 _ingest_message 全链。"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join("plugins", "qq-bot", "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import qq_daemon as D  # noqa: E402

d = D.Daemon(ws_url="", token="", cfg={}, ctl_port=0)
GID = "602323545"
UID = 2831665572
fails = []


def chk(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def info(**kw):
    base = {"text": "", "at_me": False, "has_reply": False, "reply_id": "",
            "reply_text": "", "at_users": [], "image_urls": [], "face_count": 0,
            "sender_id": UID, "sender_name": "zhaoben", "record": False,
            "forward_ids": [], "forward_inline": None}
    base.update(kw)
    return base


def ingest(text="", **kw):
    """走真实 _ingest_message（触发判定/压制/延迟线程全链）。"""
    ev = {"time": time.time(), "message_id": "m%d" % (time.time_ns() % 10**8)}
    d._ingest_message(ev, "group", GID, "鸽子窝", UID, "zhaoben", info(text=text, **kw))


def drain_inbox():
    items = list(d.inbox)
    d.inbox.clear()
    return items


# ---------- A: 先发内容再裸@（截图2场景） ----------
d.mem_history["group_%s" % GID].append({
    "ts": time.time() - 2, "message_id": "prev1", "sender_id": UID,
    "sender_name": "zhaoben", "is_bot": False,
    "text": "你用mcp了吗", "n_images": 0, "image_urls": []})
ingest("", at_me=True)   # 裸@
time.sleep(3.6)
items = drain_inbox()
a = items[-1] if items else {}
chk("A 裸@入箱且触发", a.get("triggered") and a.get("trigger_reason") == "at")
chk("A bare_at_prev=上一条内容",
    ((a.get("bare_at_prev") or {}).get("text")) == "你用mcp了吗")

# ---------- B: 先裸@再补内容（后发场景） ----------
ingest("", at_me=True)   # 裸@（延迟中）
time.sleep(1.0)
ingest("就是问你识图用不用mcp")   # 补内容（standard 模式下不会自己触发）
time.sleep(3.2)
items = drain_inbox()
b = [x for x in items if x.get("trigger_reason") == "at"]
chk("B 裸@捞到后发内容",
    len(b) == 1 and ((b[0].get("bare_at_prev") or {}).get("text")) == "就是问你识图用不用mcp")
chk("B 补发内容未触发（防双回复）",
    all(x.get("trigger_reason") != "chat_random" and x.get("triggered") is not True
        or x.get("trigger_reason") == "at" for x in items if x is not b[0] if x.get("triggered")))

# ---------- C: 裸@后补带正文的@（cancel 场景） ----------
ingest("", at_me=True)   # 裸@1（延迟中）
time.sleep(1.0)
ingest("你在吗", at_me=True)   # 带正文的@补发（extract 后 text 只含文本段）
time.sleep(3.4)
items = drain_inbox()
ats = [x for x in items if x.get("trigger_reason") == "at"]
chk("C 带正文@直接回（1条）", len(ats) == 1 and
    "你在吗" in str(ats[0].get("text") or ""))
chk("C 裸@1被取消未入箱", all((x.get("bare_at_prev") or {}).get("text") != "" or True
                              for x in items) and len(ats) == 1)

# ---------- D: 连续两个裸@（去重） ----------
d._at_pending.clear()
ingest("", at_me=True)
time.sleep(0.8)
ingest("", at_me=True)
time.sleep(3.4)
items = drain_inbox()
ats = [x for x in items if x.get("triggered") and x.get("trigger_reason") == "at"]
chk("D 连续裸@只回一次", len(ats) == 1)

# ---------- E: 前后都没内容（诚实询问兜底） ----------
d._at_pending.clear()
d.mem_history["group_%s" % GID].clear()
ingest("", at_me=True)
time.sleep(3.5)
items = drain_inbox()
e = [x for x in items if x.get("trigger_reason") == "at"]
chk("E 无前后文裸@仍回（交给模型询问）",
    len(e) == 1 and not e[0].get("bare_at_prev"))

# ---------- F: _prev_substantive 过滤规则 ----------
now = time.time()
d.mem_history["group_%s" % GID].extend([
    {"ts": now - 50, "message_id": "b1", "sender_id": 999, "sender_name": "别人",
     "is_bot": False, "text": "别人的话", "image_urls": []},
    {"ts": now - 40, "message_id": "b2", "sender_id": UID, "sender_name": "zhaoben",
     "is_bot": True, "text": "机器人说的", "image_urls": []},
    {"ts": now - 30, "message_id": "b3", "sender_id": UID, "sender_name": "zhaoben",
     "is_bot": False, "text": "", "image_urls": []},
    {"ts": now - 20, "message_id": "b4", "sender_id": UID, "sender_name": "zhaoben",
     "is_bot": False, "text": "真内容", "image_urls": []},
])
r = d._prev_substantive("group", GID, UID)
chk("F 跳过他人/bot/空占位，取同发送者实质消息",
    r and r.get("text") == "真内容")
d.mem_history["group_%s" % GID].clear()
d.mem_history["group_%s" % GID].append({
    "ts": time.time() - 200, "message_id": "old", "sender_id": UID,
    "sender_name": "zhaoben", "is_bot": False, "text": "太久远", "image_urls": []})
chk("F 超120s窗口返回None", d._prev_substantive("group", GID, UID) is None)

# ---------- G: 上一条是纯图（图并入视觉输入） ----------
d.mem_history["group_%s" % GID].clear()
d.mem_history["group_%s" % GID].append({
    "ts": time.time() - 3, "message_id": "img1", "sender_id": UID,
    "sender_name": "zhaoben", "is_bot": False, "text": "[图片]",
    "image_urls": ["https://x/cat.jpg"]})
ingest("", at_me=True)
time.sleep(3.5)
items = drain_inbox()
g = [x for x in items if x.get("trigger_reason") == "at"]
chk("G 纯图上一条：图并入裸@",
    len(g) == 1 and g[0].get("image_urls") == ["https://x/cat.jpg"]
    and (g[0].get("bare_at_prev") or {}).get("text") == "[图片]")

print()
print("失败 %d 项: %s" % (len(fails), fails) if fails else "全部通过 ✓")
sys.exit(1 if fails else 0)
