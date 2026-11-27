# -*- coding: utf-8 -*-
"""网页读取纯逻辑模块（plugins/qq-bot/scripts/qq_webfetch.py）的离线冒烟。

零真实网络：socket.getaddrinfo 与 http.client 的两个连接类全程打桩，不访问
任何真实网址（网址只用 example.com / example.org 等保留域名）。
覆盖：check_url 字面校验（scheme/主机/用户名密码/端口）；is_public_ip 与
resolve_public 对回环的各种写法（127.0.0.1、127.1、::1、0.0.0.0、localhost、
2130706433、0x7f000001、::ffff:127.0.0.1、::ffff:10.0.0.1）、私网、链路本地、
组播、保留、混合解析、解析异常；extract_text 的去 script/style/注释、实体、
编码顺序（Content-Type charset → meta charset → utf-8，失败回落 gbk）与截断；
fetch 的每跳两道检查、重定向到内网在第二跳被拒且不发起连接、跳数上限、相对
Location 补全、Content-Length 上限（不读 body）、无 Content-Length 读到上限
即停、异常收拢、404/500。

不读不写数据目录。
跑法：PYTHONDONTWRITEBYTECODE=1 PYTHONIOENCODING=utf-8 python BOT_REPORTS/_smoke_webfetch.py
"""
import http.client as _hc
import ipaddress
import pathlib
import socket
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent
                       / "plugins" / "qq-bot" / "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import qq_webfetch as W  # noqa: E402

fails = []
total = [0]


def chk(name, cond, extra=""):
    total[0] += 1
    print(("PASS " if cond else "FAIL ") + name
          + (" | " + str(extra)[:120] if extra else ""))
    if not cond:
        fails.append(name)


# ---------------- 打桩（必须在跑任何用例前装好） ----------------
_ORIG_GAI = socket.getaddrinfo
_ORIG_HTTPS = _hc.HTTPSConnection
_ORIG_HTTP = _hc.HTTPConnection


class _FakeResponse:
    def __init__(self, status, headers, body=b""):
        self.status = status
        self._headers = list(headers)
        self._buf = body
        self._pos = 0
        self.read_calls = 0

    def getheaders(self):
        return list(self._headers)

    def read(self, n=-1):
        self.read_calls += 1
        if n is None or n < 0:
            out = self._buf[self._pos:]
            self._pos = len(self._buf)
            return out
        out = self._buf[self._pos:self._pos + n]
        self._pos += len(out)
        return out


class _FakeConn:
    """按 host 脚本化的假连接；log 记录每次建连的 (host, port)。"""
    log = []
    script = {}

    def __init__(self, host, port=None, timeout=None):
        _FakeConn.log.append((host, port, getattr(self, "_pinned_ip", None)))
        entry = _FakeConn.script.get(host)
        if entry is None:
            raise RuntimeError("打桩未覆盖的连接目标: %r" % host)
        self._entry = entry

    def request(self, method, path, headers=None):
        if isinstance(self._entry.get("raise_on_request"), Exception):
            raise self._entry["raise_on_request"]
        self._entry.setdefault("requests", []).append((method, path, headers or {}))

    def getresponse(self):
        if isinstance(self._entry.get("raise_on_response"), Exception):
            raise self._entry["raise_on_response"]
        seq = self._entry.get("responses") or []
        if not seq:
            raise RuntimeError("打桩脚本耗尽: %r" % self._entry)
        return seq.pop(0)

    def close(self):
        pass


class _FakeDNS:
    table = {}

    @staticmethod
    def getaddrinfo(host, port):
        v = _FakeDNS.table.get(host)
        if isinstance(v, Exception):
            raise v
        if v is None:
            raise socket.gaierror(11001, "stub: 没有脚本化的域名 %r" % host)
        out = []
        for ip in v:
            if ipaddress.ip_address(ip).version == 6:
                out.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "",
                            (ip, port, 0, 0)))
            else:
                out.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)))
        return out


