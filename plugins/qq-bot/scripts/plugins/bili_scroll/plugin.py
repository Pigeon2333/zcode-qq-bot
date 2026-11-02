# -*- coding: utf-8 -*-
"""B 站刷视频插件：按周期以人设兴趣搜近期视频，见闻注入聊天，好物主动分享。
兴趣来源：用户关键词 + 按人设 LLM 生成的兴趣词（叠加）；都没有才回落内置池。
分享目标：B 站站内私信 / QQ（主人默认收），可加开"主人之外"的 UID/QQ/群。
链接感知（bilibili-mcp 只读能力移植）：消息里出现 BV/av 号或 b23.tv 短链时
自动带视频卡片进上下文；聊到"B站什么火"可注入当前热门（独立开关，默认关）。"""

import hashlib
import http.client
import json
import random
import re
import threading
import time
import urllib.parse

_BILI_STATE = {"last_window": 0.0}
_KEYWORDS = ("科技", "数码", "游戏", "美食", "音乐", "动画", "知识",
             "日常", "萌宠", "旅行")
# 兴趣词+避雷词缓存（6h；人设或相关记忆变更即失效）
_KW_CACHE = {"sig": "", "ts": 0.0, "kws": [], "avoid": []}
_INTEREST_HINTS = ("视频", "喜欢", "爱看", "不爱看", "讨厌", "追", "番",
                   "游戏", "剧", "安利", "up主", "主播", "音乐", "歌",
                   "电影", "看", "兴趣", "爱好")

# ---- 链接卡片 / 热门注入 ----
_BV_RE = re.compile(r"(BV[1-9A-HJ-NP-Za-km-z]{10})")
_AV_RE = re.compile(r"(?<![0-9A-Za-z])[Aa][Vv](\d{5,12})(?![0-9A-Za-z])")
_B23_RE = re.compile(r"https?://b23\.tv/[0-9A-Za-z]+")
_CARD_TTL = 3600          # 视频卡片缓存 1h
_HOT_TTL = 600            # 热门列表缓存 10min
_card_cache = {}          # vid -> {"ts":, "line":}
_hot_cache = {"ts": 0.0, "lines": []}
_last_seen = {}           # "ct:cid" -> {"ts":, "cards": [...], "hot": bool}
_HOT_WORDS = ("热门", "热榜", "什么火", "排行", "榜单", "火视频")

_api = None
_qb = None


def register(api):
    global _api
    _api = api
    api.on_tick(tick)
    api.inject_context(context_line)
    api.on_message(on_message)


def _mod():
    global _qb
    if _qb is None:
        import qq_bilibili as m
        _qb = m
    return _qb


def _persona_digest():
    try:
        import qq_bot_agent as A
        pc, _ = A.load_persona_collection()
        return ((pc.get("global") or "") + "\n" +
                str(A.load_json_const(A.PERSONA_COLLECTION_PATH, {}).get(
                    A.persona_for("private", "") or "_", "")))[:400] or \
            (pc.get("persona") or "")[:400]
    except Exception:
        return ""


def _memory_digest():
    """记忆里的兴趣素材：主人的全部近记忆 + 群记忆里带兴趣信号的
    （"主人说喜欢什么视频"这类话聊过就会进记忆流水，这里取来用）。"""
    import qq_bot_agent as A
    owner = str(A.owner_qq() or "")
    owner_lines, group_lines = [], []
    try:
        with open(A.MEMORY_STREAM_PATH, "r", encoding="utf-8") as f:
            for ln in f:
                try:
                    rec = json.loads(ln)
                except ValueError:
                    continue
                text = str(rec.get("text", ""))[:100]
                if not text:
                    continue
                if rec.get("scope") == "contact" and str(rec.get("id")) == owner:
                    owner_lines.append("主人：" + text)
                elif rec.get("scope") == "group" and any(
                        h in text for h in _INTEREST_HINTS):
                    group_lines.append("群聊：" + text)
    except Exception:
        pass
    return "\n".join(owner_lines[-30:] + group_lines[-20:])[:1000]


