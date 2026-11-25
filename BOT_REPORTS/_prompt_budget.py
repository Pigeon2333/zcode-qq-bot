# -*- coding: utf-8 -*-
"""提示词预算实测（只读分析，不改任何产品代码）。

数据源（全部只读打开，不写任何文件、不联网、不调用模型）：
  ~/.zcode-qq-bot/logs/context/ctx_YYYYMMDD.jsonl  上下文日志（每次回复组装后的完整 messages）
  ~/.zcode-qq-bot/usage_log.jsonl                  用量日志（每次成功调用的 token 数）

输出只含数量 / 长度 / 占比 / 天数等数字与固定段名。
不含任何聊天内容、人名、群名、QQ 号、密钥。
分段切分标记取自 plugins/qq-bot/scripts/qq_bot_agent.py::_build_messages_impl
的拼装模板（代码里的固定文本），标记本身写在下面常量里。

用法：
  PYTHONDONTWRITEBYTECODE=1 PYTHONIOENCODING=utf-8 python BOT_REPORTS/_prompt_budget.py
  加 --breakdown：追加 B1（自我状态与插件注入按来源）/ B2（硬性规则按主题）/
  B3（新版格式 v2 分段）三节；默认输出与不加参数时完全一致。
"""
import argparse
import glob
import json
import math
import os
import sys
import time

DATA_DIR = os.environ.get("QQBOT_DATA_DIR") or os.path.join(
    os.path.expanduser("~"), ".zcode-qq-bot")
CTX_DIR = os.path.join(DATA_DIR, "logs", "context")
USAGE_PATH = os.path.join(DATA_DIR, "usage_log.jsonl")
WINDOW_DAYS = 7
JOIN_TOL_SEC = 180         # usage.ts - ctx.ts = 请求往返耗时上限，180s

# ---------------- system 块分段（按 _build_messages_impl 拼装顺序） ----------------
# (逻辑段名, 搜索标记列表)；特殊段：
#   头部     = 串首到时间行
#   aware    = 无固定首标记（自我状态+插件注入），切点=称呼规则最后一条【行之后
#   本次消息性质 按出现处后缀分类：防插嘴/普通→闲聊跳过；任务滥用/互聊试探→拦截段
#   硬性规则 有两段（设定安全插在中间），聚合时合并
SYS_MARKS = [
    ("时间行", ["【当前真实时间"]),
    ("称呼规则", ["【自称固定", "【主人称呼"]),
    ("全局设定", ["## 全局通用设定"]),
    ("人设", ["【当前人设："]),
    ("风格卡", ["## 说话风格卡"]),
    ("自进化观察", ["## 自进化风格观察"]),
    ("长期记忆", ["## 长期记忆"]),
    ("硬性规则", ["## 硬性规则"]),
    ("设定安全", ["## 设定安全"]),
    ("硬性规则", ["- 你具备联网搜索能力"]),          # 规则尾段（与首段同名聚合）
    ("带话/画图/设备教学", ["- 你可以出现在多个群里", "- 你具备AI生图能力",
                            "- 你具备「设备操控」", "- 「设备操控」功能已开启"]),
    ("可用表情包", ["## 可用表情包"]),
    ("语音规则", ["你具备发语音消息的能力", "你有发语音消息的能力"]),
    ("表情回应规则", ["你可以给消息挂 QQ 小黄脸表情回应"]),
    ("群管规则", ["## 群管理能力"]),
    ("群管理边界", ["## 群管理边界"]),
    ("联网搜索结果", ["## 联网搜索结果"]),
    ("MCP工具索引", ["## MCP 外部工具"]),
]
NATURE_MARK = "## 本次消息性质"

SEG_ORDER = ["头部", "时间行", "称呼规则", "自我状态与插件注入", "全局设定", "人设",
             "风格卡", "自进化观察", "长期记忆", "闲聊跳过说明", "任务/互聊拦截",
             "硬性规则", "设定安全", "带话/画图/设备教学", "可用表情包", "语音规则",
             "表情回应规则", "群管规则", "群管理边界", "联网搜索结果", "MCP工具索引"]


def _find_first(s, marks, start):
    best = -1
    for m in marks:
        p = s.find(m, start)
        if p >= 0 and (best < 0 or p < best):
            best = p
    return best