socket.getaddrinfo = _FakeDNS.getaddrinfo
_hc.HTTPSConnection = _FakeConn
_hc.HTTPConnection = _FakeConn


def resp(status, ctype=None, body=b"", clen=None, location=None):
    headers = []
    if ctype:
        headers.append(("Content-Type", ctype))
    if clen is not None:
        headers.append(("Content-Length", str(clen)))
    if location is not None:
        headers.append(("Location", location))
    return _FakeResponse(status, headers, body)


# ---------------- ① check_url ----------------
for bad in ("ftp://example.com/x", "file:///etc/hosts", "http:///nohost",
            "http://user:pass@example.com/", "http://user@example.com/",
            "http://example.com:abc/", "http://example.com:0/",
            "http://example.com:70000/", ""):
    ok, reason, _p = W.check_url(bad)
    chk("check_url 拒 %r" % (bad or "<空串>"), ok is False, reason)

ok, reason, p = W.check_url("http://example.com/")
chk("check_url 过 http 缺省 80", ok and p == {"scheme": "http", "host": "example.com",
                                              "port": 80, "path": "/"}, (ok, reason, p))
ok, reason, p = W.check_url("https://Example.com:8443/a?b=1&c=2")
chk("check_url 过 https 带端口与查询", ok and p["scheme"] == "https"
    and p["host"] == "example.com" and p["port"] == 8443
    and p["path"] == "/a?b=1&c=2", (ok, reason, p))
ok, reason, p = W.check_url("https://example.com")
chk("check_url https 缺省 443 path=/", ok and p["port"] == 443 and p["path"] == "/", p)
ok, reason, p = W.check_url("HTTP://EXAMPLE.COM/X")
chk("check_url 大写 scheme/host 归一", ok and p["scheme"] == "http"
    and p["host"] == "example.com", p)

# ---------------- ② is_public_ip ----------------
for ip in ("8.8.8.8", "1.1.1.1", "2606:4700::1111", "::ffff:8.8.8.8"):
    chk("is_public_ip 公网 %s" % ip, W.is_public_ip(ip) is True)
for ip in ("127.0.0.1", "::1", "0.0.0.0", "::ffff:127.0.0.1", "::ffff:10.0.0.1",
           "10.1.2.3", "172.16.9.9", "172.31.255.1", "192.168.1.1",
           "169.254.169.254", "224.0.0.1", "239.1.1.1", "ff02::1", "240.0.0.1",
           "255.255.255.255", "::"):
    chk("is_public_ip 非公网 %s" % ip, W.is_public_ip(ip) is False)
chk("is_public_ip 乱串 False", W.is_public_ip("not-an-ip") is False)

# ---------------- ③ resolve_public（getaddrinfo 打桩） ----------------
_REJECT = {
    "localhost": ["127.0.0.1"],
    "127.1": ["127.0.0.1"],
    "2130706433": ["127.0.0.1"],
    "0x7f000001": ["127.0.0.1"],
    "v6loop.example.org": ["::1"],
    "zero.example.org": ["0.0.0.0"],
    "maploop.example.org": ["::ffff:127.0.0.1"],
    "mapprov.example.org": ["::ffff:10.0.0.1"],
    "in10.example.org": ["10.0.0.1"],
    "in172.example.org": ["172.16.3.4"],
    "in192.example.org": ["192.168.7.7"],
    "linklocal.example.org": ["169.254.169.254"],
    "mc.example.org": ["224.0.0.1"],
    "mc6.example.org": ["ff05::1"],
    "rsv.example.org": ["240.0.0.1"],
    "mixed.example.org": ["93.184.216.34", "10.0.2.3"],
}
_OK = {
    "public.example.com": ["93.184.216.34"],
    "two.example.com": ["93.184.216.34", "1.2.3.4"],
    "mapped.example.com": ["::ffff:8.8.8.8"],
}
_FakeDNS.table = dict(_REJECT)
_FakeDNS.table.update(_OK)
_FakeDNS.table["dead.example.org"] = socket.gaierror(11001, "stub down")
for host, ips in _REJECT.items():
    ok, reason, _a = W.resolve_public(host, 80)
    chk("resolve 拒 %s -> %s" % (host, ",".join(ips)), ok is False, reason)