def _interest_words():
    """按人设 + 记忆生成 (搜索词, 避雷词)。
    LLM、缓存 6 小时（人设或相关记忆一变即失效）。
    失败/无人设无记忆返回 ([], [])——调用方回落内置池。"""
    digest = _persona_digest()
    memo = _memory_digest()
    if not digest.strip() and not memo.strip():
        return [], []
    sig = hashlib.sha256((digest + "\x00" + memo).encode("utf-8")).hexdigest()
    now = time.time()
    if (_KW_CACHE["sig"] == sig and _KW_CACHE["kws"]
            and now - _KW_CACHE["ts"] < 6 * 3600):
        return list(_KW_CACHE["kws"]), list(_KW_CACHE["avoid"])
    try:
        import qq_bot_agent as A
        prompt = (
            "下面是一个聊天机器人的人设：\n%s\n\n"
            "以及机器人记得的事（含主人喜好、群里聊过的兴趣话题）：\n%s\n\n"
            "综合人设本人的口味和大家（尤其主人）的兴趣，想 6 个适合在 B 站"
            "搜索的视频关键词。要具体到能搜出东西（如'复古相机'、"
            "'星际争霸2'、'广式早茶'），不要'视频''好看''有趣'这类泛词；"
            "口味单一就集中，别强行凑多样。另外，如果记忆里有明确不喜欢/"
            "反感的（不爱看某类、讨厌某 UP 等），列进 avoid 供挑选时排除。"
            "输出纯 JSON：{\"kws\":[\"词1\",\"词2\",...],\"avoid\":[\"...\"]}"
            % (digest[:1200], memo or "（暂无相关记忆）"))
        _m, text = A.call_glm_with_fallback(
            [{"role": "system", "content": "只输出符合要求的 JSON。"},
             {"role": "user", "content": prompt}], "")
        mm = re.search(r"\{.*\}", text, re.S)
        got = json.loads(mm.group(0)) if mm else {}
        kws, avoid = [], []
        for k in (got.get("kws") or [])[:8]:
            s = str(k).strip()
            if s and len(s) <= 12 and s not in kws:
                kws.append(s)
        for k in (got.get("avoid") or [])[:6]:
            s = str(k).strip()
            if s and len(s) <= 16 and s not in avoid:
                avoid.append(s)
        if len(kws) >= 2:
            _KW_CACHE.update(sig=sig, ts=now, kws=kws, avoid=avoid)
            _api.log("interest_kws=%s avoid=%s" % (",".join(kws), ",".join(avoid)))
            return kws, avoid
    except Exception as e:
        _api.log("INTEREST_KW_FAIL %r" % e)
    return [], []


def _pick(items):
    """LLM 兴趣判定（人设口味 + 记忆里的避雷线）：返回 [(item, feeling)]。"""
    try:
        import qq_bot_agent as A
        _, avoid = _interest_words()  # 缓存命中，不重复调 LLM
        avoid_line = ("\n已知主人/大家明确不喜欢的（这类不要挑）：%s。"
                      % "、".join(avoid)) if avoid else ""
        listing = "\n".join("%d. 《%s》 UP:%s 简介:%s 时长:%s" % (
            i, it.get("title", ""), it.get("author", ""), it.get("desc", "")[:60],
            it.get("length", "")) for i, it in enumerate(items))
        prompt = (
            "你是 B 站资深用户，正按自己的兴趣刷视频。你的人设与性格：\n%s\n%s\n\n"
            "候选视频：\n%s\n\n"
            "从这些视频里挑 0-2 个你（以人设的身份和喜好）真心感兴趣的，"
            "并为每个写一句不超过 40 字的、符合人设口吻的观后感想（像发弹幕/跟朋友安利，"
            "不要书面腔；没感觉就不硬挑）。输出纯 JSON："
            "{\"picks\":[{\"idx\":序号,\"feeling\":\"感想\"}]}，没有就 {\"picks\":[]}。"
            % (_persona_digest(), avoid_line, listing))
        _m, text = A.call_glm_with_fallback(
            [{"role": "system", "content": "只输出符合要求的 JSON。"},
             {"role": "user", "content": prompt}], "")
        mm = re.search(r"\{.*\}", text, re.S)
        if not mm:
            return []
        got = json.loads(mm.group(0))
        out = []
        for p in (got.get("picks") or [])[:2]:
            try:
                idx = int(p.get("idx", -1))
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(items):
                out.append((items[idx], str(p.get("feeling") or "")[:200]))
        return out
    except Exception as e:
        _api.log("PICK_FAIL %r" % e)
        return []


