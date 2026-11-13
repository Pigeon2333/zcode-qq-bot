# -*- coding: utf-8 -*-
"""bili_sub 1.1.0 冒烟：推送历史（per-target 成败）+手动重推+私聊目标。"""
import os
import sys
import time

sys.path.insert(0, r"D:/ZCode/workspaces/qq-bot/market/bili_sub")
sys.path.insert(0, r"D:/ZCode/workspaces/qq-bot/plugins/qq-bot/scripts")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import plugin as P  # noqa: E402

fails = []
sent = []          # (chat_type, chat_id, texts)
sent_imgs = []
store = {}


def chk(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (" | " + str(extra)[:90] if extra else ""))
    if not cond:
        fails.append(name)


class MockAPI:
    def on_tick(self, fn):
        self.tick_fn = fn
    def every(self, m, fn):
        return fn
    def route(self, name, fn):
        setattr(self, "r_" + name, fn)
    def store(self, k, v):
        store[k] = v
    def load(self, k, default=None):
        return store.get(k, default)
    def log(self, msg):
        print("   [log]", str(msg)[:110])
    def send(self, ct, cid, texts):
        if cid == "BAD":
            raise RuntimeError("模拟失败")
        sent.append((ct, cid, texts))
    def send_image(self, ct, cid, file, caption=None):
        sent_imgs.append((ct, cid, file))


api = MockAPI()
P.register(api)

# ① 订阅带群+私聊双目标
r = api.r_sub_add({"uid": "2262536", "name": "测试UP", "av": 1,
                   "groups": ["602323545"], "privates": ["3349525227"]})
chk("1 群+私聊双目标订阅", r.get("ok") and
    r["sub"]["privates"] == ["3349525227"], r.get("sub", {}).get("privates"))

# ② 直播推送：历史记录 + 双目标 + per-target 成败
sent.clear()
P._send_out(api, store["subs"][0],
            "【B站直播】测试UP 开播了：\n「测试」\nhttps://live.bilibili.com/1")
hist = store.get("history") or []
chk("2 历史已记录", len(hist) == 1 and hist[0]["kind"] == "live", hist[0]["kind"])
chk("2b 双目标各送达", sorted((x[0], x[1]) for x in sent) ==
    [("group", "602323545"), ("private", "3349525227")], sent)
chk("2c ok表全真", hist[0]["ok"] == {"group:602323545": True,
                                    "private:3349525227": True}, hist[0]["ok"])

# ③ 目标失败场景（BAD 私聊目标）
sent.clear()
store["subs"][0]["privates"] = ["BAD"]
P._send_out(api, store["subs"][0], "【B站动态】测试UP：\n内容\nhttps://t.bilibili.com/1")
hist = store["history"]
chk("3 失败目标记 False", hist[-1]["ok"].get("private:BAD") is False, hist[-1]["ok"])

# ④ history 路由
h = api.r_history({})
chk("4 history 路由", h.get("ok") and len(h.get("history") or []) == 2)

# ⑤ repush：把 ③ 那条重推到群（不 record 新历史，更新原条目）
sent.clear()
rp = api.r_repush({"hid": hist[-1]["hid"], "groups": ["602323545"]})
chk("5 重推成功", rp.get("ok") and rp["results"].get("group:602323545") is True,
    rp.get("results"))
chk("5b 原条目 ok 已更新+repush 计数",
    store["history"][-1]["ok"]["group:602323545"] is True
    and store["history"][-1]["repush"] == 1)
chk("5c 重推不新增历史", len(store["history"]) == 2)

# ⑥ repush 校验链
chk("6a hid 非法拒", not api.r_repush({"hid": "x"}).get("ok"))
chk("6b 无目标拒", not api.r_repush({"hid": 2, "groups": []}).get("ok"))
chk("6c 不存在的 hid 拒", not api.r_repush({"hid": 999}).get("ok"))

# ⑦ load_state 含 history
st = api.r_load_state({})
chk("7 load_state 带 history", len(st.get("history") or []) == 2)

# ⑧ sub_update 更新 privates
u = api.r_sub_update({"uid": "2262536", "privates": ["111", "222"]})
chk("8 privates 可更新", u.get("ok") and
    store["subs"][0]["privates"] == ["111", "222"])

# ⑨ test_push 私聊路径
sent.clear()
tp = api.r_test_push({"private": "3349525227"})
chk("9 测试推送私聊", tp.get("ok") and sent[0][0] == "private"
    and sent[0][1] == "3349525227")

# ⑩ 历史环形上限（60 条）
for i in range(65):
    P._hist_add(api, {"hid": 1000 + i, "ts": time.time(), "uid": "1", "name": "x",
                      "kind": "draw", "text": "t%d" % i, "imgs": [],
                      "ok": {}, "repush": 0})
chk("10 历史上限60", len(store["history"]) == 60,
    store["history"][0].get("text"))

print()
print("失败 %d 项: %s" % (len(fails), fails) if fails else "全部通过 ✓")
sys.exit(1 if fails else 0)