ok, reason, addrs = W.resolve_public("public.example.com", 443)
chk("resolve 过单个公网", ok and addrs == ["93.184.216.34"], (ok, reason, addrs))
ok, reason, addrs = W.resolve_public("two.example.com", 80)
chk("resolve 过两个公网", ok and addrs == ["93.184.216.34", "1.2.3.4"], (ok, reason, addrs))
ok, reason, _a = W.resolve_public("mapped.example.com", 80)
chk("resolve 过映射公网 ::ffff:8.8.8.8", ok, reason)
ok, reason, _a = W.resolve_public("dead.example.org", 80)
chk("resolve 解析抛异常拒", ok is False and "解析失败" in reason, reason)
ok, reason, _a = W.resolve_public("nosuch.example.org", 80)
chk("resolve 未脚本化域名拒", ok is False, reason)

# ---------------- ④ extract_text ----------------
H1 = ("<html><head><title>T &amp; 上海</title>"
      "<style>body{color:red}</style>"
      '<meta charset="utf-8"></head><body><!-- 哨兵注释 -->'
      "<script>alert(1)</script><noscript>noscript文本</noscript>"
      "<p>你好&nbsp;&amp;&nbsp;世界   <b>粗</b></p><p>第二段</p></body></html>")
t, x = W.extract_text(H1.encode("utf-8"), "text/html; charset=utf-8")
chk("extract 标题解实体", t == "T & 上海", t)
chk("extract 正文全序列", x == "你好 & 世界 粗 第二段", x)
chk("extract 正文不以标题开头", not x.startswith("T &"), x)
chk("extract 无残留", "alert" not in x and "color:red" not in x
    and "哨兵" not in x and "noscript文本" not in x, x)

t, x = W.extract_text("<p>只有正文</p>".encode("utf-8"), "text/html")
chk("extract 无 title", t == "" and x == "只有正文", (t, x))

gbk_html = ("<html><head><title>人民币汇率</title></head>"
            "<body><p>中文正文测试</p></body></html>").encode("gbk")
t, x = W.extract_text(gbk_html, "text/html; charset=GBK")
chk("extract gbk 按 Content-Type", t == "人民币汇率"
    and x == "中文正文测试", (t, x))
meta_gbk = ('<html><head><meta charset="gbk"><title>人民币汇率</title></head>'
            "<body><p>中文正文测试</p></body></html>").encode("gbk")
t, x = W.extract_text(meta_gbk, "text/html")
chk("extract gbk 按 meta charset", t == "人民币汇率"
    and x == "中文正文测试", (t, x))
utf8_meta_gbk = ('<html><head><meta charset="gbk"><title>标题八</title></head>'
                 "<body><p>编码优先级</p></body></html>").encode("utf-8")
t, x = W.extract_text(utf8_meta_gbk, "text/html; charset=utf-8")
chk("extract Content-Type charset 优先于 meta", x == "编码优先级", x)
t, x = W.extract_text(gbk_html, "text/html; charset=utf-8")
chk("extract 解码失败回落 gbk", t == "人民币汇率"
    and x == "中文正文测试", (t, x))

t, x = W.extract_text("第一行\n\n第二行  尾  ".encode("utf-8"), "text/plain")
chk("extract text/plain 保留换行去首尾空白", t == "" and x == "第一行\n\n第二行  尾", (t, x))