def _num_list(key, limit=10):
    """设置项里的数字列表（QQ号/群号/UID，逗号或中文逗号分隔）。"""
    raw = str(_api.setting(key, "") or "").replace("，", ",")
    out = []
    for s in raw.split(","):
        s = s.strip()
        if s.isdigit() and s not in out:
            out.append(s)
    return out[:limit]


def _flag(key, old_key=None):
    """读分享开关；新键未设置过时回落旧版键（升级迁移：share_bili_dm→
    share_bili_owner、share_qq→share_qq_owner 等）。"""
    v = _api.setting(key, None)
    if v is None and old_key:
        v = _api.setting(old_key, False)
    return bool(v)


def _share_targets():
    """解析分享目标：返回 (bili_uids, qq_users, qq_groups)。
    结构与插件页一致：每个渠道「主人 / 其他人」各自开关，群独立开关。"""
    try:
        import qq_bot_agent as A
        cfg = A.C.load_app_config()
        owner_uid = str(cfg.get("owner_bilibili_uid") or "").strip()
        owner_qq = str(A.owner_qq() or "").strip()
    except Exception:
        owner_uid = owner_qq = ""
    bili_uids = []
    if _flag("share_bili_owner", "share_bili_dm") and owner_uid:
        bili_uids.append(owner_uid)
    if _flag("share_bili_others"):
        bili_uids += [u for u in _num_list("share_bili_uids")
                      if u not in bili_uids]
    qq_users = []
    if _flag("share_qq_owner", "share_qq") and owner_qq:
        qq_users.append(owner_qq)
    if _flag("share_qq_others"):
        qq_users += [u for u in _num_list("share_qq_users")
                     if u not in qq_users]
    qq_groups = _num_list("share_qq_groups") if _flag("share_qq_group") else []
    return bili_uids, qq_users, qq_groups


def _share(daemon, picked):
    bili_uids, qq_users, qq_groups = _share_targets()
    if not (bili_uids or qq_users or qq_groups):
        _api.log("SHARE_SKIP no targets")
        return
    qb = _mod()
    for it, feeling in picked:
        text = ("刚刷到一个视频，觉得挺不错分享给你：%s《%s》 %s%s" % (
            it.get("author", ""), it.get("title", ""), it.get("link", ""),
            ("\n我的观后感：" + feeling) if feeling else ""))[:480]
        for uid in bili_uids:
            try:
                ok, msg = qb.send_insite_dm(uid, text)
                _api.log("DM %s ok=%s %s" % (uid, ok, msg))
            except Exception as e:
                _api.log("DM %s FAIL %r" % (uid, e))
        for qq in qq_users:
            try:
                daemon.send("private", qq, [text])
                _api.log("QQ_SHARE private/%s" % qq)
            except Exception as e:
                _api.log("QQ_SHARE private/%s FAIL %r" % (qq, e))
        for gid in qq_groups:
            try:
                daemon.send("group", gid, [text])
                _api.log("QQ_SHARE group/%s" % gid)
            except Exception as e:
                _api.log("QQ_SHARE group/%s FAIL %r" % (gid, e))


