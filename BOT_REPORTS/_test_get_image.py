# -*- coding: utf-8 -*-
"""实测：NapCat get_image（按 message_id 取本地缓存文件）能否绕过
CDN download_file 的 Bad Request。对照 msg289(下载成功) 与 msg292(下载失败)。"""
import json
import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.join("plugins", "qq-bot", "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import qq_daemon as D  # noqa: E402

# 1) 从 daemon.log 取 289/292 的 message_id
mids = {}
for ln in open(os.path.expanduser(r"~/.zcode-qq-bot/logs/daemon.log"),
               encoding="utf-8", errors="replace"):
    m = re.search(r"AGENT_EVENT (\{.*)", ln)
    if not m:
        continue
    try:
        d = json.loads(m.group(1))
    except ValueError:
        continue
    if d.get("chat_id") == 1061684987 and d.get("ts", 0) > 1794405000 \
            and d.get("id") in (289, 292) and d.get("image_urls"):
        mids[d["id"]] = str(d.get("message_id") or "")
print("mids:", mids)

# 2) 连 NapCat ws 调 get_image
runtime = json.load(open(os.path.expanduser(r"~/.zcode-qq-bot/runtime.json"),
                         encoding="utf-8"))
tok_file = os.path.expanduser(r"~/.zcode-qq-bot/" + runtime["ws_token_file"]
                              if not os.path.isabs(runtime["ws_token_file"])
                              else runtime["ws_token_file"])
tok = open(tok_file, encoding="utf-8").read().strip() if os.path.isfile(tok_file) else ""

ws = D.WSClient(runtime["ws_url"], token=tok)
ws.connect()
print("ws connected")


def call(action, params, timeout=15):
    import uuid
    echo = "t" + uuid.uuid4().hex[:10]
    got = threading.Event()
    holder = {}

    orig_on_event = ws.on_event

    def pump():
        # 复用 WSClient 内部帧读：逐帧分发
        try:
            while True:
                fin, op, payload = ws._recv_frame()
                if op == 8:  # close
                    return
                if op == 1:  # text
                    try:
                        msg = json.loads(payload.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    if msg.get("echo") == echo:
                        holder["resp"] = msg
                        got.set()
                        return
                    holder.setdefault("others", []).append(
                        str(msg)[:150])
        except Exception as e:
            holder["err"] = repr(e)
            got.set()

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    ws.send_text(json.dumps({"action": action, "params": params, "echo": echo}))
    got.wait(timeout)
    t.join(0.2)
    return holder.get("resp") or {"error": holder.get("err", "timeout")}


for mid_id, mid in mids.items():
    r = call("get_image", {"message_id": mid}, timeout=20)
    data = r.get("data") or {}
    print("--- msg%s get_image: raw=%s" % (mid_id, str(r)[:200]))
    if r.get("error"):
        print("   others:", (r.get("others") or [])[-3:])
        continue
    print("   keys:", sorted(data.keys()) if isinstance(data, dict) else data)
    f = str(data.get("file") or "")
    print("   file:", f[:120])
    if f and os.path.isfile(f):
        head = open(f, "rb").read(12)
        print("   size=%d magic=%r" % (os.path.getsize(f), head[:8]))
    else:
        print("   (file 不存在)")
print("done")
