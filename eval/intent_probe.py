# -*- coding: utf-8 -*-
"""intent_probe.py — 主人自然语言指令识别的离线评测（C2 机器关）。

只识别、不执行：对每条输入用产品路径同款的 _intent_prompt 构造提示词，调
真实的 call_glm_with_fallback(..., route="judge")，按产品路径逐道校验
（is_cmd → 指令在表且开放 → 置信度门槛 → args 是对象 → 缺参 → validate），
全程不执行任何命令、不发消息、不写任何配置。

用法：
  python eval/intent_probe.py            # 默认：产品同款，提示词用
                                         # intent_doc(closed="mark")＝18 条全列、
                                         # 未开放九条带「暂不支持」标注
  python eval/intent_probe.py --plain    # 对照：清单全列但不加标注
                                         #（closed="plain"，看标注到底起多大作用）
  python eval/intent_probe.py --limit N  # 总共只跑前 N 条（正例在前、其后依次是
                                         # 应不执行/负例/边界；两种模式都可先用它
                                         # 做小样冒烟）

输入四类（群号一律虚构 5550xx）：
  正例 54 条＝表里 18 条 × 3 个例句，预期命中对应指令；
  应不执行 10 条＝未开放指令（暂停/恢复）的说法，预期不执行任何已开放命令
    （判成 pause/resume 后被 closed 挡掉算对）；
  负例 40 条＝不该命中的闲聊/提问/发任务/陈述；
  边界 1 条＝「你今天用了多少 token 啊，随便问问」——语义上就是在问用量，
    判成 usage 可接受，单独一类、不计入误报。
汇总里单列「正例错类命中」（预期 X、实际判成已开放的 Y 并会执行）与「应不
执行的错执行」——这两类是最危险的，逐条列出。
输出＝评测专用目录 EC.EVAL_DIR 下 intent_probe_<YYYYMMDD-HHMM>_mark.txt
（--plain 时后缀 _plain，同一分钟跑两种模式不会互相覆盖）：
逐条记录（输入、预期、模型原始输出截 300 字、各道校验、最终判定）＋汇总。

初始化照 eval_run.py / eval_common.py：只读生产配置与人设，用量记账打桩，
插件钩子不碰。并发不超过 2。
"""
import argparse
import concurrent.futures
import datetime
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eval_common as EC  # noqa: E402

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "plugins", "qq-bot", "scripts")
sys.path.insert(0, _SCRIPTS)

import qq_bot_agent as A  # noqa: E402
import qq_commands as K   # noqa: E402

# 评测进程不得产生任何生产副作用：用量记账与运行日志打桩（照 eval_run.py）
A.log_usage = lambda *x, **k: None
_LOGS = []
A.out = lambda line: _LOGS.append(str(line))

_tls = threading.local()
_ORIG_CALL = A.call_glm_with_fallback


def _recording_call(messages, *a, **kw):
    """透传的真实调用，顺手把原始回复记到线程本地（并发 2 下各记各的）。"""
    _m, reply = _ORIG_CALL(messages, *a, **kw)
    _tls.raw = str(reply or "")
    return _m, reply


A.call_glm_with_fallback = _recording_call

# ---- 三类内置用例（预期见 docstring；群里 5550xx 全是虚构群号） ----

# 应不执行：未开放指令（暂停/恢复）的说法——不被吸到已开放的安静/继续才算对
NO_EXEC_CASES = [
    "你先歇着吧，暂停，谁都别回",
    "恢复，可以继续回复了",
    "全部停了，先别回任何消息",
    "把自动回复停掉",
    "现在恢复自动回复",
    "暂停回复，等我叫你",
    "先把回复暂停一下，我这边要改东西",
    "暂停服务，我调试的时候用",
    "现在解除暂停吧",
    "恢复吧，刚才让你停的就是这个",
]

# 边界（可接受）：语义上就是在问用量，判成 usage 合理，不计入误报
BOUNDARY_CASES = [
    "你今天用了多少 token 啊，随便问问",
]