t, x = W.extract_text(b'{"a": 1}', "application/json; charset=utf-8")
chk("extract json 返回空", t == "" and x == "", (t, x))
t, x = W.extract_text(b"\x89PNG", "image/png")
chk("extract png 返回空", t == "" and x == "", (t, x))
t, x = W.extract_text(b"abc", "")
chk("extract 无 Content-Type 返回空", t == "" and x == "", (t, x))

t, x = W.extract_text(("字" * 7000).encode("utf-8"), "text/plain")
chk("extract 超长截断到 6000 并注明", x.startswith("字" * 6000)
    and "截断" in x[6000:], len(x))
chk("extract 截断后总长", len(x) == 6000 + len("…[正文超过 6000 字，已截断]"), len(x))
t, x = W.extract_text(("字" * 6000).encode("utf-8"), "text/plain")
chk("extract 恰好 6000 不截断", x == "字" * 6000 and "截断" not in x, len(x))

# ---------------- ⑤ fetch（连接类打桩） ----------------
BASE_DNS = {
    "www.example.com": ["93.184.216.34"],
    "redir.example.com": ["93.184.216.34"],
    "www.example.org": ["93.184.216.34"],
    "hop1.example.com": ["1.2.3.4"],
    "hop2.example.com": ["5.6.7.8"],
    "hop3.example.com": ["9.10.11.12"],
    "hop4.example.com": ["13.14.15.16"],
    "10.0.0.5": ["10.0.0.5"],
    "127.0.0.1": ["127.0.0.1"],
    "refused.example.org": ["6.7.8.9"],
    "timeout.example.org": ["6.7.8.9"],
    "missing.example.org": socket.gaierror(11001, "stub down"),
}


def _dns(table):
    _FakeDNS.table = dict(table)


def _conn(script):
    _FakeConn.log = []
    _FakeConn.script = script


def _requests_of(host):
    return _FakeConn.script.get(host, {}).get("requests", [])


# ⑤-1 200 正常
_dns(BASE_DNS)
page = ("<html><head><title>示例首页</title></head><body><p>正文内容</p></body></html>").encode("utf-8")
entry = {"responses": [resp(200, "text/html; charset=utf-8", page, clen=len(page))]}
_conn({"www.example.com": entry})
r = W.fetch("http://www.example.com/")
chk("fetch 200 ok", r["ok"] is True and r["status"] == 200 and r["error"] == "", r)
chk("fetch 200 title/text/final_url", r["title"] == "示例首页"
    and r["text"] == "正文内容" and r["final_url"] == "http://www.example.com/", r)
chk("fetch 200 建连一次 (host,80,已校验IP)", _FakeConn.log
    == [("www.example.com", 80, "93.184.216.34")], _FakeConn.log)
rq = _requests_of("www.example.com")
chk("fetch GET path UA/Accept/Connection", len(rq) == 1 and rq[0][0] == "GET"
    and rq[0][1] == "/" and "Mozilla" in rq[0][2].get("User-Agent", "")
    and rq[0][2].get("Accept", "").startswith("text/html")
    and rq[0][2].get("Connection") == "close", rq)

# ⑤-2 https 走 443、path 带查询
entry = {"responses": [resp(200, "text/plain", b"plain ok")]}
_conn({"www.example.org": entry})
r = W.fetch("https://www.example.org/api?x=1")
chk("fetch https 443 与 path", r["ok"] and r["text"] == "plain ok"
    and _FakeConn.log == [("www.example.org", 443, "93.184.216.34")]
    and _requests_of("www.example.org")[0][1] == "/api?x=1",
    (_FakeConn.log, r))

# ⑤-3 重定向到内网：第二跳被拒，且没有发起第二次连接
_dns(BASE_DNS)
_conn({"redir.example.com": {"responses": [resp(302, location="http://10.0.0.5/secret")]}})
r = W.fetch("http://redir.example.com/")
chk("fetch 重定向内网 拒", r["ok"] is False and r["status"] == 302, r)
chk("fetch 重定向内网 只建连一次", _FakeConn.log
    == [("redir.example.com", 80, "93.184.216.34")], _FakeConn.log)
