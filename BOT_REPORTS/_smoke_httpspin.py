# -*- coding: utf-8 -*-
"""冒烟：HTTP 死连接空转（spin）修复——CtlHandler / UiHandler.handle_one_request。

现象：两个处理类都是 HTTP/1.1（连接可复用），调用方中断连接后 readline
抛连接异常、被 handle_one_request 吞掉，但 close_connection 仍为 False，
handle() 循环立刻再读、立刻再抛——线程在死连接上无限空转。生产旧进程
积 160 个空转线程后对外服务停止响应。

全离线：QQBOT_DATA_DIR 在 import 被测模块前指到 tempfile.mkdtemp()；
两个真实 HTTP 服务器只绑 127.0.0.1 临时端口（端口 0）；不连生产端口
39763/39764/39765，不启动/停止任何生产进程，不读写生产数据目录。
daemon 与 UI 鉴权打桩（照 _smoke_wstoken / _smoke_uifix2 的隔离方式）。
跑完删临时目录。

场景（两个处理类各跑一遍，S6 仅 UI）：
  S1 连上不发字节直接 RST        → 线程 1 秒内退出，调用次数不再增加
  S2 发合法请求、读完响应后 RST   → 同上
  S3 发出请求立刻 RST（未读响应） → 同上
  S4 shutdown(SHUT_WR)+close(FIN) → 线程退出（现状已正确，钉住不回退）
  S5 正常请求：状态码/响应体与预期一致；同一条连接连发两个请求（HTTP/1.1
     复用不变，响应逐字节钉死）
  S6（UI）端点抛普通异常 → 500 + ok:false；随后这条连接被服务器关闭
  S7 全部场景后等 1 秒，线程表无还活着的服务器处理线程
  W  白盒桩：连接类异常不回包且关连接（两类）；普通异常 UI 回 500 且关
     连接、Ctl 不回包但关连接

用法：python BOT_REPORTS/_smoke_httpspin.py
"""
import collections
import http.server
import json
import os
import pathlib
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time

_TMP = tempfile.mkdtemp(prefix="qqbot_httpspin_iso_")
os.environ["QQBOT_DATA_DIR"] = _TMP          # 必须在 import 被测模块前设置
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "plugins" / "qq-bot" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import qq_daemon as D  # noqa: E402  （import 前 QQBOT_DATA_DIR 已指向临时目录）
import qq_admin_ui as U  # noqa: E402

fails = []
n_pass = 0

CTL_TOKEN = "ctltoken-httpspin"
UI_TOKEN = "uitoken-httpspin"


def chk(name, cond, extra=""):
    global n_pass
    print(("PASS " if cond else "FAIL ") + name
          + (" | " + str(extra)[:160] if extra and not cond else ""))
    if cond:
        n_pass += 1
    else:
        fails.append(name)


# ------------------------------------------------------- 调用计数桩（判定空转）
CALLS = collections.Counter()      # ident -> handle_one_request 调用次数
TBYID = {}                         # ident -> 线程对象


def install_counter(cls):
    orig = cls.handle_one_request

    def counted(self):
        t = threading.current_thread()
        CALLS[t.ident] += 1
        TBYID[t.ident] = t
        orig(self)

    cls.handle_one_request = counted


install_counter(D.CtlHandler)
install_counter(U.UiHandler)


# ------------------------------------------------------- 假 daemon（ctl 侧）
class FakeCtlDaemon:
    """CtlHandler 需要的最小替身：ctl_token + /status 用到的三样。"""

    def __init__(self):
        self.ctl_token = CTL_TOKEN
        self.lock = threading.Lock()
        self.quiet_until = 0

    def status(self):
        return {"state": "fake"}


# ------------------------------------------------------- 套接字小工具
def connect(addr):
    return socket.create_connection(addr, timeout=5)


def rst_close(s):
    """SO_LINGER(1,0) + close：发 RST，模拟调用方中断连接。"""
    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("hh", 1, 0))
    s.close()


