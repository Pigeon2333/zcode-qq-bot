# -*- coding: utf-8 -*-
"""从 daemon 的会话历史里采集长历史场景（评测「历史扩窗」效果用）。

用法：
  python eval_snapshot.py capture            # 把各会话最近 200 条消息存成快照
  python eval_snapshot.py carve [--n-at 12] [--n-random 28] [--hist 70]
                                           # 从最新快照切出场景，写 set_long.jsonl

快照与题库都放在 ~/.zcode-qq-bot/eval/ 下（含群聊原文，不进仓库）。
daemon 每个会话在内存里保留最近 800 条，组件重启后清零——所以要在机器人
跑了一段时间之后采。
"""
import argparse
import http.client
import json
import os
import pathlib
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eval_common as EC  # noqa: E402

DAEMON_PORT = 39763


def _ctl_get(path):
    tok = (pathlib.Path.home() / ".zcode-qq-bot" / "token").read_text(
        encoding="utf-8").strip()
    conn = http.client.HTTPConnection("127.0.0.1", DAEMON_PORT, timeout=10)
    try:
        conn.request("GET", path, headers={"X-QQBot-Token": tok})
        return json.loads(conn.getresponse().read().decode("utf-8"))
    finally:
        conn.close()


def _chats():
    wl = json.loads((pathlib.Path.home() / ".zcode-qq-bot" / "group_whitelist.json").read_text(
        encoding="utf-8"))
    out = [("group", str(g)) for g in (wl.get("groups") or [])]
    cfg = json.loads((pathlib.Path.home() / ".zcode-qq-bot" / "app_config.json").read_text(
        encoding="utf-8"))
    if cfg.get("owner_qq"):
        out.append(("private", str(cfg["owner_qq"])))
    return out


def capture():
    snap = {"ts": int(time.time()), "chats": {}}
    for ctype, cid in _chats():
        try:
            d = _ctl_get("/history?chat_type=%s&chat_id=%s&limit=200" % (ctype, cid))
            items = d.get("items") or []
        except Exception as e:
            print("  %s/%s 拉取失败：%r" % (ctype, cid, e))
            items = []
        keep = [{k: it.get(k) for k in ("ts", "sender_id", "sender_name", "text", "is_bot")}
                for it in items]
        snap["chats"]["%s_%s" % (ctype, cid)] = keep
        print("  %s/%s：%d 条" % (ctype, cid, len(keep)))
    p = EC.EVAL_DIR / "snapshots" / ("snap_%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    print("快照已保存：%s" % p)


def _text_ok(t):
    t = (t or "").strip()
    return bool(t) and not EC.PLACEHOLDER_RE.match(t) and len(t) <= 220 \
        and not t.startswith("[分享]") and not t.startswith("[卡片")


def carve(a):
    snaps = sorted((EC.EVAL_DIR / "snapshots").glob("snap_*.json"))
    if not snaps:
        raise SystemExit("没有快照，先运行 capture")
    snap = json.loads(snaps[-1].read_text(encoding="utf-8"))
    scen = {"at": [], "chat_random": []}
    for key, items in snap["chats"].items():
        ctype, cid = key.split("_", 1)
        oldest_first = list(reversed(items))          # /history 返回新→旧
        n_at = n_r = 0
        last_p = -99
        for p in range(a.hist, len(oldest_first)):
            cur = oldest_first[p]
            if cur.get("is_bot") or not _text_ok(cur.get("text")):
                continue
            if p - last_p < 5:
                continue
            t = cur["text"]
            asks = ("？" in t) or ("?" in t) or "吗" in t or "怎么" in t
            if asks and n_at < a.n_at:
                trig, n_at = "at", n_at + 1
            elif n_r < a.n_random:
                trig, n_r = "chat_random", n_r + 1
            else:
                continue
            last_p = p
            s = {
                "source": "snapshot", "chat_type": ctype, "chat_id": cid, "chat_name": "",
                "trigger": trig,
                "msg": {"text": t, "sender_id": str(cur.get("sender_id") or ""),
                        "sender_name": str(cur.get("sender_name") or ""),
                        "at_me": trig == "at", "has_reply": False, "reply_text": ""},
                "history": oldest_first[max(0, p - a.hist):p],
                "expect": {"speak": "yes" if trig == "at" else "maybe"},
                "synthetic_at": trig == "at",   # 真实消息前人为加一个「@机器人」
            }
            s["history"] = [{k: h.get(k) for k in ("ts", "sender_id", "sender_name", "text", "is_bot")}
                            for h in s["history"]]
            scen[trig].append(s)
    chosen = scen["at"][:a.n_at] + scen["chat_random"][:a.n_random]
    # 交错排列，避免同类场景扎堆
    for i, s in enumerate(chosen, 1):
        s["id"] = "L%03d" % i
    out = EC.dump_jsonl(EC.EVAL_DIR / "set_long.jsonl", chosen)
    hl = sorted(len(s["history"]) for s in chosen)
    print("题库：%s（%d 个场景：@ %d，闲聊 %d）" % (
        out, len(chosen), len(scen["at"][:a.n_at]), len(scen["chat_random"][:a.n_random])))
    if hl:
        print("每个场景的历史行数：最少 %d，中位 %d，最多 %d" % (hl[0], hl[len(hl) // 2], hl[-1]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("capture", "carve"))
    ap.add_argument("--n-at", type=int, default=12)
    ap.add_argument("--n-random", type=int, default=28)
    ap.add_argument("--hist", type=int, default=40, help="场景至少要带这么多行之前的历史")
    a = ap.parse_args()
    capture() if a.cmd == "capture" else carve(a)


if __name__ == "__main__":
    main()
