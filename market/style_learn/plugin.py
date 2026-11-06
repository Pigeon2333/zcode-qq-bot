# -*- coding: utf-8 -*-
"""语气风格学习（活人感）：旁观学习指定群群友的真实说话习惯，蒸馏成
「活人感参考」注入机器人上下文。

设计要点（用户需求）：
- 学习对象三种模式：指定成员白名单 / 全员 / 黑名单之外
- 学习群独立于群消息白名单（可一键同步），支持只旁观学习不发言的群
- 目的是掌握「真人会怎么说话」的活人感，**不是模仿特定人**——样本去标识
  （只存文本不存昵称），蒸馏提示词明确禁止提取个人特征/引用原句
- 隐私：涉及分析群成员消息内容，默认关闭，独立开关
- 注入块精简（≤320 字），页面显示预计 token 消耗增幅
"""

import json
import os
import threading
import time

DATA_DIR = os.environ.get("QQBOT_DATA_DIR") or os.path.join(
    os.path.expanduser("~"), ".zcode-qq-bot")
GROUPS_PATH = os.path.join(DATA_DIR, "group_whitelist.json")

_BUF_CAP = 600          # 每群样本环形上限
_LEARN_MIN = 60         # 触发一次蒸馏所需的新样本数（可在页面调）
_LEARN_GAP = 3 * 3600   # 同群两次蒸馏最小间隔
_DISTILL_N = 150        # 每次蒸馏喂给模型的样本条数

_LOCK = threading.Lock()
_BUF = {}               # gid -> {"msgs": [text...], "new": int, "since": ts}
_FLUSH_TS = [0.0]


def register(api):
    api.on_observe(lambda m: _on_msg(api, m))
    api.on_tick(api.every(1, lambda d: _round(api)))
    api.inject_context(lambda ct, cid, **kw: _inject(api, ct, cid))
    api.route("load_cfg", lambda args: _load_cfg(api))
    api.route("save_cfg", lambda args: _save_cfg(api, args or {}))
    api.route("records", lambda args: {"ok": True,
                                       "records": api.load("records", [])})
    api.route("record_save", lambda args: _record_save(api, args or {}))
    api.route("record_del", lambda args: _record_del(api, args or {}))
    api.route("wl_groups", lambda args: _wl_groups(api))
    api.route("wl_sync", lambda args: _wl_sync(api, args or {}))
    api.route("estimate", lambda args: _estimate(api))
    api.route("learn_now", lambda args: _learn_now(api, args or {}))
    api.route("status", lambda args: _status(api))
    _restore(api)
    api.log("ready")


# ---------------- 配置 ----------------

def _cfg(api):
    c = api.load("cfg", None)
    if not isinstance(c, dict):
        c = {}
    c.setdefault("enabled", False)          # 隐私：默认关
    c.setdefault("inject_on", True)
    c.setdefault("mode", "all")             # all / members / blacklist
    c.setdefault("members", [])             # QQ 号字符串列表
    c.setdefault("blacklist", [])
    c.setdefault("groups", [])              # 学习群（独立于消息白名单）
    c.setdefault("learn_min", _LEARN_MIN)
    return c


def _load_cfg(api):
    c = _cfg(api)
    return {"ok": True, "cfg": c, "estimate": _estimate(api)}


def _save_cfg(api, args):
    c = _cfg(api)
    c["enabled"] = bool(args.get("enabled"))
    c["inject_on"] = bool(args.get("inject_on", True))
    mode = str(args.get("mode") or "all")
    c["mode"] = mode if mode in ("all", "members", "blacklist") else "all"
    c["members"] = _qq_list(args.get("members"))
    c["blacklist"] = _qq_list(args.get("blacklist"))
    c["groups"] = _qq_list(args.get("groups"))
    try:
        c["learn_min"] = max(20, min(500, int(args.get("learn_min") or _LEARN_MIN)))
    except (TypeError, ValueError):
        c["learn_min"] = _LEARN_MIN
    api.store("cfg", c)
    return {"ok": True, "cfg": c, "estimate": _estimate(api)}


