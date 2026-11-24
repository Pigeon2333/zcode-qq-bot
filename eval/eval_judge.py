# -*- coding: utf-8 -*-
"""盲评：让裁判模型比较两个版本对同一场景的回复，不知道哪个是哪个。

用法：python eval_judge.py --src <scripts目录> --set set_short.jsonl \
                           <结果A.jsonl> <结果B.jsonl> [--workers 2]

规则：
  - 只评两边都发了回复的场景；
  - 每对回复让裁判判两次（甲乙位置对调），两次都判同一方胜才算该方「稳胜」，
    否则记平——抵消裁判的位置偏好；
  - 裁判看不到系统提示词、不知道版本名，只看聊天记录和两条回复。
裁判模型与凭据走被评测代码的 glm_chat（只读密钥文件，不打印）。
"""
import argparse
import concurrent.futures
import json
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eval_common as EC  # noqa: E402
import eval_run as ER  # noqa: E402

JUDGE_SYS = (
    "你是一个QQ群里的普通群友，判断力接近真人。下面是一段群聊记录、最后一条消息，"
    "以及机器人对最后这条消息的两种回复（甲、乙）。只根据「像不像一个真实的群友"
    "会说的话」来判断，标准：\n"
    "1. 自然：像真人在QQ群里打字，不像客服、助手或播音腔；长度和语气与这个群的"
    "聊天氛围相称（注意看群里真人的消息一般多长、什么语气）。\n"
    "2. 接得住：回应的是最后这条消息和说话的人，没有答非所问、叫错人、硬接话、"
    "把别人之间的对话当成对自己说的。\n"
    "3. 不出戏：没有系统腔、说教、自我说明、多余的关心叮嘱、刻意堆砌的语气词或表情。\n"
    "两边的短处都要看：过长的、像小作文的、模板化的、把天聊死的，都算差。\n"
    "回复里的①②表示机器人分几条消息连发，不是几个备选；连发短句在QQ里很常见，"
    "按连起来的整体来判断。\n"
    '只输出一个 JSON 对象：{"like_human": "甲" 或 "乙", "fits": "甲" 或 "乙", '
    '"reason": "一句话理由"}')


def scene_text(s, max_hist=30):
    lines = []
    for h in (s.get("history") or [])[-max_hist:]:
        name = "机器人" if h.get("is_bot") else str(h.get("sender_name") or "?")
        t = " ".join(str(h.get("text") or "").split())
        if t:
            lines.append("%s：%s" % (name, t))
    who = "机器人" if False else str(s["msg"].get("sender_name") or "对方")
    return ("【群聊记录（旧→新）】\n%s\n\n【最后一条】%s：%s" % (
        "\n".join(lines) or "（无）", who, s["msg"]["text"])),


_BUBBLE_MARKS = "①②③④⑤⑥⑦⑧⑨"