def tick(daemon):
    qb = _mod()
    if not qb.logged_in_uid():
        return  # 未登录 B 站：静默跳过（每窗口一次的提示也省了，避免刷日志）
    now = time.time()
    window_min = _api.setting("window_min", 180)
    try:
        window = max(30, int(window_min)) * 60
    except (TypeError, ValueError):
        window = 180 * 60
    if now - _BILI_STATE["last_window"] < window:
        return
    _BILI_STATE["last_window"] = now
    # 兴趣来源：用户关键词 + 按人设与记忆生成的兴趣（叠加去重）；
    # 两者都空（无人设无记忆/LLM 失败）才回落内置兴趣池
    user_kws = [k.strip() for k in
                str(_api.setting("keywords", "") or "").replace("，", ",").split(",")
                if k.strip()]
    persona_kws, _avoid = _interest_words()
    kws = user_kws + [k for k in persona_kws if k not in user_kws]
    if not kws:
        kws = list(_KEYWORDS)
    random.shuffle(kws)
    items = qb.search_videos(kws[:3])
    if not items:
        _api.log("搜索无结果 kws=%s" % ",".join(kws[:3]))
        return
    picked = _pick(items)
    qb.brief_append(picked if picked else items[:2])
    _api.log("items=%d picked=%d kws=%s" % (len(items), len(picked),
                                            ",".join(kws[:3])))
    if picked and _api.setting("share_enabled", False):
        _share(daemon, picked)


def context_line(chat_type, chat_id):
    qb = _mod()
    out = []
    e = _last_seen.get("%s:%s" % (chat_type, chat_id))
    if e and time.time() - e["ts"] < 300:
        if e.get("cards"):
            out.append("【消息里提到的B站视频】\n" + "\n".join(e["cards"]))
        if e.get("hot") and _hot_cache.get("lines"):
            out.append("【B站当前热门】\n" + "\n".join(_hot_cache["lines"]))
    brief = qb.brief_line()
    if brief:
        out.append(brief)
    return "\n".join(out)


# ---------------- 链接卡片 / 热门（bilibili-mcp 只读能力移植） ----------------

def _fmt_num(n):
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return "?"
    if n >= 100000000:
        return "%.1f亿" % (n / 100000000)
    if n >= 10000:
        return "%.1f万" % (n / 10000)
    return str(n)


def _api_get(path):
    """GET api.bilibili.com（带登录 Cookie 更稳；未登录也走公开接口）。"""
    qb = _mod()
    st, data, _ = qb._https_json(
        "api.bilibili.com", path,
        headers={"Referer": "https://www.bilibili.com/",
                 "Cookie": (qb._cookie_header() if qb.cookies() else "")})
    d = data if isinstance(data, dict) else {}
    if st != 200 or d.get("code") != 0:
        return None
    return d.get("data")


def _video_card(vid):
    """vid=BVxxx 或 av 数字。返回一行卡片（缓存 1h）。"""
    now = time.time()
    hit = _card_cache.get(vid)
    if hit and now - hit["ts"] < _CARD_TTL:
        return hit["line"]
    path = ("/x/web-interface/view?bvid=%s" % vid if vid.startswith("BV")
            else "/x/web-interface/view?aid=%s" % vid)
    d = _api_get(path)
    if not isinstance(d, dict) or not d.get("bvid"):
        return ""
    stat = d.get("stat") or {}
    desc = re.sub(r"\s+", " ", str(d.get("desc") or "")).strip()[:80]
    line = ("《%s》UP:%s 播放%s 赞%s 投币%s 收藏%s 弹幕%s 时长%s%s"
            % (str(d.get("title") or "?")[:60],
               str((d.get("owner") or {}).get("name") or "?")[:20],
               _fmt_num(stat.get("view")), _fmt_num(stat.get("like")),
               _fmt_num(stat.get("coin")), _fmt_num(stat.get("favorite")),
               _fmt_num(stat.get("danmaku")),
               str(d.get("duration") or "?"),
               ("｜简介:%s" % desc) if desc else ""))
    _card_cache[vid] = {"ts": now, "line": line}
    if len(_card_cache) > 50:
        for k in list(_card_cache)[:20]:
            _card_cache.pop(k, None)
    return line