chk("fetch 重定向内网 报文含 10.0.0.5", "10.0.0.5" in r["error"], r["error"])
_conn({"redir.example.com": {"responses": [resp(301, location="http://127.0.0.1:8080/admin")]}})
r = W.fetch("http://redir.example.com/x")
chk("fetch 重定向回环带端口 拒", r["ok"] is False and "127.0.0.1" in r["error"]
    and len(_FakeConn.log) == 1, (r["error"], _FakeConn.log))

# ⑤-4 重定向超过 4 跳（max_hops=4）
_conn({"hop%d.example.com" % i: {"responses": [
    resp(302, location="http://hop%d.example.com/next" % (i + 1))]}
    for i in range(1, 5)})
r = W.fetch("http://hop1.example.com/")
chk("fetch 重定向超 4 跳 拒", r["ok"] is False and "跳" in r["error"], r["error"])
chk("fetch 超 4 跳 只建连 4 次", len(_FakeConn.log) == 4, _FakeConn.log)

# ⑤-5 3 跳重定向 + 200（共 4 跳，恰在限内）
_conn({
    "hop1.example.com": {"responses": [resp(302, location="http://hop2.example.com/a")]},
    "hop2.example.com": {"responses": [resp(302, location="http://hop3.example.com/b")]},
    "hop3.example.com": {"responses": [resp(307, location="http://hop4.example.com/c")]},
    "hop4.example.com": {"responses": [resp(200, "text/plain", "到达".encode("utf-8"))]},
})
r = W.fetch("http://hop1.example.com/")
chk("fetch 3 跳重定向后 200 过", r["ok"] is True and r["text"] == "到达"
    and r["final_url"] == "http://hop4.example.com/c", r)

# ⑤-6 相对 Location 按当前地址补全
_conn({"redir.example.com": {"responses": [
    resp(302, location="/next/page?q=2"),
    resp(200, "text/plain", b"rel")]}})
r = W.fetch("http://redir.example.com/start")
chk("fetch 相对 Location 补全", r["ok"] and r["final_url"] == "http://redir.example.com/next/page?q=2", r)

# ⑤-7 Content-Length 超限：拒且未读 body
r200 = _FakeResponse(200, [("Content-Type", "text/html"),
                           ("Content-Length", "3000000")], b"x" * 100)
_conn({"www.example.com": {"responses": [r200]}})
r = W.fetch("http://www.example.com/")
chk("fetch CL 超限 拒", r["ok"] is False and "上限" in r["error"], r["error"])
chk("fetch CL 超限 未读 body", r200.read_calls == 0, r200.read_calls)
r200b = _FakeResponse(200, [("Content-Type", "text/plain"),
                            ("Content-Length", "100")], b"y" * 100)
_conn({"www.example.com": {"responses": [r200b]}})
r = W.fetch("http://www.example.com/", max_bytes=50)
chk("fetch 自定义 max_bytes 超限 拒", r["ok"] is False and "上限" in r["error"], r["error"])

# ⑤-8 无 Content-Length：读到上限即停；小 body 正常
rnf = _FakeResponse(200, [("Content-Type", "text/plain")], b"z" * 2000050)
_conn({"www.example.com": {"responses": [rnf]}})
r = W.fetch("http://www.example.com/")
chk("fetch 无 CL 超限 拒", r["ok"] is False and "上限" in r["error"], r["error"])
chk("fetch 无 CL 超限 分块读过", rnf.read_calls >= 2, rnf.read_calls)
entry = {"responses": [resp(200, "text/plain", b"small body")]}
_conn({"www.example.com": entry})
r = W.fetch("http://www.example.com/")
chk("fetch 无 CL 小 body 过", r["ok"] is True and r["text"] == "small body", r)

