# -*- coding: utf-8 -*-
"""量旧闸门在本题库上的通过率和 token，给「现状到底花多少」一个公平的对照。

eval_run 的 v1 模式不含闸门（每条旁观消息都跑一次完整调用），而线上「精简上下文」
默认开着——旧版每条旁观消息其实也先过一次轻量判定（约 500 token），通过了才发
完整调用。直接拿 v1 模式当「现状的 token」会高估现状，新版省 token 的比例也就
跟着虚高。这个脚本把那道旧闸门在同样的场景上真跑一遍。

用法：
  python eval_gate_baseline.py --src <线上版 scripts> --set set_short.jsonl \\
      --tag oldgate_short --main-run base_v1_short.jsonl --new-run pipe_json_short_v3.jsonl

  --main-run  同一批场景上「旧提示词 + 一次完整调用」的结果文件（eval_run --mode v1）
  --new-run   同一批场景上新流程的结果文件（eval_run --mode json），用来并排比较

只读生产配置与人设；不写配置、不发消息。结果写到 ~/.zcode-qq-bot/eval/runs/<tag>.jsonl。
"""
import argparse
import concurrent.futures
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eval_common as EC  # noqa: E402
import eval_run as ER  # noqa: E402

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--set", dest="set_name", default="set_short.jsonl")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--main-run", dest="main_run", default="")
    ap.add_argument("--new-run", dest="new_run", default="")
    ap.add_argument("--model", default="glm-5.4")
    ap.add_argument("--workers", type=int, default=2)
    a = ap.parse_args()

    A, _D, _C = ER.load_agent(a.src)
    A.log_usage = lambda *x, **k: None
    A.out = lambda line: None
    if not hasattr(A, "_interrupt_gate"):
        raise SystemExit("--src 指向的代码里没有旧闸门 _interrupt_gate")
    A.PLUGIN_HOST.on_message_all = lambda msg: None
    A.PLUGIN_HOST.daemon = ER._NullDaemon()
    A.PLUGIN_HOST.log = lambda line: None
    try:
        A.PLUGIN_HOST.load_all()
    except Exception as e:
        print("插件加载失败（判定只看对话，不受影响）：%r" % e)
    if not (A.chat_anti_interrupt_enabled() and A.lean_ctx_on()):
        print("注意：按当前配置，线上并不会走旧闸门（防插嘴判定或精简上下文是关着的）")

    scens = [s for s in EC.load_jsonl(EC.EVAL_DIR / a.set_name) if s["trigger"] == "chat_random"]
    cands = A._chain_candidates()
    pick = next((c for c in cands if a.model in c[2]), cands[0] if cands else None)
    if not pick:
        raise SystemExit("模型链里没有可用条目")
    kid, kv, model, mo = pick[0], pick[1], pick[2], pick[4]

    def _chain(messages, text_ctx="", budget_sec=200):
        """固定单模型 + 记账（与 eval_run 同口径，记在 eval_run 自己的线程局部变量里）。"""
        ER._tls.calls = []
        status, data, dt, usage = ER._model_call(A, model, kv, mo, messages, text_ctx)
        if status != 200 or not isinstance(data, dict):
            raise RuntimeError("http %s" % status)
        ch = (data.get("choices") or [{}])[0] or {}
        return model, str((ch.get("message") or {}).get("content") or "")

    A.call_glm_with_fallback = _chain
    print("旧闸门：%d 个闲聊场景 | 模型 %s | 并发 %d" % (len(scens), model, a.workers))
    pc, _g = A.load_persona_collection()

    def work(s):
        msg = ER.build_msg(s)
        dm = ER._NullDaemon(s)
        name = A.persona_for(msg["chat_type"], msg["chat_id"], msg.get("sender_id"))[0] \
            or pc.get("active")
        row = {"id": s["id"], "trigger": s["trigger"], "variant": a.tag, "mode": "oldgate"}
        ER._tls.calls = []
        t0 = time.time()
        try:
            skip = A._interrupt_gate(dm, msg, name or "智谱娘")
            row["passed"] = (skip is False)
            row["status"] = 200
        except Exception as e:
            row["passed"] = False
            row["status"] = 0
            row["error"] = repr(e)[:160]
        calls = list(getattr(ER._tls, "calls", None) or [])
        row["calls"] = len(calls)
        row["latency"] = round(time.time() - t0, 2)
        row["gate_tokens"] = sum(c["prompt_tokens"] for c in calls)
        return row

    rows = [None] * len(scens)
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, r in zip(range(len(scens)), ex.map(work, scens)):
            rows[i] = r
    out = EC.dump_jsonl(EC.RUNS_DIR / (a.tag + ".jsonl"), rows)
    ok = [r for r in rows if r.get("status") == 200]
    passed = [r for r in ok if r["passed"]]
    tok = sorted(r["gate_tokens"] for r in ok)
    print("结果：%s" % out)
    print("旧闸门：通过 %d / %d（%.0f%%）| 判定失败 %d | 判定调用输入 token 中位 %d，合计 %d" % (
        len(passed), len(ok), 100.0 * len(passed) / max(1, len(ok)), len(rows) - len(ok),
        statistics.median(tok) if tok else 0, sum(tok)))
    if not (a.main_run and a.new_run):
        return
    base = {r["id"]: r for r in EC.load_jsonl(EC.RUNS_DIR / a.main_run) if r["trigger"] == "chat_random"}
    new = {r["id"]: r for r in EC.load_jsonl(EC.RUNS_DIR / a.new_run) if r["trigger"] == "chat_random"}
    ids = [r["id"] for r in rows if r["id"] in base and r["id"] in new]
    old_real = sum(r["gate_tokens"] for r in rows if r["id"] in ids) + sum(
        (base[i].get("prompt_tokens") or 0) for i in ids if any(
            r["id"] == i and r["passed"] for r in rows))
    old_nogate = sum((base[i].get("prompt_tokens") or 0) for i in ids)
    new_tok = sum((new[i].get("prompt_tokens") or 0) for i in ids)
    print("同一批 %d 个闲聊场景的输入 token 合计：" % len(ids))
    print("  现状·不带闸门（eval v1 模式）: %d" % old_nogate)
    print("  现状·线上真实流程（旧闸门 + 通过后完整调用，估算）: %d" % old_real)
    print("  新版（%s）: %d" % (a.new_run, new_tok))
    if old_real:
        print("  新版相对线上真实流程：%+.0f%%" % (100.0 * (new_tok - old_real) / old_real))


if __name__ == "__main__":
    main()