def _resolve_b23(url):
    """跟随 b23.tv 短链重定向（≤3 跳，只许 bilibili.com 域），返回最终 URL。"""
    import ssl
    cur = url
    for _ in range(3):
        u = urllib.parse.urlsplit(cur)
        if not u.scheme.startswith("https") or not u.hostname:
            return ""
        if u.hostname != "b23.tv" and not u.hostname.endswith(".bilibili.com"):
            return ""
        try:
            conn = http.client.HTTPSConnection(
                u.hostname, u.port or 443, timeout=4,
                context=ssl.create_default_context())
            conn.request("GET", u.path or "/", headers={
                "User-Agent": "Mozilla/5.0", "Range": "bytes=0-0"})
            r = conn.getresponse()
            loc = r.getheader("Location") or ""
            r.read()
            conn.close()
            if not loc:
                return cur
            cur = loc if "://" in loc else "https://" + u.hostname + loc
        except OSError:
            return ""
    return cur


def _extract_vids(text):
    """从消息文本提取视频 id：BV 号 / av 号 / b23.tv 短链（解析后再提）。"""
    ids = list(_BV_RE.findall(text))
    ids += ["av%s" % a for a in _AV_RE.findall(text)]
    for u in list(_B23_RE.findall(text))[:2]:
        final = _resolve_b23(u)
        m = _BV_RE.search(final or "")
        if m:
            ids.append(m.group(1))
            continue
        m2 = _AV_RE.search(final or "")
        if m2:
            ids.append("av%s" % m2.group(1))
    return list(dict.fromkeys(ids))


def _hot_lines():
    """B站当前热门（popular 接口，公开）。缓存 10 分钟。"""
    now = time.time()
    if now - _hot_cache["ts"] < _HOT_TTL and _hot_cache["lines"]:
        return _hot_cache["lines"]
    d = _api_get("/x/web-interface/popular?ps=10&pn=1")
    lines = []
    for it in (d or {}).get("list") or []:
        st = it.get("stat") or {}
        lines.append("《%s》UP:%s 播放%s 赞%s"
                     % (str(it.get("title") or "?")[:50],
                        str((it.get("owner") or {}).get("name") or "?")[:16],
                        _fmt_num(st.get("view")), _fmt_num(st.get("like"))))
    if lines:
        _hot_cache.update(ts=now, lines=lines[:10])
    return _hot_cache.get("lines") or []


def on_message(msg):
    """消息观察（PluginAPI.on_message）：识别 BV/av/短链→预取卡片；
    'B站+热门/什么火'→刷新热门缓存。供 context_line 注入。"""
    text = str(msg.get("text") or "")
    if not text:
        return
    key = "%s:%s" % (msg.get("chat_type"), msg.get("chat_id"))
    entry = {"ts": time.time(), "cards": [], "hot": False}
    try:
        if _api.setting("link_card_enabled", True):
            for vid in _extract_vids(text)[:2]:
                card = _video_card(vid)
                if card:
                    entry["cards"].append(card)
            if entry["cards"]:
                _api.log("card x%d" % len(entry["cards"]))
        if (_api.setting("hot_inject_enabled", False)
                and ("b站" in text.lower() or "bilibili" in text.lower())
                and any(w in text for w in _HOT_WORDS)):
            entry["hot"] = True
            threading.Thread(target=_hot_lines, daemon=True).start()
    except Exception as e:
        _api.log("ONMSG_FAIL %r" % e)
    _last_seen[key] = entry
    if len(_last_seen) > 200:
        for k in list(_last_seen)[:100]:
            _last_seen.pop(k, None)
