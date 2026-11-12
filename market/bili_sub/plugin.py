# -*- coding: utf-8 -*-
"""B站订阅推送：订阅 UP 主，新视频/图文动态/直播开播自动转发到指定群。

数据来源（2026-11-12 实测）：
- 动态 feed/space：无 Cookie 一律 412 风控；带 Cookie（qq_bilibili 登录态）
  200 全量——视频(DYNAMIC_TYPE_AV)/图文(DRAW/WORD)/转发(FORWARD)/专栏
  (ARTICLE) 都在这一条流里，type 可区分勾选。
- 直播 get_status_info_by_uids：无 Cookie 批量 POST 可用（live_status
  0/1/2 + 标题 + 房间号），与动态接口互不依赖。

推送节流与防轰炸：新订阅首拉只记水位不推送；单轮单 UP 最多推 3 条；
动态请求间隔 ≥2s（多 UP 串行）；直播按 0→1 状态沿触发（下播不推）。
图片走 hdslb 下载到 cache/images（send_image 约定目录，宿主环形清理覆盖），
带 Referer 防盗链，单图 >4MB 跳过。
"""

import json
import os
import re
import threading
import time

DATA_DIR = os.environ.get("QQBOT_DATA_DIR") or os.path.join(
    os.path.expanduser("~"), ".zcode-qq-bot")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
       " (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
_MAX_PUSH_PER_ROUND = 3          # 单轮单 UP 最多推几条（防轰炸）
_DYN_GAP_SEC = 2.0               # 相邻 UP 动态请求间隔（防风控）
_IMG_MAX = 3                     # 图文动态最多带几张图
_IMG_MAX_BYTES = 4 * 1024 * 1024

_busy = {"on": False}            # 轮询重入锁（on_tick 起线程，这里防叠加）
_qb_cache = {"m": None}


def _qb():
    """宿主共享的 B站模块（Cookie/登录态；插件跑在 agent 进程可 import）。"""
    if _qb_cache["m"] is None:
        import qq_bilibili
        _qb_cache["m"] = qq_bilibili
    return _qb_cache["m"]


def _cookie():
    try:
        qb = _qb()
        return qb._cookie_header() if qb.cookies() else ""
    except Exception:
        return ""


# ---------------- HTTP（http.client 直连，Mimosa 友好形态） ----------------

def _https_json(host, path, method="GET", body=None, cookie=False):
    import http.client
    headers = {"User-Agent": _UA, "Accept": "application/json",
               "Referer": "https://www.bilibili.com/"}
    if cookie and _cookie():
        headers["Cookie"] = _cookie()
    if body is not None:
        body = body.encode("utf-8")
        headers["Content-Type"] = "application/json"
    conn = http.client.HTTPSConnection(host, timeout=20)
    try:
        conn.request(method, path, body=body, headers=headers)
        r = conn.getresponse()
        raw = r.read()
        return r.status, (json.loads(raw.decode("utf-8", "replace"))
                          if raw[:1] in (b"{", b"[") else None)
    finally:
        conn.close()


def _fetch_dyn(uid):
    """拉一页用户动态。返回 (items, err)；items 原样（最新在前）。"""
    st, d = _https_json(
        "api.bilibili.com",
        "/x/polymer/web-dynamic/v1/feed/space?host_mid=%s" % uid,
        cookie=True)
    if st != 200 or not isinstance(d, dict) or d.get("code") != 0:
        return None, "http=%s code=%s" % (st, (d or {}).get("code"))
    items = ((d.get("data") or {}).get("items")) or []
    return items, ""


def _fetch_live(uids):
    """批量直播状态。返回 {uid: {live_status,title,uname,room_id}} 或 None。"""
    st, d = _https_json("api.live.bilibili.com",
                        "/room/v1/Room/get_status_info_by_uids",
                        method="POST",
                        body=json.dumps({"uids": [int(u) for u in uids]}))
    if st != 200 or not isinstance(d, dict) or d.get("code") != 0:
        return None
    return d.get("data") or {}


def _fetch_name(uid):
    """uid → UP 名（card 接口，带 Cookie）。"""
    st, d = _https_json("api.bilibili.com",
                        "/x/web-interface/card?mid=%s" % uid, cookie=True)
    card = ((d or {}).get("data") or {}).get("card") or {}
    return str(card.get("name") or "") if st == 200 else ""