def read_response(s):
    """按 Content-Length 读完整响应，返回 (status, headers, body)。"""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = int(lines[0].split()[1])
    headers = {}
    for ln in lines[1:]:
        k, _, v = ln.partition(b":")
        headers[k.strip().lower().decode()] = v.strip().decode()
    want = int(headers.get("content-length", "0"))
    while len(rest) < want:
        chunk = s.recv(4096)
        if not chunk:
            break
        rest += chunk
    return status, headers, rest[:want]


def ctl_req(path="/status", token=CTL_TOKEN):
    req = "GET %s HTTP/1.1\r\nHost: 127.0.0.1\r\n" % path
    if token is not None:
        req += "X-QQBot-Token: %s\r\n" % token
    return (req + "\r\n").encode("ascii")


def ui_req(path="/api/group-modes", token=UI_TOKEN):
    req = "GET %s HTTP/1.1\r\nHost: 127.0.0.1\r\n" % path
    if token is not None:
        req += "X-UI-Token: %s\r\n" % token
    return (req + "\r\n").encode("ascii")


# 期望值写死（与改前逐字节一致；Date/Server 头随时间变化，不比）
CTL_STATUS_BODY = '{"ok": true, "state": "fake", "quiet_until": 0}'.encode("utf-8")
CTL_401_BODY = '{"ok": false, "error": "token 校验失败"}'.encode("utf-8")
UI_MODES_BODY = '{"ok": true, "modes": {}}'.encode("utf-8")
UI_401_BODY = '{"ok": false, "error": "unauthorized"}'.encode("utf-8")
JSON_TYPE = "application/json; charset=utf-8"


def expect_normal(label, status, headers, body, want_body):
    chk(label + " 状态码 200", status == 200, status)
    chk(label + " Content-Type 不变", headers.get("content-type") == JSON_TYPE,
        headers.get("content-type"))
    chk(label + " Content-Length 不变",
        int(headers.get("content-length", -1)) == len(want_body),
        headers.get("content-length"))
    chk(label + " 响应体逐字节一致", body == want_body, body[:120])


# ------------------------------------------------------- 空转判定（1 秒窗口）
def spin_case(label, do_fn, max_calls=3):
    """执行 do_fn 后等 1 秒：新出现的处理线程调用次数停止增加（≤max_calls）
    且线程已退出。"""
    before = sum(CALLS.values())
    idents_before = set(CALLS)
    try:
        do_fn()
    except OSError:
        pass  # 客户端自身可能收到 RST，不影响服务端判定
    time.sleep(1.0)
    mid = sum(CALLS.values())
    time.sleep(0.3)
    after = sum(CALLS.values())
    new_threads = [TBYID[i] for i in (set(CALLS) - idents_before)]
    chk(label + " 调用次数停止且≤%d" % max_calls,
        after == mid and after - before <= max_calls,
        "Δ1s=%d Δ0.3s=%d totalΔ=%d" % (mid - before, after - mid, after - before))
    chk(label + " 处理线程已退出",
        bool(new_threads) and all(not t.is_alive() for t in new_threads),
        "%d 个新线程存活情况: %s" % (len(new_threads),
                                  [t.is_alive() for t in new_threads]))


# ------------------------------------------------------- 各场景
def s_no_bytes_rst(label, addr, req_builder):
    def do():
        s = connect(addr)
        time.sleep(0.2)   # 等 accept + 处理线程进入 readline（不发任何字节），
        rst_close(s)      # 否则 Windows 会把未 accept 的 RST 连接直接丢出 backlog
    spin_case(label, do)


def s_read_then_rst(label, addr, req_builder, want_body):
    def do():
        s = connect(addr)
        s.sendall(req_builder())
        st, hd, body = read_response(s)     # 先读完正常响应
        expect_normal(label + " 正常响应", st, hd, body, want_body)
        rst_close(s)                         # 再中断
    spin_case(label, do)


def s_send_then_rst(label, addr, req_builder):
    def do():
        s = connect(addr)
        s.sendall(req_builder())             # 发出请求，不读响应
        rst_close(s)
    spin_case(label, do)


