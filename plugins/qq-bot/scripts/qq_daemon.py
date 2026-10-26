#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qq_daemon.py — QQ 机器人守护进程（OneBot v11 连接器，零文件写入设计）

职责：
  1. 作为 WebSocket 客户端连接 OneBot v11 实现端（NapCat / LLOneBot 的正向 WS 端口）
  2. 接收 QQ 消息事件 -> 触发过滤 -> 写入内存收件箱(inbox)与内存历史
  3. 提供本地 HTTP 控制接口（仅绑定 127.0.0.1）：收件箱读取/确认、发消息、历史查询
  4. 结构化事件同步打印到 stdout（AGENT_EVENT: {json}），由启动方重定向落盘
  5. 纯 Python 标准库；WS 协议(RFC 6455)内置实现；本进程自身不写任何文件

设计说明：
  - 持久化职责分离：记忆/人设/风格卡/表情包仓库/消息日志落盘均由 ZCode agent
    （技能层）与 qqctl.py 承担；daemon 只维护进程生命周期内的内存状态，
    因此本文件不包含任何文件写入操作，天然无路径穿越与数据破坏面。
  - 发送图片仅接受位于约定数据目录（~/.zcode-qq-bot 下 cache/images、stickers）
    内的图片文件（resolve_media_file 白名单校验，防本机进程借接口外传任意文件）。
  - WS 握手校验 101 状态与 Upgrade 头；Sec-WebSocket-Accept 值校验（RFC 6455 要求
    基于 SHA-1 的握手校验算法）在本地回环/受信内网目标场景下无实际防护价值，故跳过，
    wss 场景由 TLS 提供信道保护。

