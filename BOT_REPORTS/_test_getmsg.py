# -*- coding: utf-8 -*-
"""get_msg 诊断：看引用+图消息的完整段结构（image 段的本地 file 字段）。"""
import json
import os
import sys
import threading
import uuid

sys.path.insert(0, os.path.join("plugins", "qq-bot", "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import qq_daemon as D  # noqa: E402

runtime = json.load(open(os.path.expanduser(r"~/.zcode-qq-bot/runtime.json"),
                         encoding="utf-8"))
tf = os.path.expanduser(r"~/.zcode-qq-bot/" + runtime["ws_token_file"])
tok = open(tf, encoding="utf-8").read().strip() if os.path.isfile(tf) else ""
ws = D.WSClient(runtime["ws_url"], token=tok)
ws.connect()


def call(action, params, timeout=15):
    echo = "t" + uuid.uuid4().hex[:10]
    got = threading.Event()
    holder = {}

    def pump():
        try:
            while True:
                fin, op, payload = ws._recv_frame()
                if op == 8:
                    return
                if op == 1:
                    try:
                        msg = json.loads(payload.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    if msg.get("echo") == echo:
                        holder["resp"] = msg
                        got.set()
                        return
        except Exception as e:
            holder["err"] = repr(e)
            got.set()

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    ws.send_text(json.dumps({"action": action, "params": params, "echo": echo}))
    got.wait(timeout)
    t.join(0.2)
    return holder.get("resp") or {"error": holder.get("err", "timeout")}


for mid in ("563135710", "271079230"):
    r = call("get_msg", {"message_id": mid})
    d = r.get("data") or {}
    print("=== mid=%s retcode=%s" % (mid, r.get("retcode")))
    segs = d.get("message") or []
    if isinstance(segs, str):
        print("  CQ 形态:", segs[:400])
        continue
    for s in segs:
        if isinstance(s, dict):
            data = s.get("data") or {}
            keep = {k: str(v)[:110] for k, v in data.items()}
            print("  [%s] %s" % (s.get("type"), json.dumps(keep, ensure_ascii=False)[:420]))
