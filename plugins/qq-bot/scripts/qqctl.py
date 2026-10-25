#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qqctl.py — QQ 机器人守护进程控制客户端（纯 HTTP 客户端，无文件写入）

用法：
  python qqctl.py status [--port 39763] [--token XXX]
  python qqctl.py inbox [--triggered] [--limit 50]
  python qqctl.py ack 1,2,3
  python qqctl.py history --type group --id 123456 [--limit 30]
  python qqctl.py send --type group --id 123456 --text "你好[[SPLIT]]第二条"
  python qqctl.py send-image --type group --id 123456 --file <图片路径> [--caption 说明]
  python qqctl.py stop

说明：
  - 消息中的图片 URL 下载由 agent 侧完成（技能层指引，保存到数据目录 cache/images/）
  - token 来源：--token 参数 > 环境变量 QQBOT_TOKEN > 数据目录 token 文件
  - 输出均为 JSON（供 agent 解析）
  - 控制请求仅发往本机回环：目标为字面量回环地址 + 解析后 IP 逐地址复核 is_loopback
"""

import argparse
import http.client
import ipaddress
import json
import os
import socket
import sys

DATA_DIR = os.path.join(os.path.expanduser("~"), ".zcode-qq-bot")
DEFAULT_PORT = 39763
LOCALHOST = "127.0.0.1"


def load_token():
    p = os.path.join(DATA_DIR, "token")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def assert_loopback_resolved(port):
    """控制端点固定为本机回环：解析后逐地址复核 is_loopback。"""
    infos = socket.getaddrinfo(LOCALHOST, int(port), socket.AF_UNSPEC, socket.SOCK_STREAM)
    for info in infos:
        ip = info[4][0]
        if not ipaddress.ip_address(ip).is_loopback:
            raise ValueError("控制端点解析到非回环地址 %s，拒绝请求" % ip)


def ctl_request(path, method="GET", body=None, port=DEFAULT_PORT, token="", timeout=10):
    """向本机 daemon 控制接口发请求：目标为字面量回环地址，不访问任何外部 URL。"""
    if not path.startswith("/") or ".." in path:
        raise ValueError("非法请求路径")
    assert_loopback_resolved(port)
    headers = {"Accept": "application/json"}
    if token:
        headers["X-QQBot-Token"] = token
    payload = None
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection(LOCALHOST, int(port), timeout=timeout)
    try:
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        return json.loads(resp.read().decode("utf-8"))
    finally:
        conn.close()


def main():
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="qq-bot daemon control")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--port", type=int, default=DEFAULT_PORT)
        sp.add_argument("--token", default="")

    sp = sub.add_parser("status"); common(sp)
    sp = sub.add_parser("inbox"); common(sp)
    sp.add_argument("--triggered", action="store_true")
    sp.add_argument("--limit", type=int, default=50)
    sp = sub.add_parser("ack"); common(sp)
    sp.add_argument("ids", help="逗号分隔的消息ID")
    sp = sub.add_parser("history"); common(sp)
    sp.add_argument("--type", dest="chat_type", default="group", choices=["group", "private"])
    sp.add_argument("--id", dest="chat_id", required=True)
    sp.add_argument("--limit", type=int, default=30)
    sp = sub.add_parser("send"); common(sp)
    sp.add_argument("--type", dest="chat_type", default="group", choices=["group", "private"])
    sp.add_argument("--id", dest="chat_id", required=True)
    sp.add_argument("--text", required=True, help="文本，多条用[[SPLIT]]分隔")
    sp = sub.add_parser("send-image"); common(sp)
    sp.add_argument("--type", dest="chat_type", default="group", choices=["group", "private"])
    sp.add_argument("--id", dest="chat_id", required=True)
    sp.add_argument("--file", required=True)
    sp.add_argument("--caption", default="")
    sp = sub.add_parser("stop"); common(sp)

    args = ap.parse_args()
    token = args.token or os.environ.get("QQBOT_TOKEN", "") or load_token()
    try:
        if args.cmd == "status":
            res = ctl_request("/status", port=args.port, token=token)
        elif args.cmd == "inbox":
            q = "/inbox?limit=%d" % args.limit
            if args.triggered:
                q += "&triggered=1"
            res = ctl_request(q, port=args.port, token=token)
        elif args.cmd == "ack":
            ids = [i.strip() for i in args.ids.split(",") if i.strip()]
            res = ctl_request("/ack", "POST", {"ids": ids}, args.port, token)
        elif args.cmd == "history":
            q = "/history?chat_type=%s&chat_id=%s&limit=%d" % (args.chat_type, args.chat_id, args.limit)
            res = ctl_request(q, port=args.port, token=token)
        elif args.cmd == "send":
            texts = [t for t in args.text.split("[[SPLIT]]")]
            res = ctl_request("/send", "POST",
                              {"chat_type": args.chat_type, "chat_id": args.chat_id,
                               "texts": [t.strip() for t in texts if t.strip()]},
                              args.port, token)
        elif args.cmd == "send-image":
            res = ctl_request("/send-image", "POST",
                              {"chat_type": args.chat_type, "chat_id": args.chat_id,
                               "file": args.file, "caption": args.caption},
                              args.port, token)
        elif args.cmd == "stop":
            res = ctl_request("/shutdown", "POST", {}, args.port, token)
        else:
            res = {"ok": False, "error": "unknown cmd"}
        print(json.dumps(res, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"ok": False, "error": repr(e)}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