# ---------------- 动态解析 ----------------

def _parse_dyn(item):
    """一条动态 → {kind: av/draw, text, imgs, url}；不认识的类型返回 None。"""
    mods = (item or {}).get("modules") or {}
    md = mods.get("module_dynamic") or {}
    major = md.get("major") or {}
    author = str(((mods.get("module_author") or {}).get("name")) or "")
    desc = str(((md.get("desc") or {}).get("text")) or "").strip()
    t = item.get("type")
    did = str(item.get("id_str") or "")
    if t == "DYNAMIC_TYPE_AV":
        arc = major.get("archive") or {}
        title = str(arc.get("title") or "").strip()
        bvid = str(arc.get("bvid") or "")
        d2 = re.sub(r"\s+", " ", str(arc.get("desc") or "")).strip()[:70]
        return {"kind": "av",
                "text": "【B站新视频】%s 发布了新视频\n《%s》%s\nhttps://www.bilibili.com/video/%s"
                        % (author or "UP", title[:60],
                           ("\n简介：" + d2) if d2 else "", bvid)}
    if t in ("DYNAMIC_TYPE_DRAW", "DYNAMIC_TYPE_WORD", "DYNAMIC_TYPE_FORWARD",
             "DYNAMIC_TYPE_ARTICLE"):
        txt = desc
        imgs = []
        if t == "DYNAMIC_TYPE_DRAW":
            for it in ((major.get("draw") or {}).get("items") or [])[:_IMG_MAX]:
                u = str((it.get("src") or ""))
                if u.startswith("http"):
                    imgs.append(u)
        elif t == "DYNAMIC_TYPE_ARTICLE":
            art = major.get("article") or {}
            title = str(art.get("title") or "").strip()
            if title:
                txt = (txt + "\n《" + title[:50] + "》").strip()
        elif t == "DYNAMIC_TYPE_FORWARD":
            # 转发：拼被转发的摘要一行（ orig 里 major 归一处理成本高，
            # V1 取原文字段；拿不到就只发转发语）
            try:
                om = (((item.get("orig") or {}).get("modules") or {})
                      .get("module_dynamic") or {})
                odesc = str(((om.get("desc") or {}).get("text")) or "").strip()
                if odesc:
                    txt = (txt + "\n└ 转发自：" + odesc[:80]).strip()
            except Exception:
                pass
        if not txt and not imgs:
            return None
        return {"kind": "draw",
                "text": "【B站动态】%s：\n%s\nhttps://t.bilibili.com/%s"
                        % (author or "UP", (txt or "（无文字）")[:200], did),
                "imgs": imgs}
    return None


def _dl_img(url, name_base):
    """下载动态图到 cache/images（send_image 约定目录）。失败返回 None。"""
    import http.client
    import urllib.parse
    p = urllib.parse.urlsplit(url)
    host = p.hostname or "i0.hdslb.com"
    conn = http.client.HTTPSConnection(host, timeout=20)
    try:
        conn.request("GET", p.path + (("?" + p.query) if p.query else ""),
                     headers={"User-Agent": _UA,
                              "Referer": "https://www.bilibili.com/"})
        r = conn.getresponse()
        data = r.read()
        if r.status != 200 or len(data) > _IMG_MAX_BYTES:
            return None
    except Exception:
        return None
    finally:
        conn.close()
    ext = ".jpg"
    ddir = os.path.join(DATA_DIR, "cache", "images")
    try:
        os.makedirs(ddir, exist_ok=True)
        import pathlib
        path = pathlib.Path(ddir) / (name_base + ext)
        path.write_bytes(data)
        return str(path)
    except OSError:
        return None


# ---------------- 轮询主体 ----------------

def _api_send_text(api, chat_type, chat_id, texts):
    """PluginAPI.send 兼容层：0.5.15 及更早宿主的 api.send 存在
    方法名错配（send_texts vs send）从未工作过——这里两层回退，
    新旧宿主都能发。"""
    try:
        return api.send(chat_type, chat_id, texts)
    except AttributeError:
        daemon = getattr(api, "_host", None)
        daemon = getattr(daemon, "daemon", None)
        fn = getattr(daemon, "send", None)
        if fn is None:
            raise
        return fn(chat_type, chat_id, texts)


