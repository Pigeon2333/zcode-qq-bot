# -*- coding: utf-8 -*-
"""给一次或多次评测结果打分，并与同一批场景里真人的说话方式对照。

用法：python eval_score.py --set set_short.jsonl <结果1.jsonl> [结果2.jsonl …]
结果文件在 ~/.zcode-qq-bot/eval/runs/ 下。
"""
import argparse
import collections
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eval_common as EC  # noqa: E402


def pct(a, b):
    return "%d/%d" % (a, b)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", dest="set_name", default="set_short.jsonl")
    ap.add_argument("runs", nargs="+")
    a = ap.parse_args()
    scens = {s["id"]: s for s in EC.load_jsonl(EC.EVAL_DIR / a.set_name)}
    # 裸@场景：「现在」后面跟的是系统给模型的指路文字，不是对方说的话——不计分
    bad = {k for k, s in scens.items() if str(s["msg"]["text"]).startswith("只@了你")}
    if bad:
        scens = {k: v for k, v in scens.items() if k not in bad}
        print("（跳过 %d 个裸@场景：正文是系统指路文字）" % len(bad))
    trig = {k: s["trigger"] for k, s in scens.items()}

    all_rows = []
    for name in a.runs:
        rows = [r for r in EC.load_jsonl(EC.RUNS_DIR / name) if r["id"] in scens]
        tag = (rows[0].get("variant") if rows else name) or name
        print("\n## %s（%s）" % (tag, name))
        by = collections.defaultdict(list)
        for r in rows:
            by[trig.get(r["id"], "?")].append(r)
        for t in ("at", "reply_to_bot", "chat_random"):
            rs = by.get(t)
            if not rs:
                continue
            kinds = collections.Counter(r.get("kind") or "error" for r in rs)
            err = [r for r in rs if r.get("error")]
            print("- 触发类型 %s：%d 个场景 → %s" % (
                t, len(rs), dict(kinds)))
            if err and t == "at":
                for r in err[:3]:
                    print("    错误样例：%s" % str(r.get("error"))[:150])
        ok = [r for r in rows if not r.get("error") and "latency" in r]
        if ok:
            lat = sorted(r["latency"] for r in ok)
            print("- 延迟：中位 %.1fs  p90 %.1fs" % (
                statistics.median(lat), lat[min(len(lat) - 1, int(.9 * len(lat)))]))
        sy = [r["system_chars"] for r in rows]
        us = [r["user_chars"] for r in rows]
        pt = [r.get("prompt_tokens") or 0 for r in rows]
        ct = [r.get("completion_tokens") or 0 for r in rows]
        print("- 提示词：system 中位 %d 字  user 中位 %d 字" % (
            statistics.median(sy), statistics.median(us)))
        print("- token：输入合计 %d（均 %d/条）  输出合计 %d" % (
            sum(pt), sum(pt) // max(1, len(pt)), sum(ct)))
        if any("json_ok" in r for r in rows):
            n = len(rows)
            print("- JSON：可解析 %s  符合结构 %s；want 分布 %s" % (
                pct(sum(1 for r in rows if r.get("json_ok")), n),
                pct(sum(1 for r in rows if r.get("json_schema_ok")), n),
                dict(collections.Counter(r.get("want") for r in rows))))
        all_rows.append((tag, rows))

    print("\n# 风格对照（只统计真正发出去的回复）")
    table = []
    for tag, rows in all_rows:
        table.append((tag, EC.aggregate([r.get("final") or "" for r in rows])))
        at = [r.get("final") or "" for r in rows if trig.get(r["id"]) == "at"]
        if at:
            table.append((tag + "·仅@", EC.aggregate(at)))
    table.append(("真人（同一批场景）", EC.aggregate(EC.human_reference(list(scens.values())))))
    EC.fmt_table(table, "回复风格：机器人各版本 vs 真人")

    print("\n# 残留标记 / 空回复")
    for tag, rows in all_rows:
        leak = [r for r in rows if r.get("final") and EC.LEAK_RE.search(r["final"])]
        empty = [r for r in rows if r.get("kind") == "stripped"]
        print("- %s：清洗后仍有标记 %d 条；清洗后变空 %d 条" % (tag, len(leak), len(empty)))
        for r in leak[:3]:
            print("    %s：%s" % (r["id"], r["final"][:80].replace("\n", " ")))


if __name__ == "__main__":
    main()