def _qq_list(v):
    if isinstance(v, str):
        v = v.replace("，", ",").replace("、", ",").replace("\n", ",").split(",")
    out, seen = [], set()
    for x in (v or []):
        s = str(x).strip()[:20]
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out[:60]


# ---------------- 样本采集（全量观察钩子） ----------------

_SKIP_PATTERNS = ("[图片]", "[表情包]", "[语音]", "[戳了一戳]", "http://", "https://")


def _on_msg(api, msg):
    c = _cfg(api)
    if not c.get("enabled") or msg.get("chat_type") != "group":
        return
    gid = str(msg.get("chat_id") or "")
    if gid not in c.get("groups", []):
        return
    sid = str(msg.get("sender_id") or "")
    mode = c.get("mode", "all")
    if mode == "members" and sid not in c.get("members", []):
        return
    if mode == "blacklist" and sid in c.get("blacklist", []):
        return
    text = str(msg.get("text") or "").strip()
    if not (2 <= len(text) <= 80):
        return
    if any(text.startswith(p) for p in _SKIP_PATTERNS) or text.startswith("/"):
        return
    # 纯数字/纯标点（验证码、接龙编号类）没有语气价值
    if not any(ch.isalpha() or "\u4e00" <= ch <= "\u9fff" for ch in text):
        return
    # 去标识：只存文本，不存昵称/QQ（蒸馏时模型无从知道是谁说的）
    with _LOCK:
        b = _BUF.setdefault(gid, {"msgs": [], "new": 0, "since": time.time()})
        b["msgs"].append(text)
        if len(b["msgs"]) > _BUF_CAP:
            del b["msgs"][:len(b["msgs"]) - _BUF_CAP]
        b["new"] += 1


def _restore(api):
    """agent 重启后恢复持久化的样本缓冲（学习进度不丢）。"""
    saved = api.load("buf", {})
    if isinstance(saved, dict):
        with _LOCK:
            for gid, b in saved.items():
                if isinstance(b, dict):
                    _BUF[str(gid)] = {"msgs": list(b.get("msgs") or [])[-_BUF_CAP:],
                                      "new": int(b.get("new") or 0),
                                      "since": float(b.get("since") or time.time())}


def _persist(api):
    with _LOCK:
        snap = {g: {"msgs": b["msgs"][-200:], "new": b["new"], "since": b["since"]}
                for g, b in _BUF.items()}
    api.store("buf", snap)


# ---------------- 蒸馏（LLM 提炼活人感共性） ----------------

_LEARN_SYS = (
    "你是聊天行为分析师。输入是某个QQ群里真实群友们（已完全去标识，只有文本行、"
    "没有昵称，行序随机）的聊天摘样。任务：总结这群真人聊天体现出的「活人感特征」"
    "——即怎样说话才像活人。观察维度：句长分布、语气词与标点习惯、口语化程度"
    "（缩写/错字/中英混排）、表情梗的使用密度、话题切换与接话方式、情绪表达方式。"
    "输出要求：\n"
    "- 4-6 条，每条一行，以「- 」开头\n"
    "- 每条要具体、可执行（例：短句为主，多数消息 15 字以内，很少写长句）\n"
    "- 只描述这群人的**共性习惯**，绝对不要描述任何具体个人、不要出现任何昵称\n"
    "- 绝对不要引用或近似复述样本里的原句\n"
    "- 这是给 AI 聊天助手的说话质感参考（让她更像活人），不是模仿任何人的说明书\n"
    "- 直接输出清单，不要开场白和说明"
)