def _send_out(api, sub, text, imgs=None):
    for gid in (sub.get("groups") or []):
        try:
            _api_send_text(api, "group", str(gid), [text])
        except Exception as e:
            api.log("push fail group=%s %r" % (gid, e))
        for i, u in enumerate((imgs or [])[:_IMG_MAX]):
            p = _dl_img(u, "bili_%s_%d" % (sub.get("uid"), i))
            if p:
                try:
                    api.send_image("group", str(gid), p, caption="")
                except Exception:
                    pass


def _poll_dyn_one(api, sub, last, updates):
    """拉一个 UP 的动态并推送新增。返回新的水位（失败返回原水位）。"""
    uid = str(sub.get("uid"))
    items, err = _fetch_dyn(uid)
    if items is None:
        api.log("dyn fail uid=%s (%s)" % (uid, err))
        return last.get(uid)
    top = 0
    fresh = []
    for it in items:
        try:
            i = int(str(it.get("id_str") or "0"))
        except ValueError:
            continue
        top = max(top, i)
        if last.get(uid) and i > int(last[uid]):
            fresh.append(it)
    if not last.get(uid):
        # 新订阅：只记水位不推送（防历史轰炸）
        api.log("subscribe baseline uid=%s top=%d" % (uid, top))
    else:
        fresh.sort(key=lambda x: int(str(x.get("id_str") or "0")))
        for it in fresh[-_MAX_PUSH_PER_ROUND:]:
            parsed = _parse_dyn(it)
            if not parsed:
                continue
            if parsed["kind"] == "av" and not sub.get("av"):
                continue
            if parsed["kind"] == "draw" and not sub.get("draw"):
                continue
            _send_out(api, sub, parsed["text"], parsed.get("imgs"))
            api.log("push uid=%s kind=%s id=%s" % (uid, parsed["kind"],
                                                   it.get("id_str")))
    if top:
        updates[uid] = str(top)
        return str(top)
    return last.get(uid)


def _poll_live_all(api, subs, live_on):
    uids = [str(s.get("uid")) for s in subs if s.get("live")]
    if not uids:
        return
    info = _fetch_live(uids)
    if info is None:
        api.log("live fail (api)")
        return
    for s in subs:
        if not s.get("live"):
            continue
        uid = str(s.get("uid"))
        row = info.get(uid) or info.get(int(uid)) or {}
        on = 1 if row.get("live_status") == 1 else 0
        prev = int(live_on.get(uid) or 0)
        if on and not prev:
            room = row.get("room_id") or ""
            _send_out(api, s, "【B站直播】%s 开播了：\n「%s」\nhttps://live.bilibili.com/%s"
                      % (row.get("uname") or s.get("name") or "UP",
                         str(row.get("title") or "")[:60], room))
            api.log("push live uid=%s room=%s" % (uid, room))
        live_on[uid] = on


def _tick(api):
    if _busy["on"]:
        return
    try:
        interval = max(2, min(60, int(api.load("interval", 5))))
    except (TypeError, ValueError):
        interval = 5
    subs = api.load("subs", []) or []
    if not subs:
        api.store("next_poll", time.time() + interval * 60)
        return
    if time.time() < float(api.load("next_poll", 0) or 0):
        return
    _busy["on"] = True
    threading.Thread(target=_poll_worker, args=(api, subs, interval),
                     daemon=True).start()


def _poll_worker(api, subs, interval):
    try:
        last = dict(api.load("last_dyn", {}) or {})
        live_on = dict(api.load("live_on", {}) or {})
        updates = {}
        want_dyn = [s for s in subs if s.get("av") or s.get("draw")]
        for i, s in enumerate(want_dyn):
            if i:
                time.sleep(_DYN_GAP_SEC)
            _poll_dyn_one(api, s, last, updates)
        last.update(updates)
        api.store("last_dyn", last)
        _poll_live_all(api, subs, live_on)
        api.store("live_on", live_on)
    except Exception as e:
        api.log("poll error %r" % (e,))
    finally:
        api.store("next_poll", time.time() + interval * 60)
        _busy["on"] = False


# ---------------- 管理端点 ----------------

def _wl_groups():
    """白名单群 {gid: name}（读宿主白名单文件；读不到返回空）。"""
    try:
        import pathlib
        p = pathlib.Path(DATA_DIR) / "group_whitelist.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        return {str(g): "" for g in (d.get("groups") or [])}
    except (OSError, ValueError):
        return {}


