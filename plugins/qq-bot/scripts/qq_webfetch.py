# -*- coding: utf-8 -*-
"""qq_webfetch.py — 网页正文抓取的纯逻辑模块（web_fetch 工具的底层，本模块不进工具表）

设计依据：BOT_REPORTS/20261126-大改造与指令代理化-施工总计划.md「D 批方向」D2。
只用标准库；对外 HTTP 一律 http.client 连接类，不用 urllib.request.urlopen。

安全模型（硬约束，写死在本模块）：
  - 只收 http 与 https；拒绝带用户名密码的地址（user:pass@host）
  - 每一跳（含重定向后的每一跳）都先 check_url、再 resolve_public，两道都过才连接；
    DNS 解析出的全部地址必须都是公网（回环、私网、链路本地、保留、组播、未指定
    一律拒）——机器人本机有带令牌的管理接口，这条是底线
  - 连接只连已校验的 IP（防 DNS 重绑定：不让 http.client 内部再用主机名解析一次）；
    SNI、证书校验与 Host 头仍按主机名
  - 只认 text/html 与 text/plain；响应体上限默认 2 MB；超时默认 15 秒
  - 任何异常都收成 ok=False 加原因，不外抛

公开函数：
  check_url(url) -> (ok, reason, parts)              URL 字面校验，不碰网络
  is_public_ip(ip_text) -> bool                      单个 IP 是否公网
  resolve_public(host, port) -> (ok, reason, addrs)  DNS 解析并逐个判公网
                                                     （模块内唯一做网络查询的函数）
  extract_text(body_bytes, content_type) -> (title, text)   抽标题与正文
  fetch(url, timeout=15.0, max_bytes=2000000, max_hops=4) -> dict   抓取
"""

import html as _html
import http.client
import ipaddress
import re
import socket
import urllib.parse

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_TEXT_MAX = 6000            # 正文超过 6000 字截断并注明
_TRUNC_NOTE = "…[正文超过 6000 字，已截断]"
_META_SNIFF = 2048          # 找 <meta charset> 只看开头这么多字节
_REDIRECT_CODES = (301, 302, 303, 307, 308)
_CHUNK = 65536

_RE_COMMENT = re.compile(r"<!--.*?-->", re.S)
_RE_BLOCK = re.compile(r"(?is)<(script|style|noscript)\b[^>]*>.*?</\1\s*>")
_RE_TAG = re.compile(r"<[^>]+>")
_RE_WS = re.compile(r"\s+")
_RE_TITLE = re.compile(r"(?is)<title[^>]*>(.*?)</title\s*>")
_RE_CT_CHARSET = re.compile(r'charset\s*=\s*["\']?([\w\-.]+)', re.I)
_RE_META_CHARSET = re.compile(r'<meta[^>]+charset\s*=\s*["\']?([\w\-.]+)', re.I)