NEGATIVE_CASES = [
    # 感慨 / 陈述事实
    "今天状态不错",
    "群里最近好安静",
    "谢谢",
    "辛苦啦，今天表现不错",
    "哈哈哈，笑死我了",
    "我觉得你今天说话挺有意思的",
    "状态不太妙啊，感觉你今天有点卡",
    "现在的用量应该不小了吧，有点担心",
    # 提问 / 闲聊
    "帮我看看今天天气",
    "你能干嘛呀，随便聊聊",
    "今晚吃什么好呢",
    "你现在什么情况，是出什么事了吗",
    "天气预报说明天有雨，是真的吗",
    "你累不累呀",
    "看图这个功能好用吗，你评价一下",
    "联网搜索你平时常用吗",
    "防滥用会误伤人吗",
    "日志里都有啥好玩的内容吗",
    "定时任务这个功能怎么用啊，教教我",
    "人设里你最喜欢哪一个？随便聊聊",
    # 发任务 / 请机器人做事（不是主人遥控指令）
    "任务：写个脚本统计文件夹大小",
    "把刚才那张图画成猫",
    "去群里说一声今晚不开播",
    "帮我写一首关于秋天的诗",
    "把这段话翻译成英文：你好世界",
    "帮我记一下：明天三点要去开会",
    "帮我盯着点群里的消息，有事随时告诉我",
    "把这个群最近聊的话题整理成一段总结",
    # 像指令但不是指令的陈述 / 否定 / 转述
    "我想安静一会",
    "你安静点也没关系，我不介意",
    "555011 这个群好热闹啊",
    "555012 那个群我退了，你不用管",
    "群里 555013 有人吵架了，你去看看",
    "昨天那个定时任务跑完了吗",
    "今天先不设定时了",
    "我记得你有个帮助菜单？不用发，就问问",
    "群列表我看过啦，不用再发了",
    "继续我们刚才的话题吧",
    "把昨天的日志翻出来看看？算了不用了",
    "上次说的开关看图的事，我再想想",
]

MISFIRE_VERDICTS = ("命中", "命中(错指令)", "反问")
WANT_LABEL = {"hit": "应命中 %s", "no_exec": "应不执行（未开放指令）",
              "boundary": "边界（可接受）", "miss": "不应命中"}