def _round(api):
    """每分钟：持久化缓冲 + 检查各群是否够蒸馏。蒸馏开线程（LLM 调用秒级）。"""
    c = _cfg(api)
    now = time.time()
    if now - _FLUSH_TS[0] > 30:
        _FLUSH_TS[0] = now
        _persist(api)
    if not c.get("enabled"):
        return
    learn_state = api.load("learn_state", {})
    try:
        learn_min = max(20, int(c.get("learn_min") or _LEARN_MIN))
    except (TypeError, ValueError):
        learn_min = _LEARN_MIN
    for gid in c.get("groups", []):
        with _LOCK:
            b = _BUF.get(gid)
            n_new = b["new"] if b else 0
        st = learn_state.get(gid) or {}
        if n_new >= learn_min and now - float(st.get("ts") or 0) >= _LEARN_GAP:
            threading.Thread(target=_distill, args=(api, gid), daemon=True).start()


def _learn_now(api, args):
    c = _cfg(api)
    gid = str((args or {}).get("gid") or "").strip()
    if gid not in c.get("groups", []):
        return {"ok": False, "error": "该群不在学习群列表里"}
    with _LOCK:
        n = len((_BUF.get(gid) or {}).get("msgs") or [])
    if n < 10:
        return {"ok": False, "error": "样本太少（%d 条，至少攒 10 条再学）" % n}
    threading.Thread(target=_distill, args=(api, gid), daemon=True).start()
    return {"ok": True, "started": True, "samples": n}


def _gname(api, gid):
    try:
        daemon = api._host.daemon
        names = daemon.group_names() if daemon else {}
        return str((names or {}).get(gid) or "")
    except Exception:
        return ""


def _distill(api, gid):
    """把样本喂给模型蒸馏活人感共性 → 更新该群的风格记录。"""
    with _LOCK:
        b = _BUF.get(gid)
        if not b or len(b["msgs"]) < 10:
            return
        sample = list(b["msgs"][-_DISTILL_N:])
    try:
        import qq_bot_agent as A
        msgs = [{"role": "system", "content": _LEARN_SYS},
                {"role": "user", "content": "\n".join(sample)[:5000]}]
        _m, reply = A.call_glm_with_fallback(msgs, "")
        lines = [ln.strip().lstrip("-•·").strip()[:70]
                 for ln in str(reply or "").splitlines()]
        lines = [ln for ln in lines if 6 <= len(ln) <= 70][:8]
        if len(lines) < 3:
            api.log("distill %s: only %d lines, skip" % (gid, len(lines)))
            return
    except Exception as e:
        api.log("distill %s fail %r" % (gid, e))
        return
    traits = "\n".join("- " + ln for ln in lines)
    recs = [r for r in api.load("records", []) if str(r.get("id")) != gid]
    recs.insert(0, {"id": gid, "gid": gid, "gname": _gname(api, gid),
                    "traits": traits, "samples": len(sample),
                    "created": time.time(), "updated": time.time()})
    api.store("records", recs[:40])
    learn_state = api.load("learn_state", {})
    learn_state[gid] = {"ts": time.time()}
    api.store("learn_state", learn_state)
    with _LOCK:
        b = _BUF.get(gid)
        if b:
            b["msgs"] = b["msgs"][-20:]   # 留少量种子，其余已消化
            b["new"] = 0
    _persist(api)
    api.log("distilled %s from %d samples (%d traits)"
            % (gid, len(sample), len(lines)))


# ---------------- 注入与估算 ----------------

def _inject(api, chat_type, chat_id, **kw):
    c = _cfg(api)
    if not c.get("enabled") or not c.get("inject_on") or chat_type != "group":
        return ""
    if str(chat_id) not in c.get("groups", []):
        return ""
    for r in api.load("records", []):
        if str(r.get("gid")) == str(chat_id):
            traits = str(r.get("traits") or "").strip()[:320]
            if traits:
                return ("\n\n## 活人感参考（学自本群群友的真实聊天习惯——是说话质感的"
                        "参考，让你知道真人怎么说话；**不是模仿任何人**，绝不照抄"
                        "句式或堆砌口头禅，融合进你自己的语气）\n" + traits)
    return ""


