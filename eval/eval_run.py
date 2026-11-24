# -*- coding: utf-8 -*-
"""对题库跑一遍某个代码版本，记录每条回复与开销。

用法：
  python eval_run.py --src <scripts目录> --mode v1|v2|json --set set_short.jsonl
                     --tag <结果名> [--limit N] [--workers 2] [--dry] [--resume]

  --src   要评测的源码目录（主分支或改造分支的 plugins/qq-bot/scripts）
  --mode  v1   = 旧逻辑：旧提示词 + 主调用 + 文本协议解析（不含闸门）
          v2   = 改造分支的真实处理流程（文本协议）：v2 提示词 → 统一判定 →
                 主调用 → 策略层
          json = 同上，但主调用用结构化输出
  --dry   只构造提示词、不调模型（比较两种提示词的体积）
  --dump  另存构造好的提示词（配合 golden_compare.py 做逐字节回归）
  --resume 结果文件已存在时只补跑出错/缺失的场景

v2 / json 直接调用被测代码里的 _reply_gate_v2 / _generate_reply_v2，
模型调用替换成「固定单模型 + 记账」的实现，所以评测的就是将来上线的那条路径。
只读生产配置与人设；不写任何生产文件，不发送任何消息。
结果写到 ~/.zcode-qq-bot/eval/runs/<tag>.jsonl。
"""
import argparse
import concurrent.futures
import json
import os
import statistics
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eval_common as EC  # noqa: E402

BOT_QQ = EC.BOT_QQ
_tls = threading.local()


class _NullDaemon(object):
    """只提供 build_messages 需要的 history；其余任何方法都是无副作用的空操作。"""

    def __init__(self, scen=None):
        self.scen = scen or {}

    def history(self, chat_type, chat_id, limit=30):
        h = self.scen.get("history") or []
        return h[-limit:] if limit else h

    def group_names(self):
        return {}

    def __getattr__(self, name):
        def _noop(*a, **k):
            return {"ok": False, "error": "eval: no side effects"}

        return _noop


def load_agent(src_dir):
    src_dir = os.path.abspath(src_dir)
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    import qq_bot_agent as A  # noqa: E402
    import qq_daemon as D  # noqa: E402
    import qq_common as C  # noqa: E402
    return A, D, C


def build_msg(s):
    m = s["msg"]
    return {
        "id": "eval", "chat_type": s["chat_type"], "chat_id": s["chat_id"],
        "chat_name": s.get("chat_name") or s["chat_id"],
        "sender_id": m["sender_id"], "sender_name": m["sender_name"],
        "text": m["text"], "trigger_reason": s["trigger"], "message_id": "0",
        "image_urls": [], "at_me": bool(m.get("at_me")), "at_users": [],
        "has_reply": bool(m.get("has_reply")), "reply_to": None,
        "reply_text": m.get("reply_text") or "", "face_count": 0,
    }


def _model_call(A, model, kv, mo, messages, text_ctx):
    """与生产相同的参数口径（思考档位、输出上限）。返回 (status, data, 耗时, usage)。"""
    th, mt = A.resolve_thinking(model, text_ctx)
    if mo:
        mt = max(1500 if mt > 600 else 600, min(mo, 32768))
    body = {**th, "max_tokens": mt}
    last = None
    for attempt in range(5):
        t0 = time.time()
        status, data = A.glm_chat(model, messages, timeout=150, extra_body=body, key=kv)
        dt = time.time() - t0
        usage = (data.get("usage") or {}) if isinstance(data, dict) else {}
        _tls.calls = getattr(_tls, "calls", [])
        _tls.calls.append({"status": status, "latency": dt,
                          "prompt_tokens": usage.get("prompt_tokens") or 0,
                          "completion_tokens": usage.get("completion_tokens") or 0,
                          "chars": len(str(messages[0]["content"])) + len(str(messages[1]["content"]))})
        if status in (429, 500, 502, 503, 504) and attempt < 4:
            try:
                wait = A._cooldown_sec(str((data.get("error") or {}).get("message") or ""))
            except Exception:
                wait = 5
            last = (status, data, dt, usage)
            time.sleep(min(40, max(5, wait)))
            continue
        return status, data, dt, usage
    return last


