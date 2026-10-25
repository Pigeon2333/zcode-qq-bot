#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mock_onebot.py — 模拟 OneBot v11 服务端（NapCat 替身，用于本地联调/冒烟测试）

功能：
  1. WS 服务端（默认 ws://127.0.0.1:3300）：完成 RFC 6455 握手并收发 text 帧
     （配套 qq_daemon.py 的客户端不校验 Sec-WebSocket-Accept，故握手响应省略该头）
  2. HTTP 控制 API（默认 127.0.0.1:18000）：
     POST /inject {event}   -> 向已连接的 WS 客户端推送一条事件（模拟 QQ 消息）
     GET  /sent             -> 返回已收到的 OneBot 动作列表（send_group_msg 等）
     POST /reset            -> 清空动作记录
  3. 收到动作 {action, params, echo} 后回执 {status: "ok", retcode: 0, echo, data}

用法：python mock_onebot.py [--ws-port 3300] [--api-port 18000]
"""

import argparse
import base64
import http.server
import json
import os
import socket
import struct
import sys
import threading
import time

WS_MAGIC_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def log(msg):
    print("[mock] %s" % msg, flush=True)


class MockOneBot:
    def __init__(self, ws_port, api_port):
        self.ws_port = int(ws_port)
        self.api_port = int(api_port)
        self.client_sock = None
        self.client_lock = threading.Lock()
        self.sent_actions = []
        self.send_lock = threading.Lock()
        self.stop = threading.Event()

    # ---------- WS 服务端 ----------

    def ws_serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", self.ws_port))
        srv.listen(2)
        log("WS listening on 127.0.0.1:%d" % self.ws_port)
        while not self.stop.is_set():
            try:
                srv.settimeout(1.0)
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_ws_client, args=(conn,), daemon=True).start()
        srv.close()

    def _handle_ws_client(self, conn):
        try:
            req = b""
            while b"\r\n\r\n" not in req:
                chunk = conn.recv(4096)
                if not chunk:
                    conn.close()
                    return
                req += chunk
            head = req.split(b"\r\n\r\n", 1)[0].decode("latin-1")
            if "upgrade: websocket" not in head.lower():
                conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                conn.close()
                return
            key = ""
            for line in head.split("\r\n"):
                if line.lower().startswith("sec-websocket-key:"):
                    key = line.split(":", 1)[1].strip()
            # 配套客户端不校验 Accept，返回占位值即可完成握手
            accept = base64.b64encode(("mock-" + key).encode()).decode()[:24]
            resp = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Accept: %s\r\n\r\n" % accept
            )
            conn.sendall(resp.encode("ascii"))
            with self.client_lock:
                if self.client_sock:
                    try:
                        self.client_sock.close()
                    except OSError:
                        pass
                self.client_sock = conn
            log("WS client connected")
            leftover = req.split(b"\r\n\r\n", 1)[1]
            self._ws_read_loop(conn, leftover)
        except OSError as e:
            log("WS client error: %r" % e)
        finally:
            with self.client_lock:
                if self.client_sock is conn:
                    self.client_sock = None
            try:
                conn.close()
            except OSError:
                pass

    def _ws_read_loop(self, conn, initial=b""):
        buf = initial
        while not self.stop.is_set():
            # 读一帧（客户端帧必掩码）
            buf = self._read_n(conn, buf, 2)
            b1, b2 = buf[0], buf[1]
            opcode = b1 & 0x0F
            masked = bool(b2 & 0x80)
            length = b2 & 0x7F
            if length == 126:
                buf = self._read_n(conn, buf[2:], 2)
                length = struct.unpack(">H", buf[:2])[0]
                rest = buf[2:]
            elif length == 127:
                buf = self._read_n(conn, buf[2:], 8)
                length = struct.unpack(">Q", buf[:8])[0]
                rest = buf[8:]
            else:
                rest = buf[2:]
            if length > 1 << 20:
                raise ConnectionError("frame too large")
            mask = b""
            if masked:
                rest = self._read_n(conn, rest, 4)
                mask = rest[:4]
                rest = rest[4:]
            rest = self._read_n(conn, rest, length)
            payload = rest[:length]
            rest = rest[length:]
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                break
            if opcode == 0x9:  # ping -> pong
                self._ws_send(conn, 0xA, payload)
                buf = rest
                continue
            if opcode in (0x1, 0x2, 0x0):
                text = payload.decode("utf-8", "replace")
                try:
                    msg = json.loads(text)
                except ValueError:
                    buf = rest
                    continue
                self._handle_action(conn, msg)
                buf = rest

    @staticmethod
    def _read_n(conn, buf, n):
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("closed")
            buf += chunk
        return buf

    def _ws_send(self, conn, opcode, payload):
        # 服务端发送不掩码
        length = len(payload)
        if length < 126:
            header = struct.pack(">BB", 0x80 | opcode, length)
        elif length < 65536:
            header = struct.pack(">BBH", 0x80 | opcode, 126, length)
        else:
            header = struct.pack(">BBQ", 0x80 | opcode, 127, length)
        with self.send_lock:
            conn.sendall(header + payload)

    def _handle_action(self, conn, msg):
        action = msg.get("action")
        echo = msg.get("echo")
        with self.client_lock:
            pass
        self.sent_actions.append({"ts": int(time.time()), "action": action, "params": msg.get("params")})
        log("action: %s" % action)
        resp = {"status": "ok", "retcode": 0, "echo": echo, "data": {"message_id": rand_msg_id()}}
        self._ws_send(conn, 0x1, json.dumps(resp, ensure_ascii=False).encode("utf-8"))

    def push_event(self, event):
        with self.client_lock:
            conn = self.client_sock
        if not conn:
            raise RuntimeError("无 WS 客户端连接（daemon 未连接 mock）")
        self._ws_send(conn, 0x1, json.dumps(event, ensure_ascii=False).encode("utf-8"))

    # ---------- HTTP 控制 API ----------

    def api_serve(self):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                pass

            def _reply(self, code, obj):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/sent":
                    with outer.client_lock:
                        actions = list(outer.sent_actions)
                    return self._reply(200, {"ok": True, "actions": actions})
                return self._reply(404, {"ok": False})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
                except ValueError:
                    return self._reply(400, {"ok": False, "error": "bad json"})
                if self.path == "/inject":
                    try:
                        outer.push_event(body.get("event") or body)
                        return self._reply(200, {"ok": True, "injected": True})
                    except Exception as e:
                        return self._reply(400, {"ok": False, "error": repr(e)})
                if self.path == "/reset":
                    with outer.client_lock:
                        outer.sent_actions.clear()
                    return self._reply(200, {"ok": True})
                return self._reply(404, {"ok": False})

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", self.api_port), Handler)
        log("API listening on 127.0.0.1:%d" % self.api_port)
        while not self.stop.is_set():
            srv.timeout = 1.0
            srv.handle_request()
        srv.server_close()


_MSG_SEQ = [1000]


def rand_msg_id():
    _MSG_SEQ[0] += 1
    return -_MSG_SEQ[0]  # 负数模拟 QQNT 消息ID


def make_group_message(group_id, user_id, text, at_me=False, bot_qq=10000):
    segs = []
    if at_me:
        segs.append({"type": "at", "data": {"qq": str(bot_qq)}})
    segs.append({"type": "text", "data": {"text": text}})
    return {
        "post_type": "message",
        "message_type": "group",
        "time": int(time.time()),
        "self_id": bot_qq,
        "message_id": rand_msg_id(),
        "user_id": user_id,
        "group_id": group_id,
        "message": segs,
        "raw_message": text,
        "sender": {"user_id": user_id, "nickname": "测试用户%d" % (user_id % 100), "card": ""},
    }


def main():
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="mock OneBot v11 server")
    ap.add_argument("--ws-port", type=int, default=3300)
    ap.add_argument("--api-port", type=int, default=18000)
    args = ap.parse_args()
    mock = MockOneBot(args.ws_port, args.api_port)
    threading.Thread(target=mock.ws_serve, daemon=True).start()
    mock.api_serve()


if __name__ == "__main__":
    main()
