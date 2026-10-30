# -*- coding: utf-8 -*-
"""群成员画像插件：每日增量更新成员画像（一次 LLM 调用），聊天时按发言者注入。
自 agent 内联 _member_profile_block/_member_profiles_review 迁入（0c8e407）。
数据文件 member_profiles.json 由 UI 记忆页共用管理。"""

import json
import os
import re
import time

DATA_DIR = os.environ.get("QQBOT_DATA_DIR") or os.path.join(
    os.path.expanduser("~"), ".zcode-qq-bot")
PROFILES_PATH = os.path.join(DATA_DIR, "member_profiles.json")
_API = None


def register(api):
    global _API
    _API = api
    api.on_tick(api.every(6 * 60, _review))
    api.inject_context(_block)


def _load():
    try:
        with open(PROFILES_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    m = d.get("members") if isinstance(d, dict) else None
    return m if isinstance(m, dict) else {}


def _row(qq):
    m = _load().get(str(qq)) or {}
    note = str(m.get("note") or "").strip()
    if not note:
        return ""
    name = str(m.get("name") or "").strip()
    return "- %s(QQ:%s)：%s" % (name or "（昵称未知）", qq, note[:150])


def _block(chat_type, chat_id, sender_id=None, sender_name=None):
    # 注入签名是 (ct, cid)；sender 信息从 agent 侧的包装传入（见 _profiles_bridge）
    if sender_id is None:
        return ""
    members = _load()
    if not members:
        return ""
    mine = _row(sender_id)
    others = []
    if chat_type == "group" and (_API is None or _API.setting("inject_others", True)):
        for qq, m in members.items():
            if str(qq) == str(sender_id):
                continue
            if str(chat_id) not in [str(g) for g in (m.get("groups") or [])]:
                continue
            r = _row(qq)
            if r:
                others.append(r)
            if len(others) >= 3:
                break
    if not mine and not others:
        return ""
    block = "\n\n## 群成员画像（你对这些人的长期了解，交流时可自然体现）"
    if mine:
        block += "\n### 发言者\n" + mine
    if others:
        block += "\n### 本群其他人\n" + "\n".join(others)
    return block


def _review(daemon):
    try:
        import qq_bot_agent as A
        members = _load()
        wl = A.load_json_const(A.GROUP_WL_PATH, {})
        gids = [str(g) for g in (wl.get("groups") or [])][:4] if isinstance(wl, dict) else []
        today = {}
        for gid in gids:
            try:
                hist = daemon.history("group", gid, limit=40) or []
            except Exception:
                continue
            for h in hist:
                qq = str(h.get("sender_id") or "")
                if not qq or h.get("is_bot") or len(qq) < 5:
                    continue
                ent = today.setdefault(qq, {"name": str(h.get("sender_name") or ""),
                                            "groups": set(), "lines": []})
                ent["groups"].add(gid)
                if len(ent["lines"]) < 4:
                    t = str(h.get("text") or "").strip()
                    if t:
                        ent["lines"].append(t[:60])
        if not today:
            return
        sample = []
        for qq, ent in sorted(today.items(), key=lambda kv: -len(kv[1]["groups"]))[:8]:
            cur = (members.get(qq) or {}).get("note", "")
            sample.append("QQ:%s 昵称:%s 已有画像:%s\n今天说过: %s"
                          % (qq, ent["name"], cur or "（无）",
                             " / ".join(ent["lines"]) or "（无文字）"))
        prompt = (
            "你是群聊观察助手。下面是今天活跃的群成员及其发言摘要。"
            "请为每个人写/更新一条长期画像（第三人称，不超过60字：身份特征、兴趣、"
            "和机器人相处注意事项等），只依据给出的事实，不要编造。"
            "输出纯 JSON 对象：{\"QQ号\": {\"name\": \"昵称\", \"note\": \"画像\"}}，"
            "只输出 JSON。\n\n" + "\n\n".join(sample))
        _m, text = A.call_glm_with_fallback(
            [{"role": "system", "content": "只输出符合要求的 JSON。"},
             {"role": "user", "content": prompt}], "")
        mm = re.search(r"\{.*\}", text, re.S)
        if not mm:
            return
        got = json.loads(mm.group(0))
        changed = 0
        for qq, info in got.items():
            qq = str(qq)
            if not qq.isdigit() or not isinstance(info, dict):
                continue
            note = str(info.get("note") or "").strip()[:150]
            if not note:
                continue
            old = members.get(qq) or {}
            members[qq] = {"name": str(info.get("name") or old.get("name") or ""),
                           "note": note,
                           "groups": sorted(set(
                               [str(g) for g in (old.get("groups") or [])] +
                               [str(g) for g in (today.get(qq) or {}).get("groups", ())])),
                           "updated": int(time.time())}
            changed += 1
        if changed:
            _save(members)
            _API.log("profiles +%d (total %d)" % (changed, len(members)))
    except Exception as e:
        if _API:
            _API.log("REVIEW_FAIL %r" % e)


def _save(members):
    import pathlib
    pathlib.Path(PROFILES_PATH).write_text(
        json.dumps({"members": members}, ensure_ascii=False, indent=2),
        encoding="utf-8")