def s_fin(label, addr):
    def do():
        s = connect(addr)
        s.shutdown(socket.SHUT_WR)           # 正常半关（FIN）
        s.close()
    spin_case(label, do, max_calls=1)


def s_keepalive(label, addr, req_builder, want_body):
    s = connect(addr)
    try:
        s.sendall(req_builder())
        st1, hd1, b1 = read_response(s)
        expect_normal(label + " 第一个请求", st1, hd1, b1, want_body)
        s.sendall(req_builder())             # 同一条连接第二个请求
        st2, hd2, b2 = read_response(s)
        expect_normal(label + " 复用第二个请求", st2, hd2, b2, want_body)
    finally:
        s.shutdown(socket.SHUT_WR)
        s.close()
    time.sleep(0.3)


def s_auth(label, addr, req_builder, want_body):
    s = connect(addr)
    try:
        s.sendall(req_builder(token=None))   # 无令牌
        st, hd, body = read_response(s)
        chk(label + " 无令牌状态码 401", st == 401, st)
        chk(label + " 无令牌响应体不变", body == want_body, body[:120])
    finally:
        s.shutdown(socket.SHUT_WR)
        s.close()
    time.sleep(0.3)


def s_ui_500(label, addr):
    orig_ljc = U.load_json_const

    def boom(*a, **k):
        raise RuntimeError("boom")

    U.load_json_const = boom             # /api/group-modes 端点打桩抛普通异常
    try:
        s = connect(addr)
        try:
            s.sendall(ui_req())
            st, hd, body = read_response(s)
            obj = json.loads(body.decode("utf-8"))
            chk(label + " 收到 500", st == 500, st)
            chk(label + " JSON 体 ok 为假", obj.get("ok") is False, obj)
            chk(label + " error 带异常信息",
                "RuntimeError" in str(obj.get("error")), obj.get("error"))
            # 随后这条连接应被服务器关闭：再发一个请求会失败或读到 EOF
            closed = False
            try:
                s.sendall(ui_req())
                again = s.recv(4096)
                closed = (again == b"")   # EOF；读到数据 = 连接没关
                if not closed:
                    extra = "连接未关闭，读到了 %d 字节" % len(again)
                else:
                    extra = ""
            except OSError as e:
                closed = True
                extra = type(e).__name__
            chk(label + " 500 之后连接被服务器关闭", closed, extra)
        finally:
            try:
                s.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            s.close()
        time.sleep(1.0)
    finally:
        U.load_json_const = orig_ljc


# ------------------------------------------------------- 白盒桩（不连网络）
class _RaisingRfile:
    def __init__(self, exc):
        self._exc = exc

    def readline(self, *a):
        raise self._exc


def whitebox():
    saved_calls = dict(CALLS)
    saved_tbyid = dict(TBYID)
    try:
        _whitebox_checks()
    finally:
        # 白盒在主线程直调 handle_one_request，计数桩会把 MainThread 记进来；
        # 恢复快照，避免污染后续场景窗口与 S7 的线程表判定。
        CALLS.clear()
        CALLS.update(saved_calls)
        TBYID.clear()
        TBYID.update(saved_tbyid)


