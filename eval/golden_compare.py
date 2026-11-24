# -*- coding: utf-8 -*-
"""逐字节比对两份导出的提示词（eval_run.py --dump 的产物）。

用法：python golden_compare.py <a.jsonl> <b.jsonl>
比对前把随时间变化的行（当前真实时间）归一化。默认要求完全一致；
加 --show 时对有差异的场景打印第一处差异的上下文。
"""
import difflib
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eval_common as EC  # noqa: E402

TIME_RE = re.compile(r"【当前真实时间：[^】]*】")


def norm(s):
    return TIME_RE.sub("【当前真实时间：T】", s)


def first_diff(a, b, width=60):
    i = next((k for k, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    return "位置 %d\n  A: …%s…\n  B: …%s…" % (i, a[max(0, i - width):i + width].replace("\n", "⏎"),
                                          b[max(0, i - width):i + width].replace("\n", "⏎"))


def main():
    show = "--show" in sys.argv
    files = [x for x in sys.argv[1:] if not x.startswith("--")]
    if len(files) != 2:
        raise SystemExit(__doc__)
    A = {r["id"]: r for r in EC.load_jsonl(EC.EVAL_DIR / files[0])}
    B = {r["id"]: r for r in EC.load_jsonl(EC.EVAL_DIR / files[1])}
    ids = [k for k in A if k in B]
    same = 0
    diffs = []
    for k in ids:
        a_s, b_s = norm(A[k]["system"]), norm(B[k]["system"])
        a_u, b_u = norm(A[k]["user"]), norm(B[k]["user"])
        if a_s == b_s and a_u == b_u:
            same += 1
        else:
            diffs.append(k)
    print("共同场景 %d 个：完全一致 %d，有差异 %d" % (len(ids), same, len(diffs)))
    if diffs:
        print("有差异的场景：", diffs[:20])
        if show:
            k = diffs[0]
            for part in ("system", "user"):
                a_t, b_t = norm(A[k][part]), norm(B[k][part])
                if a_t != b_t:
                    print("\n[%s · %s] 首处差异 %s" % (k, part, first_diff(a_t, b_t)))
                    al, bl = a_t.split("\n"), b_t.split("\n")
                    n = 0
                    for ln in difflib.unified_diff(al, bl, lineterm="", n=0):
                        if ln[:1] in "+-" and ln[:3] not in ("+++", "---"):
                            print("   ", ln[:150])
                            n += 1
                            if n >= 24:
                                break
    sys.exit(1 if diffs else 0)


if __name__ == "__main__":
    main()