# eval_judge.py 沿用这个名字
model_call = _model_call


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--mode", choices=("v1", "v2", "json"), default="v1")
    ap.add_argument("--set", dest="set_name", default="set_short.jsonl")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--model", default="glm-5.4")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--dump", default="")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--no-reply-v2", action="store_true",
                    help="只用 v2 提示词、不开回复策略 v2（给还没有回复策略的旧提交导提示词用）")
    a = ap.parse_args()
    # 走被测代码的真实处理流程（--no-reply-v2 时只开 v2 提示词）
    pipe = a.mode in ("v2", "json") and not a.no_reply_v2

    A, D, C = load_agent(a.src)
    # 评测进程不得产生任何生产副作用
    A.log_usage = lambda *x, **k: None

    def _collect(line):
        # 只留判定/重试类事件，随结果行存下来（看某条场景为什么多调了几次）
        if str(line).startswith(("AGENT_JSON", "AGENT_MUST_REPLY")):
            _tls.logs = getattr(_tls, "logs", [])
            _tls.logs.append(str(line)[:80])

    A.out = _collect
    A.PLUGIN_HOST.on_message_all = lambda msg: None
    A.PLUGIN_HOST.daemon = _NullDaemon()
    A.PLUGIN_HOST.log = lambda line: None
    try:
        A.PLUGIN_HOST.load_all()
    except Exception as e:
        print("插件加载失败（提示词里将缺少插件注入）：%r" % e)
    if hasattr(A, "PROMPT_V2_FORCE"):
        A.PROMPT_V2_FORCE = a.mode in ("v2", "json")
    if hasattr(A, "REPLY_V2_FORCE"):
        A.REPLY_V2_FORCE = pipe
        A.REPLY_JSON_FORCE = (a.mode == "json")
    if pipe and not hasattr(A, "_generate_reply_v2"):
        raise SystemExit("--mode %s 需要包含回复策略 v2 的代码（改造分支）" % a.mode)

    scens = EC.load_jsonl(EC.EVAL_DIR / a.set_name)
    if a.limit:
        scens = scens[:a.limit]

    # 构造提示词（顺序执行，快）
    built = []
    for s in scens:
        if hasattr(A, "_TOPIC_LAST"):
            A._TOPIC_LAST.clear()
        msg = build_msg(s)
        if A.lean_ctx_on():
            cands = A._chain_candidates()
            ctx_k = cands[0][3] if cands else 32
        else:
            ctx_k = 99
        messages, persona = A.build_messages(_NullDaemon(s), msg, ctx_k=ctx_k)
        built.append((s, msg, messages, persona))

    sys_len = [len(b[2][0]["content"]) for b in built]
    usr_len = [len(b[2][1]["content"]) for b in built]
    print("%s：%d 个场景 | system 中位 %d 字（p90 %d）| user 中位 %d 字 | 合计中位 %d 字" % (
        a.mode, len(built), statistics.median(sys_len), sorted(sys_len)[int(.9 * len(sys_len))],
        statistics.median(usr_len), statistics.median([x + y for x, y in zip(sys_len, usr_len)])))
    for _t in ("at", "reply_to_bot", "chat_random"):
        _idx = [i for i, b in enumerate(built) if b[0]["trigger"] == _t]
        if _idx:
            print("  触发类型 %-13s %2d 个：system 中位 %d 字  user 中位 %d 字" % (
                _t, len(_idx), statistics.median(sys_len[i] for i in _idx),
                statistics.median(usr_len[i] for i in _idx)))
    if a.dump:
        dp = EC.dump_jsonl(EC.EVAL_DIR / a.dump, [
            {"id": b[0]["id"], "system": b[2][0]["content"], "user": b[2][1]["content"]}
            for b in built])
        print("提示词已导出：%s" % dp)
    if a.dry:
        return

    cands = A._chain_candidates()
    pick = next((c for c in cands if a.model in c[2]), cands[0] if cands else None)
    if not pick:
        raise SystemExit("模型链里没有可用条目")
    kid, kv, model, _ctx, mo = pick[0], pick[1], pick[2], pick[3], pick[4]
    print("模型：%s（key=%s，max_out=%s）| 并发 %d" % (model, kid, mo or 0, a.workers))

    def _chain(messages, text_ctx="", budget_sec=200):
        """替换被测代码的 call_glm_with_fallback：固定单模型、逐次记账、失败重试。
        记账表在 work() 里按场景清零——判定、主调用、重试全算进同一条场景。"""
        status, data, dt, usage = _model_call(A, model, kv, mo, messages, text_ctx)
        if status != 200 or not isinstance(data, dict):
            raise RuntimeError("eval http %s: %s" % (
                status, json.dumps(data, ensure_ascii=False)[:120] if data else "empty"))
        ch = (data.get("choices") or [{}])[0] or {}
        return model, str((ch.get("message") or {}).get("content") or "")

    if pipe:
        A.call_glm_with_fallback = _chain

    results = [None] * len(built)
    todo = list(range(len(built)))
    out_path = EC.RUNS_DIR / (a.tag + ".jsonl")
    if a.resume and out_path.exists():
        prev = {r["id"]: r for r in EC.load_jsonl(out_path)}
        for i, b in enumerate(built):
            old = prev.get(b[0]["id"])
            if old and not old.get("error") and old.get("status") == 200:
                results[i] = old
        todo = [i for i in range(len(built)) if results[i] is None]
        print("续跑：已有 %d 条有效结果，补跑 %d 条" % (len(built) - len(todo), len(todo)))
        if not todo:
            return

    def work(idx):
        s, msg, messages, persona = built[idx]
        _tls.calls = []
        _tls.logs = []
        _wall0 = time.time()
        row = {"id": s["id"], "trigger": s["trigger"], "variant": a.tag, "mode": a.mode,
               "system_chars": len(messages[0]["content"]),
               "user_chars": len(messages[1]["content"]), "persona": persona}
        try:
            if pipe:
                fake = _NullDaemon(s)
                if s["trigger"] == "chat_random" and A.chat_anti_interrupt_enabled():
                    g = A._reply_gate_v2(fake, msg, messages, persona)
                    row["gate"] = g
                    if g == "N":
                        row.update({"status": 200, "kind": "skip", "final": None,
                                    "calls": 1, "latency": 0.0,
                                    "prompt_tokens": 0, "completion_tokens": 0})
                        _c = getattr(_tls, "calls", [])
                        row["latency"] = round(sum(c["latency"] for c in _c), 2)
                        row["prompt_tokens"] = sum(c["prompt_tokens"] for c in _c)
                        row["completion_tokens"] = sum(c["completion_tokens"] for c in _c)
                        if getattr(_tls, "logs", None):
                            row["logs"] = list(_tls.logs)
                        results[idx] = row
                        return row
                _m, reply = A._generate_reply_v2(fake, msg, [dict(x) for x in messages], gate=g)
                row["legacy"] = reply
                react = bool(A._REACT_MARK_RE.search(reply))
                texts, _st, _memo, _dr, _sg = A.parse_reply(reply)
                if texts == "WAIT":
                    row["kind"], final = "wait", None
                elif texts is None:
                    row["kind"], final = ("react" if react else "skip"), None
                else:
                    final = D.Daemon._strip_control_marks("\n".join(texts))
                    # 只挂表情回应（不发文字）是真实流程里的正常出口，不算「清洗后变空」
                    row["kind"] = ("reply" if final else
                                   "react" if react and not (texts and texts[0].strip())
                                   else "stripped")
                    row["bubbles"] = len(texts)
                row["react"] = react
                row["status"] = 200
            else:
                status, data, dt, usage = _model_call(A, model, kv, mo, messages, s["msg"]["text"])
                row.update({"status": status, "latency": round(dt, 2)})
                if status != 200 or not isinstance(data, dict):
                    row["error"] = (json.dumps(data, ensure_ascii=False)[:200]
                                    if data else "empty")
                    results[idx] = row
                    return row
                ch = (data.get("choices") or [{}])[0] or {}
                msgo = ch.get("message") or {}
                content = str(msgo.get("content") or "")
                row.update({"finish": ch.get("finish_reason"), "raw": content,
                            "reasoning_chars": len(str(msgo.get("reasoning_content") or "")),
                            "prompt_tokens": usage.get("prompt_tokens"),
                            "completion_tokens": usage.get("completion_tokens")})
                texts, _st, _memo, _dr, _sg = A.parse_reply(content)
                if texts == "WAIT":
                    row["kind"], final = "wait", None
                elif texts is None:
                    row["kind"], final = "skip", None
                else:
                    final = D.Daemon._strip_control_marks("\n".join(texts))
                    row["kind"] = "reply" if final else "stripped"
        except Exception as e:
            row["error"] = repr(e)[:200]
        calls = getattr(_tls, "calls", [])
        row["calls"] = len(calls)
        # 限流重试：这些次调用没拿到结果，等待时间不在 latency 里，单记 wall
        row["rate_limited"] = sum(1 for c in calls if c.get("status") != 200)
        row["wall"] = round(time.time() - _wall0, 1)
        logs = getattr(_tls, "logs", [])
        if logs:
            row["logs"] = logs
        row["latency"] = round(sum(c["latency"] for c in calls), 2)
        row["prompt_tokens"] = sum(c["prompt_tokens"] for c in calls)
        row["completion_tokens"] = sum(c["completion_tokens"] for c in calls)
        row["final"] = locals().get("final")
        results[idx] = row
        return row

    t_start = time.time()
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(work, i) for i in todo]
        for f in concurrent.futures.as_completed(futs):
            try:
                f.result()
            except Exception as e:
                print("worker error: %r" % e)
            done += 1
            if done % 10 == 0 or done == len(todo):
                print("  进度 %d/%d（%.0fs）" % (done, len(todo), time.time() - t_start))
                EC.dump_jsonl(out_path, [r for r in results if r])
    rows = [r for r in results if r]
    out = EC.dump_jsonl(out_path, rows)
    kinds = {}
    for r in rows:
        k = r.get("kind") or ("error" if r.get("error") else "?")
        kinds[k] = kinds.get(k, 0) + 1
    ok = [r for r in rows if not r.get("error")]
    lat = sorted(r["latency"] for r in ok)
    pt = [r.get("prompt_tokens") or 0 for r in rows]
    ct = [r.get("completion_tokens") or 0 for r in rows]
    print("结果：%s" % out)
    print("类型分布：%s" % kinds)
    if pipe:
        g = {}
        for r in rows:
            if "gate" in r:
                g[r["gate"]] = g.get(r["gate"], 0) + 1
        print("统一判定（闲聊触发）：%s" % g)
        print("每条场景的模型调用次数：合计 %d，最多 %d" % (
            sum(r.get("calls") or 0 for r in rows), max(r.get("calls") or 0 for r in rows)))
    if lat:
        print("延迟（单条场景全部调用之和）：中位 %.1fs  p90 %.1fs" % (
            statistics.median(lat), lat[min(len(lat) - 1, int(.9 * len(lat)))]))
    print("token：输入合计 %d（均 %d/条）  输出合计 %d" % (
        sum(pt), sum(pt) // max(1, len(pt)), sum(ct)))


if __name__ == "__main__":
    main()
