# -*- coding: utf-8 -*-
"""评测工具公共部分：数据目录、题库格式、风格指标。

题库（JSONL，每行一个场景）：
  {"id": "s001", "source": "ctx|snapshot",
   "chat_type": "group|private", "chat_id": "…", "chat_name": "…",
   "trigger": "at|reply_to_bot|chat_random",
   "msg": {"text": "…", "sender_id": "…", "sender_name": "…",
           "reply_text": "", "has_reply": false},
   "history": [{"ts": 1795…, "sender_id": "…", "sender_name": "…",
                "text": "…", "is_bot": false}, …],   # 旧→新，不含当前这条
   "expect": {"speak": "yes|maybe"}}

题库与结果都放在 ~/.zcode-qq-bot/eval/ 下（含群聊原文，不进仓库）。
"""
import json
import pathlib
import re
import statistics

EVAL_DIR = pathlib.Path.home() / ".zcode-qq-bot" / "eval"
RUNS_DIR = EVAL_DIR / "runs"
BOT_QQ = "3126940548"

# 与审计文档第 0 章「回复风格对照」同一口径
CARE_WORDS = ("记得", "别忘", "穿", "冷", "吃饭", "早点睡", "别熬", "小心", "注意")
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")
# 清洗之后仍残留的「不像聊天文本」的东西：方括号标记、时刻、think 标签、分类前缀
LEAK_RE = re.compile(
    r"\[[^\]\n]{1,32}\]|【(?:时[:：]|回应|SPLIT|跳过|SKIP|WAIT)"
    r"|(?<!\d)\d{1,2}[:：]\d{2}(?!\d)|</?think>|^[A-DＡ-Ｄ]\s*类\s*[—\-－]*")
PLACEHOLDER_RE = re.compile(r"^\[(?:图片|表情|表情包|语音|视频|文件|分享|卡片消息)[^\]]*\]$")