def split_system(s):
    """把 system 串切成逻辑段，返回 (段名->字符数, 出现段名集合)；失败返回 None。"""
    cuts = [(0, "头部")]
    t = _find_first(s, ["【当前真实时间"], 0)
    if t < 0:
        return None
    cuts.append((t, "时间行"))
    # 称呼规则：从【自称固定/【主人称呼 起，连续以【开头的行
    n = _find_first(s, ["【自称固定", "【主人称呼"], t)
    if n >= 0:
        cuts.append((n, "称呼规则"))
        end = s.find("\n", n)
        while end >= 0:
            nxt = s.find("\n", end + 1)
            line = s[end + 1:nxt if nxt >= 0 else len(s)]
            if line.startswith("【"):
                end = nxt
            else:
                break
        aware_at = (end + 1) if end >= 0 else len(s)
    else:
        te = s.find("\n", t)
        aware_at = (te + 1) if te >= 0 else len(s)
    cuts.append((aware_at, "自我状态与插件注入"))
    for name, marks in SYS_MARKS:
        p = _find_first(s, marks, cuts[-1][0])
        if p >= 0 and p > cuts[-1][0]:
            cuts.append((p, name))
    # 本次消息性质变体（0~2 处），按后缀分类
    occs, p = [], s.find(NATURE_MARK)
    while p >= 0:
        occs.append(p)
        p = s.find(NATURE_MARK, p + 1)
    for o in occs:
        chunk = s[o:o + 32]
        nm = "任务/互聊拦截" if ("任务滥用" in chunk or "互聊试探" in chunk) \
            else "闲聊跳过说明"
        cuts.append((o, nm))
    cuts.sort(key=lambda x: x[0])
    # 去掉同位置重复并保证单调
    clean = [cuts[0]]
    for c in cuts[1:]:
        if c[0] > clean[-1][0]:
            clean.append(c)
    spans = {}
    found = set()
    for i, (pos, nm) in enumerate(clean):
        end = clean[i + 1][0] if i + 1 < len(clean) else len(s)
        spans[nm] = spans.get(nm, 0) + (end - pos)
        found.add(nm)
    return spans, found


def split_user(u):
    """user 串切四段：场景卡 / 历史块 / 历史说明 /【现在】（含引用注释）。
    两种历史格式：v2「【最近对话】（旧→新）」/ 旧版「…| 最近对话】」。"""
    pr = u.find("【最近对话】")
    fmt = 2
    if pr < 0:
        pr = u.find("| 最近对话】")
        fmt = 1
    if pr < 0:
        return None
    pn = u.find("（每行", pr)
    if pn < 0:
        return None
    kw = u.find("【现在】", pn)
    if kw < 0:
        return None
    hist = u[pr:pn]
    entries = sum(1 for ln in hist.split("\n") if ln.startswith("- "))
    return {"scene": pr, "hist": pn - pr, "note": kw - pn, "now": len(u) - kw,
            "hist_entries": entries, "fmt": fmt}


def pct(xs_sorted, p):
    """最近秩百分位。"""
    n = len(xs_sorted)
    if n == 0:
        return 0
    k = max(1, math.ceil(p / 100.0 * n))
    return xs_sorted[min(n - 1, k - 1)]


def median(xs):
    return pct(sorted(xs), 50)


