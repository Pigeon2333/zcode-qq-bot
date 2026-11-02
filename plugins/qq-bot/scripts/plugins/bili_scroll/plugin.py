# -*- coding: utf-8 -*-
"""B 站刷视频插件：按周期以人设兴趣搜近期视频，见闻注入聊天，好物主动分享。
兴趣来源：用户关键词 + 按人设 LLM 生成的兴趣词（叠加）；都没有才回落内置池。
分享目标：B 站站内私信 / QQ（主人默认收），可加开"主人之外"的 UID/QQ/群。"""

import hashlib
import json
import random
import re
import time

_BILI_STATE = {"last_window": 0.0}
_KEYWORDS = ("科技", "数码", "游戏", "美食", "音乐", "动画", "知识",
             "日常", "萌宠", "旅行")
_KW_CACHE = {"sig": "", "ts": 0.0, "kws": []}  # 人设兴趣词缓存（6h/人设变更失效）
_api = None
_qb = None


def register(api):
    global _api
    _api = api
    api.on_tick(tick)
    api.inject_context(context_line)


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


def _persona_keywords():
    """按人设兴趣生成 B 站搜索词（LLM，缓存 6 小时且人设变更即失效）。
    失败/无人设返回 []——调用方回落内置池。"""
    digest = _persona_digest()
    if not digest.strip():
        return []
    sig = hashlib.sha256(digest.encode("utf-8")).hexdigest()
    now = time.time()
    if (_KW_CACHE["sig"] == sig and _KW_CACHE["kws"]
            and now - _KW_CACHE["ts"] < 6 * 3600):
        return list(_KW_CACHE["kws"])
    try:
        import qq_bot_agent as A
        prompt = (
            "下面是一个聊天机器人的人设：\n%s\n\n"
            "以这个人设本人的兴趣和口味，想 6 个适合在 B 站搜索的视频关键词。"
            "要具体到能搜出东西（如'复古相机'、'星际争霸2'、'广式早茶'），"
            "不要'视频''好看''有趣'这类泛词；口味单一就集中，别强行凑多样。"
            "输出纯 JSON：{\"kws\":[\"词1\",\"词2\",...]}" % digest[:1200])
        _m, text = A.call_glm_with_fallback(
            [{"role": "system", "content": "只输出符合要求的 JSON。"},
             {"role": "user", "content": prompt}], "")
        mm = re.search(r"\{.*\}", text, re.S)
        got = json.loads(mm.group(0)) if mm else {}
        kws = []
        for k in (got.get("kws") or [])[:8]:
            s = str(k).strip()
            if s and len(s) <= 12 and s not in kws:
                kws.append(s)
        if len(kws) >= 2:
            _KW_CACHE.update(sig=sig, ts=now, kws=kws)
            _api.log("persona_kws=%s" % ",".join(kws))
            return kws
    except Exception as e:
        _api.log("PERSONA_KW_FAIL %r" % e)
    return []


def _pick(items):
    """LLM 兴趣判定：返回 [(item, feeling)]。"""
    try:
        import qq_bot_agent as A
        listing = "\n".join("%d. 《%s》 UP:%s 简介:%s 时长:%s" % (
            i, it.get("title", ""), it.get("author", ""), it.get("desc", "")[:60],
            it.get("length", "")) for i, it in enumerate(items))
        prompt = (
            "你是 B 站资深用户，正按自己的兴趣刷视频。你的人设与性格：\n%s\n\n"
            "候选视频：\n%s\n\n"
            "从这些视频里挑 0-2 个你（以人设的身份和喜好）真心感兴趣的，"
            "并为每个写一句不超过 40 字的、符合人设口吻的观后感想（像发弹幕/跟朋友安利，"
            "不要书面腔；没感觉就不硬挑）。输出纯 JSON："
            "{\"picks\":[{\"idx\":序号,\"feeling\":\"感想\"}]}，没有就 {\"picks\":[]}。"
            % (_persona_digest(), listing))
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


def _share_targets():
    """解析分享目标：返回 (bili_uids, qq_users, qq_groups)。
    主人默认收（B站=owner_bilibili_uid / QQ=主人QQ，配置了才发）；
    列表里的其他人/群仅在 share_others 开启时计入。"""
    try:
        import qq_bot_agent as A
        cfg = A.C.load_app_config()
        owner_uid = str(cfg.get("owner_bilibili_uid") or "").strip()
        owner_qq = str(A.owner_qq() or "").strip()
    except Exception:
        owner_uid = owner_qq = ""
    others = bool(_api.setting("share_others", False))
    bili_uids, qq_users, qq_groups = [], [], []
    if _api.setting("share_bili_dm", False):
        bili_uids = [owner_uid] if owner_uid else []
        if others:
            bili_uids += [u for u in _num_list("share_bili_uids")
                          if u not in bili_uids]
    if _api.setting("share_qq", False):
        qq_users = [owner_qq] if owner_qq else []
        if others:
            qq_users += [u for u in _num_list("share_qq_users")
                         if u not in qq_users]
            qq_groups = _num_list("share_qq_groups")
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
    # 兴趣来源：用户关键词 + 人设生成的兴趣（叠加去重）；
    # 两者都空（无人设/LLM 失败）才回落内置兴趣池
    user_kws = [k.strip() for k in
                str(_api.setting("keywords", "") or "").replace("，", ",").split(",")
                if k.strip()]
    persona_kws = _persona_keywords()
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
    return qb.brief_line()
