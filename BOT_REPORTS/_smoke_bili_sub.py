# -*- coding: utf-8 -*-
"""bili_sub 冒烟：mock PluginAPI + 真实 B站 API。
覆盖：register/load_state/probe/sub_add/水位基线/新动态推送/直播状态沿/参数校验。"""
import os
import sys
import time

sys.path.insert(0, r"D:/ZCode/workspaces/qq-bot/market/bili_sub")
sys.path.insert(0, r"D:/ZCode/workspaces/qq-bot/plugins/qq-bot/scripts")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import plugin as P  # noqa: E402

fails = []
sent = []          # (chat_type, chat_id, texts)
sent_imgs = []     # (chat_type, chat_id, file)
store = {}


def chk(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (" | " + str(extra)[:110] if extra else ""))
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
        print("   [log]", str(msg)[:120])
    def send(self, ct, cid, texts):
        sent.append((ct, cid, texts))
    def send_image(self, ct, cid, file, caption=None):
        sent_imgs.append((ct, cid, file))


api = MockAPI()
P.register(api)

# ① load_state 结构
st = api.r_load_state({})
chk("1 load_state ok/含白名单/登录态",
    st.get("ok") and "wl_groups" in st and isinstance(st.get("bili_login"), bool),
    "login=%s wl=%d" % (st.get("bili_login"), len(st.get("wl_groups") or {})))

# ② probe 真实 uid
pr = api.r_probe({"uid": "https://space.bilibili.com/2267573"})
chk("2 probe 真实空间链接→uid+名字", pr.get("ok") and pr.get("uid") == "2267573",
    pr.get("name") or pr.get("error"))

# ③ sub_add 校验链
bad = api.r_sub_add({"uid": "abc"})
chk("3a 非法uid拒绝", not bad.get("ok"))
bad = api.r_sub_add({"uid": "2267573", "groups": []})
chk("3b 无目标群拒绝", not bad.get("ok"))
bad = api.r_sub_add({"uid": "2262536", "groups": ["602323545"]})
chk("3c 无勾选类型拒绝", not bad.get("ok"))
r = api.r_sub_add({"uid": "https://space.bilibili.com/2262536/", "name": "测试UP",
                   "av": 1, "draw": 1, "live": 1, "groups": ["602323545", "1072592437"]})
chk("3d 添加成功", r.get("ok") and len(store["subs"]) == 1, r.get("sub", {}).get("groups"))
dup = api.r_sub_add({"uid": "2262536", "av": 1, "groups": ["602323545"]})
chk("3e 重复订阅拒绝", not dup.get("ok"))

# ④ 首轮轮询=只记水位不推
store["interval"] = 2
store["next_poll"] = 0
api.tick_fn(None)
for _ in range(40):
    if not P._busy["on"]:
        break
    time.sleep(0.5)
chk("4a 首轮零推送（防轰炸）", len(sent) == 0 and len(sent_imgs) == 0)
chk("4b 水位已记", bool((store.get("last_dyn") or {}).get("2262536")),
    (store.get("last_dyn") or {}).get("2262536"))
chk("4c 直播基线已记", "2262536" in (store.get("live_on") or {}),
    (store.get("live_on") or {}).get("2262536"))

# ⑤ 模拟水位回拨=全量当新动态（真实API+推送路径）
sent.clear()
store["last_dyn"] = {"2262536": "1"}
store["next_poll"] = 0
api.tick_fn(None)
for _ in range(60):
    if not P._busy["on"]:
        break
    time.sleep(0.5)
chk("5a 推送到两个群", len(sent) >= 2, "sent=%d imgs=%d" % (len(sent), len(sent_imgs)))
if sent:
    t = sent[0][2][0]
    chk("5b 推送格式（【B站前缀+链接）",
        ("bilibili.com/video/" in t or "t.bilibili.com/" in t) and "【B站" in t,
        t[:90])
chk("5b2 图文动态图片已发（下载+send_image 全链）", len(sent_imgs) > 0,
    sent_imgs[0][2] if sent_imgs else "")
groups_hit = {s[1] for s in sent}
chk("5c 目标群全覆盖", {"602323545", "1072592437"} <= groups_hit, groups_hit)

# ⑥ 直播 0→1 状态沿（mock _fetch_live）
sent.clear()
orig_fetch = P._fetch_live
P._fetch_live = lambda uids: {list(uids)[0]: {"live_status": 1, "title": "测试直播标题",
                                              "uname": "测试UP", "room_id": 12345}}
store["live_on"] = {"2262536": 0}
P._poll_live_all(api, store["subs"], store["live_on"])
P._fetch_live = orig_fetch
chk("6 开播推送（0→1 沿）", len(sent) == 2 and "live.bilibili.com/12345" in sent[0][2][0],
    sent[0][2][0][:80] if sent else "")

# ⑦ sub_update / set_interval / test_push / sub_del
u = api.r_sub_update({"uid": "2262536", "av": 0, "groups": ["602323545"]})
chk("7a 更新订阅", u.get("ok") and store["subs"][0]["av"] == 0
    and store["subs"][0]["groups"] == ["602323545"])
iv = api.r_set_interval({"interval": 200})
chk("7b 间隔 clamp", iv.get("ok") and iv["interval"] == 60)
tp = api.r_test_push({"group": "602323545"})
chk("7c 测试推送", tp.get("ok") and "插件工作正常" in sent[-1][2][0])
dl = api.r_sub_del({"uid": "2262536"})
chk("7d 退订", dl.get("ok") and store["subs"] == [])

print()
print("失败 %d 项: %s" % (len(fails), fails) if fails else "全部通过 ✓")
sys.exit(1 if fails else 0)
