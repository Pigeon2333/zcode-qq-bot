# -*- coding: utf-8 -*-
"""build_messages 三场景真实构建冒烟（改动它必跑——83f01ed 规矩）：
裸@带prev / 裸@无prev / 正常文字消息 + 引用消息。验证不抛错且占位正确。"""
import os
import sys

sys.path.insert(0, os.path.join("plugins", "qq-bot", "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import qq_daemon as D  # noqa: E402
import qq_bot_agent as A  # noqa: E402

d = D.Daemon(ws_url="", token="", cfg={}, ctl_port=0)
fails = []


def chk(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (" | " + extra if extra else ""))
    if not cond:
        fails.append(name)


def base_msg(**kw):
    m = {"id": 1, "ts": 0, "chat_type": "group", "chat_id": "602323545",
         "chat_name": "鸽子窝", "message_id": "m1", "sender_id": 2831665572,
         "sender_name": "zhaoben", "triggered": True, "trigger_reason": "at",
         "text": "", "at_me": True, "at_users": [], "image_urls": [],
         "has_reply": False, "reply_to": None, "reply_text": "",
         "face_count": 0, "has_record": False, "reply_id": "",
         "record_file": "", "record_url": "", "forward_lines": []}
    m.update(kw)
    return m


def build(msg):
    msgs, pname = A.build_messages(d, msg, ctx_k=32)
    user = [m for m in msgs if m.get("role") == "user"][-1]["content"]
    return user, pname


# 场景1：裸@带 prev
m1 = base_msg(bare_at_prev={"text": "你用mcp了吗", "ts": 0})
u1, p1 = build(m1)
chk("1 裸@带prev：提示上一条内容", "你用mcp了吗" in u1 and "多半就是在说这条" in u1, p1)
chk("1 不再出现图片误导占位", "（发了一张图/表情）" not in u1)

# 场景2：裸@无 prev（图也没并进来）
m2 = base_msg()
u2, _ = build(m2)
chk("2 裸@无prev：引导询问", "只@了你" in u2 and "问 TA 想说什么" in u2)
chk("2 无图片误导占位", "（发了一张图/表情）" not in u2)

# 场景3：正常文字消息（占位符不受影响）
m3 = base_msg(text="今天天气不错", trigger_reason="chat_random", at_me=False)
u3, _ = build(m3)
chk("3 正常消息原样", "今天天气不错" in u3)

# 场景4：纯图消息占位仍正确（不误伤）
m4 = base_msg(text="", at_me=False, image_urls=["https://x/1.jpg"],
              trigger_reason="chat_random")
u4, _ = build(m4)
chk("4 纯图占位保留", "（发了一张图/表情）" in u4)

# 场景5：裸@+并入的图（prev 是纯图）
m5 = base_msg(bare_at_prev={"text": "[图片]", "ts": 0},
              image_urls=["https://x/cat.jpg"])
u5, _ = build(m5)
chk("5 裸@+图：指路看图", "并进本条" in u5 or "已附在下方" in u5)

# 场景6：引用消息（回归：rt_note 三分支不炸）
m6 = base_msg(text="这是什么意思呀", has_reply=True, reply_id="r1",
              reply_to={"sender_name": "某人", "text": "被引的话", "is_bot": False},
              reply_text="被引的话", trigger_reason="reply_to_bot", at_me=False)
u6, _ = build(m6)
chk("6 引用消息正常构建", "被引的话" in u6)

print()
print("失败 %d 项: %s" % (len(fails), fails) if fails else "全部通过 ✓")
sys.exit(1 if fails else 0)