def load_jsonl(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                out.append(json.loads(ln))
    return out


def dump_jsonl(path, rows):
    """写题库/结果文件。只允许写在评测数据目录（~/.zcode-qq-bot/eval）之内。"""
    root = EVAL_DIR.resolve()
    p = pathlib.Path(path).resolve()
    if root not in p.parents:
        raise ValueError("评测数据只能写在 %s 下" % root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                 encoding="utf-8")
    return p


def text_metrics(text):
    t = (text or "").strip()
    n = len(t)
    return {
        "len": n,
        "le10": n <= 10,
        "b11_30": 11 <= n <= 30,
        "b31_60": 31 <= n <= 60,
        "gt60": n > 60,
        "question": ("？" in t) or ("?" in t),
        "ellipsis": ("……" in t) or ("..." in t),
        "bracket": (("（" in t) and ("）" in t)) or (("(" in t) and (")" in t)),
        "care": any(w in t for w in CARE_WORDS),
        "emoji": bool(EMOJI_RE.search(t)),
        "leak": bool(LEAK_RE.search(t)),
    }


def aggregate(texts):
    """一组回复文本的风格指标。空文本不计。"""
    rows = [text_metrics(t) for t in texts if t and t.strip()]
    n = len(rows)
    if not n:
        return {"n": 0}
    lens = sorted(r["len"] for r in rows)

    def pct(key):
        return 100.0 * sum(1 for r in rows if r[key]) / n

    return {
        "n": n,
        "len_med": statistics.median(lens),
        "len_p90": lens[min(n - 1, int(0.9 * n))],
        "le10": pct("le10"), "b11_30": pct("b11_30"),
        "b31_60": pct("b31_60"), "gt60": pct("gt60"),
        "question": pct("question"), "ellipsis": pct("ellipsis"),
        "bracket": pct("bracket"), "care": pct("care"), "emoji": pct("emoji"),
        "leak": pct("leak"),
    }


def human_reference(scens):
    """同一批场景里真人说过的话（去重），作为风格对照的基线。"""
    seen = set()
    texts = []
    for s in scens:
        for h in s.get("history") or []:
            if h.get("is_bot"):
                continue
            t = str(h.get("text") or "").strip()
            if not t or PLACEHOLDER_RE.match(t) or t.startswith("[QQ表情"):
                continue
            key = (str(h.get("sender_id")), t)
            if key in seen:
                continue
            seen.add(key)
            texts.append(t)
    return texts


def fmt_table(rows, title):
    """rows: [(名称, 指标字典)] → 打印对比表。"""
    cols = [("n", "条数", "{:.0f}"), ("len_med", "长度中位", "{:.0f}"),
            ("le10", "≤10字%", "{:.0f}"), ("b11_30", "11-30字%", "{:.0f}"),
            ("b31_60", "31-60字%", "{:.0f}"), ("gt60", ">60字%", "{:.0f}"),
            ("question", "含问号%", "{:.0f}"), ("ellipsis", "省略号%", "{:.0f}"),
            ("bracket", "括号动作%", "{:.0f}"), ("care", "叮嘱词%", "{:.0f}"),
            ("emoji", "emoji%", "{:.0f}"), ("leak", "残留标记%", "{:.1f}")]
    print("\n### " + title)
    print("| 来源 | " + " | ".join(c[1] for c in cols) + " |")
    print("|---|" + "---|" * len(cols))
    for name, m in rows:
        cells = []
        for key, _label, f in cols:
            v = m.get(key)
            cells.append("-" if v is None else f.format(v))
        print("| %s | %s |" % (name, " | ".join(cells)))


def make_fixed_chain(call_fn, model, kv, mo, pick_model=None):
    """做一条「固定单模型」的调用链，用来替换被测代码的 call_glm_with_fallback。

    call_fn 形如 functools.partial(_model_call, A)：即 _model_call 的首参
    （module_or_A）由调用方绑定，本函数以 call_fn(model, kv, mo, messages,
    text_ctx) 调它；返回 (status, data, 耗时, usage)——与 eval_run._model_call
    的真实签名与返回一致。

    返回的函数与生产 call_glm_with_fallback 同签名：
    (messages, text_ctx="", budget_sec=None, route="reply", **kw)。
    budget_sec 与多余的关键字参数一律收下，现阶段不参与控制。

    pick_model(route) 可选：返回本次的模型名（str）——只影响返回值里的模型名
    与记账，call_fn 仍用固定的 model / kv / mo；也可返回 (model, kv, mo)
    三元组（可只给前几项，缺的沿用固定值）——这时 call_fn 本次就按三元组里的
    模型名 / key / 输出上限调，供按路由取不同 key / max_out 的场景用。
    返回 None / 空串或不给 pick_model 时，一律用固定模型。

    status != 200 或 data 不是 dict 时抛 RuntimeError("eval http …")；
    成功返回 (本次模型名, 回复文本)。

    按 route 记账（{route: 次数}，call_fn 抛异常的那次也计）：
    chain.call_counts 是计数 dict 本体，chain.get_counts() 取副本，
    chain.reset_counts() 清零——调用方按场景清零。
    """
    counts = {}

    def chain(messages, text_ctx="", budget_sec=None, route="reply", **kw):
        call_m, use_kv, use_mo = model, kv, mo
        name = model
        if pick_model:
            pm = pick_model(route)
            if pm:
                if isinstance(pm, tuple):
                    call_m, use_kv, use_mo = (list(pm) + [model, kv, mo][len(pm):])[:3]
                    name = call_m
                else:
                    name = pm   # 只换返回值与记账里的模型名，实际调用仍用固定模型
        counts[route] = counts.get(route, 0) + 1
        status, data, dt, usage = call_fn(call_m, use_kv, use_mo, messages, text_ctx)
        if status != 200 or not isinstance(data, dict):
            raise RuntimeError("eval http %s: %s" % (
                status, json.dumps(data, ensure_ascii=False)[:120] if data else "empty"))
        ch = (data.get("choices") or [{}])[0] or {}
        return name, str((ch.get("message") or {}).get("content") or "")

    chain.call_counts = counts
    chain.get_counts = lambda: dict(counts)
    chain.reset_counts = counts.clear
    return chain
