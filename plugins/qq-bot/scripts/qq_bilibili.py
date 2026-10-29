#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qq_bilibili.py — 机器人主动刷 B 站（未来可扩展 AcFun/抖音/知乎等平台）

能力（全部默认关闭，见 reply_config 的 bili_scroll_enabled / bili_share_enabled）：
  1. 扫码登录机器人自己的 B 站账号（passport qrcode → Set-Cookie 凭证持久化）
  2. 按人设兴趣搜近期视频 → LLM 以人设口吻挑选 0-2 条并写一句"观后感"
  3. 感兴趣的内容：站内私信主人（需主人 B 站 UID）/ QQ 私信主人（bili_share_enabled）
  4. 浏览见闻写入 bilibili_brief.json，注入聊天上下文（"最近刷到…"），更像真人

安全模型（沿用项目约定）：
  - 仅 https 出站；API host 白名单（passport/api/api.vc.bilibili.com）
  - Cookie 只存本机数据目录（bilibili_cookies.json），不入日志、不入仓库
  - 分享只发给主人（QQ=owner_qq；站内=owner 配置的 B 站 UID），绝不群发

API 依据（dsh-bili-scroll 实测结论，2026-10 验证）：
  - 登录：GET passport.bilibili.com/x/passport-login/web/qrcode/generate|poll
    成功凭证在轮询响应的 Set-Cookie 头（data.url 里没有 SESSDATA）
  - 搜索：GET api.bilibili.com/x/web-interface/search/type?search_type=video&keyword=…
    （未登录可用；带 Cookie 个性化更好；返回 data.result[]）
  - 站内私信：POST api.vc.bilibili.com/web_im/v1/web_im/send_msg
    msg[type]=1 文本；content 为 JSON 字符串；csrf=bili_jct；有 msg_key 才算送达