def _whitebox_checks():
    # Ctl：连接类异常——不回包、关连接
    h = D.CtlHandler.__new__(D.CtlHandler)
    h.rfile = _RaisingRfile(ConnectionResetError("reset"))
    replies = []
    h._reply = lambda code, obj: replies.append((code, obj))
    h.handle_one_request()
    chk("W1 Ctl 连接类异常不回包", replies == [], replies)
    chk("W1 Ctl 连接类异常关连接",
        getattr(h, "close_connection", None) is True,
        getattr(h, "close_connection", None))
    # Ctl：普通异常——同样不回包、关连接
    h2 = D.CtlHandler.__new__(D.CtlHandler)
    h2.rfile = _RaisingRfile(ValueError("boom"))
    replies2 = []
    h2._reply = lambda code, obj: replies2.append((code, obj))
    h2.handle_one_request()
    chk("W1 Ctl 普通异常不回包", replies2 == [], replies2)
    chk("W1 Ctl 普通异常关连接",
        getattr(h2, "close_connection", None) is True,
        getattr(h2, "close_connection", None))
    # UI：连接类异常——不回包、关连接
    u = U.UiHandler.__new__(U.UiHandler)
    u.rfile = _RaisingRfile(ConnectionResetError("reset"))
    urep = []
    u._reply = lambda code, obj: urep.append((code, obj))
    u.handle_one_request()
    chk("W2 UI 连接类异常不回包", urep == [], urep)
    chk("W2 UI 连接类异常关连接",
        getattr(u, "close_connection", None) is True,
        getattr(u, "close_connection", None))
    # UI：普通异常——回 500（ok:false）且关连接
    u2 = U.UiHandler.__new__(U.UiHandler)
    u2.rfile = _RaisingRfile(ValueError("boom"))
    urep2 = []
    u2._reply = lambda code, obj: urep2.append((code, obj))
    u2.handle_one_request()
    ok500 = bool(urep2) and urep2[0][0] == 500 and urep2[0][1].get("ok") is False
    chk("W2 UI 普通异常回 500 且 ok 为假", ok500, urep2)
    chk("W2 UI 普通异常关连接",
        getattr(u2, "close_connection", None) is True,
        getattr(u2, "close_connection", None))


# ------------------------------------------------------- 主流程
def main():
    print("# 临时隔离目录:", _TMP)
    fake = FakeCtlDaemon()
    ctl_srv = D.CtlServer(("127.0.0.1", 0), D.CtlHandler)
    ctl_srv.daemon = fake
    ui_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), U.UiHandler)
    ui_srv.daemon_threads = True
    orig_ui_token = U.ui_token
    U.ui_token = lambda: UI_TOKEN
    ctl_t = threading.Thread(target=ctl_srv.serve_forever,
                             kwargs={"poll_interval": 0.05}, daemon=True)
    ui_t = threading.Thread(target=ui_srv.serve_forever,
                            kwargs={"poll_interval": 0.05}, daemon=True)
    ctl_t.start()
    ui_t.start()
    try:
        ctl = ctl_srv.server_address
        ui = ui_srv.server_address
        whitebox()
        for tag, addr, rb in (("Ctl", ctl, ctl_req), ("UI", ui, ui_req)):
            s_no_bytes_rst("S1.%s 连上不发字节直接 RST" % tag, addr, rb)
            s_read_then_rst("S2.%s 读完响应后 RST" % tag, addr, rb,
                            CTL_STATUS_BODY if tag == "Ctl" else UI_MODES_BODY)
            s_send_then_rst("S3.%s 发出请求立刻 RST" % tag, addr, rb)
            s_fin("S4.%s 正常 FIN" % tag, addr)
            s_keepalive("S5.%s 正常请求与连接复用" % tag, addr, rb,
                        CTL_STATUS_BODY if tag == "Ctl" else UI_MODES_BODY)
            s_auth("S5x.%s 鉴权行为不变" % tag, addr, rb,
                   CTL_401_BODY if tag == "Ctl" else UI_401_BODY)
        s_ui_500("S6.UI 端点异常回 500 后关连接", ui)
        time.sleep(1.0)
        alive = [t.name for t in TBYID.values() if t.is_alive()]
        chk("S7 线程表无还活着的服务器处理线程", alive == [], alive)
    finally:
        ctl_srv.shutdown()
        ui_srv.shutdown()
        ctl_srv.server_close()
        ui_srv.server_close()
        U.ui_token = orig_ui_token
        shutil.rmtree(str(_TMP), ignore_errors=True)
        print("# 临时目录已清理: exists=%s" % pathlib.Path(_TMP).exists())
    print()
    print("通过 %d 项，失败 %d 项" % (n_pass, len(fails)))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
