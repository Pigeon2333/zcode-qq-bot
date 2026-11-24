# -*- coding: utf-8 -*-
"""对题库跑一遍某个代码版本，记录每条回复与开销。

用法：
  python eval_run.py --src <scripts目录> --mode v1|v2|json --set set_short.jsonl
                     --tag <结果名> [--limit N] [--workers 3] [--dry]

  --src   要评测的源码目录（主分支或改造分支的 plugins/qq-bot/scripts）
  --mode  v1 = 现有逻辑；v2 = 打开 prompt_v2 开关；
          json = v2 提示词 + 结构化输出要求（测 5.4 的 JSON 稳定性）
  --dry   只构造提示词、不调模型（比较两种提示词的体积）

只读生产配置与人设；不写任何生产文件，不发送任何消息。
结果写到 ~/.zcode-qq-bot/eval/runs/<tag>.jsonl。
"""
import argparse
import concurrent.futures
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eval_common as EC  # noqa: E402

JSON_RULE = (
    "\n\n## 本次输出格式（必须严格遵守，优先级高于前文所有关于输出格式的说明）\n"
    "只输出一个 JSON 对象，不要 Markdown 代码块，不要任何多余文字。字段：\n"
    '{"want": 0到3的整数，你想接这句话的程度（0=不接，1=只给对方消息挂个表情，'
    "2=简短回一句，3=正常回复），\n"
    ' "emoji": 想挂的表情 id（整数）或 null，仅 want 为 1 时使用，\n'
    ' "say": 要发给对方的消息，字符串数组，每个元素是一条消息；不说话时为空数组，\n'
    ' "memory": 要长期记住的事，数组，每项形如 {"qq": "对方QQ号", "text": "一句话"}；'
    "没有就为空数组}\n"
    "say 里只写要发给对方看的话，不要出现任何方括号标记、系统说明或思考过程。")

BOT_QQ = EC.BOT_QQ


class _NullDaemon(object):
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
    trig = s["trigger"]
    msg = {
        "id": "eval", "chat_type": s["chat_type"], "chat_id": s["chat_id"],
        "chat_name": s.get("chat_name") or s["chat_id"],
        "sender_id": m["sender_id"], "sender_name": m["sender_name"],
        "text": m["text"], "trigger_reason": trig, "message_id": "0",
        "image_urls": [], "at_me": bool(m.get("at_me")), "at_users": [],
        "has_reply": bool(m.get("has_reply")), "reply_to": None,
        "reply_text": m.get("reply_text") or "", "face_count": 0,
    }
    return msg


def model_call(A, model, kv, mo, messages, text_ctx, extra=None):
    """与生产完全相同的参数口径（思考档位、输出上限）。返回 (status, data, 耗时, max_tokens)。"""
    th, mt = A.resolve_thinking(model, text_ctx)
    if mo:
        mt = max(1500 if mt > 600 else 600, min(mo, 32768))
    body = {**th, "max_tokens": mt, **(extra or {})}
    last = None
    for attempt in range(4):
        t0 = time.time()
        status, data = A.glm_chat(model, messages, timeout=150, extra_body=body, key=kv)
        dt = time.time() - t0
        if status in (429, 500, 502, 503, 504) and attempt < 3:
            try:
                wait = A._cooldown_sec(str((data.get("error") or {}).get("message") or ""))
            except Exception:
                wait = 5
            last = (status, data, dt, mt)
            time.sleep(min(30, max(4, wait)))
            continue
        return status, data, dt, mt
    return last