def probe_one(case, doc):
    """跑一条：提示词 → 判定模型 → 产品路径同款逐道校验。返回
    (原始输出, 校验字典, 判定)；closed 指令两种模式都按产品路径挡掉。"""
    prompt = A._intent_prompt(case["text"], doc)
    _tls.raw = ""
    d = A._intent_json(prompt)
    raw = _tls.raw
    st = {}
    if not isinstance(d, dict):
        st["parse"] = "失败"
        return raw, st, "格式失败"
    st["parse"] = "OK"
    is_cmd = d.get("is_cmd") is True
    st["is_cmd"] = "True" if is_cmd else str(d.get("is_cmd"))
    name = str(d.get("name") or "").strip()
    st["name"] = name or "（空）"
    if not is_cmd:
        return raw, st, "未命中"
    ent = K.by_name(name)
    if ent is None:
        st["open"] = "不在表"
        return raw, st, "未命中"
    st["open"] = "是" if ent["intent"] else "未开放"
    if not ent["intent"]:
        return raw, st, "未命中"
    gate = K.INTENT_MIN_CONF[ent["risk"]]
    conf_raw = d.get("confidence")
    conf_ok = (isinstance(conf_raw, (int, float))
               and not isinstance(conf_raw, bool)
               and conf_raw == conf_raw and float(conf_raw) >= gate)
    st["conf"] = "%s（门槛 %.2f）" % (conf_raw, gate)
    if not conf_ok:
        return raw, st, "未命中"
    args = d.get("args")
    st["args"] = "OK" if isinstance(args, dict) else "不是对象"
    if not isinstance(args, dict):
        return raw, st, "未命中"
    need = K.missing(name, args)
    if need:
        st["missing"] = "缺 " + "、".join(need)
        return raw, st, "反问"
    st["missing"] = "无"
    ok, _norm, why = K.validate(name, args, case["text"])
    st["validate"] = "OK" if ok else str(why)[:80]
    if not ok:
        return raw, st, "未命中"
    if case["expect"] == "hit" and name != case["cmd"]:
        return raw, st, "命中(错指令)"
    return raw, st, "命中"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plain", action="store_true",
                    help="对照模式：清单全列但不加「暂不支持」标注"
                         "（closed=plain），看标注到底起多大作用")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    mode = "plain" if a.plain else "mark"

    doc = K.intent_doc(closed=mode)
    positives = [{"expect": "hit", "cmd": c["name"], "text": ex}
                 for c in K.COMMANDS for ex in c["examples"]]
    no_exec = [{"expect": "no_exec", "cmd": None, "text": t}
               for t in NO_EXEC_CASES]
    boundary = [{"expect": "boundary", "cmd": None, "text": t}
                for t in BOUNDARY_CASES]
    negatives = [{"expect": "miss", "cmd": None, "text": t}
                 for t in NEGATIVE_CASES]
    cases = positives + no_exec + negatives + boundary
    if a.limit:
        cases = cases[:a.limit]
    n_pos = sum(1 for c in cases if c["expect"] == "hit")
    n_noe = sum(1 for c in cases if c["expect"] == "no_exec")
    n_neg = sum(1 for c in cases if c["expect"] == "miss")
    n_bnd = len(cases) - n_pos - n_noe - n_neg
    print("指令识别离线评测：closed=%s 模式，%d 条（正例 %d＋应不执行 %d＋"
          "负例 %d＋边界 %d），并发 2"
          % (mode, len(cases), n_pos, n_noe, n_neg, n_bnd))
    print("判定路由预算 %d 秒；只识别、不执行、不发消息"
          % A._route_budget_sec("judge"))

    results = [None] * len(cases)

    def work(i):
        try:
            results[i] = probe_one(cases[i], doc)
        except Exception as e:
            results[i] = ("异常: %r" % (e,), {"parse": "异常"}, "格式失败")

    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(work, i) for i in range(len(cases))]
        for f in concurrent.futures.as_completed(futs):
            f.result()
            done += 1
            if done % 10 == 0 or done == len(cases):
                print("  进度 %d/%d" % (done, len(cases)))

    lines = []
    lines.append("主人自然语言指令识别 · 离线评测（只识别、不执行）")
    lines.append("模式：closed=%s%s  用例 %d 条（正例 %d＋应不执行 %d＋负例 %d"
                 "＋边界 %d）"
                 % (mode, "（对照：无标注）" if a.plain else "（产品同款：带标注）",
                    len(cases), n_pos, n_noe, n_neg, n_bnd))
    lines.append("")
    for i, (case, r) in enumerate(zip(cases, results)):
        raw, st, verdict = r
        want = WANT_LABEL[case["expect"]]
        if case["expect"] == "hit":
            want = want % case["cmd"]
        lines.append("[%03d] 预期=%s | 输入=%s" % (i + 1, want, case["text"]))
        lines.append("     原始输出：%s" % raw[:300].replace("\n", " "))
        lines.append("     校验：%s" % "  ".join("%s=%s" % kv for kv in st.items()))
        lines.append("     判定：%s" % verdict)
    lines.append("")

    # ---- 汇总 ----
    per_cmd = {}
    pos_hit = 0
    pos_ask = 0
    pos_miss = 0
    wrong_exec = []      # 正例错类命中：预期 X、实际判成已开放的 Y 并会执行
    noe_bad = []         # 应不执行的错执行：被判成已开放命令并会执行
    neg_misfire = []
    boundary_fired = []
    fmt_fail = 0
    for case, r in zip(cases, results):
        _raw, _st, verdict = r
        if verdict == "格式失败":
            fmt_fail += 1
        if case["expect"] == "hit":
            per_cmd.setdefault(case["cmd"], [0, 0])
            per_cmd[case["cmd"]][1] += 1
            if verdict == "命中":
                per_cmd[case["cmd"]][0] += 1
                pos_hit += 1
            elif verdict == "命中(错指令)":
                wrong_exec.append((case["cmd"], _st.get("name"),
                                   case["text"], _raw))
            elif verdict == "反问":
                pos_ask += 1
            else:
                pos_miss += 1
        elif case["expect"] == "no_exec":
            if verdict in ("命中", "反问"):
                noe_bad.append((_st.get("name"), case["text"], _raw))
        elif case["expect"] == "boundary":
            if verdict in MISFIRE_VERDICTS:
                boundary_fired.append((_st.get("name"), case["text"], _raw))
        elif verdict in MISFIRE_VERDICTS:
            neg_misfire.append((case["text"], verdict, _raw))

    lines.append("==== 汇总 ====")
    lines.append("正例 %d 条：命中 %d（%.0f%%），错类命中 %d（最危险，逐条见下），"
                 "反问 %d，未命中 %d"
                 % (n_pos, pos_hit, 100.0 * pos_hit / max(1, n_pos),
                    len(wrong_exec), pos_ask, pos_miss))
    lines.append("应不执行 %d 条：错执行 %d（逐条见下）"
                 % (n_noe, len(noe_bad)))
    lines.append("负例 %d 条：误报 %d（%s）"
                 % (n_neg, len(neg_misfire),
                    "见下" if neg_misfire else "零误报"))
    lines.append("边界 %d 条：判成命中的 %d（可接受，不计误报）"
                 % (n_bnd, len(boundary_fired)))
    lines.append("格式失败 %d 条（解析不出 JSON 对象）" % fmt_fail)
    lines.append("正例按命令（命中/总数）：")
    for c in K.COMMANDS:
        hit, total = per_cmd.get(c["name"], [0, 0])
        if total:
            lines.append("  %-15s %d/%d%s" % (
                c["name"], hit, total,
                "" if c["intent"] else "（未开放，判成本条也只会被 closed 挡掉）"))
    if wrong_exec:
        lines.append("正例错类命中（预期 X、实际判成已开放的 Y 并会执行）逐条列出：")
        for want, got, text, raw in wrong_exec:
            lines.append("  - 预期 %s → 实际 %s ｜ %s ｜ 输出：%s"
                         % (want, got, text, raw[:100].replace("\n", " ")))
    if noe_bad:
        lines.append("应不执行却被判成会执行的，逐条列出：")
        for got, text, raw in noe_bad:
            lines.append("  - 判成 %s ｜ %s ｜ 输出：%s"
                         % (got, text, raw[:100].replace("\n", " ")))
    if neg_misfire:
        lines.append("负例里被判成命中的逐条列出：")
        for text, verdict, raw in neg_misfire:
            lines.append("  - [%s] %s ｜ 模型输出：%s"
                         % (verdict, text, raw.replace("\n", " ")))
    if boundary_fired:
        lines.append("边界被判成命中的（可接受）：")
        for got, text, raw in boundary_fired:
            lines.append("  - 判成 %s ｜ %s" % (got, text))
    lines.append("运行日志条数（AGENT_*，不含原话与参数值）：%d" % len(_LOGS))

    EC.EVAL_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    out_path = EC.EVAL_DIR / ("intent_probe_%s_%s.txt" % (stamp, mode))
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("结果：%s" % out_path)
    print("正例命中 %d/%d（错类命中 %d），应不执行错执行 %d，负例误报 %d，"
          "格式失败 %d"
          % (pos_hit, n_pos, len(wrong_exec), len(noe_bad),
             len(neg_misfire), fmt_fail))
    for want, got, text, _raw in wrong_exec:
        print("  错类命中：预期 %s → 实际 %s ｜ %s" % (want, got, text))
    for got, text, _raw in noe_bad:
        print("  错执行：判成 %s ｜ %s" % (got, text))
    for text, verdict, _raw in neg_misfire:
        print("  误报 [%s] %s" % (verdict, text))


if __name__ == "__main__":
    main()
