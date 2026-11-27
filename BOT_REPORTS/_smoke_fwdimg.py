# -*- coding: utf-8 -*-
"""冒烟：Z-8 合并转发记录里的图（2026-11-27）。

覆盖：
  A1 转发记录消息（6 图、文字含「这是什么」）被闲聊抽中：不进识图、不等后台
     描述、不登记速写、视觉模型零调用；走纯文本回复；发给模型的提示带
     「转发的聊天记录」系统注；_img_ref_check 对它判否（三处「提到图」判断
     共用的入口）；image_urls 字段本身不被删改。
  A2 普通带图消息文字含「这是什么」：仍判为提到图（骰子跳过、等后台描述）；
     对照组（不含指代词）照常掷骰（概率 0 → 未中跳过）。
  A3 普通消息 6 张图全部能本地化：视觉调用只带 4 张图片段，系统注写明
     「共 6 张图，只能看到附带的 4 张（按顺序是第 1 到第 4 张）」。
  A4 3 张图里 1 张本地化失败：带 2 张，系统注写明「共 3 张图，只能看到附带
     的 2 张」；中间那张失败时不谎报「第 1 到第 2 张」；末尾失败时如实带序号。
  A5 单图成功：系统注与改动前逐字一致（改动前字符串原样钉进断言）。
  A6 全部本地化失败：照旧抛「图片全部获取失败」，由调用方降级。
  B1 daemon 邻近图并入：就地展平的转发记录（forward_inline）的内部图不进
     _nearby_img 缓存，之后的纯文字条不再并进记录内部的图。
  B2 daemon 历史回捞：转发记录行（text 带前缀）的内部图不被回捞进后续
     「这图」类消息；对照组普通带图行照常回捞。
  B3 agent 侧不登记速写的两条路：观察队列轮询跳过转发记录条目（对照组普通
     条目照常登记）；_IMG_REF_RE 正则本身一字未改。
  A7/A8（裁决 2）：「该不该接话」判定按纯文字看待——v2 判定与旧轻量判定
     对转发记录不再因带图跳过（判「不接」→ 不回复、不调主模型）；对照组
     普通带图消息（未等到描述）仍跳过 v2 判定（行为不变）。
  B4/B5（裁决 1）：引用转发记录——本地历史命中与 get_msg 兜底两条路径都
     不再把被引记录内部的图并进当前消息视觉输入；引用普通带图消息照旧并入。

隔离：import 被测模块前把 QQBOT_DATA_DIR 指到一次性临时目录（照
_smoke_visionprobe.py 的做法）；daemon 用 ws_url=""/ctl_port=0 的离线实例
（照 _smoke_vision_merge2.py 的做法）；全部模型调用打桩，零联网。
用法：python BOT_REPORTS/_smoke_fwdimg.py
"""
import contextlib
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join("plugins", "qq-bot", "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 数据目录隔离（必须在 import 被测模块之前生效）
_SMOKE_OWN_DATA_DIR = None
if not os.environ.get("QQBOT_DATA_DIR"):
    _SMOKE_OWN_DATA_DIR = tempfile.mkdtemp(prefix="qqbot_fwdimg_iso_")
    os.environ["QQBOT_DATA_DIR"] = _SMOKE_OWN_DATA_DIR

import qq_daemon as D  # noqa: E402
import qq_bot_agent as A  # noqa: E402

fails = []


def chk(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name
          + (" | " + str(extra)[:150] if extra else ""))
    if not cond:
        fails.append(name)


@contextlib.contextmanager
def patched(obj, **names):
    old = {k: getattr(obj, k) for k in names}
    for k, v in names.items():
        setattr(obj, k, v)
    try:
        yield old
    finally:
        for k, v in old.items():
            setattr(obj, k, v)


ALOG = []


# 改动前的识图系统注（call_vision 里的 _ground，改动前原样抄录）——
# A5 用它钉住「单图成功时系统注逐字不变」。
_GROUND_BEFORE = ("\n\n（系统注：这条消息带有图片，图片就在下方——必须先看懂图片"
                  "实际内容再回复：文字的意思往往建立在图片语境上（梗、价格、"
                  "画面、表情、场景），只按文字字面回答会答非所问；回复要体现"
                  "你真的看懂了图，但别用说明腔，自然聊。特别注意：文字里的"
                  "\"这个/那个/它/图中\"等指代词，指的就是图片里的主体——不是"
                  "聊天历史里的任何东西（历史里在聊食物而图是遮光罩时，\"想买"
                  "这个\"指的是遮光罩）。）")


class _RTFake(object):
    """call_vision 用到的 RT 面：cooldowns / cooldown / set_current。"""

    def __init__(self):
        self.cooldowns = {}
        self.current = None

    def cooldown(self, k, sec=0):
        self.cooldowns[k] = time.time() + max(0, sec)

    def set_current(self, m):
        self.current = m


class NullDaemon(object):
    """agent 侧桩：只带 process 会用到的方法，其余 no-op。"""

    def __init__(self, history=None):
        self._history = list(history or [])
        self.acked = []
        self.sent = []

    def history(self, chat_type, chat_id, limit=30):
        return self._history[-limit:] if limit else list(self._history)

    def ack(self, ids):
        self.acked += list(ids)

    def send(self, chat_type, chat_id, texts, reply_message_id=None):
        self.sent.append(list(texts))

    def observe(self, since=0):
        return (self._seq, self._items)

    _seq = 0
    _items = []

    def __getattr__(self, name):
        def _noop(*a, **k):
            return {"ok": False, "error": "smoke: no side effects"}
        return _noop


def group_msg(text, mid, trig="chat_random", sender="777", msg_id=7001, **kw):
    m = {"id": msg_id, "chat_type": "group", "chat_id": "555001",
         "chat_name": "冒烟群", "sender_id": sender, "sender_name": "小张",
         "text": text, "trigger_reason": trig, "message_id": mid,
         "image_urls": [], "at_me": False, "at_users": [], "has_reply": False,
         "reply_to": None, "reply_text": "", "face_count": 0}
    m.update(kw)
    return m


# ---------------- A1：转发记录被闲聊抽中 → 按纯文字处理 ----------------

FWD_TEXT = ("[合并转发记录，30条]\n张三: 我其实还不知道这是什么类型的游戏 "
            "[图片]\n李四: 冲 [图片]\n王五: 好家伙 [图片]")
IMG6 = ["https://img.example/%d.jpg" % i for i in range(1, 7)]


def run_process_case(msg, **extra):
    """统一的 process() 打桩面：识图/速写/生成全记录，模型零真实调用。
    extra：按场景覆盖单个开关桩（如 chat_anti_interrupt_enabled、_reply_gate_v2）。"""
    rec = {"vcalls": [], "caps": [], "cap_gets": [], "v2msgs": [],
           "bm_msgs": []}

    def _cv(messages, image_urls, daemon=None):
        rec["vcalls"].append(list(image_urls or []))
        raise AssertionError("A1/A2 不应走到同步识图")

    def _bm(daemon, m, ctx_k=32):
        msgs = [{"role": "system", "content": "SYS"},
                {"role": "user", "content": "BODY"}]
        rec["bm_msgs"].append(msgs)
        return msgs, "P"

    def _v2(daemon, m, messages, gate=""):
        rec["v2msgs"].append(messages)
        return "m2", "纯文本回复"

    def _ce(chat_type, chat_id, mid, urls, front=False, allowed=None):
        rec["caps"].append((mid, list(urls or []), front))

    def _cg(chat_type, chat_id, mid):
        rec["cap_gets"].append(mid)
        return {"texts": ["后台速写内容"]}   # 立即返回，避免真等 4 秒

    stubs = dict(build_messages=_bm, call_vision=_cv,
                 _generate_reply_v2=_v2, _caption_enqueue=_ce,
                 _caption_get=_cg, reply_v2_on=lambda: True,
                 vision_enabled=lambda: True,
                 vision_mode=lambda: "all_prob",
                 vision_caption_on=lambda: True,
                 vision_prob=lambda: 0.0,
                 chat_anti_interrupt_enabled=lambda: False,
                 lean_ctx_on=lambda: True,
                 owner_qq=lambda: "10086")
    stubs.update(extra)
    dm = NullDaemon()
    with patched(A, out=lambda line: ALOG.append(str(line)),
                 log_usage=lambda *a, **k: None,
                 _log_context_dump=lambda *a, **k: None):
        A.PLUGIN_HOST.on_message_all = lambda msg: None
        A.PLUGIN_HOST.daemon = dm
        with patched(A, **stubs):
            n0 = len(ALOG)
            A.process(dm, msg)
            rec["log"] = ALOG[n0:]
    rec["dm"] = dm
    return rec


_mids = iter(range(7100, 7900))

msg_fwd = group_msg(FWD_TEXT, "fw%d" % next(_mids),
                    image_urls=list(IMG6))
rec1 = run_process_case(msg_fwd)

chk("A1a 判成转发记录（_fwd_record 打标）", msg_fwd.get("_fwd_record") is True)
chk("A1b image_urls 字段本身未被删改（仍是 6 个）",
    msg_fwd.get("image_urls") == IMG6)
chk("A1c 视觉模型零调用", rec1["vcalls"] == [], rec1["vcalls"])
chk("A1d 不登记后台速写", rec1["caps"] == [], rec1["caps"])
chk("A1e 不等后台描述（_caption_get 零调用、_vcap 未设）",
    rec1["cap_gets"] == [] and not msg_fwd.get("_vcap"))
chk("A1f 走纯文本回复（_generate_reply_v2 生成、消息发出）",
    len(rec1["v2msgs"]) == 1 and rec1["dm"].sent
    and any("纯文本回复" in t for t in rec1["dm"].sent[-1]), rec1["dm"].sent)
_note_ok = rec1["v2msgs"] and rec1["v2msgs"][0][1]["content"] == \
    "BODY\n\n" + A._FWD_RECORD_NOTE
chk("A1g 提示带「转发的聊天记录」系统注（原文钉住，正文保留）", _note_ok,
    (rec1["v2msgs"][0][1]["content"][:120] if rec1["v2msgs"] else ""))
chk("A1h 「提到图」判断为假（三处共用的 _img_ref_check 入口）",
    A._img_ref_check(msg_fwd) is False)
chk("A1i 无识图/速写相关日志",
    not any("AGENT_VISION" in l or "AGENT_VCAP" in l for l in rec1["log"]),
    rec1["log"])
chk("A1j 消息被正常 ack 收尾", msg_fwd["id"] in rec1["dm"].acked)

# ---------------- A2：普通带图消息含「这是什么」→ 行为不变 ----------------

msg_norm = group_msg("这是什么", "nm%d" % next(_mids),
                     image_urls=["https://img.example/n1.jpg"])
rec2 = run_process_case(msg_norm)
chk("A2a 普通消息仍判「提到图」", A._img_ref_check(msg_norm) is True)
chk("A2b 本场景日志有 VCAP_HIT、无 PROB_SKIP（不掷骰，直接等描述）",
    any("AGENT_VCAP_HIT" in l for l in rec2["log"])
    and not any("AGENT_VISION_PROB_SKIP" in l for l in rec2["log"]),
    rec2["log"])
chk("A2c 等描述有登记（front=True 插队）",
    any(f[2] is True for f in rec2["caps"]), rec2["caps"])
chk("A2d 拿到速写后不再同步识图",
    rec2["vcalls"] == [] and msg_norm.get("_vcap") == "后台速写内容")

# 对照组：不含指代词的普通带图消息照常掷骰（概率 0 → 未中跳过、登记后排队）
msg_ctrl = group_msg("今天天气真不错", "cm%d" % next(_mids),
                     image_urls=["https://img.example/c1.jpg"])
rec3 = run_process_case(msg_ctrl)
chk("A2e 对照组（无指代词）掷骰未中 → PROB_SKIP、不等待",
    any("AGENT_VISION_PROB_SKIP" in l for l in rec3["log"])
    and rec3["cap_gets"] == [] and not msg_ctrl.get("_vcap"), rec3["log"])
chk("A2f 对照组仍登记速写（无人在等，排后续队）",
    any(f[2] is False for f in rec3["caps"]), rec3["caps"])

# ---------------- A7/A8（裁决 2）：「该不该接话」判定按纯文字看待 ----------------

_gate_calls = []


def _gate_n(daemon, m, messages, persona):
    _gate_calls.append(m.get("message_id"))
    return "N"


def _gate_old(daemon, m, persona_name):
    _gate_calls.append(m.get("message_id"))
    return True   # 不插话


# A7：v2 判定——转发记录被闲聊抽中 → 进判定；判「不接」→ 不回复、不调主模型
msg_fwd2 = group_msg(FWD_TEXT, "fw%d" % next(_mids), image_urls=list(IMG6))
rec_g = run_process_case(msg_fwd2, chat_anti_interrupt_enabled=lambda: True,
                         _reply_gate_v2=_gate_n)
chk("A7a 转发记录进了 v2 接话判定（不再被当成带图消息跳过）",
    _gate_calls == [msg_fwd2["message_id"]], _gate_calls)
chk("A7b 判定不接 → 不回复、不调主模型（_generate_reply_v2 零调用）",
    rec_g["v2msgs"] == [] and rec_g["dm"].sent == []
    and msg_fwd2["id"] in rec_g["dm"].acked
    and any("AGENT_GATE_V2" in l and "verdict=N" in l for l in rec_g["log"]),
    rec_g["log"])
chk("A7c 判定不接也不走识图、不登记速写、不带转发记录之外的视觉痕迹",
    rec_g["vcalls"] == [] and rec_g["caps"] == [] and rec_g["cap_gets"] == []
    and msg_fwd2.get("_fwd_record") is True)

# 对照：普通带图消息（all_prob、还没等到描述）仍跳过 v2 判定——行为不变。
# 掷骰必中（vision_prob 1.0）后真等 4 秒等不到描述，走到 gate 条件处被图片
# 条件跳过（这正是裁决要求保留的原行为）。
_gate_calls.clear()
msg_img2 = group_msg("普通闲聊一句", "ni%d" % next(_mids),
                     image_urls=["https://img.example/z1.jpg"])
rec_gc = run_process_case(msg_img2, chat_anti_interrupt_enabled=lambda: True,
                          _reply_gate_v2=_gate_n,
                          vision_prob=lambda: 1.0,
                          _caption_get=lambda *a, **k: None,
                          call_vision=lambda messages, image_urls, daemon=None:
                          ("vm", "好图"))
chk("A7d 普通带图消息（未等到描述）仍跳过 v2 判定",
    _gate_calls == [] and not msg_img2.get("_vcap")
    and any("AGENT_VCAP_MISS" in l for l in rec_gc["log"]), _gate_calls)

# A8：旧轻量判定（v2 关）——转发记录同样按纯文字进判定
_gate_calls.clear()
msg_fwd3 = group_msg(FWD_TEXT, "fw%d" % next(_mids), image_urls=list(IMG6))
rec_g2 = run_process_case(msg_fwd3, reply_v2_on=lambda: False,
                          chat_anti_interrupt_enabled=lambda: True,
                          _interrupt_gate=_gate_old)
chk("A8a 转发记录进了旧轻量判定",
    _gate_calls == [msg_fwd3["message_id"]], _gate_calls)
chk("A8b 判定不插话 → ack 收尾、不生成回复",
    msg_fwd3["id"] in rec_g2["dm"].acked and rec_g2["dm"].sent == []
    and any("AGENT_GATE_SKIP" in l for l in rec_g2["log"]), rec_g2["log"])

# ---------------- A3–A6：call_vision 缺图如实告知 ----------------


def run_call_vision(urls, ok_urls):
    """打桩本地化与模型链，跑一次 call_vision，带回视觉请求与结果。"""
    rec = {"attempts": []}

    def _uri(u, daemon=None):
        return "data:image/png;base64,AAA" if u in ok_urls else None

    def _attempt(m, kv, vmsgs, th, mt):
        rec["attempts"].append(vmsgs)
        return (200, {"choices": [{"message": {"content": "看到了"}}]})

    def _no_glm(*a, **k):
        raise AssertionError("离线冒烟不应直接调 glm_chat")

    with patched(A, out=lambda line: ALOG.append(str(line)),
                 log_usage=lambda *a, **k: None, RT=_RTFake(),
                 glm_chat=_no_glm,
                 vision_model=lambda: "vm-x",
                 _vision_candidates=lambda pref: [("k1", "KEY", "vm-x", 0, 0)],
                 resolve_thinking=lambda m, t="": (False, 64),
                 _img_to_datauri=_uri, _vision_attempt=_attempt):
        try:
            m, text = A.call_vision(
                [{"role": "system", "content": "SYS"},
                 {"role": "user", "content": "BODY"}], list(urls))
            rec["text"], rec["model"] = text, m
            rec["err"] = None
        except RuntimeError as e:
            rec["err"] = str(e)
    return rec


_all6 = ["https://img.example/g%d.jpg" % i for i in range(1, 7)]
rec_v6 = run_call_vision(_all6, set(_all6))
_v6 = rec_v6["attempts"][0][1]["content"]
_parts6 = [p for p in _v6 if p.get("type") == "image_url"]
chk("A3a 6 图全可本地化：只带 4 个图片段", len(_parts6) == 4, len(_parts6))
chk("A3b 系统注写明共 6 张、只看到 4 张、按顺序是第 1 到第 4 张",
    "共 6 张图" in _v6[0]["text"]
    and "只能看到附带的 4 张" in _v6[0]["text"]
    and "（按顺序是第 1 到第 4 张）" in _v6[0]["text"],
    _v6[0]["text"][-200:])
chk("A3c 缺图说明在原系统注之后（原注仍完整）",
    _v6[0]["text"].startswith("BODY" + _GROUND_BEFORE))

_three = ["https://img.example/h1.jpg", "https://img.example/h2.jpg",
          "https://img.example/h3.jpg"]
rec_v3mid = run_call_vision(_three, {_three[0], _three[2]})   # 中间那张失败
_v3m = rec_v3mid["attempts"][0][1]["content"]
chk("A4a 3 图 1 失败（中间）：带 2 个图片段",
    len([p for p in _v3m if p.get("type") == "image_url"]) == 2)
chk("A4b 系统注写明共 3 张、只看到 2 张",
    "共 3 张图" in _v3m[0]["text"] and "只能看到附带的 2 张" in _v3m[0]["text"],
    _v3m[0]["text"][-200:])
chk("A4c 附的不是前 2 张时不谎报序号",
    "第 1 到第" not in _v3m[0]["text"], _v3m[0]["text"][-200:])

rec_v3tail = run_call_vision(_three, {_three[0], _three[1]})  # 末尾那张失败
_v3t = rec_v3tail["attempts"][0][1]["content"]
chk("A4d 附的恰好是前 2 张时如实带序号",
    "（按顺序是第 1 到第 2 张）" in _v3t[0]["text"], _v3t[0]["text"][-200:])

rec_v1 = run_call_vision(["https://img.example/one.jpg"],
                         {"https://img.example/one.jpg"})
_v1 = rec_v1["attempts"][0][1]["content"]
chk("A5 单图成功：系统注与改动前逐字一致",
    _v1[0]["text"] == "BODY" + _GROUND_BEFORE,
    repr(_v1[0]["text"][-120:]))

rec_v0 = run_call_vision(["https://img.example/f1.jpg",
                          "https://img.example/f2.jpg"], set())
chk("A6 全部本地化失败：照旧抛「图片全部获取失败」",
    rec_v0["err"] == "视觉调用：图片全部获取失败" and not rec_v0["attempts"],
    rec_v0["err"])

# ---------------- B：daemon 两处并入不再吃转发记录内部的图 ----------------

GID = "555001"
OWNER = 3349525227
BOT_SELF = 1234567
CK = "group_%s_%s" % (GID, OWNER)
_mid = iter(range(1000, 1999))


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


FWD_SUBS = [
    {"sender": {"user_id": 111, "nickname": "张三"},
     "message": [{"type": "text",
                  "data": {"text": "我其实还不知道这是什么类型的游戏"}},
                 {"type": "image", "data": {"url": "https://img.example/1.jpg"}}]},
    {"sender": {"user_id": 222, "nickname": "李四"},
     "message": [{"type": "image", "data": {"url": "https://img.example/2.jpg"}},
                 {"type": "text", "data": {"text": "冲"}}]},
]

d = D.Daemon(ws_url="", token="", cfg={}, ctl_port=0)
DLOGSink = []
D.out = lambda line: DLOGSink.append(str(line))

chk("B0 展平前缀判定常量与实际拼接一致",
    D._forward_flattened_text(["a: b"]).startswith(D._FWD_TEXT_PREFIX)
    and D._FWD_TEXT_PREFIX == "[合并转发记录"
    and A._FWD_RECORD_PREFIX == D._FWD_TEXT_PREFIX)

# B1：就地展平的转发记录 → 内部图不进邻近图缓存
d.inbox.clear()
d._nearby_img.clear()
t0 = time.time()
d._handle_message_event(ev_of(
    [{"type": "forward", "data": {"content": FWD_SUBS}}], ts=t0))
fwd_item = d.inbox[-1] if d.inbox else None
chk("B1a 转发记录照常展平入库（带前缀文字 + 内部图仍在 image_urls）",
    bool(fwd_item and fwd_item.get("text", "").startswith("[合并转发记录")
         and len(fwd_item.get("image_urls") or []) == 2),
    (fwd_item or {}).get("text", "")[:60])
chk("B1b 记录内部图不进 _nearby_img 缓存", CK not in d._nearby_img,
    d._nearby_img.keys())
d._handle_message_event(txt_ev("继续", ts=t0 + 3))
cont = d.inbox[-1] if d.inbox else None
chk("B1c 之后的纯文字条不再并进记录内部的图",
    bool(cont and cont.get("text") == "继续"
         and not cont.get("image_urls")
         and not cont.get("merged_nearby_image")),
    (cont or {}).get("image_urls"))

# B2：历史回捞跳过转发记录行；对照组普通带图行照常回捞
d.inbox.clear()
hk = "group_%s" % GID
d.mem_history[hk] = []
d._handle_message_event(at_txt_ev("这图呢"))
time.sleep(3.2)   # 无图触发的文字条走 2.5s 延迟入箱（既定行为）
miss = d.inbox[-1] if d.inbox else None
chk("B2a 转发记录行不被回捞（触发的「这图」不带记录内部图）",
    bool(miss and "这图呢" in str(miss.get("text") or "")
         and not miss.get("image_urls")),
    (miss or {}).get("image_urls"))
d.inbox.clear()
d.mem_history[hk] = [{"ts": time.time() - 30, "message_id": "ok1",
                      "sender_id": "999", "sender_name": "路人",
                      "is_bot": False, "text": "看看这个",
                      "n_images": 1,
                      "image_urls": ["https://img.example/ok.jpg"]}]
d._handle_message_event(at_txt_ev("再看一下这图"))
got = d.inbox[-1] if d.inbox else None
chk("B2b 对照组：普通带图行照常回捞（行为不变）",
    bool(got and got.get("image_urls")
         and "https://img.example/ok.jpg" in got["image_urls"]
         and got.get("merged_nearby_image")),
    (got or {}).get("image_urls"))

# B3：观察队列登记速写跳过转发记录条目
cap_calls = []
_dmb = NullDaemon()
_dmb._seq, _dmb._items = 6, [
    {"chat_type": "group", "chat_id": GID, "message_id": "fw1",
     "text": "[合并转发记录，2条]\n张三: 看图 [图片]",
     "image_urls": ["https://img.example/1.jpg"]},
    {"chat_type": "group", "chat_id": GID, "message_id": "ok2",
     "text": "看看这个", "image_urls": ["https://img.example/ok.jpg"]}]
_cap_save = {k: A._CAP[k] for k in ("init", "seq", "poll")}
try:
    with patched(A, _caption_enqueue=lambda *a, **k: cap_calls.append(a),
                 _cap_chat_allowed=lambda ct, cid: True):
        A._CAP["init"] = True
        A._CAP["seq"] = 5
        A._CAP["poll"] = 0.0
        A._caption_poll(_dmb)
finally:
    for k, v in _cap_save.items():
        A._CAP[k] = v
_mids_enq = [c[2] for c in cap_calls]
chk("B3a 观察队列：转发记录条目不登记速写、普通条目照常",
    _mids_enq == ["ok2"], _mids_enq)

chk("B3b _IMG_REF_RE 正则本身一字未改（对「这是什么」仍命中）",
    A._IMG_REF_RE.search("这是什么") is not None
    and A._IMG_REF_RE.search("这图") is not None)

# ---------------- B4/B5（裁决 1）：引用转发记录不再并进记录内部图 ----------------


def reply_ev(rid, t, uid=OWNER):
    return {"message_type": "group", "group_id": GID, "self_id": BOT_SELF,
            "time": time.time(), "message_id": "m%d" % next(_mid),
            "sender": {"user_id": uid, "nickname": "鸽纸"},
            "message": [{"type": "reply", "data": {"id": rid}},
                        {"type": "text", "data": {"text": t}}]}


def poll_inbox(d, pred, timeout=6.0):
    """等补查/重编号线程把 item 送进收件箱。"""
    end = time.time() + timeout
    while time.time() < end:
        for it in reversed(d.inbox):
            if pred(it):
                return it
        time.sleep(0.1)
    return None


# B4：本地历史命中路径——被引行带展平前缀 → 不并图；普通带图行照旧
d2 = D.Daemon(ws_url="", token="", cfg={}, ctl_port=0)
hk2 = "group_%s" % GID
d2.mem_history[hk2] = [
    {"ts": time.time() - 60, "message_id": "q1", "sender_id": "111",
     "sender_name": "张三", "is_bot": False,
     "text": D._forward_flattened_text(["张三: 看这个 [图片]"]),
     "n_images": 1, "image_urls": ["https://img.example/1.jpg"]},
    {"ts": time.time() - 30, "message_id": "q2", "sender_id": "222",
     "sender_name": "李四", "is_bot": False, "text": "看看这张图",
     "n_images": 1, "image_urls": ["https://img.example/q2.jpg"]}]
d2.inbox.clear()
d2._handle_message_event(reply_ev("q1", "引用一下这个"))
r1 = poll_inbox(d2, lambda it: it.get("reply_id") == "q1")
chk("B4a 引用转发记录行：记录内部图不并进视觉输入",
    bool(r1) and not r1.get("image_urls") and not r1.get("reply_has_image"),
    (r1 or {}).get("image_urls"))
chk("B4b 被引摘要照常给（展平文字做被引原文）",
    bool(r1 and (r1.get("reply_to") or {}).get("text", "")
         .startswith("[合并转发记录")),
    (r1 or {}).get("reply_to"))
d2._handle_message_event(reply_ev("q2", "引用一条带图的"))
r2 = poll_inbox(d2, lambda it: it.get("reply_id") == "q2")
chk("B4c 引用普通带图行：照旧并进被引图（行为不变）",
    bool(r2 and "https://img.example/q2.jpg" in (r2.get("image_urls") or [])),
    (r2 or {}).get("image_urls"))

# B5：get_msg 兜底路径（本地历史没有被引行）——被引是转发卡片 → 不并图
d3 = D.Daemon(ws_url="", token="", cfg={}, ctl_port=0)
d3.mem_history[hk2] = []
_fwd_card = {"message": [{"type": "forward", "data": {"content": FWD_SUBS}}],
             "sender": {"nickname": "张三", "user_id": 111}}
_pic_card = {"message": [{"type": "image",
                          "data": {"url": "https://img.example/q5.jpg"}}],
             "sender": {"nickname": "李四", "user_id": 222}}
d3._call_action = lambda action, params, timeout=None: (
    _fwd_card if str((params or {}).get("message_id")) == "q5" else _pic_card)
d3.inbox.clear()
d3._handle_message_event(reply_ev("q5", "引用一个转发"))
r5 = poll_inbox(d3, lambda it: it.get("reply_id") == "q5")
chk("B5a get_msg 兜底：被引是转发卡片 → 不并图、不标 reply_has_image",
    bool(r5) and not r5.get("image_urls") and not r5.get("reply_has_image"),
    (r5 or {}).get("image_urls"))
chk("B5b 兜底路径被引摘要照常给",
    bool(r5 and (r5.get("reply_to") or {}).get("sender_name") == "张三"),
    (r5 or {}).get("reply_to"))
d3._handle_message_event(reply_ev("q6", "引用一张图"))
r6 = poll_inbox(d3, lambda it: it.get("reply_id") == "q6")
chk("B5c get_msg 兜底：普通被引图照旧并入并标 reply_has_image（行为不变）",
    bool(r6 and "https://img.example/q5.jpg" in (r6.get("image_urls") or [])
         and r6.get("reply_has_image")),
    (r6 or {}).get("image_urls"))

# ---------------- 收尾 ----------------

if _SMOKE_OWN_DATA_DIR:
    shutil.rmtree(_SMOKE_OWN_DATA_DIR, ignore_errors=True)

print("\n== 结果: %s ==" % ("全部通过" if not fails else "失败 %d 项" % len(fails)))
sys.exit(1 if fails else 0)