# ⑤-9 非 200 状态
for code in (404, 500):
    _conn({"www.example.com": {"responses": [resp(code, "text/html", b"gone")]}})
    r = W.fetch("http://www.example.com/")
    chk("fetch HTTP %d 错误" % code, r["ok"] is False and str(code) in r["error"], r["error"])

# ⑤-10 重定向缺 Location
_conn({"redir.example.com": {"responses": [resp(302)]}})
r = W.fetch("http://redir.example.com/")
chk("fetch 重定向缺 Location 拒", r["ok"] is False and "Location" in r["error"], r["error"])

# ⑤-11 异常收拢
_conn({
    "refused.example.org": {"raise_on_request": ConnectionRefusedError("stub refuse")},
    "timeout.example.org": {"raise_on_response": TimeoutError("stub timeout")},
})
r = W.fetch("http://refused.example.org/")
chk("fetch 建连异常收拢", r["ok"] is False and "refuse" in r["error"], r["error"])
r = W.fetch("http://timeout.example.org/")
chk("fetch 响应超时收拢", r["ok"] is False and "timeout" in r["error"], r["error"])
r = W.fetch("http://missing.example.org/")
chk("fetch DNS 失败收拢", r["ok"] is False and "解析" in r["error"], r["error"])

# ⑤-12 200 但非文本类型：抓取成功、正文为空
_conn({"www.example.com": {"responses": [resp(200, "application/json", b'{"a":1}')]}})
r = W.fetch("http://www.example.com/")
chk("fetch 200 非文本类型 正文空", r["ok"] is True and r["title"] == "" and r["text"] == "", r)

# ---------------- 还原打桩 ----------------
socket.getaddrinfo = _ORIG_GAI
_hc.HTTPSConnection = _ORIG_HTTPS
_hc.HTTPConnection = _ORIG_HTTP

# ---------------- ⑥ 只连已校验 IP（真实连接类 + socket.create_connection 打桩） ----------------
# 必须在连接类还原之后跑：桩改的是 http.client 模块的全局名，真 HTTPSConnection
# 的零参 super()（client.py:1510）会查到假类。
_ORIG_CC = socket.create_connection
created = []


class _FakeSock:
    def setsockopt(self, *a, **k):
        pass

    def close(self):
        pass


def _fake_cc(address, timeout=None, source_address=None):
    created.append((address, timeout, source_address))
    return _FakeSock()


socket.create_connection = _fake_cc
try:
    # http 支：connect 底层只收到 (已校验IP, port)，self.host 仍是主机名
    c = W._pinned_connection_cls(_ORIG_HTTP, "93.184.216.34")("example.com", 80, timeout=2)
    c.connect()
    chk("⑥ http connect 只连已校验 IP",
        created == [(("93.184.216.34", 80), 2, None)], created)
    chk("⑥ http self.host 仍为主机名", c.host == "example.com", c.host)
    c.close()

    # https 支：TLS 的 server_hostname 用主机名（SNI 与证书校验不受换 IP 影响）
    created.clear()

    class _FakeCtx:
        def __init__(self):
            self.snis = []

        def wrap_socket(self, sock, server_hostname=None):
            self.snis.append(server_hostname)
            return sock

    ctx = _FakeCtx()
    c = W._pinned_connection_cls(_ORIG_HTTPS, "93.184.216.34")("example.com", 443, timeout=2)
    c._context = ctx
    c.connect()
    chk("⑥ https connect 只连已校验 IP",
        created == [(("93.184.216.34", 443), 2, None)], created)
    chk("⑥ https SNI 用主机名", ctx.snis == ["example.com"], ctx.snis)
    c.close()
finally:
    socket.create_connection = _ORIG_CC

print()
if fails:
    print("失败 %d / %d 项: %s" % (len(fails), total[0], ", ".join(fails[:10])))
    sys.exit(1)
print("全部通过：共 %d 个用例（getaddrinfo 与连接类打桩已还原）" % total[0])
sys.exit(0)