def shown(r):
    """把回复呈现给裁判。多条消息（连发）用 ①② 标出。
    此前是拿「 / 」拼成一串，裁判会把「A / B」读成拼接痕迹或几个备选——
    冤枉了连发这种很真人的说话方式，而结构化输出连发更多，等于系统性压低它的分。"""
    parts = [x.strip() for x in str(r.get("final") or "").split("\n") if x.strip()]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return " ".join("%s%s" % (_BUBBLE_MARKS[i] if i < len(_BUBBLE_MARKS) else "(%d)" % (i + 1), p)
                    for i, p in enumerate(parts))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--set", dest="set_name", default="set_short.jsonl")
    ap.add_argument("--model", default="glm-5.4")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("runs", nargs=2)
    a = ap.parse_args()
    A, _D, _C = ER.load_agent(a.src)
    A.log_usage = lambda *x, **k: None
    A.out = lambda line: None
    scens = {s["id"]: s for s in EC.load_jsonl(EC.EVAL_DIR / a.set_name)}
    ra = {r["id"]: r for r in EC.load_jsonl(EC.RUNS_DIR / a.runs[0])}
    rb = {r["id"]: r for r in EC.load_jsonl(EC.RUNS_DIR / a.runs[1])}
    tag_a = next(iter(ra.values()))["variant"]
    tag_b = next(iter(rb.values()))["variant"]
    ids = [k for k in scens if k in ra and k in rb
           and ra[k].get("final") and rb[k].get("final")]
    print("两边都回复的场景：%d 个（%s vs %s）" % (len(ids), tag_a, tag_b))

    cands = A._chain_candidates()
    pick = next((c for c in cands if a.model in c[2]), cands[0])
    kid, kv, model, mo = pick[0], pick[1], pick[2], pick[4]
    rnd = random.Random(20261124)

    def judge(sid, swap):
        s = scens[sid]
        first, second = (rb[sid], ra[sid]) if swap else (ra[sid], rb[sid])
        user = "%s\n\n【回复甲】%s\n\n【回复乙】%s" % (
            scene_text(s)[0], shown(first), shown(second))
        status, data, dt, _mt = ER.model_call(A, model, kv, mo, [
            {"role": "system", "content": JUDGE_SYS},
            {"role": "user", "content": user}], user)
        row = {"id": sid, "swap": swap}
        if status != 200 or not isinstance(data, dict):
            row["error"] = json.dumps(data, ensure_ascii=False)[:160]
            return row
        content = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        row["latency"] = round(dt, 2)
        try:
            obj = json.loads(content[content.find("{"):content.rfind("}") + 1])
            pick_name = obj.get("like_human")
            row["winner"] = None if pick_name not in ("甲", "乙") else (
                tag_a if ((pick_name == "甲") != swap) else tag_b)
            fit = obj.get("fits")
            row["fit"] = None if fit not in ("甲", "乙") else (
                tag_a if ((fit == "甲") != swap) else tag_b)
            row["reason"] = str(obj.get("reason") or "")[:160]
        except ValueError:
            row["error"] = "bad json: " + content[:80]
        return row

    jobs = [(sid, swap) for sid in ids for swap in (False, True)]
    rnd.shuffle(jobs)
    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as ex:
        for r in ex.map(lambda j: judge(*j), jobs):
            rows.append(r)
            if len(rows) % 10 == 0:
                print("  进度 %d/%d" % (len(rows), len(jobs)))
    out = EC.dump_jsonl(EC.RUNS_DIR / ("judge_%s__vs__%s.jsonl" % (tag_a, tag_b)), rows)

    # 汇总：两次位置对调都判同一方 = 稳胜
    win = {tag_a: 0, tag_b: 0}
    tie = err = 0
    by_trig = {}
    reasons = {tag_a: [], tag_b: []}
    for sid in ids:
        rs = [r for r in rows if r["id"] == sid]
        if len(rs) < 2 or any(r.get("error") for r in rs):
            err += 1
            continue
        w = set(r.get("winner") for r in rs)
        t = scens[sid]["trigger"]
        d = by_trig.setdefault(t, {tag_a: 0, tag_b: 0, "tie": 0})
        if len(w) == 1 and None not in w:
            win[next(iter(w))] += 1
            d[next(iter(w))] += 1
            reasons[next(iter(w))].append((sid, rs[0].get("reason")))
        else:
            tie += 1
            d["tie"] += 1
    lat = sorted(r["latency"] for r in rows if r.get("latency"))
    print("\n盲评结果（%d 对，稳胜 = 两次位置对调都判同一方）" % len(ids))
    print("  %s 稳胜 %d   %s 稳胜 %d   平 %d   出错 %d" % (
        tag_a, win[tag_a], tag_b, win[tag_b], tie, err))
    for t, d in by_trig.items():
        print("  触发类型 %s：%s=%d  %s=%d  平=%d" % (t, tag_a, d[tag_a], tag_b, d[tag_b], d["tie"]))
    if lat:
        print("  裁判延迟：中位 %.1fs  p90 %.1fs" % (
            statistics.median(lat), lat[min(len(lat) - 1, int(.9 * len(lat)))]))
    for tag in (tag_a, tag_b):
        print("  %s 稳胜的理由样例：" % tag)
        for sid, why in reasons[tag][:4]:
            print("    %s：%s" % (sid, why))
    print("明细：%s" % out)


if __name__ == "__main__":
    main()