def _norm_uid(raw):
    m = re.search(r"(?:space\.bilibili\.com/)?(\d{3,12})", str(raw or ""))
    return m.group(1) if m else ""


def register(api):
    api.on_tick(api.every(1, lambda d: _tick(api)))
    api.route("load_state", lambda args: {
        "ok": True,
        "subs": api.load("subs", []) or [],
        "interval": int(api.load("interval", 5)),
        "bili_login": bool(_cookie()),
        "wl_groups": _wl_groups(),
        "next_poll": float(api.load("next_poll", 0) or 0)})
    api.route("probe", lambda args: _probe(args))
    api.route("sub_add", lambda args: _sub_add(api, args))
    api.route("sub_del", lambda args: _sub_del(api, args))
    api.route("sub_update", lambda args: _sub_update(api, args))
    api.route("set_interval", lambda args: _set_interval(api, args))
    api.route("test_push", lambda args: _test_push(api, args))
    api.log("ready")


def _probe(args):
    uid = _norm_uid((args or {}).get("uid"))
    if not uid:
        return {"ok": False, "error": "识别不到 UID（支持纯数字或空间链接）"}
    name = _fetch_name(uid)
    if not name:
        return {"ok": False, "error": "拉取用户信息失败（B站未登录或接口风控）"}
    return {"ok": True, "uid": uid, "name": name}


def _sub_add(api, args):
    a = args or {}
    uid = _norm_uid(a.get("uid"))
    if not uid:
        return {"ok": False, "error": "识别不到 UID"}
    subs = api.load("subs", []) or []
    if any(str(s.get("uid")) == uid for s in subs):
        return {"ok": False, "error": "该 UP 已订阅"}
    groups = [str(g).strip() for g in (a.get("groups") or [])
              if str(g).strip()][:20]
    if not groups:
        return {"ok": False, "error": "至少选择一个目标群"}
    sub = {"uid": uid, "name": str(a.get("name") or "")[:40] or ("UID:%s" % uid),
           "av": 1 if a.get("av") else 0,
           "draw": 1 if a.get("draw") else 0,
           "live": 1 if a.get("live") else 0,
           "groups": groups}
    if not (sub["av"] or sub["draw"] or sub["live"]):
        return {"ok": False, "error": "至少勾选一类内容"}
    subs.append(sub)
    api.store("subs", subs)
    last = dict(api.load("last_dyn", {}) or {})
    last.pop(uid, None)          # 新订阅：下轮首拉只记水位
    api.store("last_dyn", last)
    api.log("sub add %s %s -> %s" % (uid, sub["name"], groups))
    return {"ok": True, "sub": sub}


def _sub_del(api, args):
    uid = _norm_uid((args or {}).get("uid"))
    subs = [s for s in (api.load("subs", []) or [])
            if str(s.get("uid")) != uid]
    api.store("subs", subs)
    return {"ok": True, "count": len(subs)}


def _sub_update(api, args):
    a = args or {}
    uid = _norm_uid(a.get("uid"))
    subs = api.load("subs", []) or []
    hit = False
    for s in subs:
        if str(s.get("uid")) != uid:
            continue
        hit = True
        for k in ("av", "draw", "live"):
            if k in a:
                s[k] = 1 if a.get(k) else 0
        if isinstance(a.get("groups"), list):
            s["groups"] = [str(g).strip() for g in a["groups"]
                           if str(g).strip()][:20]
    if not hit:
        return {"ok": False, "error": "订阅不存在"}
    api.store("subs", subs)
    return {"ok": True}


def _set_interval(api, args):
    try:
        n = max(2, min(60, int((args or {}).get("interval"))))
    except (TypeError, ValueError):
        return {"ok": False, "error": "间隔需为 2-60 的数字（分钟）"}
    api.store("interval", n)
    return {"ok": True, "interval": n}


def _test_push(api, args):
    gid = str((args or {}).get("group") or "").strip()
    if not gid.isdigit():
        return {"ok": False, "error": "群号非法"}
    try:
        _api_send_text(api, "group", gid,
                       ["【B站订阅推送】测试消息——插件工作正常，"
                        "订阅的 UP 有新内容时会像这样推送到本群。"])
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": repr(e)[:120]}