def _estimate(api):
    """注入块 token 估算：中文 ≈ chars/1.7；按典型 system prompt 4500 token
    基数算增幅。另给每次蒸馏的固定成本（输入样本+输出特征）。"""
    c = _cfg(api)
    recs = {str(r.get("gid")): r for r in api.load("records", [])}
    active = [g for g in c.get("groups", []) if g in recs]
    chars = sum(len(str(recs[g].get("traits") or "")) + 60 for g in active)
    inject_tokens = int(chars / 1.7)
    base = 4500
    pct = round(inject_tokens * 100.0 / base, 1)
    with _LOCK:
        pending = {g: (b["new"] if (b := _BUF.get(g)) else 0) for g in c.get("groups", [])}
    return {"ok": True, "active_groups": len(active), "inject_tokens": inject_tokens,
            "base_tokens": base, "pct": pct,
            "learn_cost_per_run": "约 1.0-1.5k token/次（每群每 3 小时至多一次）",
            "pending": pending}


def _status(api):
    c = _cfg(api)
    learn_state = api.load("learn_state", {})
    out = []
    for gid in c.get("groups", []):
        with _LOCK:
            b = _BUF.get(gid)
            n, n_new = (len(b["msgs"]), b["new"]) if b else (0, 0)
        out.append({"gid": gid, "gname": _gname(api, gid), "buffered": n,
                    "new": n_new, "last_learn": float((learn_state.get(gid) or {}).get("ts") or 0),
                    "learn_min": c.get("learn_min")})
    return {"ok": True, "groups": out}


# ---------------- 风格库 CRUD（页面直管，帮助清理无用风格） ----------------

def _record_save(api, args):
    gid = str((args or {}).get("gid") or "").strip()[:20]
    traits = str((args or {}).get("traits") or "").strip()[:1200]
    if not gid or not traits:
        return {"ok": False, "error": "群号和特征内容都要填"}
    recs = [r for r in api.load("records", []) if str(r.get("id")) != gid]
    recs.insert(0, {"id": gid, "gid": gid, "gname": str((args or {}).get("gname") or "")[:40],
                    "traits": traits, "samples": int((args or {}).get("samples") or 0),
                    "created": float((args or {}).get("created") or time.time()),
                    "updated": time.time(), "manual": True})
    api.store("records", recs[:40])
    return {"ok": True}


def _record_del(api, args):
    gid = str((args or {}).get("id") or "").strip()[:20]
    recs = [r for r in api.load("records", []) if str(r.get("id")) != gid]
    api.store("records", recs)
    return {"ok": True}


# ---------------- 群消息白名单联动 ----------------

def _wl_groups(api):
    """读群消息白名单（供页面展示/勾选）；群名走 daemon 缓存。"""
    try:
        with open(GROUPS_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        gids = [str(g) for g in (d.get("groups") or []) if str(g).strip()]
    except (OSError, ValueError):
        return {"ok": False, "error": "群消息白名单读取失败"}
    names = {}
    try:
        daemon = api._host.daemon
        if daemon:
            names = daemon.group_names() or {}
    except Exception:
        pass
    return {"ok": True, "groups": [{"id": g, "name": str(names.get(g) or "")} for g in gids]}


def _wl_sync(api, args):
    """一键从群消息白名单同步到学习群（默认合并；replace=true 整体替换）。"""
    try:
        with open(GROUPS_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        wl = [str(g) for g in (d.get("groups") or []) if str(g).strip()]
    except (OSError, ValueError):
        return {"ok": False, "error": "群消息白名单读取失败"}
    c = _cfg(api)
    if (args or {}).get("replace"):
        c["groups"] = wl[:60]
    else:
        merged = list(c.get("groups", []))
        for g in wl:
            if g not in merged:
                merged.append(g)
        c["groups"] = merged[:60]
    api.store("cfg", c)
    return {"ok": True, "groups": c["groups"], "added": len(c["groups"])}