def parse_json_reply(text):
    """解析结构化输出。返回 (ok, schema_ok, obj_or_err)。"""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t.split("\n", 1)[1] if "\n" in t else t
    try:
        obj = json.loads(t)
    except ValueError:
        m = __import__("re").search(r"\{.*\}", t, __import__("re").S)
        if not m:
            return False, False, "no json"
        try:
            obj = json.loads(m.group(0))
        except ValueError as e:
            return False, False, str(e)[:80]
    if not isinstance(obj, dict):
        return True, False, "not object"
    ok = True
    want = obj.get("want")
    if not isinstance(want, int) or isinstance(want, bool) or not 0 <= want <= 3:
        ok = False
    say = obj.get("say")
    if not isinstance(say, list) or not all(isinstance(x, str) for x in say):
        ok = False
        say = []
    mem = obj.get("memory")
    if not isinstance(mem, list) or not all(isinstance(x, dict) for x in mem):
        ok = False
        mem = []
    return True, ok, {"want": want, "say": say, "emoji": obj.get("emoji"), "memory": mem}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--mode", choices=("v1", "v2", "json"), default="v1")
    ap.add_argument("--set", dest="set_name", default="set_short.jsonl")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--model", default="glm-5.4")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--dump", default="",
                    help="把构造好的提示词另存到评测目录（用于新旧代码逐字节比对）")
    ap.add_argument("--resume", action="store_true",
                    help="结果文件已存在时只补跑出错/缺失的场景")
    a = ap.parse_args()

    A, D, C = load_agent(a.src)
    # 评测进程不得产生任何生产副作用
    A.log_usage = lambda *x, **k: None
    A.out = lambda line: None
    A.PLUGIN_HOST.on_message_all = lambda msg: None
    A.PLUGIN_HOST.daemon = _NullDaemon()
    A.PLUGIN_HOST.log = lambda line: None
    try:
        A.PLUGIN_HOST.load_all()
    except Exception as e:
        print("插件加载失败（提示词里将缺少插件注入）：%r" % e)
    if hasattr(A, "PROMPT_V2_FORCE"):
        A.PROMPT_V2_FORCE = a.mode in ("v2", "json")

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
        if a.mode == "json":
            messages = [dict(m) for m in messages]
            messages[0]["content"] = messages[0]["content"] + JSON_RULE
        built.append((s, messages, persona))

    sys_len = [len(m[1][0]["content"]) for m in built]
    usr_len = [len(m[1][1]["content"]) for m in built]
    print("%s：%d 个场景 | system 中位 %d 字（p90 %d）| user 中位 %d 字 | 合计中位 %d 字" % (
        a.mode, len(built), statistics.median(sys_len), sorted(sys_len)[int(.9 * len(sys_len))],
        statistics.median(usr_len), statistics.median([x + y for x, y in zip(sys_len, usr_len)])))
    for _t in ("at", "reply_to_bot", "chat_random"):
        _idx = [i for i, (s, _m, _p) in enumerate(built) if s["trigger"] == _t]
        if _idx:
            print("  触发类型 %-13s %2d 个：system 中位 %d 字  user 中位 %d 字" % (
                _t, len(_idx), statistics.median(sys_len[i] for i in _idx),
                statistics.median(usr_len[i] for i in _idx)))
    if a.dump:
        dp = EC.dump_jsonl(EC.EVAL_DIR / a.dump, [
            {"id": s["id"], "system": m[0]["content"], "user": m[1]["content"]}
            for s, m, _p in built])
        print("提示词已导出：%s" % dp)
    if a.dry:
        return

    cands = A._chain_candidates()
    pick = next((c for c in cands if a.model in c[2]), cands[0] if cands else None)
    if not pick:
        raise SystemExit("模型链里没有可用条目")
    kid, kv, model, _ctx, mo = pick[0], pick[1], pick[2], pick[3], pick[4]
    print("模型：%s（key=%s，max_out=%s）| 并发 %d" % (model, kid, mo or 0, a.workers))

    results = [None] * len(built)
    todo = list(range(len(built)))
    out_path = EC.RUNS_DIR / (a.tag + ".jsonl")
    if a.resume and out_path.exists():
        prev = {r["id"]: r for r in EC.load_jsonl(out_path)}
        for i, (s, _m, _p) in enumerate(built):
            old = prev.get(s["id"])
            if old and not old.get("error") and old.get("status") == 200:
                results[i] = old
        todo = [i for i in range(len(built)) if results[i] is None]
        print("续跑：已有 %d 条有效结果，补跑 %d 条" % (len(built) - len(todo), len(todo)))
        if not todo:
            return

    def work(idx):
        s, messages, persona = built[idx]
        extra = None
        if a.mode == "json" and idx % 2 == 1:
            extra = {"response_format": {"type": "json_object"}}
        status, data, dt, mt = model_call(A, model, kv, mo, messages, s["msg"]["text"])
        row = {"id": s["id"], "trigger": s["trigger"], "variant": a.tag, "mode": a.mode,
               "system_chars": len(messages[0]["content"]),
               "user_chars": len(messages[1]["content"]), "persona": persona,
               "status": status, "latency": round(dt, 2), "max_tokens": mt,
               "json_mode": bool(extra)}
        if status != 200 or not isinstance(data, dict):
            row["error"] = json.dumps(data, ensure_ascii=False)[:200] if data else "empty"
            results[idx] = row
            return row
        ch = (data.get("choices") or [{}])[0] or {}
        msgo = ch.get("message") or {}
        content = str(msgo.get("content") or "")
        usage = data.get("usage") or {}
        row.update({"finish": ch.get("finish_reason"), "raw": content,
                    "reasoning_chars": len(str(msgo.get("reasoning_content") or "")),
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens")})
        if a.mode == "json":
            ok, schema_ok, obj = parse_json_reply(content)
            row["json_ok"], row["json_schema_ok"] = ok, schema_ok
            if ok:
                row["want"] = obj["want"]
                row["memory_n"] = len(obj["memory"])
                texts = list(obj["say"])
            else:
                row["json_err"] = str(obj)
                texts = []
            joined = "\n".join(texts) if texts else None
        else:
            try:
                texts, _st, _memo, _dr, _sg = A.parse_reply(content)
            except Exception as e:
                row["parse_error"] = repr(e)[:120]
                texts = None
            if texts == "WAIT":
                row["kind"] = "wait"
                texts = None
            elif texts is None:
                texts = None
            joined = "\n".join(texts) if texts else None
        if joined:
            final = D.Daemon._strip_control_marks(joined)
        else:
            final = None
        if a.mode == "json":
            row["kind"] = "reply" if (joined and row.get("want", 0) >= 1) else (
                "react" if row.get("want") == 1 else "skip")
        else:
            row["kind"] = "reply" if final else ("skip" if not joined else "stripped")
        row["final"] = final
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
        kinds[r.get("kind") or ("error" if r.get("error") else "?")] = \
            kinds.get(r.get("kind") or ("error" if r.get("error") else "?"), 0) + 1
    lat = sorted(r["latency"] for r in rows if not r.get("error"))
    pt = [r.get("prompt_tokens") or 0 for r in rows]
    ct = [r.get("completion_tokens") or 0 for r in rows]
    print("结果：%s" % out)
    print("类型分布：%s" % kinds)
    if lat:
        print("延迟：中位 %.1fs  p90 %.1fs" % (statistics.median(lat), lat[min(len(lat) - 1, int(.9 * len(lat)))]))
    print("token：输入合计 %d（均 %d/条）  输出合计 %d" % (sum(pt), sum(pt) // max(1, len(pt)), sum(ct)))
    if a.mode == "json":
        n = len(rows)
        jo = sum(1 for r in rows if r.get("json_ok"))
        js = sum(1 for r in rows if r.get("json_schema_ok"))
        print("JSON：可解析 %d/%d  符合结构 %d/%d" % (jo, n, js, n))
        for flag in (False, True):
            sub = [r for r in rows if r.get("json_mode") is flag]
            if sub:
                print("  response_format=%s：可解析 %d/%d  符合结构 %d/%d" % (
                    flag, sum(1 for r in sub if r.get("json_ok")), len(sub),
                    sum(1 for r in sub if r.get("json_schema_ok")), len(sub)))


if __name__ == "__main__":
    main()