"""

import http.client
import json
import os
import pathlib
import re
import time
import urllib.parse
import uuid

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_ALLOWED_HOSTS = ("passport.bilibili.com", "api.bilibili.com",
                  "api.vc.bilibili.com", "message.bilibili.com")

DATA_DIR = os.environ.get("QQBOT_DATA_DIR") or os.path.join(
    os.path.expanduser("~"), ".zcode-qq-bot")
COOKIES_PATH = os.path.join(DATA_DIR, "bilibili_cookies.json")
BRIEF_PATH = os.path.join(DATA_DIR, "bilibili_brief.json")
SCAN_PATH = os.path.join(DATA_DIR, "bilibili_login_scan.json")

_qrcode_lib = None


def _conn(host):
    if host not in _ALLOWED_HOSTS:
        raise ValueError("host 不在白名单: %r" % host)
    return http.client.HTTPSConnection(host, timeout=20)


def _https_json(host, path, method="GET", body=None, headers=None):
    conn = _conn(host)
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    payload = None
    if body is not None:
        payload = urllib.parse.urlencode(body).encode("utf-8")
        h["Content-Type"] = "application/x-www-form-urlencoded"
    conn.request(method, path, body=payload, headers=h)
    resp = conn.getresponse()
    set_cookies = [v for k, v in resp.getheaders() if k.lower() == "set-cookie"]
    raw = resp.read().decode("utf-8", "replace")
    conn.close()
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    return resp.status, data, set_cookies


def _write_json(path, obj):
    pathlib.Path(path).write_text(
        json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path, default):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


# ---------------- 登录（扫码） ----------------

def login_start():
    """生成扫码登录会话：返回 {key, url, qr_png}（qr_png=二维码图片路径，可空）。"""
    st, data, _ = _https_json(
        "passport.bilibili.com",
        "/x/passport-login/web/qrcode/generate?source=main-fe-header")
    if st != 200 or not isinstance(data, dict) or not (data.get("data") or {}).get("qrcode_key"):
        raise RuntimeError("生成二维码失败: %s" % (data or {}).get("message", st))
    key, url = data["data"]["qrcode_key"], data["data"]["url"]
    qr_png = ""
    try:
        global _qrcode_lib
        if _qrcode_lib is None:
            import qrcode as _q
            _qrcode_lib = _q
        png = os.path.join(DATA_DIR, "bilibili_qr.png")
        _qrcode_lib.make(url).save(png)
        qr_png = png
    except Exception:
        qr_png = ""  # 没装 qrcode 库就只给链接（UI 提示手抄到手机）
    _write_json(SCAN_PATH, {"key": key, "ts": time.time()})
    return {"ok": True, "key": key, "url": url, "qr_png": qr_png}


def _pick_cookies(set_cookies):
    jar = {}
    want = {"SESSDATA": "SESSDATA", "bili_jct": "bili_jct",
            "DedeUserID": "DedeUserID", "buvid3": "buvid3"}
    for raw in set_cookies or []:
        first = str(raw).split(";")[0]
        i = first.find("=")
        if i <= 0:
            continue
        name, val = first[:i].strip(), first[i + 1:].strip()
        if name in want:
            jar[want[name]] = val
    return jar


def login_poll(key):
    """轮询扫码结果：waiting/scanned/ok/expired。成功时持久化 Cookie。"""
    st, data, set_cookies = _https_json(
        "passport.bilibili.com",
        "/x/passport-login/web/qrcode/poll?qrcode_key="
        + urllib.parse.quote(str(key)) + "&source=main-fe-header",
        headers={"Referer": "https://www.bilibili.com/"})
    code = (data or {}).get("data", {}).get("code") if isinstance(data, dict) else None
    mapping = {86101: "waiting", 86090: "scanned", 86038: "expired", 0: "ok"}
    state = mapping.get(code, "unknown")
    if state == "ok":
        jar = _pick_cookies(set_cookies)
        if not jar.get("SESSDATA"):
            return {"state": "unknown", "msg": "登录成功但响应缺 SESSDATA"}
        if not jar.get("buvid3"):
            try:
                _st, spi, _ = _https_json("api.bilibili.com", "/x/frontend/finger/spi")
                v = (spi or {}).get("data", {}).get("b_3")
                if v:
                    jar["buvid3"] = v
            except Exception:
                pass
        jar["ts"] = int(time.time())
        _write_json(COOKIES_PATH, jar)
        try:
            pathlib.Path(SCAN_PATH).unlink()
        except OSError:
            pass
        return {"state": "ok", "uid": jar.get("DedeUserID", "")}
    return {"state": state, "code": code}


def cookies():
    c = _read_json(COOKIES_PATH, {})
    return c if isinstance(c, dict) and c.get("SESSDATA") else None


def logged_in_uid():
    c = cookies()
    return str(c.get("DedeUserID") or "") if c else ""


def _cookie_header():
    c = cookies()
    return "SESSDATA=%s; bili_jct=%s; buvid3=%s; DedeUserID=%s" % (
        c.get("SESSDATA", ""), c.get("bili_jct", ""),
        c.get("buvid3", ""), c.get("DedeUserID", ""))


# ---------------- 内容获取（搜索） ----------------

def search_videos(keywords, per_kw=6):
    """按关键词搜近视频：[{bvid,title,author,desc,length,pubdate,link}]。"""
    out = []
    seen = set()
    for kw in keywords:
        if not kw:
            continue
        try:
            st, data, _ = _https_json(
                "api.bilibili.com",
                "/x/web-interface/search/type?search_type=video&keyword="
                + urllib.parse.quote(str(kw)[:40]) + "&order=pubdate&per_page=20",
                headers={"Referer": "https://www.bilibili.com/",
                         "Cookie": _cookie_header()})
            if st != 200 or (data or {}).get("code") != 0:
                continue
            for it in ((data.get("data") or {}).get("result") or [])[:per_kw]:
                bvid = str(it.get("bvid") or "")
                if not bvid or bvid in seen:
                    continue
                seen.add(bvid)
                title = re.sub(r"<[^>]+>", "", str(it.get("title") or ""))
                out.append({
                    "bvid": bvid,
                    "title": title[:80],
                    "author": str(it.get("author") or "")[:30],
                    "desc": str(it.get("description") or "")[:120],
                    "length": str(it.get("duration") or ""),
                    "pubdate": int(it.get("pubdate") or 0),
                    "link": "https://www.bilibili.com/video/%s" % bvid,
                })
        except Exception:
            continue
        if len(out) >= 24:
            break
    return out[:24]


# ---------------- LLM 兴趣判定（由 agent 注入回调，避免循环依赖） ----------------

_pick_fn = None  # agent 注入：fn(persona_digest, items) -> {"picks":[{idx, feeling}]}


def set_pick_handler(fn):
    global _pick_fn
    _pick_fn = fn


def pick_and_feel(persona_digest, items):
    if not _pick_fn or not items:
        return []
    try:
        res = _pick_fn(persona_digest, items)
        picks = res.get("picks") if isinstance(res, dict) else None
        if not isinstance(picks, list):
            return []
        valid = []
        for p in picks[:2]:
            if not isinstance(p, dict):
                continue
            try:
                idx = int(p.get("idx", -1))
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(items):
                valid.append((items[idx], str(p.get("feeling") or "")[:200]))
        return valid
    except Exception:
        return []


# ---------------- 分享 ----------------

def send_insite_dm(uid, text):
    """站内私信主人。成功（有 msg_key）返回 (True, msg)；失败返回 (False, 原因)。"""
    c = cookies()
    if not c:
        return False, "B 站未登录"
    text = str(text or "").strip()[:500]
    if not text or not str(uid).strip().isdigit():
        return False, "参数缺失（uid/text）"
    ts = int(time.time())
    payload = {
        "msg": json.dumps({
            "sender_uid": int(c.get("DedeUserID") or 0),
            "receiver_id": int(uid),
            "receiver_type": 1,
            "msg_type": 1,
            "msg_status": 0,
            "content": json.dumps({"content": text}, ensure_ascii=False),
            "timestamp": ts,
            "new_platform": 1,
            "msg_seq": int(ts % 100000),
        }, ensure_ascii=False),
        "from_filework": 0,
        "csrf_token": c.get("bili_jct", ""),
        "csrf": c.get("bili_jct", ""),
    }
    try:
        st, data, _ = _https_json(
            "api.vc.bilibili.com", "/web_im/v1/web_im/send_msg", method="POST",
            body=payload,
            headers={"Cookie": _cookie_header(),
                     "Referer": "https://message.bilibili.com/",
                     "Origin": "https://message.bilibili.com"})
        code = (data or {}).get("code")
        inner = (data or {}).get("data") or {}
        if st != 200 or code != 0:
            return False, "接口拒绝 code=%s %s" % (code, (data or {}).get("message", ""))
        if not inner.get("msg_key"):
            return False, "接口成功但无投递回执（可能对方不收陌生人私信/风控）"
        return True, "已投递 %s" % str(inner.get("msg_key"))[:12]
    except Exception as e:
        return False, repr(e)


# ---------------- 见闻 brief（注入聊天） ----------------

def brief_load():
    d = _read_json(BRIEF_PATH, {})
    return d if isinstance(d, dict) else {}


def brief_line(max_items=3):
    """注入聊天的"最近刷到"行（72 小时内的记录；无记录返回空串=零 token）。"""
    d = brief_load()
    items = d.get("items") if isinstance(d.get("items"), list) else []
    now = time.time()
    rows = []
    for it in items:
        if not isinstance(it, dict) or now - float(it.get("ts") or 0) > 72 * 3600:
            continue
        rows.append("- %s《%s》%s" % (
            str(it.get("author") or "UP主"),
            str(it.get("title") or "")[:40],
            ("（我的感想：%s）" % str(it.get("feeling") or "")[:80]) if it.get("feeling") else ""))
        if len(rows) >= max_items:
            break
    if not rows:
        return ""
    return ("\n\n## 你最近刷 B 站看到的视频（聊天时可以自然提起、当谈资；"
            "不是必须每次都提）\n" + "\n".join(rows))


def brief_append(items_with_feelings):
    d = brief_load()
    items = d.get("items") if isinstance(d.get("items"), list) else []
    for it, feeling in items_with_feelings:
        items.append({"title": it.get("title", ""), "author": it.get("author", ""),
                      "bvid": it.get("bvid", ""), "feeling": feeling,
                      "ts": time.time()})
    items = items[-30:]
    _write_json(BRIEF_PATH, {"items": items, "updated": time.time()})


def brief_clear():
    try:
        pathlib.Path(BRIEF_PATH).unlink()
    except OSError:
        pass