def check_url(url):
    """URL 字面校验：scheme、主机、用户名密码、端口。返回 (ok, reason, parts)。

    parts = {scheme, host, port, path(带查询串)}；端口缺省按 scheme 补 80/443。
    """
    try:
        sp = urllib.parse.urlsplit(url)
    except ValueError as e:
        return False, "网址无法解析: %s" % e, None
    scheme = (sp.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, "只允许 http/https，收到 %r" % scheme, None
    host = sp.hostname
    if not host:
        return False, "缺少主机", None
    if sp.username or sp.password:
        return False, "不允许带用户名密码的地址", None
    try:
        port = sp.port
    except ValueError:
        return False, "端口必须是数字", None
    if port is None:
        port = 80 if scheme == "http" else 443
    if not (1 <= port <= 65535):
        return False, "端口必须在 1-65535，收到 %s" % port, None
    path = sp.path or "/"
    if sp.query:
        path += "?" + sp.query
    return True, "", {"scheme": scheme, "host": host, "port": port, "path": path}


def is_public_ip(ip_text):
    """单个 IP 字符串是否公网。IPv4 映射的 IPv6（::ffff:a.b.c.d）先取出 IPv4 再判。

    回环、私网、链路本地、保留、组播、未指定一律 False。注意 3.13 里组播的
    is_global 是 True，所以组播/保留/未指定要显式拦。
    """
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return False
    return bool(ip.is_global)


def resolve_public(host, port):
    """DNS 解析出全部地址，每一个都必须是公网才通过。返回 (ok, reason, addrs)。

    addrs 是按解析顺序去重的 IP 字符串列表；拒绝时返回到该点为止的列表。
    本函数是模块内唯一做网络查询的函数（DNS）。
    """
    try:
        infos = socket.getaddrinfo(host, port)
    except Exception as e:
        return False, "DNS 解析失败: %s" % e, []
    addrs = []
    for info in infos:
        try:
            ip_text = info[4][0]
        except (IndexError, TypeError):
            return False, "DNS 返回了不认识的地址项", addrs
        if ip_text not in addrs:
            addrs.append(ip_text)
        if not is_public_ip(ip_text):
            return False, "地址不是公网: %s（host=%r）" % (ip_text, host), addrs
    if not addrs:
        return False, "DNS 没有返回任何地址", []
    return True, "", addrs


def _pinned_connection_cls(base, ip):
    """由连接类派生「只连已校验 IP」的子类（DNS 重绑定防护）。

    依据本机 Python 3.13 的 Lib/http/client.py：
    - HTTPConnection.__init__ 在末尾把实例属性 _create_connection 设为
      socket.create_connection（925-927 行，注释明说留作单测替换），所以必须在
      super().__init__() 之后再换成只连 (ip, port) 的实例函数；
    - HTTPConnection.connect() 调 _create_connection((host, port), timeout,
      source_address)（1051-1052 行）；
    - HTTPSConnection.connect() 先 super().connect() 再用 self.host 做
      server_hostname 包 TLS（1517-1528 行），SNI 与证书校验不受换 IP 影响，
      Host 请求头也仍由库按 self.host 生成。
    """
    class _Pinned(base):
        def __init__(self, host, port=None, timeout=None):
            self._pinned_ip = ip
            super().__init__(host, port, timeout=timeout)

            def _create_connection(address, timeout=None, source_address=None):
                return socket.create_connection((ip, address[1]), timeout=timeout,
                                                source_address=source_address)
            self._create_connection = _create_connection

    return _Pinned


def _clean(s):
    return _RE_WS.sub(" ", s).strip()


def _decode_body(body_bytes, content_type):
    """定编码顺序：Content-Type charset → <meta charset> → utf-8；解码失败回落 gbk。

    返回 (text, 实际采用的编码描述)。"""
    enc = None
    m = _RE_CT_CHARSET.search(content_type or "")
    if m:
        enc = m.group(1)
    if not enc:
        head = body_bytes[:_META_SNIFF].decode("latin-1", "replace")
        m = _RE_META_CHARSET.search(head)
        if m:
            enc = m.group(1)
    enc = enc or "utf-8"
    for candidate in (enc, "gbk"):
        try:
            return body_bytes.decode(candidate), candidate
        except (UnicodeDecodeError, LookupError):
            continue
    return body_bytes.decode("utf-8", "replace"), "utf-8(replace)"


def extract_text(body_bytes, content_type):
    """从响应体抽 (title, text)。只认 text/html 与 text/plain，其它类型 ("", "")。

    HTML：去掉 script、style、noscript、注释和全部标签，解实体，连续空白压成
    一个；<title> 另取。正文超过 6000 字截断并在末尾注明。
    """
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype not in ("text/html", "text/plain"):
        return "", ""
    body, _enc = _decode_body(body_bytes, content_type)
    title = ""
    if ctype == "text/plain":
        text = body.strip()
    else:
        m = _RE_TITLE.search(body)
        if m:
            title = _clean(_html.unescape(m.group(1)))
        h = _RE_COMMENT.sub(" ", body)
        h = _RE_TITLE.sub(" ", h)    # 标题整段去掉，正文不留标题文字
        h = _RE_BLOCK.sub(" ", h)
        h = _RE_TAG.sub(" ", h)
        text = _clean(_html.unescape(h))
    if len(text) > _TEXT_MAX:
        text = text[:_TEXT_MAX] + _TRUNC_NOTE
    return title, text


def fetch(url, timeout=15.0, max_bytes=2000000, max_hops=4):
    """抓网页。返回 {ok, final_url, status, title, text, error}，任何异常不外抛。

    每一跳：先 check_url、再 resolve_public，两道都过才连接；连接只连已校验 IP
    （防 DNS 重绑定），SNI/证书/Host 头仍按主机名。301/302/303/307/308
    取 Location（相对地址按当前地址补全）进入下一跳并重新过两道检查；跳数超过
    max_hops → 错误；200 → 按 Content-Type 抽正文；其它状态 → 错误。
    """
    result = {"ok": False, "final_url": "", "status": None,
              "title": "", "text": "", "error": ""}
    current = url
    for hop in range(1, max_hops + 1):
        result["final_url"] = current
        ok, reason, parts = check_url(current)
        if not ok:
            result["error"] = "第 %d 跳地址不合法: %s" % (hop, reason)
            return result
        ok, reason, addrs = resolve_public(parts["host"], parts["port"])
        if not ok:
            result["error"] = "第 %d 跳拒绝（%s）: %s" % (hop, parts["host"], reason)
            return result
        conn = None
        try:
            cls = _pinned_connection_cls(
                http.client.HTTPSConnection if parts["scheme"] == "https"
                else http.client.HTTPConnection, addrs[0])
            conn = cls(parts["host"], parts["port"], timeout=timeout)
            conn.request("GET", parts["path"], headers={
                "User-Agent": UA,
                "Accept": "text/html,text/plain;q=0.9,*/*;q=0.5",
                "Connection": "close",
            })
            resp = conn.getresponse()
            result["status"] = resp.status
            headers = {k.lower(): v for k, v in resp.getheaders()}
            if resp.status in _REDIRECT_CODES:
                loc = (headers.get("location") or "").strip()
                if not loc:
                    result["error"] = "第 %d 跳 %d 重定向缺少 Location" % (hop, resp.status)
                    return result
                if hop >= max_hops:
                    result["error"] = "重定向超过最大跳数 %d" % max_hops
                    return result
                current = urllib.parse.urljoin(current, loc)
                continue
            if resp.status != 200:
                result["error"] = "HTTP %d" % resp.status
                return result
            ctype = headers.get("content-type", "")
            cl = headers.get("content-length")
            if cl:
                try:
                    if int(cl) > max_bytes:
                        result["error"] = ("响应体 Content-Length %s 超过上限 %d 字节"
                                           % (cl, max_bytes))
                        return result
                except ValueError:
                    pass    # 非法 Content-Length 当没有，按流读
            chunks, total = [], 0
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    result["error"] = ("响应体超过上限 %d 字节（无 Content-Length，"
                                       "读到上限即停）" % max_bytes)
                    return result
            title, text = extract_text(b"".join(chunks), ctype)
            result["ok"] = True
            result["title"] = title
            result["text"] = text
            return result
        except Exception as e:
            result["error"] = "第 %d 跳请求失败: %s" % (hop, e)
            return result
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    result["error"] = result["error"] or "重定向超过最大跳数 %d" % max_hops
    return result
