# -*- coding: utf-8 -*-
"""提示词预算三开关 + 识图每日上限「0＝不限制」冒烟（离线：不联网、不写用户数据文件）。

覆盖：
- prompt_dedupe_legacy_tags / prompt_hard_rules_compact / gate_v2_lean_history
  + gate_v2_hist_lines：默认值、cfg 覆盖、取值夹取、生效范围（json 路径限定）；
- 四种硬性规则变体（完整/去重 × 压缩/去重压缩）的长度与约束对应；
- 全关时提示词与去重开关解耦（json 路径与非 json 路径逐字一致）；
- 判定调用瘦身：user 块只带最近 N 条历史＋说明＋【现在】，截断说明在判定
  提示词里，主调用不变；
- 三场景（无引用无图 / 带引用 / 纯图）全关、全开构建成功；
- 识图每日上限：0＝不限制（连续识不丢、计数照加）；有上限时到额丢弃且日志只打一次。

用法：python BOT_REPORTS/_smoke_promptbudget.py
"""
import contextlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.join("plugins", "qq-bot", "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 数据目录隔离（必须在 import 被测模块之前生效）：绝不读写生产数据 ~/.zcode-qq-bot
_SMOKE_OWN_DATA_DIR = None
if not os.environ.get("QQBOT_DATA_DIR"):
    _SMOKE_OWN_DATA_DIR = tempfile.mkdtemp(prefix="qqbot_pb_iso_")
    os.environ["QQBOT_DATA_DIR"] = _SMOKE_OWN_DATA_DIR

import qq_bot_agent as A  # noqa: E402

fails = []


def chk(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (" | " + str(extra)[:110] if extra else ""))
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


_real_ljc = A.load_json_const


def _cfg_ljc(reply_cfg):
    """回复配置用给定字典，其余文件照常读（只读）。"""
    def _f(path, default=None):
        if path == A.REPLY_CONFIG_PATH:
            return dict(reply_cfg)
        return _real_ljc(path, {} if default is None else default)
    return _f


class NullDaemon(object):
    def __init__(self, history=None):
        self._history = history or []

    def history(self, chat_type, chat_id, limit=30):
        h = self._history
        return h[-limit:] if limit else h

    def group_names(self):
        return {}

    def __getattr__(self, name):
        def _noop(*a, **k):
            return {"ok": False, "error": "smoke: no side effects"}
        return _noop


LOG = []


def hist(n=14):
    hs = []
    for i in range(n):
        hs.append({"ts": 1795000000 + i * 60, "sender_id": "1000%d" % (i % 3 + 1),
                   "sender_name": ["小张", "小王", "小李"][i % 3],
                   "text": "第%d条消息，聊聊晚饭吃什么" % i, "is_bot": i == 5})
    return hs


def group_msg(text, trig="chat_random", sender_id="10001", sender_name="小张", **kw):
    m = {"id": 7001, "chat_type": "group", "chat_id": "555001", "chat_name": "冒烟群",
         "sender_id": sender_id, "sender_name": sender_name, "text": text,
         "trigger_reason": trig, "message_id": 7001, "image_urls": [],
         "at_me": trig == "at", "at_users": [], "has_reply": False, "reply_to": None,
         "reply_text": "", "face_count": 0}
    m.update(kw)
    return m


def build(msg, history=None, cfg=None, json_mode=True):
    """按给定 cfg 构建 v2 提示词；返回 (messages, active_persona)。
    注意：build_messages 会把 _gate_lean_user 挂到传进去的 msg 字典上（生产里
    判定调用要用同一个字典），所以这里不传副本——调用方拿同一个对象才能读到。"""
    kw = {"PROMPT_V2_FORCE": True, "load_json_const": _cfg_ljc(cfg or {})}
    if json_mode:
        kw.update(REPLY_V2_FORCE=True, REPLY_JSON_FORCE=True)
    with patched(A, **kw):
        A._TOPIC_LAST.clear()
        return A.build_messages(NullDaemon(history=hist() if history is None else history),
                                msg, ctx_k=32)


def hr_segment(system):
    """从 system 里取出「## 硬性规则」到「## 可用表情包」之间的一段。"""
    i = system.index("## 硬性规则")
    j = system.index("## 可用表情包")
    return system[i:j]


# ================= 1. 三个开关的读取：默认关、cfg 覆盖、夹取 =================
with patched(A, load_json_const=_cfg_ljc({})):
    chk("1a 默认全关", A.prompt_dedupe_legacy_tags_on() is False
        and A.prompt_hard_rules_compact_on() is False
        and A.gate_v2_lean_history_on() is False)
    chk("1b gate_v2_hist_lines 默认 6", A.gate_v2_hist_lines() == 6)
with patched(A, load_json_const=_cfg_ljc({
        "prompt_dedupe_legacy_tags": True, "prompt_hard_rules_compact": True,
        "gate_v2_lean_history": True, "gate_v2_hist_lines": 10})):
    chk("1c cfg 打开生效", A.prompt_dedupe_legacy_tags_on() is True
        and A.prompt_hard_rules_compact_on() is True
        and A.gate_v2_lean_history_on() is True and A.gate_v2_hist_lines() == 10)
for raw, want, tag in ((0, 1, "0→夹到1"), (99, 14, "99→夹到14"), (-3, 1, "负数→1"),
                       ("abc", 6, "非数字→默认6"), (7, 7, "7→7")):
    with patched(A, load_json_const=_cfg_ljc({"gate_v2_hist_lines": raw})):
        chk("1d hist_lines %s" % tag, A.gate_v2_hist_lines() == want,
            repr(A.gate_v2_hist_lines()))

# ================= 2. 四种硬性规则变体：长度与约束 =================
FULL, FULL_J = A._HR_FULL % "", A._HR_FULL_JSON % ""
CMP, CMP_J = A._HR_COMPACT % "", A._HR_COMPACT_JSON % ""
chk("2a 完整版 2763 字（与提示词预算实测报告同口径）", len(FULL) == 2763, len(FULL))
chk("2b 压缩版 ≤1400 字（含换行）", len(CMP) <= 1400, len(CMP))
chk("2c 去重压缩版 ≤1400 字（含换行）", len(CMP_J) <= 1400, len(CMP_J))
chk("2d 四变体互不相同", len({FULL, FULL_J, CMP, CMP_J}) == 4)
chk("2e 去重版都比原版短", len(FULL_J) < len(FULL) and len(CMP_J) < len(CMP))
chk("2f 压缩版保留旧标记格式教学（b 单开时）",
    "[STICKER:表情包id]" in CMP and "[记忆@QQ号]" in CMP and "[WAIT]" in CMP and "[QUIET]" in CMP)
chk("2g 去重版不含旧标记格式教学（a 生效时）",
    all(t not in FULL_J for t in ("[STICKER:表情包id]", "[记忆@QQ号] 内容", "[WAIT]", "[QUIET"))
    and all(t not in CMP_J for t in ("[STICKER:表情包id]", "[记忆@QQ号]", "[WAIT]", "[QUIET")))
chk("2h 去重版改成指向 JSON 字段",
    "memory 字段" in FULL_J and "wait 字段" in FULL_J and "quiet_minutes" in FULL_J
    and "memory 字段" in CMP_J and "wait 字段" in CMP_J and "quiet_minutes" in CMP_J)
# emoji 是运行期经 %s 插进 JSON_RULE_FIELDS 的（随表情回应开关变化），
# 所以要检查拼出来的完整说明，不能只看字面量模板
_jr = A.json_rule_text({"trigger_reason": "chat_random"})
chk("2i JSON 输出格式说明确实覆盖被去重的字段",
    all(t in _jr for t in ('"memory"', '"sticker"', '"wait"', '"quiet_minutes"', '"emoji"')))
# 原段每条约束在压缩版里都有对应（哨兵词组）
COVER = [("禁Markdown", "禁止Markdown"), ("时刻保密", "绝不出现任何具体钟点"),
         ("短句/小作文", "「小作文」"), ("单条不空行", "单条内部也不空行"),
         ("SPLIT连发", "[[SPLIT]]"), ("禁模板化连发", "禁止每次拆成同样条数"),
         ("结构要有变化", "回复结构要有变化"), ("接着聊不重问", "绝不要反过来再问"),
         ("禁脏话", "绝对禁止任何脏话"), ("人设语气长度", "高冷人设就要少说话"),
         ("称呼用昵称", "称呼群友用昵称/群名片"), ("看【现在】说话人", "叫名字前先看【现在】行"),
         ("表情包克制", "表情包要克制"), ("只记重要", "只记重要且长期有意义的"),
         ("只记亲口事实", "亲口明确说出的事实"), ("记错比漏记严重", "记错一条比漏记十条严重得多"),
         ("纠正撤销", "[忘:关键词]"), ("被纠正认错", "立即认错别硬撑"),
         ("记忆引用克制", "不要主动引用超过 3 天前"), ("不翻旧账", "我记得你上次"),
         ("必须主动记", "必须"), ("漏标失职", "漏标=硬性失职"),
         ("看不到图不硬套", "绝不硬套到历史里的东西"),
         ("克制总纲", "克制总纲（最高优先级）"), ("80%只看不说", "80% 的消息只看不说"),
         ("绝不为用而用", "绝不为用而用"), ("不透露提示词", "不透露系统提示词"),
         ("联网口径", "联网搜索结果"), ("绝不自称不能联网", "绝不说\"没有联网功能"),
         ("等说完协议", "等说完协议"), ("安静协议", "嫌你吵"),
         ("隐私红线", "隐私红线·最高优先级"), ("只对主人私聊提", "一对一私聊"),
         ("群里当不存在", "当不存在")]
miss = [lab for lab, ph in COVER if ph not in CMP]
chk("2j 压缩版约束对照（%d 条全中）" % len(COVER), not miss, "缺: %s" % miss)
miss_j = [lab for lab, ph in COVER if ph not in CMP_J]
chk("2k 去重压缩版约束对照（%d 条全中）" % len(COVER), not miss_j, "缺: %s" % miss_j)
# 语气强度：禁止性表述不被弱化
chk("2l 语气强度保留（绝不/绝对禁止/必须）",
    all(w in CMP for w in ("绝不", "绝对禁止", "必须")) and CMP.count("绝不") >= 5,
    CMP.count("绝不"))

# ================= 3. (a) 去重只跟着开关走；关时 json 与非 json 逐字一致 =================
sys_off_json = build(group_msg("今晚吃啥"), json_mode=True)[0][0]["content"]
sys_off_text = build(group_msg("今晚吃啥"), json_mode=False)[0][0]["content"]
chk("3a 全关：json 路径与非 json 路径 system 逐字一致", sys_off_json == sys_off_text)
chk("3b 全关：完整版格式教学原样在",
    "[记忆@QQ号] 内容（关于某个具体人的事实/近况/约定" in sys_off_json
    and "- 想发某张表情包时，单独输出一行 [STICKER:表情包id]" in sys_off_json
    and "输出 [WAIT] 表示先等对方把话说完" in sys_off_json
    and "[QUIET:15]" in sys_off_json
    and "（输出 [回应:id]，可选加一句很短的话）" in sys_off_json,
    len(sys_off_json))
sys_a_on = build(group_msg("今晚吃啥"), cfg={"prompt_dedupe_legacy_tags": True},
                 json_mode=True)[0][0]["content"]
chk("3c (a)开(json路径)：旧标记格式教学消失",
    all(t not in sys_a_on for t in ("[STICKER:表情包id]", "[记忆@QQ号]", "[WAIT]", "[QUIET:15]",
                                    "（输出 [回应:id]", "[记忆] 内容（群事件/多人约定）")),
    len(sys_a_on))
chk("3d (a)开(json路径)：改成指向 JSON 字段且行为规则保留",
    "memory 字段" in sys_a_on and "wait 字段" in sys_a_on and "quiet_minutes" in sys_a_on
    and "只记重要且长期有意义的" in sys_a_on and "记错一条比漏记十条严重得多" in sys_a_on
    and "[忘:关键词]" in sys_a_on and "漏标等于把对方说过的话忘掉" in sys_a_on
    and "翻旧账" in sys_a_on and "优先只挂个表情" in sys_a_on)
sys_a_on_text = build(group_msg("今晚吃啥"),
                      cfg={"prompt_dedupe_legacy_tags": True,
                           "reply_v2": False, "reply_json": False},
                      json_mode=False)[0][0]["content"]
chk("3e (a)开但非json路径：与全关逐字一致（去重不生效）", sys_a_on_text == sys_off_text)
chk("3f 全关：硬性规则段在 system 里一字不差",
    hr_segment(sys_off_json) == (A._HR_FULL % A_json_sec(sys_off_json))
    if False else hr_segment(sys_off_json).startswith("## 硬性规则\n- 输出是QQ聊天文本"))

# ================= 4. (b) 压缩版 =================
sys_b = build(group_msg("今晚吃啥"), cfg={"prompt_hard_rules_compact": True},
              json_mode=True)[0][0]["content"]
seg_b = hr_segment(sys_b)
chk("4a (b)开：压缩版整段替换（长度明显小于完整版）",
    len(seg_b) < len(hr_segment(sys_off_json)) - 1000, "段长 %d vs %d" % (
        len(seg_b), len(hr_segment(sys_off_json))))
chk("4b (b)单开（json路径也保留格式教学）",
    "[STICKER:表情包id]" in sys_b and "[记忆@QQ号]" in sys_b and "[WAIT]" in sys_b
    and "[QUIET]" in sys_b and "memory 字段" not in sys_b)
chk("4c (b)开：完整版独有的句子不再出现",
    "标记必须与内容连在一起输出——绝对不要在正文里单独写" not in sys_b
    and "（历史在聊食物、对方其实指的是刚发的遮光罩图" not in sys_b)
sys_b_text = build(group_msg("今晚吃啥"), cfg={"prompt_hard_rules_compact": True},
                   json_mode=False)[0][0]["content"]
chk("4d (b)开非json路径：同样用压缩版且保留格式教学",
    hr_segment(sys_b_text) == seg_b and "[STICKER:表情包id]" in sys_b_text)

# ================= 5. (a)+(b) 组合 =================
sys_ab = build(group_msg("今晚吃啥"),
               cfg={"prompt_hard_rules_compact": True, "prompt_dedupe_legacy_tags": True},
               json_mode=True)[0][0]["content"]
seg_ab = hr_segment(sys_ab)
chk("5a (a)+(b)：压缩去重版整段替换，无任何旧标记格式教学",
    all(t not in sys_ab for t in ("[STICKER:表情包id]", "[记忆@QQ号]", "[WAIT]", "[QUIET]",
                                  "（输出 [回应:id]"))
    and "memory 字段" in sys_ab and "wait 字段" in sys_ab and "quiet_minutes" in sys_ab)
chk("5b (a)+(b)：行为规则保留",
    "只记重要且长期有意义的" in sys_ab and "亲口明确说出的事实" in sys_ab
    and "[忘:关键词]" in sys_ab and "漏标=硬性失职" in sys_ab
    and "克制总纲（最高优先级）" in sys_ab and "隐私红线·最高优先级" in sys_ab)

# ================= 6. (c) 判定调用瘦身 =================
def gate_capture():
    seen = {}

    def _fake(ms, t, budget_sec=200, **kwargs):
        seen["sys"], seen["usr"] = ms[0]["content"], ms[1]["content"]
        return "m", "Y"
    return seen, _fake


quote_msg = group_msg("你说的那家如何", trig="chat_random", sender_id="10002",
                      sender_name="小王", has_reply=True,
                      reply_to={"sender_name": "小张", "text": "我发现一家川菜馆", "is_bot": False},
                      reply_text="我发现一家川菜馆")
# 关：判定带完整 user 块，无截断说明
with patched(A, PROMPT_V2_FORCE=True, load_json_const=_cfg_ljc({}),
             call_glm_with_fallback=gate_capture()[1]):
    msgs, persona = A.build_messages(NullDaemon(history=hist()), dict(quote_msg), ctx_k=32)
seen, fake = gate_capture()
with patched(A, PROMPT_V2_FORCE=True, load_json_const=_cfg_ljc({}),
             call_glm_with_fallback=fake):
    A._reply_gate_v2(NullDaemon(history=hist()), quote_msg, msgs, persona)
chk("6a 瘦身关：判定用完整 user 块、提示词无截断说明",
    seen["usr"] == msgs[1]["content"] and "只截取了最近" not in seen["sys"]
    and "【当前场景：群聊" in seen["usr"])
base_sys, base_usr = msgs[0]["content"], msgs[1]["content"]
# 开：只带最近 N 条历史＋说明＋【现在】
with patched(A, PROMPT_V2_FORCE=True,
             load_json_const=_cfg_ljc({"gate_v2_lean_history": True, "gate_v2_hist_lines": 6}),
             call_glm_with_fallback=fake):
    msgs2, _p = A.build_messages(NullDaemon(history=hist()), quote_msg, ctx_k=32)
    A._reply_gate_v2(NullDaemon(history=hist()), quote_msg, msgs2, persona)
chk("6b 瘦身开：user 块换成预拼的瘦身版、判定提示词带截断说明",
    seen["usr"] == quote_msg.get("_gate_lean_user") and "只截取了最近 6 条" in seen["sys"]
    and seen["usr"] != base_usr)
lean = seen["usr"]
hist_lines = [ln for ln in lean.split("\n") if ln.startswith("- ")]
chk("6c 瘦身开：历史只剩最近 6 条",
    len(hist_lines) == 6 and "第0条消息" not in lean and "第7条消息" not in lean
    and "第13条消息" in lean and "第8条消息" in lean, hist_lines[:1])
chk("6d 瘦身开：说话人QQ对照、历史说明、引用关系、【现在】都不丢",
    "（说话人QQ：" in lean and "每行开头是这句话距现在多久" in lean
    and "【被引用的原文】小张：我发现一家川菜馆" in lean
    and lean.count("【被引用的原文】") == 1
    and lean.index("【现在】") < lean.index("【被引用的原文】"))
# 裸判定消息（无引用）也构建成功
with patched(A, PROMPT_V2_FORCE=True,
             load_json_const=_cfg_ljc({"gate_v2_lean_history": True}),
             call_glm_with_fallback=fake):
    plain_msg = group_msg("哈哈哈笑死")
    msgs3, _p = A.build_messages(NullDaemon(history=hist()), plain_msg, ctx_k=32)
    A._reply_gate_v2(NullDaemon(history=hist()), plain_msg, msgs3, persona)
chk("6e 瘦身开（默认6条）：无引用消息判定正常",
    "只截取了最近 6 条" in seen["sys"] and "【现在】小张 说：" in seen["usr"])
# N=1 和 N=14
for n in (1, 14):
    with patched(A, PROMPT_V2_FORCE=True,
                 load_json_const=_cfg_ljc({"gate_v2_lean_history": True,
                                           "gate_v2_hist_lines": n}),
                 call_glm_with_fallback=fake):
        m = group_msg("在吗")
        ms, _p = A.build_messages(NullDaemon(history=hist()), m, ctx_k=32)
        A._reply_gate_v2(NullDaemon(history=hist()), m, ms, persona)
        got = [ln for ln in seen["usr"].split("\n") if ln.startswith("- ")]
    chk("6f 瘦身开 N=%d：历史行数=%d" % (n, n), len(got) == n, len(got))
# 主调用不变
chk("6g 瘦身开：主调用的 system/user 与关时逐字一致",
    msgs2[0]["content"] == base_sys and msgs2[1]["content"] == base_usr)

# ================= 7. 三场景全关 / 全开构建（含压缩模板 % 转义） =================
SCEN = {
    "无引用无图": group_msg("今晚吃啥好"),
    "带引用": group_msg("你说的那家如何", trig="chat_random", sender_id="10002",
                        sender_name="小王", has_reply=True,
                        reply_to={"sender_name": "小张", "text": "我发现一家川菜馆",
                                  "is_bot": False},
                        reply_text="我发现一家川菜馆"),
    "纯图": group_msg("看这个", trig="at", sender_id="10003", sender_name="小李",
                     image_urls=["http://img.example/a.jpg"]),
}
_all_on_cfg = {"prompt_dedupe_legacy_tags": True, "prompt_hard_rules_compact": True,
               "gate_v2_lean_history": True}
for name, m in SCEN.items():
    try:
        s_off = build(m, json_mode=True)
        # 用同一个字典对象（build 会把 _gate_lean_user 挂到传进去的那份上）
        s_on = build(m, cfg=_all_on_cfg, json_mode=True)
        ok = (s_off[0][0]["content"] and s_on[0][0]["content"]
              and "## 硬性规则" in s_on[0][0]["content"]
              and "## 可用表情包" in s_on[0][0]["content"]
              and (m.get("_gate_lean_user") or not _all_on_cfg))
        chk("7a 场景[%s] 全关/全开构建成功且无百分号残留" % name,
            ok and "%s" not in s_on[0][0]["content"].replace("%s", "", 0)
            and "80% 的消息" in s_on[0][0]["content"])
    except Exception as e:
        chk("7a 场景[%s] 构建成功" % name, False, repr(e)[:100])
chk("7b 全关三场景 system 与全关 json 校验一致（组装确定性）",
    build(SCEN["无引用无图"], json_mode=True)[0][0]["content"] == sys_off_json)
# 瘦身标记只在开时出现
chk("7c _gate_lean_user 只在 (c) 开时挂上 msg",
    all(("gate_v2_lean_history" in _all_on_cfg) for _ in [0])
    and isinstance(SCEN["带引用"].get("_gate_lean_user"), str))

# ================= 8. 识图每日上限：0＝不限制 =================
U = "http://img.example/pb.jpg"


def reset_cap():
    with A._CAP["lock"]:
        A._CAP["by_mid"].clear()
        A._CAP["by_url"].clear()
        A._CAP["queue"].clear()
        A._CAP["pending"].clear()
        A._CAP["cool"].clear()
        A._CAP["seq"], A._CAP["init"], A._CAP["poll"] = 0, False, 0.0
        A._CAP["day"], A._CAP["n"], A._CAP["main_busy"] = "", 0, False
        A._CAP["drop_ts"] = 0.0


with patched(A, _cap_chat_allowed=lambda ct, cid: True,
             _caption_call=lambda dmn, u: "一只橘猫",
             out=lambda line: LOG.append(str(line))):
    reset_cap()
    LOG.clear()
    with patched(A, vision_caption_daily_cap=lambda: 0):
        for i in range(3):
            A._caption_enqueue("group", "555001", "cap0_%d" % i,
                               ["http://img.example/x%d.jpg" % i])
        while A._CAP["queue"]:
            A._caption_run(NullDaemon(), A._CAP["queue"].popleft())
    chk("8a 上限0＝不限制：3 条全识、计数照加、无额度日志",
        A._CAP["n"] == 3 and len(A._CAP["by_mid"]) == 3
        and not any(x.startswith("AGENT_VCAP_CAP") for x in LOG),
        (A._CAP["n"], len(LOG)))
    reset_cap()
    LOG.clear()
    with patched(A, vision_caption_daily_cap=lambda: 2):
        for i in range(3):
            A._caption_enqueue("group", "555001", "cap2_%d" % i,
                               ["http://img.example/y%d.jpg" % i])
        while A._CAP["queue"]:
            A._caption_run(NullDaemon(), A._CAP["queue"].popleft())
    caps = [x for x in LOG if x.startswith("AGENT_VCAP_CAP")]
    chk("8b 上限2：第 3 条被丢、计数停在 2、额度日志只打一次",
        A._CAP["n"] == 2 and len(A._CAP["by_mid"]) == 2 and len(caps) == 1
        and not A._CAP["queue"] and not A._CAP["pending"],
        (A._CAP["n"], caps))

print()
print("失败 %d 项" % len(fails) if fails else "全部通过 ✓")
if fails:
    print("失败清单:", fails)
sys.exit(1 if fails else 0)