用法：python qq_daemon.py [--ws-url ws://127.0.0.1:3001] [--port 39763] [--token xxx]
"""

import argparse
import base64
import http.server
import ipaddress
import json
import os
import socket
import ssl
import sys
import threading
import time
import urllib.parse
import uuid
from collections import defaultdict, deque

from qq_common import econ_active

DATA_DIR = os.path.join(os.path.expanduser("~"), ".zcode-qq-bot")
DATA_ROOT = os.path.realpath(DATA_DIR)
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
MEDIA_SUBDIRS = ("cache/images", "stickers")  # 允许发送的图片所在数据目录子集
GROUP_WHITELIST_PATH = os.path.realpath(os.path.join(DATA_DIR, "group_whitelist.json"))
PRIVATE_CONFIG_PATH = os.path.realpath(os.path.join(DATA_DIR, "private_config.json"))

DEFAULT_PRIVATE_CONFIG = {
    "enabled": False,        # 是否回应私信（默认关闭）
    "allow_all": True,       # 开启后：True=回复所有人；False=仅白名单
    "qq_whitelist": [],      # 私信白名单 QQ 号
}


def load_private_config_file():
    """读取私聊配置（模块级常量路径，由管理 UI 维护，daemon 只读）。

    文件不存在或格式异常时返回默认值（enabled=False，安全侧默认）。
    """
    try:
        with open(PRIVATE_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return dict(DEFAULT_PRIVATE_CONFIG)
    if not isinstance(data, dict):
        return dict(DEFAULT_PRIVATE_CONFIG)
    cfg = dict(DEFAULT_PRIVATE_CONFIG)
    cfg["enabled"] = bool(data.get("enabled", False))
    cfg["allow_all"] = bool(data.get("allow_all", True))
    wl = data.get("qq_whitelist")
    if isinstance(wl, list):
        cfg["qq_whitelist"] = [str(q).strip() for q in wl
                               if str(q).strip().isdigit()][:100]
    return cfg
REPLY_CONFIG_PATH = os.path.realpath(os.path.join(DATA_DIR, "reply_config.json"))
GROUP_MODES_PATH = os.path.realpath(os.path.join(DATA_DIR, "group_reply_modes.json"))

_print_lock = threading.Lock()

DEFAULT_REPLY_CONFIG = {
    "reply_mode": "standard",       # standard | keyword | chat
    "keywords": [],                  # keyword 模式的关键词
    "chat_probability": 0.15,        # chat 模式：每条群消息的回复概率 0~1
    "chat_interesting": True,        # chat 模式：agent 按人设做感兴趣判定
    "chat_min_interval_sec": 45,     # chat 模式：两次主动搭话最小间隔
}


def load_reply_config_file():
    """读取回复模式配置（模块级常量路径，由管理 UI 维护，daemon 只读）。"""
    try:
        with open(REPLY_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return dict(DEFAULT_REPLY_CONFIG)
    if not isinstance(data, dict):
        return dict(DEFAULT_REPLY_CONFIG)
    cfg = dict(DEFAULT_REPLY_CONFIG)
    mode = data.get("reply_mode")
    if mode in ("standard", "keyword", "chat"):
        cfg["reply_mode"] = mode
    kw = data.get("keywords")
    if isinstance(kw, list):
        cfg["keywords"] = [str(k).strip() for k in kw if str(k).strip()][:50]
    try:
        p = float(data.get("chat_probability", cfg["chat_probability"]))
        cfg["chat_probability"] = min(1.0, max(0.0, p))
    except (TypeError, ValueError):
        pass
    cfg["chat_interesting"] = bool(data.get("chat_interesting", True))
    try:
        sec = int(data.get("chat_min_interval_sec", cfg["chat_min_interval_sec"]))
        cfg["chat_min_interval_sec"] = max(0, min(sec, 3600))
    except (TypeError, ValueError):
        pass
    # 高峰降耗字段原样透传（判定在 qq_common.econ_active；后两项为旧字段兼容）
    for k in ("econ_enabled", "econ_manual", "econ_start", "econ_end",
              "econ_days", "econ_workdays_only", "econ_mode_override",
              "wait_gate_enabled", "burst_merge_enabled"):
        if k in data:
            cfg[k] = data[k]
    return cfg


def load_group_whitelist_file():
    """读取群白名单文件（模块级常量路径，由管理 UI 维护，daemon 只读）。

    返回 {"allow_all": bool, "groups": [...]}；文件不存在或格式不符返回 None。
    """
    try:
        with open(GROUP_WHITELIST_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
        return None
    return {
        "enabled": bool(data.get("enabled", True)),
        "allow_all": bool(data.get("allow_all", False)),
        "groups": [str(g).strip() for g in data["groups"] if str(g).strip()],
    }


def file_sig(path):
    """配置文件变更签名（mtime_ns + size；文件不存在返回 None）。
    供各配置热加载做秒级失效：管理 UI 一保存，下一条消息即用新配置。"""
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def out(line):
    """线程安全地打印结构化行（stdout 由启动方重定向到日志文件）。
    行首带 [HH:MM:SS] 时间戳，供运行日志页展示。"""
    with _print_lock:
        print("[%s] %s" % (time.strftime("%H:%M:%S"), line), flush=True)


def now_ts():
    return int(time.time())


def rand_int(lo, hi):
    """非加密场景随机数（打字延迟等）；敏感值一律使用 uuid4。"""
    if hi <= lo:
        return lo
    return lo + uuid.uuid4().int % (hi - lo + 1)


def resolve_media_file(file_path):
    """校验待发送的本地图片：扩展名白名单 + 必须位于数据目录约定子目录内。"""
    p = os.path.realpath(os.path.abspath(os.path.expanduser(str(file_path))))
    ext = os.path.splitext(p)[1].lower()
    if ext not in IMAGE_EXTS:
        raise ValueError("仅支持图片文件（%s），当前: %s" % (", ".join(sorted(IMAGE_EXTS)), ext))
    allowed = [os.path.realpath(os.path.join(DATA_DIR, sub)) for sub in MEDIA_SUBDIRS]
    if not any(p.startswith(r.rstrip("\\/") + os.sep) for r in allowed):
        raise ValueError("图片必须位于 %s 之下，拒绝访问: %s" % (", ".join(MEDIA_SUBDIRS), p))
    if not os.path.isfile(p):
        raise FileNotFoundError("文件不存在: %s" % p)
    return p


# ---------------------------------------------------------------------------
# WebSocket 客户端（RFC 6455 子集：text 帧 + ping/pong/close，客户端帧必掩码）
# ---------------------------------------------------------------------------

class WSClient:
    def __init__(self, url, token="", on_event=None, on_status=None, idle_timeout=120):
        self.url = url
        self.token = token
        self.on_event = on_event or (lambda ev: None)
        self.on_status = on_status or (lambda st, detail: None)
        self.idle_timeout = idle_timeout
        self.sock = None
        self.connected = False
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._initial_buf = b""

    def connect(self):
        p = urllib.parse.urlsplit(self.url)
        if p.scheme not in ("ws", "wss"):
            raise ValueError("ws_url 必须是 ws:// 或 wss://，当前: %s" % self.url)
        host = p.hostname or "127.0.0.1"
        port = p.port or (443 if p.scheme == "wss" else 80)
        raw = socket.create_connection((host, port), timeout=10)
        if p.scheme == "wss":
            ctx = ssl.create_default_context()
            raw = ctx.wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        path = p.path or "/"
        if p.query:
            path += "?" + p.query
        headers = [
            "GET %s HTTP/1.1" % path,
            "Host: %s:%d" % (host, port),
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Key: %s" % key,
            "Sec-WebSocket-Version: 13",
        ]
        if self.token:
            headers.append("Authorization: Bearer %s" % self.token)
        raw.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = raw.recv(4096)
            if not chunk:
                raise ConnectionError("握手阶段连接断开")
            resp += chunk
            if len(resp) > 65536:
                raise ConnectionError("握手响应过大")
        head, _, leftover = resp.partition(b"\r\n\r\n")
        head_text = head.decode("latin-1")
        status_line = head_text.split("\r\n", 1)[0]
        if " 101 " not in status_line:
            raise ConnectionError("WS 握手被拒绝: %s" % status_line)
        if "upgrade: websocket" not in head_text.lower():
            raise ConnectionError("WS 握手响应缺少 Upgrade 头")
        # RFC 6455 另要求校验 Sec-WebSocket-Accept（SHA-1 派生值）；本实现面向
        # 本机回环/受信内网服务端，仅校验 101+Upgrade（见文件头设计说明）。
        self.sock = raw
        self.connected = True
        self._initial_buf = leftover
        self.on_status("connected", self.url)

    def _read_exact(self, n):
        buf = self._initial_buf
        self._initial_buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("连接断开")
            buf += chunk
        return buf

    def _recv_frame(self):
        b1, b2 = self._read_exact(2)
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            length = int.from_bytes(self._read_exact(2), "big")
        elif length == 127:
            length = int.from_bytes(self._read_exact(8), "big")
        if length > 16 * 1024 * 1024:
            raise ConnectionError("帧过大(%d)" % length)
        mask = self._read_exact(4) if masked else None
        payload = self._read_exact(length) if length else b""
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    def _send_frame(self, opcode, payload):
        if not self.connected or not self.sock:
            raise ConnectionError("未连接")
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header = bytes([0x80 | opcode, 0x80 | length])
        elif length < 65536:
            header = bytes([0x80 | opcode, 0x80 | 126]) + length.to_bytes(2, "big")
        else:
            header = bytes([0x80 | opcode, 0x80 | 127]) + length.to_bytes(8, "big")
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._send_lock:
            self.sock.sendall(header + mask + masked)

    def send_text(self, text):
        self._send_frame(0x1, text.encode("utf-8"))

    def send_pong(self, payload=b""):
        self._send_frame(0xA, payload)

    def send_ping(self):
        self._send_frame(0x9, b"hb")

    def recv_message(self):
        """聚合分片返回一条消息（text），自动应答 ping，空闲时主动探活。"""
        parts = []
        last_active = time.time()
        while True:
            if self._stop.is_set():
                raise ConnectionError("stopping")
            self.sock.settimeout(max(1.0, self.idle_timeout))
            try:
                fin, opcode, payload = self._recv_frame()
                last_active = time.time()
            except socket.timeout:
                if time.time() - last_active >= self.idle_timeout:
                    self.send_ping()
                    last_active = time.time()
                continue
            if opcode == 0x8:
                raise ConnectionError("对端关闭连接")
            if opcode == 0x9:
                self.send_pong(payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x0, 0x1, 0x2):
                if opcode != 0x0:
                    parts = [payload]
                else:
                    parts.append(payload)
                if fin:
                    return b"".join(parts).decode("utf-8", "replace")

    def close(self):
        self._stop.set()
        self.connected = False
        try:
            if self.sock:
                self._send_frame(0x8, (1000).to_bytes(2, "big"))
        except OSError:
            pass
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass

    def reset(self):
        """硬复位：只断开当前 socket（不置 _stop），让 run_forever 走重连。
        用于「半死连接」：TCP 在但对端既不响应动作也不推事件。"""
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass

    def run_forever(self):
        """阻塞式：连接 -> 收事件 -> 断线自动重连（指数退避，上限 60s）。"""
        backoff = 1
        while not self._stop.is_set():
            try:
                self.connect()
                backoff = 1
                while not self._stop.is_set():
                    text = self.recv_message()
                    try:
                        ev = json.loads(text)
                    except ValueError:
                        out("AGENT_LOG {\"msg\": \"non_json_frame\"}")
                        continue
                    self.on_event(ev)
            except (ConnectionError, OSError, ValueError, ssl.SSLError) as e:
                self.connected = False
                self.on_status("disconnected", str(e))
                if self._stop.is_set():
                    break
                out("AGENT_LOG {\"msg\": \"ws_disconnected\", \"detail\": %s}" % json.dumps(str(e), ensure_ascii=False))
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, 60)
            finally:
                self.connected = False
                try:
                    if self.sock:
                        self.sock.close()
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# OneBot 消息段解析
# ---------------------------------------------------------------------------

import re  # noqa: E402

CQ_CODE_RE = re.compile(r"\[CQ:(?P<type>[a-zA-Z0-9_.-]+)(?P<params>[^\]]*)\]")


def parse_cq_params(s):
    out = {}
    for kv in re.findall(r",([A-Za-z0-9_.-]+)=((?:[^,\\]|\\.)*)", "," + s):
        out[kv[0]] = kv[1].replace("\\,", ",").replace("\\\\", "\\").replace("&amp;", "&")
    return out


def segments_of(message):
    """OneBot message 字段兼容 array / CQ 码字符串两种形态。"""
    if isinstance(message, list):
        return message
    if isinstance(message, str):
        segs = []
        pos = 0
        for m in CQ_CODE_RE.finditer(message):
            if m.start() > pos:
                segs.append({"type": "text", "data": {"text": message[pos:m.start()]}})
            segs.append({"type": m.group("type"), "data": parse_cq_params(m.group("params"))})
            pos = m.end()
        if pos < len(message):
            segs.append({"type": "text", "data": {"text": message[pos:]}})
        return segs
    return []


def extract_message_info(segments, self_id):
    info = {
        "text": "",
        "at_me": False,
        "at_users": [],
        "image_urls": [],
        "has_reply": False,
        "reply_id": None,
        "face_count": 0,
        "record": False,
    }
    texts = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        t = seg.get("type")
        d = seg.get("data") or {}
        if t == "text":
            texts.append(str(d.get("text", "")))
        elif t == "at":
            qq = str(d.get("qq", ""))
            info["at_users"].append(qq)
            if self_id is not None and qq == str(self_id):
                info["at_me"] = True
        elif t == "image":
            info["image_urls"].append(str(d.get("url") or ""))
        elif t == "forward":
            # 合并转发：NapCat 提供 content 列表或 id；提取每条文本
            fc = d.get("content")
            if isinstance(fc, list):
                for fs in fc:
                    if isinstance(fs, dict):
                        ft = str(fs.get("text") or "").strip()
                        if ft:
                            texts.append("[转发] " + ft[:60])
            fid = str(d.get("id") or "")
            if fid:
                info.setdefault("forward_ids", []).append(fid)
        elif t == "reply":
            info["has_reply"] = True
            # 被引用消息可能带图片（NapCat 的 reply 段 content 里含 CQ 码）
            rc = str(d.get("content") or "")
            import re as _re_img
            _img_re = _re_img.compile(r"url=([^,\]]+)")
            _cq_re = _re_img.compile(r"CQ:[a-zA-Z_]+\[[^\]]*\]")
            for mu in _img_re.findall(rc):
                info["image_urls"].append(mu.strip())
            info["reply_text"] = _cq_re.sub("", rc).strip()[:80]
            info["reply_id"] = str(d.get("id", ""))
        elif t == "face":
            info["face_count"] += 1
        elif t == "record":
            info["record"] = True
    info["text"] = "".join(texts).strip()
    return info


# ---------------------------------------------------------------------------
# 守护进程主体（纯内存状态）
# ---------------------------------------------------------------------------

class Daemon:
    def __init__(self, ws_url, token="", cfg=None, ctl_port=39763, ctl_token=""):
        self.cfg = cfg or {}
        self.ctl_port = int(ctl_port)
        self.ctl_token = str(ctl_token)
        self.start_time = now_ts()
        self.lock = threading.RLock()
        self.seq = 0
        self.inbox = deque(maxlen=500)
        self.acked_max = -1
        self.msg_id_index = {}
        self.rate_window = {}
        self.send_queue = deque()
        self._drain_lock = threading.Lock()
        self.mem_history = defaultdict(lambda: deque(maxlen=800))
        self.login = {"user_id": None, "nickname": ""}
        self._wl_cache = None
        self._wl_sig = object()  # 首次必加载（与 None/任何 sig 不等）
        self._rc_cfg = dict(DEFAULT_REPLY_CONFIG)
        self._rc_sig = object()
        self._gm_cache = {}
        self._gm_sig = object()
        self._pc_cache = dict(DEFAULT_PRIVATE_CONFIG)
        self._pc_sig = object()
        self._last_chat_ts = 0.0
        self.pending_echoes = {}
        self.stop_flag = threading.Event()
        self.stats = {"received": 0, "triggered": 0, "sent": 0, "dropped_rate": 0}
        self.quiet_until = 0.0  # 安静模式截止时间戳（0=关闭；@/回复不受影响）
        self.ws_last_ok = time.time()  # 动作通道最近一次成功响应
        self.ws = WSClient(
            ws_url, token,
            on_event=self._dispatch_ws_event,
            on_status=self._on_ws_status,
            idle_timeout=int(self.cfg.get("ws_idle_timeout", 120)),
        )

    # ---------- WS 分发 ----------

    def _dispatch_ws_event(self, ev):
        if isinstance(ev, dict) and ev.get("echo") and "retcode" in ev \
                and str(ev["echo"]) in self.pending_echoes:
            got, holder = self.pending_echoes[str(ev["echo"])]
            holder["resp"] = ev
            got.set()
            return
        if not isinstance(ev, dict):
            return
        post_type = ev.get("post_type")
        if post_type == "notice":
            self._handle_notice_event(ev)
            return
        if post_type in ("meta_event", "request"):
            return
        try:
            if post_type == "message_sent":
                self._record_outgoing_echo(ev)
            elif post_type == "message":
                self._handle_message_event(ev)
        except Exception as e:
            out("AGENT_LOG {\"msg\": \"event_error\", \"detail\": %s}" % json.dumps(repr(e)))

    def _on_ws_status(self, st, detail):
        out("AGENT_LOG {\"msg\": \"ws_%s\", \"detail\": %s}" % (st, json.dumps(str(detail), ensure_ascii=False)))
        if st == "connected":
            threading.Thread(target=self._query_login, daemon=True).start()

    def _query_login(self):
        for _ in range(5):
            if self.stop_flag.is_set():
                return
            try:
                data = self._call_action("get_login_info", {}, timeout=8)
                with self.lock:
                    self.login = {"user_id": data.get("user_id"), "nickname": data.get("nickname", "")}
                out("AGENT_LOG {\"msg\": \"login\", \"nickname\": %s, \"user_id\": %s}"
                    % (json.dumps(self.login["nickname"], ensure_ascii=False), self.login["user_id"]))
                return
            except Exception:
                time.sleep(2)

    # ---------- 收消息 ----------

    def _handle_notice_event(self, ev):
        """notice 事件：戳一戳（poke）→ 入 inbox 触发 agent 回应。"""
        if ev.get("notice_type") != "notify" or ev.get("sub_type") != "poke":
            return
        target_id = str(ev.get("target_id") or "")
        self_id = str(ev.get("self_id") or "")
        if target_id != self_id:
            return  # 只响应戳自己的
        user_id = ev.get("user_id") or ev.get("user_id")
        if user_id is None:
            return
        chat_type = "group" if ev.get("group_id") else "private"
        chat_id = ev.get("group_id") or user_id
        chat_name = str(ev.get("group_name") or chat_id)
        sender_name = str((ev.get("sender") or {}).get("nickname") or user_id)
        triggered, reason = True, "poke"
        with self.lock:
            self.seq += 1
            item = {
                "id": self.seq, "ts": ev.get("time") or now_ts(),
                "chat_type": chat_type, "chat_id": chat_id, "chat_name": chat_name,
                "message_id": "poke_%d" % self.seq,
                "sender_id": user_id, "sender_name": sender_name,
                "triggered": triggered, "trigger_reason": reason,
                "text": "[戳了一戳]", "at_me": True, "at_users": [],
                "image_urls": [], "has_reply": False, "reply_to": None,
                "reply_text": "", "face_count": 0, "has_record": False,
            }
            self.inbox.append(item)
            self.stats["received"] += 1
            if triggered:
                self.stats["triggered"] += 1
        out("AGENT_EVENT " + json.dumps(item, ensure_ascii=False))

    def _handle_message_event(self, ev):
        chat_type = "group" if ev.get("message_type") == "group" else "private"
        sender = ev.get("sender") or {}
        user_id = sender.get("user_id") or ev.get("user_id")
        group_id = ev.get("group_id")
        self_id = ev.get("self_id") or self.login.get("user_id")
        if user_id is None or str(user_id) == str(self_id):
            return
        if str(user_id) in [str(x) for x in self.cfg.get("user_blacklist", [])]:
            return

        if chat_type == "group":
            if not self._group_allowed(group_id):
                return
            chat_id = group_id
            chat_name = str(ev.get("group_name") or group_id)
            sender_name = str(sender.get("card") or sender.get("nickname") or user_id)
        else:
            pc = self._private_config()
            if not pc["enabled"]:
                return
            if not pc["allow_all"] and str(user_id) not in pc["qq_whitelist"]:
                return
            chat_id = user_id
            chat_name = str(sender.get("nickname") or user_id)
            sender_name = chat_name

        info = extract_message_info(segments_of(ev.get("message")), self_id)
        triggered, reason = self._decide_trigger(chat_type, info, chat_id)

        with self.lock:
            self.seq += 1
            # 回复目标上下文：被回复的消息若还在内存历史里，取摘要给 agent 判定用
            reply_to = None
            if info["has_reply"] and info["reply_id"]:
                for rec in reversed(self.mem_history.get(
                        "%s_%s" % (chat_type, chat_id), [])):
                    if str(rec.get("message_id")) == str(info["reply_id"]):
                        reply_to = {"sender_name": rec.get("sender_name"),
                                    "text": str(rec.get("text") or "")[:60]}
                        break
            item = {
                "id": self.seq,
                "ts": ev.get("time") or now_ts(),
                "chat_type": chat_type,
                "chat_id": chat_id,
                "chat_name": chat_name,
                "message_id": str(ev.get("message_id") or ""),
                "sender_id": user_id,
                "sender_name": sender_name,
                "triggered": triggered,
                "trigger_reason": reason,
                "text": info["text"],
                "at_me": info["at_me"],
                "at_users": info["at_users"][:5],
                "image_urls": info["image_urls"][:6],
                "has_reply": info["has_reply"],
                "reply_to": reply_to,
                "reply_text": info.get("reply_text", ""),
                "face_count": info["face_count"],
                "has_record": info["record"],
            }
            self.inbox.append(item)
            self.stats["received"] += 1
            if triggered:
                self.stats["triggered"] += 1
            self._mem_push(item, ev.get("message_id"), is_bot=False)
        out("AGENT_EVENT " + json.dumps(item, ensure_ascii=False))

    def _mem_push(self, item, message_id, is_bot, text=None, n_images=0):
        rec = {
            "ts": item.get("ts") or now_ts(),
            "message_id": str(message_id or ""),
            "sender_id": item.get("sender_id"),
            "sender_name": ("BOT" if is_bot else item.get("sender_name")),
            "is_bot": is_bot,
            "text": text if text is not None else item.get("text", ""),
            "n_images": n_images,
        }
        self.mem_history["%s_%s" % (item.get("chat_type"), item.get("chat_id"))].append(rec)
        if message_id:
            self.msg_id_index[str(message_id)] = is_bot
            if len(self.msg_id_index) > 4000:
                for k in list(self.msg_id_index.keys())[:2000]:
                    del self.msg_id_index[k]

    def _group_whitelist(self):
        """群白名单热加载（mtime 秒级失效）：来源 group_whitelist.json（管理 UI 维护），
        返回 {"allow_all": bool, "groups": [...]}；文件不存在或格式不符时
        回退启动参数 --groups。本进程只读不写。"""
        sig = file_sig(GROUP_WHITELIST_PATH)
        if sig != self._wl_sig:
            entry = load_group_whitelist_file()
            if entry is None:
                entry = {"allow_all": False,
                         "groups": [str(x) for x in self.cfg.get("group_whitelist", [])]}
            with self.lock:
                self._wl_cache = entry
                self._wl_sig = sig
        return self._wl_cache

    def _group_allowed(self, group_id):
        entry = self._group_whitelist()
        if not entry.get("enabled", True):
            return False
        bl = [str(x) for x in self.cfg.get("group_blacklist", [])]
        g = str(group_id)
        if g in bl:
            return False
        if entry.get("allow_all"):
            return True
        wl = entry.get("groups", [])
        if wl and g not in wl:
            return False
        return True

    def _private_config(self):
        """私聊配置热加载（mtime 秒级失效，来源 private_config.json，daemon 只读）。"""
        sig = file_sig(PRIVATE_CONFIG_PATH)
        if sig != self._pc_sig:
            with self.lock:
                self._pc_cache = load_private_config_file()
                self._pc_sig = sig
        return self._pc_cache

    def _reply_config(self):
        """回复模式配置热加载（mtime 秒级失效，来源 reply_config.json，daemon 只读）。"""
        sig = file_sig(REPLY_CONFIG_PATH)
        if sig != self._rc_sig:
            with self.lock:
                self._rc_cfg = load_reply_config_file()
                self._rc_sig = sig
        return self._rc_cfg

    def _group_modes(self):
        """分群回复模式覆盖热加载（mtime 秒级失效，来源 group_reply_modes.json）。"""
        sig = file_sig(GROUP_MODES_PATH)
        if sig != self._gm_sig:
            modes = {}
            try:
                with open(GROUP_MODES_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for gid, ov in data.items():
                        if isinstance(ov, dict):
                            modes[str(gid)] = ov
            except (OSError, ValueError):
                modes = {}
            with self.lock:
                self._gm_cache = modes
                self._gm_sig = sig
        return self._gm_cache

    def _reply_config_for(self, chat_type, chat_id):
        """按会话取生效配置：全局默认 + 群级覆盖合并（字段级，未覆盖项回落全局）。"""
        cfg = dict(self._reply_config())
        if chat_type != "group":
            return cfg
        ov = self._group_modes().get(str(chat_id))
        if not isinstance(ov, dict):
            return cfg
        if ov.get("paused"):
            cfg["paused"] = True
        mode = ov.get("reply_mode")
        if mode in ("standard", "keyword", "chat"):
            cfg["reply_mode"] = mode
        if isinstance(ov.get("keywords"), list):
            cfg["keywords"] = [str(k).strip() for k in ov["keywords"]
                               if str(k).strip()][:50]
        try:
            cfg["chat_probability"] = min(1.0, max(
                0.0, float(ov.get("chat_probability", cfg["chat_probability"]))))
        except (TypeError, ValueError):
            pass
        try:
            cfg["chat_min_interval_sec"] = max(0, min(
                3600, int(ov.get("chat_min_interval_sec",
                                 cfg["chat_min_interval_sec"]))))
        except (TypeError, ValueError):
            pass
        return cfg

    def _is_bursting(self, chat_type, chat_id, sender_id, gap=3.0):
        """碎片合并判定：此人 gap 秒内在同一会话连发了 2+ 条消息（还在打字中）。
        开启 burst_merge 后，主动触发（关键词/闲聊）在连发期间暂缓处理。"""
        if not self._reply_config().get("burst_merge_enabled", True):
            return False
        key = "%s_%s" % (chat_type, chat_id)
        with self.lock:
            dq = self.mem_history.get(key, deque())
            ts_list = [r.get("ts", 0) for r in list(dq)[-6:]
                       if str(r.get("sender_id")) == str(sender_id)
                       and not r.get("is_bot")]
        if len(ts_list) < 2:
            return False
        now = time.time()
        # 最后一条距今 < gap 且倒数第二条距今 < gap*3 → 连发中
        return (now - ts_list[-1] < gap) and (now - ts_list[-2] < gap * 3)

    def _decide_trigger(self, chat_type, info, chat_id=""):
        if chat_type == "private":
            return True, "private"
        cfg = self._reply_config_for(chat_type, chat_id)
        # 分群「暂停回应」：该群一切回应停止（@/回复也不触发），群仍留在白名单
        if cfg.get("paused"):
            return False, "group_paused"
        if info["at_me"] and self.cfg.get("mention_trigger", True):
            return True, "at"
        if info["has_reply"] and self.cfg.get("reply_to_bot_trigger", True):
            rid = info["reply_id"]
            if rid and self.msg_id_index.get(str(rid)):
                return True, "reply_to_bot"
        # 安静模式：@/回复机器人不受影响，其余群触发（关键词/闲聊）全部静默
        with self.lock:
            quiet_until = self.quiet_until
        if quiet_until and time.time() < quiet_until:
            return False, "quiet"
        # 结构性防插嘴：消息明确指向他人时，主动插话一律不触发
        # （@我/回复我的消息在上面已放行；走到这里说明没@我也没回复我）
        if info["at_users"] and not info["at_me"]:
            return False, "at_other"
        if info["has_reply"]:
            return False, "reply_other"
        # 碎片合并：此人还在连发中，主动插话暂缓（@/回复不受影响；
        # 等此人静默 gap 秒后下一条自然走正常触发，上下文也更完整）
        if self._is_bursting(chat_type, chat_id, info.get("sender_id", "")):
            return False, "burst_wait"
        mode = cfg["reply_mode"]
        # 高峰降耗生效时：关键词/闲聊主动插话全部压制为标准模式（@/回复不受影响）
        if mode in ("keyword", "chat") and econ_active(self._reply_config()):
            mode = "standard"
        text = info["text"]
        if mode in ("keyword", "chat") and text:
            for w in cfg["keywords"]:
                if w and w in text:
                    return True, "keyword"
        if mode == "chat" and chat_type == "group":
            now = time.time()
            with self.lock:
                gap = now - self._last_chat_ts
            if gap >= cfg["chat_min_interval_sec"]:
                if rand_int(0, 9999) < int(cfg["chat_probability"] * 10000):
                    with self.lock:
                        self._last_chat_ts = now
                    return True, "chat_random"
        return False, ""

    # ---------- 历史查询（内存） ----------

    def history(self, chat_type, chat_id, limit=30):
        with self.lock:
            recs = list(self.mem_history.get("%s_%s" % (chat_type, chat_id), []))
        recs.reverse()
        return recs[:max(1, min(int(limit), 200))]

    def recent_chats(self, within=6 * 3600):
        """最近有消息往来的会话（主动发消息选目标用）；within 秒内。"""
        now = time.time()
        best = {}
        with self.lock:
            for key, dq in self.mem_history.items():
                if not dq:
                    continue
                last = dq[-1]
                if now - last.get("ts", 0) > within:
                    continue
                ct, _, cid = key.partition("_")
                prev = best.get((ct, cid))
                if prev is None or last.get("ts", 0) > prev:
                    best[(ct, cid)] = last.get("ts", 0)
        return [{"chat_type": ct, "chat_id": cid, "ts": ts}
                for (ct, cid), ts in sorted(best.items(), key=lambda x: -x[1])]

    # ---------- OneBot 动作调用 ----------

    def _call_action(self, action, params, timeout=None):
        echo = "e" + uuid.uuid4().hex[:12]
        got = threading.Event()
        holder = {}
        self.pending_echoes[echo] = (got, holder)
        try:
            self.ws.send_text(json.dumps(
                {"action": action, "params": params, "echo": echo}, ensure_ascii=False))
            if not got.wait(timeout or 15):
                raise TimeoutError("OneBot 动作超时: %s" % action)
            resp = holder.get("resp") or {}
            if resp.get("retcode") not in (0, 1):
                raise RuntimeError("OneBot 动作失败 %s: %s" % (action, resp.get("wording") or resp.get("msg")))
            return resp.get("data") or {}
        finally:
            self.pending_echoes.pop(echo, None)

    # ---------- 发送 ----------

    def _rate_ok(self, chat_key):
        now = time.time()
        with self.lock:
            ts_list = self.rate_window.setdefault(chat_key, [])
            ts_list[:] = [t for t in ts_list if now - t < 60]
            if len(ts_list) >= int(self.cfg.get("rate_limit_per_min", 20)):
                self.stats["dropped_rate"] += 1
                return False
            ts_list.append(now)
            return True

    def send_texts(self, chat_type, chat_id, texts, reply_message_id=None):
        if not isinstance(texts, list) or not texts:
            raise ValueError("texts 必须是非空数组")
        chat_key = "%s_%s" % (chat_type, chat_id)
        if not self._rate_ok(chat_key):
            raise RuntimeError("触发每分钟限流，请稍后再发")
        lo, hi = [int(x) for x in self.cfg.get("typing_delay_ms", [600, 2000])]
        reply_to = str(reply_message_id).strip() if reply_message_id else ""
        n = 0
        for i, t in enumerate(texts[:8]):
            self.send_queue.append({
                "chat_type": chat_type, "chat_id": chat_id, "kind": "text",
                "text": str(t),
                # 引用仅挂在第一条消息上（多条连发只引用一次，符合真人习惯）
                "reply_to": reply_to if (i == 0 and reply_to) else "",
                "delay": 0 if i == 0 else rand_int(lo, max(hi, lo)) / 1000.0,
            })
            n += 1
        threading.Thread(target=self._drain_send_queue, daemon=True).start()
        return {"queued": n}

    def send_image(self, chat_type, chat_id, file_path, caption=None):
        p = resolve_media_file(file_path)
        chat_key = "%s_%s" % (chat_type, chat_id)
        if not self._rate_ok(chat_key):
            raise RuntimeError("触发每分钟限流，请稍后再发")
        self.send_queue.append({"chat_type": chat_type, "chat_id": chat_id, "kind": "image",
                                "file": p, "text": caption or "", "delay": 0})
        threading.Thread(target=self._drain_send_queue, daemon=True).start()
        return {"queued": 1}

    def _drain_send_queue(self):
        with self._drain_lock:
            while self.send_queue:
                job = self.send_queue.popleft()
                if job.get("delay"):
                    time.sleep(job["delay"])
                try:
                    self._send_one(job)
                except Exception as e:
                    out("AGENT_LOG {\"msg\": \"send_failed\", \"detail\": %s}" % json.dumps(repr(e)))

    def _send_one(self, job):
        chat_type, chat_id = job["chat_type"], job["chat_id"]
        segs = []
        if job.get("reply_to"):
            segs.append({"type": "reply", "data": {"id": job["reply_to"]}})
        if job["kind"] == "image":
            file_uri = "file:///" + job["file"].replace("\\", "/")
            segs.append({"type": "image", "data": {"file": file_uri}})
            if job.get("text"):
                segs.append({"type": "text", "data": {"text": job["text"]}})
        else:
            segs.append({"type": "text", "data": {"text": job["text"]}})
        if chat_type == "group":
            action, params = "send_group_msg", {"group_id": chat_id, "message": segs}
        else:
            action, params = "send_private_msg", {"user_id": chat_id, "message": segs}
        data = self._call_action(action, params)
        mid = str((data or {}).get("message_id", ""))
        with self.lock:
            self._mem_push(
                {"chat_type": chat_type, "chat_id": chat_id,
                 "sender_id": self.login.get("user_id"), "ts": now_ts()},
                mid, is_bot=True, text=job.get("text") or "[图片]",
                n_images=1 if job["kind"] == "image" else 0)
            self.stats["sent"] += 1

    def _record_outgoing_echo(self, ev):
        chat_type = "group" if ev.get("message_type") == "group" else "private"
        chat_id = ev.get("group_id") or ev.get("user_id")
        if chat_id is None:
            return
        info = extract_message_info(segments_of(ev.get("message")), None)
        with self.lock:
            self._mem_push(
                {"chat_type": chat_type, "chat_id": chat_id,
                 "sender_id": self.login.get("user_id"), "ts": ev.get("time") or now_ts()},
                ev.get("message_id"), is_bot=True,
                text=info["text"], n_images=len(info["image_urls"]))

    # ---------- 收件箱 ----------

    def inbox_get(self, only_triggered=False, limit=50):
        with self.lock:
            items = [it for it in self.inbox if it["id"] > self.acked_max]
            if only_triggered:
                items = [it for it in items if it["triggered"]]
            return items[:max(1, min(int(limit), 200))]

    def inbox_ack(self, ids):
        with self.lock:
            valid = [int(i) for i in ids if str(i).isdigit()]
            m = max(valid, default=None)
            if m is not None and m > self.acked_max:
                self.acked_max = m
            while self.inbox and self.inbox[0]["id"] <= self.acked_max:
                self.inbox.popleft()
        return {"acked_max": self.acked_max}

    def status(self):
        last_ok = self.ws_last_ok
        return {
            "running": True,
            "pid": os.getpid(),
            "uptime": now_ts() - self.start_time,
            "ws_url": self.ws.url,
            "ws_connected": self.ws.connected,
            "ws_last_ok": last_ok,
            # 动作通道探活：超过 3 个探测周期无成功响应 = 半死
            "ws_alive": bool(last_ok) and (now_ts() - last_ok) < 180,
            "login": self.login,
            "inbox_pending": len(self.inbox),
            "stats": self.stats,
            "data_dir": DATA_DIR,
        }

    def _probe_loop(self):
        """动作通道探活（45s 一次）：get_login_info 探测 NapCat 是否仍响应。
        连续 2 次失败且连接显示在线 = 半死连接 → 硬复位 socket 触发重连。"""
        fails = 0
        while not self.stop_flag.is_set():
            time.sleep(45)
            if self.stop_flag.is_set() or not self.ws.connected:
                fails = 0
                continue
            try:
                self._call_action("get_login_info", {}, timeout=8)
                self.ws_last_ok = time.time()
                fails = 0
            except Exception as e:
                fails += 1
                out("AGENT_LOG {\"msg\": \"ws_probe_fail\", \"detail\": %s, \"fails\": %d}"
                    % (json.dumps(repr(e)[:120]), fails))
                if fails >= 2:
                    out("AGENT_LOG {\"msg\": \"ws_probe_reset\", \"detail\": \"半死连接，硬复位重连\"}")
                    try:
                        self.ws.reset()
                    except Exception:
                        pass
                    fails = 0


# ---------------------------------------------------------------------------
# HTTP 控制服务（仅绑定 127.0.0.1）
# ---------------------------------------------------------------------------

class CtlHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    @property
    def daemon(self):
        return self.server.daemon

    def _reply(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_token(self):
        token = self.daemon.ctl_token
        return not token or self.headers.get("X-QQBot-Token", "") == token

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            raise ValueError("请求体不是合法 JSON")

    def do_GET(self):
        if not self._check_token():
            return self._reply(401, {"ok": False, "error": "token 校验失败"})
        u = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path == "/status":
                st = self.daemon.status()
                with self.daemon.lock:
                    st["quiet_until"] = self.daemon.quiet_until
                return self._reply(200, {"ok": True, **st})
            if u.path == "/inbox":
                items = self.daemon.inbox_get(
                    only_triggered=q.get("triggered") in ("1", "true"),
                    limit=int(q.get("limit", 50)))
                return self._reply(200, {"ok": True, "items": items})
            if u.path == "/recent-chats":
                return self._reply(200, {"ok": True,
                                         "chats": self.daemon.recent_chats()})
            if u.path == "/history":
                recs = self.daemon.history(
                    q.get("chat_type", "group"), q.get("chat_id", ""),
                    limit=int(q.get("limit", 30)))
                return self._reply(200, {"ok": True, "items": recs})
            return self._reply(404, {"ok": False, "error": "unknown path %s" % u.path})
        except Exception as e:
            return self._reply(400, {"ok": False, "error": repr(e)})

    def do_POST(self):
        if not self._check_token():
            return self._reply(401, {"ok": False, "error": "token 校验失败"})
        u = urllib.parse.urlsplit(self.path)
        try:
            body = self._body()
            d = self.daemon
            if u.path == "/ack":
                return self._reply(200, {"ok": True, **d.inbox_ack(body.get("ids", []))})
            if u.path == "/quiet":
                # 安静模式：{minutes: 0} 取消；正数=持续 N 分钟（@/回复仍响应）
                try:
                    minutes = max(0.0, min(float(body.get("minutes", 0)), 720.0))
                except (TypeError, ValueError):
                    return self._reply(400, {"ok": False, "error": "minutes 非法"})
                with d.lock:
                    if minutes > 0:
                        d.quiet_until = time.time() + minutes * 60
                    else:
                        d.quiet_until = 0.0
                    until = d.quiet_until
                out("AGENT_LOG {\"msg\": \"quiet\", \"until\": %s}" % until)
                return self._reply(200, {"ok": True, "quiet_until": until})
            if u.path == "/send":
                res = d.send_texts(body["chat_type"], body["chat_id"], body["texts"],
                                   reply_message_id=body.get("reply_message_id"))
                return self._reply(200, {"ok": True, **res})
            if u.path == "/send-image":
                res = d.send_image(body["chat_type"], body["chat_id"],
                                   body["file"], body.get("caption"))
                return self._reply(200, {"ok": True, **res})
            if u.path == "/test/inject":
                d._dispatch_ws_event(body["event"])
                return self._reply(200, {"ok": True, "injected": True})
            if u.path == "/shutdown":
                self._reply(200, {"ok": True, "bye": True})
                threading.Thread(target=self._delayed_stop, daemon=True).start()
                return
            return self._reply(404, {"ok": False, "error": "unknown path %s" % u.path})
        except Exception as e:
            return self._reply(400, {"ok": False, "error": repr(e)})

    def _delayed_stop(self):
        time.sleep(0.3)
        self.server.daemon.stop_flag.set()
        self.server.daemon.ws.close()
        threading.Thread(target=lambda: (time.sleep(0.8), os._exit(0)), daemon=True).start()


class CtlServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main():
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="qq-bot OneBot v11 daemon")
    ap.add_argument("--ws-url", default="ws://127.0.0.1:3001")
    ap.add_argument("--ws-token", default="")
    ap.add_argument("--port", type=int, default=39763)
    ap.add_argument("--ctl-token", default="")
    ap.add_argument("--wake-words", default="", help="逗号分隔的唤醒词")
    ap.add_argument("--groups", default="", help="群号白名单，逗号分隔；空=全部")
    ap.add_argument("--no-private", action="store_true")
    args = ap.parse_args()

    cfg = {
        "wake_words": [w.strip() for w in args.wake_words.split(",") if w.strip()],
        "group_whitelist": [g.strip() for g in args.groups.split(",") if g.strip()],
        "private_always": not args.no_private,
    }

    d = Daemon(args.ws_url, args.ws_token, cfg, ctl_port=args.port, ctl_token=args.ctl_token)
    ctl = CtlServer(("127.0.0.1", args.port), CtlHandler)
    ctl.daemon = d
    out(json.dumps({"daemon_ready": True, "pid": os.getpid(), "ctl_port": args.port,
                    "ws_url": args.ws_url, "data_dir": DATA_DIR}, ensure_ascii=False))
    threading.Thread(target=d.ws.run_forever, daemon=True).start()
    threading.Thread(target=d._probe_loop, daemon=True).start()
    try:
        while not d.stop_flag.is_set():
            ctl.handle_request()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        ctl.server_close()
        d.ws.close()
        out("AGENT_LOG {\"msg\": \"daemon_exit\"}")


if __name__ == "__main__":
    main()
