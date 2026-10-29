# -*- coding: utf-8 -*-
"""B 站刷视频插件：按周期以人设兴趣搜近期视频，见闻注入聊天，好物私聊分享主人。
逻辑自 agent 内联代码迁入（bc93030）；QQ/站内私信只发主人，分享有独立开关。"""

import json
import random
import re
import time

_BILI_STATE = {"last_window": 0.0}
_KEYWORDS = ("科技", "数码", "游戏", "美食", "音乐", "动画", "知识",
             "日常", "萌宠", "旅行")
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


def _share(daemon, picked):
    try:
        import qq_bot_agent as A
        owner = A.owner_qq()
        cfg = A.C.load_app_config()
        uid = str(cfg.get("owner_bilibili_uid") or "").strip()
        qb = _mod()
        for it, feeling in picked:
            text = ("刚刷到一个视频觉得你会喜欢：%s《%s》 %s%s" % (
                it.get("author", ""), it.get("title", ""), it.get("link", ""),
                ("\n我的观后感：" + feeling) if feeling else ""))[:480]
            if uid:
                ok, msg = qb.send_insite_dm(uid, text)
                _api.log("DM ok=%s %s" % (ok, msg))
            if owner:
                try:
                    daemon.send("private", owner, [text])
                    _api.log("QQ_SHARE private/%s" % owner)
                except Exception as e:
                    _api.log("QQ_SHARE_FAIL %r" % e)
    except Exception as e:
        _api.log("SHARE_FAIL %r" % e)


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
    kws = [k.strip() for k in str(_api.setting("keywords", "") or "").split(",")
           if k.strip()] or list(_KEYWORDS)
    random.shuffle(kws)
    items = qb.search_videos(kws[:3])
    if not items:
        _api.log("搜索无结果 kws=%s" % ",".join(kws[:3]))
        return
    picked = _pick(items)
    qb.brief_append(picked if picked else items[:2])
    _api.log("items=%d picked=%d" % (len(items), len(picked)))
    if picked and _api.setting("share_enabled", False):
        _share(daemon, picked)


def context_line(chat_type, chat_id):
    qb = _mod()
    return qb.brief_line()