def main():
    now = time.time()
    day0 = time.localtime(now - (WINDOW_DAYS - 1) * 86400)
    cutoff = time.mktime((day0.tm_year, day0.tm_mon, day0.tm_mday, 0, 0, 0, 0, 0, -1))
    d0 = time.strftime("%Y%m%d", day0)
    d1 = time.strftime("%Y%m%d", time.localtime(now))
    print("=== 0. 窗口 ===")
    print("window_days=%d date_from=%s date_to=%s" % (WINDOW_DAYS, d0, d1))

    # ---- 用量日志（流式） ----
    usage = []            # (ts, model, ptok, ctok)
    usage_lines_seen = 0
    usage_parse_fail = 0
    if os.path.isfile(USAGE_PATH):
        with open(USAGE_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                usage_lines_seen += 1
                try:
                    r = json.loads(line)
                except Exception:
                    usage_parse_fail += 1
                    continue
                ts = r.get("ts")
                if not isinstance(ts, int) or ts < cutoff:
                    continue
                usage.append((ts, str(r.get("model")),
                              int(r.get("prompt_tokens") or 0),
                              int(r.get("completion_tokens") or 0)))
    print("=== 1. 用量按天 / 按模型 ===")
    print("usage_lines_total=%d usage_in_window=%d usage_parse_fail=%d"
          % (usage_lines_seen, len(usage), usage_parse_fail))
    print("usage_fields=ts,model,prompt_tokens,completion_tokens,total_tokens")
    print("usage_has_chat_type_field=0 usage_has_trigger_field=0")
    by_day = {}
    for ts, _m, pt, ct in usage:
        by_day.setdefault(time.strftime("%Y%m%d", time.localtime(ts)), []).append((pt, ct))
    for d in sorted(by_day):
        ps = sorted(x[0] for x in by_day[d])
        cs = sorted(x[1] for x in by_day[d])
        print("day=%s n=%d prompt_med=%d prompt_p90=%d completion_med=%d"
              % (d, len(ps), pct(ps, 50), pct(ps, 90), pct(cs, 50)))
    by_model = {}
    for ts, m, pt, ct in usage:
        by_model.setdefault(m, []).append((pt, ct))
    for m in sorted(by_model):
        ps = sorted(x[0] for x in by_model[m])
        cs = sorted(x[1] for x in by_model[m])
        print("model=%s n=%d prompt_med=%d prompt_p90=%d completion_med=%d"
              % (m, len(ps), pct(ps, 50), pct(ps, 90), pct(cs, 50)))
    if usage:
        ap = sorted(x[2] for x in usage)
        ac = sorted(x[3] for x in usage)
        print("usage_all n=%d prompt_med=%d prompt_p90=%d completion_med=%d"
              % (len(ap), pct(ap, 50), pct(ap, 90), pct(ac, 50)))

    # ---- 上下文日志（按天文件，流式逐行） ----
    ctx = []              # dict: ts, chat, spans, found, usplit, sys_len, total_len
    parse_fail = 0
    files = sorted(glob.glob(os.path.join(CTX_DIR, "ctx_*.jsonl")))
    win_files = []
    for path in files:
        day = os.path.basename(path)[4:12]
        if day < d0 or day > d1:
            continue
        win_files.append(path)
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    parse_fail += 1
                    continue
                roles = r.get("roles")
                if not isinstance(roles, list) or not roles:
                    parse_fail += 1
                    continue
                sys_c = usr_c = None
                for m in roles:
                    if not isinstance(m, dict):
                        continue
                    if m.get("role") == "system" and isinstance(m.get("content"), str):
                        sys_c = m["content"]
                    elif m.get("role") == "user" and isinstance(m.get("content"), str):
                        usr_c = m["content"]
                if sys_c is None or usr_c is None:
                    parse_fail += 1
                    continue
                sp = split_system(sys_c)
                us = split_user(usr_c)
                chat = str(r.get("chat") or "")
                ctype = chat.split("/", 1)[0] if "/" in chat else chat
                ts = r.get("ts")
                ctx.append({"ts": ts if isinstance(ts, int) else 0,
                            "chat": ctype, "spans": sp[0] if sp else None,
                            "found": sp[1] if sp else None,
                            "usplit": us, "sys_len": len(sys_c),
                            "total_len": len(sys_c) + len(usr_c),
                            "fmt": us["fmt"] if us else 0})
    n_parsed = sum(1 for c in ctx if c["spans"] and c["usplit"])
    print("=== 2. 上下文日志概况 ===")
    print("ctx_files_in_window=%d ctx_records=%d ctx_parsed=%d ctx_parse_fail=%d"
          % (len(win_files), len(ctx), n_parsed, parse_fail + len(ctx) - n_parsed))
    fmt_n = {1: 0, 2: 0, 0: 0}
    for c in ctx:
        fmt_n[c.get("fmt", 0)] += 1
    print("user_fmt_v2_n=%d user_fmt_v1_n=%d user_fmt_unknown_n=%d"
          % (fmt_n[2], fmt_n[1], fmt_n[0]))

    # ---- 字符/token 系数：ctx 槽位 -> 落盘之后完成的同批调用 ----
    # 识图模型 prompt 含图片 token：窗口内有识图调用的槽位整槽剔除（其主调用
    # 的 prompt 构成与落盘文本不可比）；其余槽位取窗口内 prompt 最大的
    # 非视觉调用配对（判定类小调用不带 system，永远争不过主回复调用）。
    import bisect
    vision_models = {"glm-4.6v", "glm-4.6v-flash", "glm-4v-flash"}
    vis_ts = sorted(ts for ts, m, _p, _c in usage if m in vision_models)
    cand = sorted([(ts, ptok) for ts, m, ptok, _ct in usage
                   if m not in vision_models], key=lambda x: x[0])
    cand_ts = [x[0] for x in cand]
    ctx_sorted = sorted([c for c in ctx if c["spans"] and c["usplit"]],
                        key=lambda x: x["ts"])
    used = [False] * len(cand)
    pairs = []            # (ptok, chars_total, chat, ratio)
    n_vis_slots = 0
    for c in ctx_sorted:
        lo_v = bisect.bisect_left(vis_ts, c["ts"] - 5)
        hi_v = bisect.bisect_right(vis_ts, c["ts"] + JOIN_TOL_SEC)
        if hi_v > lo_v:
            n_vis_slots += 1
            continue
        lo = bisect.bisect_left(cand_ts, c["ts"])
        hi = bisect.bisect_right(cand_ts, c["ts"] + JOIN_TOL_SEC)
        best = -1
        for k in range(lo, hi):
            if not used[k] and (best < 0 or cand[k][1] > cand[best][1]):
                best = k
        if best >= 0:
            used[best] = True
            chars = c["total_len"]
            if chars > 0 and cand[best][1] > 0:
                pairs.append((cand[best][1], chars, c["chat"],
                              cand[best][1] / chars, cand[best][0] - c["ts"]))
    print("=== 3. 字符换 token 系数（估算） ===")
    print("joined_pairs=%d ctx_slots=%d vision_slots_skipped=%d match_rate_pct=%.1f "
          "lag_window_sec=%d join_rule=max_prompt_nonvision_no_vision_slot"
          % (len(pairs), len(ctx_sorted), n_vis_slots,
             (100.0 * len(pairs) / len(ctx_sorted)) if ctx_sorted else 0.0,
             JOIN_TOL_SEC))
    tot_tok = tot_ch = 0
    coef = 0.0
    if pairs:
        rs = sorted(p[3] for p in pairs)
        print("ratio_token_per_char_all p10=%.4f p25=%.4f med=%.4f p75=%.4f p90=%.4f"
              % (pct(rs, 10), pct(rs, 25), pct(rs, 50), pct(rs, 75), pct(rs, 90)))
        clean = [p for p in pairs if p[3] >= 0.40]
        bad = [p for p in pairs if p[3] < 0.40]
        if clean:
            tot_tok = sum(p[0] for p in clean)
            tot_ch = sum(p[1] for p in clean)
            coef = tot_tok / tot_ch
            cl = sorted(p[4] for p in clean)
            print("clean_pairs_n=%d coef_token_per_char=%.4f coef_chars_per_token=%.2f "
                  "clean_ratio_med=%.4f clean_lag_med_sec=%d clean_lag_p90_sec=%d"
                  % (len(clean), coef, tot_ch / tot_tok, pct(sorted(p[3] for p in clean), 50),
                     pct(cl, 50), pct(cl, 90)))
        if bad:
            bl = sorted(p[4] for p in bad)
            print("rejected_pairs_n=%d rejected_ptok_med=%d rejected_lag_med_sec=%d "
                  "rejected_lag_p90_sec=%d"
                  % (len(bad), pct(sorted(p[0] for p in bad), 50),
                     pct(bl, 50), pct(bl, 90)))

    # ---- system 分段统计 ----
    print("=== 4. system 分段（parsed=%d）===" % n_parsed)
    seg_chars = {s: [] for s in SEG_ORDER}      # 出现时的字符数
    seg_sum = {s: 0 for s in SEG_ORDER}         # 全体记录上的字符总和（缺席记 0）
    tot_prompt_chars = 0
    tot_sys_chars = 0
    sys_lens = []
    total_lens = []
    for c in ctx_sorted:
        if not c["spans"]:
            continue
        sys_lens.append(c["sys_len"])
        total_lens.append(c["total_len"])
        tot_sys_chars += c["sys_len"]
        tot_prompt_chars += c["total_len"]
        for s in SEG_ORDER:
            v = c["spans"].get(s, 0)
            seg_sum[s] += v
            if s in c["found"]:
                seg_chars[s].append(v)
    n_sys = len(sys_lens)
    if n_sys and tot_prompt_chars:
        print("sys_chars_med=%d sys_chars_p90=%d prompt_chars_med=%d prompt_chars_p90=%d"
              % (pct(sorted(sys_lens), 50), pct(sorted(sys_lens), 90),
                 pct(sorted(total_lens), 50), pct(sorted(total_lens), 90)))
        rows = []
        for s in SEG_ORDER:
            xs = sorted(seg_chars[s])
            pres = 100.0 * len(xs) / n_sys if n_sys else 0.0
            share = 100.0 * seg_sum[s] / tot_prompt_chars
            est_med = int(pct(xs, 50) * coef) if xs and coef else 0
            exp = share / 100.0 * pct(sorted(total_lens), 50) * coef if coef else 0.0
            rows.append((share * pres, s, pres, share, xs, est_med, exp))
        for _, s, pres, share, xs, est_med, exp in sorted(rows, reverse=True):
            print("seg=%s present_pct=%.1f chars_med=%d chars_p90=%d est_tok_med=%d "
                  "share_of_prompt_pct=%.2f expected_tok_per_msg=%.1f"
                  % (s, pres, pct(xs, 50), pct(xs, 90), est_med, share, exp))

    # ---- TOP8（占比 × 出现率） ----
    print("=== 5. TOP8 段（按 share_of_prompt × present_pct）===")
    if n_sys and tot_prompt_chars:
        coef = (tot_tok / tot_ch) if pairs and tot_ch else 0.0
        ranked = sorted(rows, reverse=True)[:8]
        for i, (_, s, pres, share, xs, est_med, exp) in enumerate(ranked, 1):
            print("top%d seg=%s rank_score=%.1f present_pct=%.1f share_pct=%.2f "
                  "est_tok_med=%d expected_tok_per_msg=%.1f"
                  % (i, s, share * pres, pres, share, est_med, exp))

    # ---- user 块 ----
    print("=== 6. user 块 ===")
    he, hc, nc, nw, sc = [], [], [], [], []
    for c in ctx_sorted:
        u = c["usplit"]
        if not u:
            continue
        he.append(u["hist_entries"])
        hc.append(u["hist"])
        nc.append(u["note"])
        nw.append(u["now"])
        sc.append(u["scene"])
    if he:
        he_s, hc_s, nc_s, nw_s, sc_s = map(sorted, (he, hc, nc, nw, sc))
        print("hist_entries_med=%d hist_entries_p90=%d" % (pct(he_s, 50), pct(he_s, 90)))
        print("hist_chars_med=%d hist_chars_p90=%d" % (pct(hc_s, 50), pct(hc_s, 90)))
        print("note_chars_med=%d now_chars_med=%d now_chars_p90=%d scene_chars_med=%d"
              % (pct(nc_s, 50), pct(nw_s, 50), pct(nw_s, 90), pct(sc_s, 50)))

    # ---- 按会话类型（取自 ctx 的 chat 前缀） ----
    print("=== 7. 按会话类型（ctx；token 为配对估算） ===")
    by_chat = {}
    for c in ctx_sorted:
        by_chat.setdefault(c["chat"], []).append(c)
    pair_by_chat = {}
    for ptok, _chars, ch, ratio, _lag in pairs:
        if ratio >= 0.40:      # 与系数样本同一口径（剔掉顶替的中小调用）
            pair_by_chat.setdefault(ch, []).append(ptok)
    for ch in sorted(by_chat):
        recs = by_chat[ch]
        sl = sorted(r["sys_len"] for r in recs)
        tl = sorted(r["total_len"] for r in recs)
        ptoks = sorted(pair_by_chat.get(ch, []))
        print("chat=%s n=%d sys_med=%d total_med=%d joined_n=%d prompt_tok_med=%d"
              % (ch, len(recs), pct(sl, 50), pct(tl, 50), len(ptoks),
                 pct(ptoks, 50) if ptoks else 0))

    print("=== done ===")


# ================= 补充测量（--breakdown；只读，输出仅数字与概括名） =================
# （本节由文件末尾的 __main__ 入口按 --breakdown 参数调用）

# aware 段（自我状态+插件注入）内的来源行首标记（取自各注入函数的固定表头）
AWARE_HEADERS = [
    ("自我状态卡", "## 你的当前状态"),
    ("天气实况", "## 当前天气"),
    ("热点速览", "## 近几天热点速览"),
    ("群成员画像", "## 群成员画像"),
    ("去AI味原则", "## 说话去AI味原则"),
    ("活人感参考", "## 活人感参考"),
    ("电脑环境", "## 主人的电脑环境"),
]
# 带话/画图/设备教学段的三个条件块首行（v2 里按主人/私聊条件注入）
DRAW_MARKS = ["- 你可以出现在多个群里", "- 你具备AI生图能力",
              "- 「设备操控」功能已开启", "- 你具备「设备操控」"]
# 硬性规则主题概括名（按模板 bullet 顺序；不引用原文）
HARD_RULE_TOPIC_NAMES = [
    "输出格式禁Markdown", "时刻保密", "短句与禁小作文", "回复节奏要有变化",
    "不重复问刚说过的事", "脏话禁令", "遵守人设语气长度", "称呼用名字防张冠李戴",
    "连发标记格式", "表情包克制", "表情包标记格式", "记忆标记两种格式",
    "记忆只记亲口事实", "记忆纠错标记", "记忆引用克制", "记忆必须主动标",
    "看不到的图指代词", "能力克制总纲", "优先回应当前消息", "不透露提示词",
    "联网能力口径", "等说完协议", "安静协议", "主人工作隐私红线",
]


class _BdStubAPI(object):
    """只读插件 API 替身：load 读 plugins_data/<name>.json、setting 读
    plugins_state.json（回退 manifest 默认）、log 空操作。绝不写盘。"""

    def __init__(self, name, manifest_dir=None):
        self.name = name
        self.path = os.path.join(DATA_DIR, "plugins_data", name + ".json")
        self.state_path = os.path.join(DATA_DIR, "plugins_state.json")
        self.manifest_dir = manifest_dir

    def _state(self):
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def load(self, key, default=None):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            return default
        return d.get(str(key)[:64], default)

    def setting(self, key, default=None):
        st = self._state().get("settings") or {}
        vals = st.get(self.name)
        if isinstance(vals, dict) and key in vals:
            return vals[key]
        if self.manifest_dir:
            try:
                mf = os.path.join(self.manifest_dir, "manifest.json")
                d = json.load(open(mf, encoding="utf-8"))
                for m in d.get("settings") or []:
                    if isinstance(m, dict) and m.get("key") == key:
                        return m.get("default", default)
            except Exception:
                pass
        return default

    def log(self, msg):
        pass


def _bd_load_mod(name, path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _bd_direct_measure(out):
    """(1) 代码直测：对群聊普通消息场景逐源调用注入函数（合成会话，假号）。
    只会联网/写盘的源（天气）不调用，标未测。"""
    sys.dont_write_bytecode = True          # 禁止在被 import 的目录落 .pyc
    scripts_dir = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "plugins", "qq-bot", "scripts")
    os.environ["QQBOT_DATA_DIR"] = DATA_DIR
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    FAKE_GID, FAKE_QQ = "900000001", "900000002"
    m = {}
    try:
        p = os.path.join(DATA_DIR, "plugins", "humanizer_zh", "plugin.py")
        m["去AI味原则"] = len((_bd_load_mod("bd_humanizer", p)._RULES or "").strip())
    except Exception as e:
        out("b1_direct_fail src=去AI味原则 %r" % e)
    try:
        p = os.path.join(DATA_DIR, "plugins", "hot_brief", "plugin.py")
        hb = _bd_load_mod("bd_hotbrief", p)
        m["热点速览"] = len((hb._line("group", FAKE_GID) or "").strip())
    except Exception as e:
        out("b1_direct_fail src=热点速览 %r" % e)
    try:
        p = os.path.join(scripts_dir, "plugins", "member_profiles", "plugin.py")
        mp = _bd_load_mod("bd_mprofiles", p)
        fake = mp._block("group", FAKE_GID, sender_id=FAKE_QQ, sender_name="测试") or ""
        m["群成员画像"] = len(fake.strip())          # 假号无画像 → 现实为 0
        rows = [len((mp._row(qq) or "").strip()) for qq, mm in mp._load().items()
                if isinstance(mm, dict) and str(mm.get("note") or "").strip()]
        out("b1_profiles_members_with_note=%d row_chars_med=%d row_chars_max=%d "
            "block_chars_med_withnote=%d"
            % (len(rows), pct(sorted(rows), 50) if rows else 0,
               max(rows) if rows else 0,
               len("## 群成员画像（你对这些人的长期了解，交流时可自然体现）"
                   "### 发言者") + (pct(sorted(rows), 50) if rows else 0)))
    except Exception as e:
        out("b1_direct_fail src=群成员画像 %r" % e)
    try:
        p = os.path.join(DATA_DIR, "plugins", "style_learn", "plugin.py")
        sl = _bd_load_mod("bd_stylelearn", p)
        api = _BdStubAPI("style_learn")
        cfg = sl._cfg(api)
        lens = []
        for g in set(str(x) for x in (cfg.get("groups") or [])):
            v = sl._inject(api, "group", g) or ""
            if v.strip():
                lens.append(len(v.strip()))
        out("b1_stylelearn enabled=%s inject_on=%s mode=%s scope=%s cfg_groups=%d "
            "records=%d inject_groups=%d inject_chars_med=%d inject_chars_max=%d"
            % (bool(cfg.get("enabled")), bool(cfg.get("inject_on")),
               cfg.get("mode"), cfg.get("scope"), len(cfg.get("groups") or []),
               len(api.load("records", []) or []), len(lens),
               pct(sorted(lens), 50) if lens else 0,
               max(lens) if lens else 0))
        if bool(cfg.get("enabled")) and bool(cfg.get("inject_on")) and lens:
            m["活人感参考"] = pct(sorted(lens), 50)
    except Exception as e:
        out("b1_direct_fail src=活人感参考 %r" % e)
    try:
        p = os.path.join(scripts_dir, "plugins", "env_pulse", "plugin.py")
        ep = _bd_load_mod("bd_envpulse", p)
        api = _BdStubAPI("env_pulse", os.path.join(scripts_dir, "plugins", "env_pulse"))
        m["电脑环境"] = len((ep._line("group", FAKE_GID, api) or "").strip())
        oq = ""
        try:
            oq = str(json.load(open(os.path.join(DATA_DIR, "app_config.json"),
                                    encoding="utf-8")).get("owner_qq") or "")
        except Exception:
            pass
        if oq:      # 主人私聊场景参考读数（号码不外泄）
            out("b1_envpulse_ownerprivate_chars=%d"
                % len((ep._line("private", oq, api) or "").strip()))
    except Exception as e:
        out("b1_direct_fail src=电脑环境 %r" % e)
    try:
        import qq_bot_agent as A
        m["自我状态卡"] = len((A._self_state_line("group", FAKE_GID) or "").strip())
        # 发送时追加、ctx 日志不落盘的三块（量确切大小，群聊场景参数）
        out("b1_not_logged json_rule_chars=%d gate_sys_chars=%d "
            "gate_note_Y_chars=%d gate_note_E_json_chars=%d"
            % (len(A.json_rule_text({"trigger_reason": "chat_random", "text": "x"})),
               len(A._gate_system("测试")), len(A._GATE_Y_NOTE),
               len(A._GATE_E_NOTE_JSON)))
    except Exception as e:
        out("b1_direct_fail src=自我状态卡/追加块 %r" % e)
    for k in sorted(m):
        out("b1_direct src=%s chars=%d" % (k, m[k]))
    out("b1_direct src=天气实况 chars=未测（插件已停用且其注入会拉取实况，按规矩不调用）")
    return m


def breakdown():
    """B1/B2/B3：逐来源拆分、硬性规则按主题、新版格式分段。只读、只出数字。"""
    now = time.time()
    day0 = time.localtime(now - (WINDOW_DAYS - 1) * 86400)
    d0 = time.strftime("%Y%m%d", day0)
    d1 = time.strftime("%Y%m%d", time.localtime(now))
    print("=== B0. 补充测量（--breakdown；窗口同上 %s–%s）===" % (d0, d1))
    out = print
    recs = []      # dict: fmt, chat, sys, aware, aware_spans, hard_p1, hard_p2
    parse_fail = 0
    for path in sorted(glob.glob(os.path.join(CTX_DIR, "ctx_*.jsonl"))):
        day = os.path.basename(path)[4:12]
        if day < d0 or day > d1:
            continue
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    parse_fail += 1
                    continue
                sys_c = usr_c = None
                for mmsg in (r.get("roles") or []):
                    if not isinstance(mmsg, dict):
                        continue
                    if mmsg.get("role") == "system" and isinstance(mmsg.get("content"), str):
                        sys_c = mmsg["content"]
                    elif mmsg.get("role") == "user" and isinstance(mmsg.get("content"), str):
                        usr_c = mmsg["content"]
                if sys_c is None or usr_c is None:
                    parse_fail += 1
                    continue
                us = split_user(usr_c)
                rec = {"fmt": us["fmt"] if us else 0,
                       "chat": str(r.get("chat") or "").split("/", 1)[0],
                       "sys": sys_c}
                # aware 段文本（起点/终点与 split_system 同一启发式）
                t = sys_c.find("【当前真实时间")
                n = _find_first(sys_c, ["【自称固定", "【主人称呼"], t) if t >= 0 else -1
                a0 = 0
                if n >= 0:
                    end = sys_c.find("\n", n)
                    while end >= 0:
                        nxt = sys_c.find("\n", end + 1)
                        ln = sys_c[end + 1:nxt if nxt >= 0 else len(sys_c)]
                        if ln.startswith("【"):
                            end = nxt
                        else:
                            break
                    a0 = (end + 1) if end >= 0 else len(sys_c)
                j = len(sys_c)
                for mk in ("## 全局通用设定", "【当前人设：", "## 硬性规则"):
                    p = sys_c.find(mk, a0)
                    if p >= 0 and p < j:
                        j = p
                rec["aware"] = sys_c[a0:j] if t >= 0 and n >= 0 else ""
                # 硬性规则两段（设定安全插在中间）
                h0 = sys_c.find("## 硬性规则")
                h1 = sys_c.find("## 设定安全")
                rec["hard_p1"] = sys_c[h0:h1].rstrip("\n") if 0 <= h0 < h1 else ""
                p2 = sys_c.find("- 你具备联网搜索能力")
                p2e = len(sys_c)
                for mk in DRAW_MARKS + ["## 可用表情包"]:
                    q = sys_c.find(mk, p2 + 1) if p2 >= 0 else -1
                    if q >= 0 and q < p2e:
                        p2e = q
                rec["hard_p2"] = sys_c[p2:p2e].rstrip("\n") if p2 >= 0 else ""
                recs.append(rec)
    n = len(recs)
    print("b_records=%d b_parse_fail=%d" % (n, parse_fail))

    # ---- B1：自我状态与插件注入按来源 ----
    print("=== B1. 自我状态与插件注入按来源（场景=群聊普通消息；直测+日志双口径） ===")
    direct = _bd_direct_measure(out)
    aware_med = pct(sorted(len(r["aware"]) for r in recs), 50) if recs else 0
    print("b1_aware_chars_med=%d（占比分母）" % aware_med)
    print("src | 直测字符（群聊场景） | 日志出现率 | 日志字数中位 | 占该段中位")
    for name, mark in AWARE_HEADERS:
        lens, present = [], 0
        for r in recs:
            aw = r["aware"]
            i = aw.find(mark)
            if i < 0:
                continue
            present += 1
            nxt = len(aw)
            for _n2, mk2 in AWARE_HEADERS:
                q = aw.find(mk2, i + len(mark))
                if q >= 0 and q < nxt:
                    nxt = q
            lens.append(len(aw[i:nxt].strip("\n")))
        d = direct.get(name)
        d_s = ("未测" if d is None else d)
        share = (100.0 * (pct(sorted(lens), 50) * present / n) / aware_med) if aware_med and n else 0.0
        print("src=%s direct=%s present_pct=%.1f chars_med=%s share_of_aware_pct=%.1f"
              % (name, d_s, (100.0 * present / n) if n else 0.0,
                 pct(sorted(lens), 50) if lens else "-", share))
    # 未归因剩余（应≈换行等空白）
    rest = []
    for r in recs:
        aw = r["aware"]
        used = 0
        for _n2, mk in AWARE_HEADERS:
            i = aw.find(mk)
            if i >= 0:
                nxt = len(aw)
                for _n3, mk2 in AWARE_HEADERS:
                    q = aw.find(mk2, i + len(mk))
                    if q >= 0 and q < nxt:
                        nxt = q
                used += len(aw[i:nxt])
        rest.append(max(0, len(aw.strip("\n")) - used))
    print("b1_unattributed_chars_med=%d" % (pct(sorted(rest), 50) if rest else 0))

    # ---- B2：硬性规则按主题（固定文本直切） ----
    print("=== B2. 硬性规则按主题（模板固定文本；主题名为概括） ===")
    p1s = set()
    p2s = set()
    for r in recs:
        if r["hard_p1"]:
            p1s.add(len(r["hard_p1"]))
        if r["hard_p2"]:
            p2s.add(len(r["hard_p2"]))
    print("b2_part1_len_variants=%s part2_len_variants=%s（各含「## 硬性规则」头/两段合并口径）"
          % (sorted(p1s), sorted(p2s)))
    base = next((r for r in recs if r["hard_p1"] and r["hard_p2"]), None)
    if base is not None:
        topics = []
        for part, tag in ((base["hard_p1"], "首段"), (base["hard_p2"], "尾段")):
            cur = None
            for ln in part.split("\n"):
                if ln.startswith("- "):
                    if cur is not None:
                        topics.append((tag, len(cur.rstrip())))
                    cur = ln
                elif cur is not None and ln.strip():
                    cur += ln        # 不以 - 开头的续行并入当前主题（如记忆格式第二行）
            if cur is not None:
                topics.append((tag, len(cur.rstrip())))
        hdr = len("## 硬性规则")
        total = len(base["hard_p1"]) + len(base["hard_p2"])
        print("b2_total_chars=%d（含标题 %d 字；两段合并口径，与 §4 硬性规则读数同口径）"
              % (total, hdr))
        names = list(HARD_RULE_TOPIC_NAMES)
        for i, (tag, ch) in enumerate(topics):
            nm = names[i] if i < len(names) else "主题#%d" % (i + 1)
            print("topic=%s part=%s chars=%d share_of_hard_rules_pct=%.1f"
                  % (nm, tag, ch, (100.0 * ch / total) if total else 0.0))
        print("b2_bullets=%d named=%d（bullet 数与概括名数不一致时后者按序号兜底）"
              % (len(topics), min(len(topics), len(names))))
    else:
        print("b2_no_record")

    # ---- B3：新版格式（v2）分段 ----
    print("=== B3. 新版格式（v2）分段（样本少，仅参考） ===")
    v2 = [r for r in recs if r["fmt"] == 2]
    nv = len(v2)
    print("b3_sample_n=%d v2_group_n=%d v2_private_n=%d（样本少，只作参考）"
          % (nv, sum(1 for r in v2 if r["chat"] == "group"),
             sum(1 for r in v2 if r["chat"] == "private")))
    if nv:
        seg_chars = {s: [] for s in SEG_ORDER}
        seg_sum = {s: 0 for s in SEG_ORDER}
        tot_seg_chars = 0
        for r in v2:
            sp = split_system(r["sys"])
            if not sp:
                continue
            tot_seg_chars += sum(sp[0].values())
            for s in SEG_ORDER:
                seg_sum[s] += sp[0].get(s, 0)
                if s in sp[1]:
                    seg_chars[s].append(sp[0].get(s, 0))
        rows = []
        for s in SEG_ORDER:
            xs = sorted(seg_chars[s])
            pres = 100.0 * len(xs) / nv
            share = (100.0 * seg_sum[s] / tot_seg_chars) if tot_seg_chars else 0.0
            rows.append((share * pres, s, pres, share, xs))
        for _sc, s, pres, share, xs in sorted(rows, reverse=True):
            print("v2seg=%s present_pct=%.1f chars_med=%d share_of_v2_system_pct=%.2f"
                  % (s, pres, pct(xs, 50), share))
        # 带话/画图/设备教学的条件核对（v2 应只对主人注入）
        for mk in DRAW_MARKS:
            g = sum(1 for r in v2 if r["chat"] == "group" and mk in r["sys"])
            p = sum(1 for r in v2 if r["chat"] == "private" and mk in r["sys"])
            print("v2_drawmark group_n=%d private_n=%d mark=%s" % (g, p, mk))
        # v2 相对旧版的新段/差异段：行首「## 」表头集合对比
        def _headers(rr):
            hs = set()
            for r in rr:
                for ln in r["sys"].split("\n"):
                    if ln.startswith("## "):
                        hs.add(ln[:20])
            return hs
        v1 = [r for r in recs if r["fmt"] == 1]
        only_v2 = sorted(_headers(v2) - _headers(v1))
        only_v1 = sorted(_headers(v1) - _headers(v2))
        print("b3_header_only_in_v2=%s" % (only_v2 if only_v2 else "无"))
        print("b3_header_only_in_v1=%s" % (only_v1 if only_v1 else "无"))
        jr = sum(1 for r in v2 if "## 本次输出格式" in r["sys"])
        print("b3_json_rule_logged_n=%d（发送时追加、落盘前未含——见 b1_not_logged）" % jr)


if __name__ == "__main__":
    _ap = argparse.ArgumentParser(add_help=False)
    _ap.add_argument("--breakdown", action="store_true")
    _bd_args, _ = _ap.parse_known_args()
    main()
    if _bd_args.breakdown:
        breakdown()
