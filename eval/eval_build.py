# -*- coding: utf-8 -*-
"""从上下文注入日志冻结题库（短历史版：每个场景带当时提示词里的那段历史）。

用法：python eval_build.py [--days 8] [--out set_short.jsonl]
       [--n-at 15] [--n-reply 10] [--n-random 25]

数据源：~/.zcode-qq-bot/logs/context/ctx_YYYYMMDD.jsonl（每条记录含当时发给
模型的完整 system+user）。这里只取 user 段里的「最近对话」+「现在」还原成场景，
不保存当时的 system（两个版本都由各自的代码重新构造，保证只比较代码差异）。
"""
import argparse
import collections
import glob
import json
import os
import pathlib
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eval_common as EC  # noqa: E402

HIST_RE = re.compile(r"^- \[(\d{2}:\d{2})\] (.+?)\(QQ:(\d+)\): ?(.*)$", re.M)
SCENE_G_RE = re.compile(r"^【当前场景：群聊（群号 (.+?)，")
LEAD_AT_RE = re.compile(r"^(.+?)\(QQ:(\d+)\) (?:@你和QQ:[\d,]+|@你)说：(.*)$", re.S)
LEAD_AT_OTHER_RE = re.compile(r"^(.+?)\(QQ:(\d+)\) （@的是QQ:\d+，不是你）说：(.*)$", re.S)
LEAD_REPLY_RE = re.compile(r"^(.+?)\(QQ:(\d+)\) 回复(.+?)的消息「(.*?)」说：(.*)$", re.S)
LEAD_PLAIN_RE = re.compile(r"^(.+?)\(QQ:(\d+)\) 说：(.*)$", re.S)
LEAD_PRIVATE_RE = re.compile(r"^对方\(QQ:(\d+)\)对你说：(.*)$", re.S)
RT_SPLIT = "\n【被引用的原文】"
IMG_BODY = "（发了一张图/表情）"
BOT_NAMES = ("BOT", "你", "智谱娘")


def parse_record(rec):
    roles = rec.get("roles") or []
    if len(roles) < 2 or roles[1].get("role") != "user":
        return None
    content = str(roles[1].get("content") or "")
    head, sep, now = content.partition("\n\n【现在】")
    if not sep:
        return None
    chat_type, chat_id = (str(rec.get("chat") or "/").split("/", 1) + [""])[:2]
    is_group = chat_type == "group"
    chat_name = ""
    if is_group:
        m = SCENE_G_RE.match(head)
        if m:
            chat_name = m.group(1)
    base_ts = int(rec.get("ts") or 0)
    lt = time.localtime(base_ts)
    base_min = lt.tm_hour * 60 + lt.tm_min
    history = []
    for hhmm, name, qq, text in HIST_RE.findall(head):
        mo = int(hhmm[:2]) * 60 + int(hhmm[3:])
        ts = base_ts - ((base_min - mo) % 1440) * 60
        history.append({"ts": ts, "sender_id": qq, "sender_name": name, "text": text,
                        "is_bot": qq == EC.BOT_QQ})
    if is_group:
        m = LEAD_AT_RE.match(now)
        if m:
            lead, trigger, at_me = m, "at", True
        else:
            m = LEAD_AT_OTHER_RE.match(now)
            if m:
                lead, trigger, at_me = m, "chat_random", False
            else:
                m = LEAD_REPLY_RE.match(now)
                if m:
                    lead, trigger, at_me = m, "reply", False
                else:
                    m = LEAD_PLAIN_RE.match(now)
                    lead, trigger, at_me = (m if m else None), "chat_random", False
    else:
        m = LEAD_PRIVATE_RE.match(now)
        lead, trigger, at_me = m, "at", False   # 私聊一律视为对方在跟机器人说
    if not lead:
        return None
    if is_group:
        sender_name, sender_id = lead.group(1), lead.group(2)
        body = lead.group(lead.lastindex)
    else:
        sender_name, sender_id = str(rec.get("sender") or ""), lead.group(1)
        body = lead.group(2)
    reply_text, has_reply = "", False
    quoted_bot = False
    if trigger == "reply":
        quoted, reply_text0 = lead.group(3), lead.group(4)
        has_reply = True
        reply_text = reply_text0
        quoted_bot = quoted in BOT_NAMES or any(
            h["is_bot"] and reply_text0 and reply_text0[:12] in h["text"] for h in history)
        body = lead.group(5)
        if quoted_bot:
            trigger = "reply_to_bot"
    if RT_SPLIT in body:
        body, _rt = body.split(RT_SPLIT, 1)
    return {
        "source": "ctx", "chat_type": chat_type, "chat_id": chat_id, "chat_name": chat_name,
        "trigger": trigger,
        "msg": {"text": body.strip("\n"), "sender_id": sender_id, "sender_name": sender_name,
                "at_me": at_me, "has_reply": has_reply,
                "reply_text": reply_text if trigger == "reply_to_bot" else ""},
        "history": history,
        "persona_hint": rec.get("persona") or "",
        "rec_ts": base_ts,
    }


def usable(s):
    body = s["msg"]["text"].strip()
    if not body or body == IMG_BODY or EC.PLACEHOLDER_RE.match(body):
        return False
    if body.startswith("[分享]") or body.startswith("[卡片") or len(body) > 220:
        return False
    n_text = sum(1 for h in s["history"]
                 if not EC.PLACEHOLDER_RE.match(h["text"].strip()))
    return n_text >= 5


def pick(pool, n, key):
    """按会话轮转取样，保证场景分散在不同群。"""
    by_chat = collections.OrderedDict()
    for s in sorted(pool, key=lambda x: -x["rec_ts"]):
        by_chat.setdefault(s["chat_id"], []).append(s)
    out, seen = [], set()
    while len(out) < n and any(by_chat.values()):
        for cid in list(by_chat):
            lst = by_chat[cid]
            while lst:
                s = lst.pop(0)
                k = key(s)
                if k in seen:
                    continue
                seen.add(k)
                out.append(s)
                break
            if not lst:
                del by_chat[cid]
            if len(out) >= n:
                break
    return out[:n]


def dedupe_key(s):
    last = s["history"][-1]["text"][:30] if s["history"] else ""
    return (s["chat_id"], s["msg"]["sender_id"], s["msg"]["text"][:30], last)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=8)
    ap.add_argument("--out", default="set_short.jsonl")
    ap.add_argument("--n-at", type=int, default=15)
    ap.add_argument("--n-reply", type=int, default=10)
    ap.add_argument("--n-random", type=int, default=25)
    a = ap.parse_args()
    log_dir = pathlib.Path.home() / ".zcode-qq-bot" / "logs" / "context"
    files = sorted(glob.glob(str(log_dir / "ctx_*.jsonl")))[-a.days:]
    pool = {"at": [], "reply_to_bot": [], "chat_random": []}
    total = 0
    for fp in files:
        with open(fp, encoding="utf-8", errors="replace") as f:
            for ln in f:
                try:
                    rec = json.loads(ln)
                except ValueError:
                    continue
                total += 1
                s = parse_record(rec)
                if s and usable(s):
                    pool[s["trigger"]].append(s)
    print("记录 %d 条，文件 %d 个（%s … %s）" % (
        total, len(files), os.path.basename(files[0]), os.path.basename(files[-1])))
    print("可用场景：at=%d reply_to_bot=%d chat_random=%d" % (
        len(pool["at"]), len(pool["reply_to_bot"]), len(pool["chat_random"])))
    chosen = pick(pool["at"], a.n_at, dedupe_key)
    chosen += pick(pool["reply_to_bot"], a.n_reply, dedupe_key)
    chosen += pick(pool["chat_random"], a.n_random, dedupe_key)
    for i, s in enumerate(chosen, 1):
        s["id"] = "s%03d" % i
        s.pop("rec_ts", None)
        s["expect"] = {"speak": "yes" if s["trigger"] in ("at", "reply_to_bot") else "maybe"}
    out = EC.dump_jsonl(EC.EVAL_DIR / a.out, chosen)
    print("题库：%s（%d 个场景）" % (out, len(chosen)))
    print("按触发类型：", dict(collections.Counter(s["trigger"] for s in chosen)))
    print("按会话：", dict(collections.Counter(
        (s["chat_type"] + ":" + (s["chat_name"] or s["chat_id"])[-8:]) for s in chosen)))
    hl = [len(s["history"]) for s in chosen]
    print("每个场景的历史行数：最少 %d，中位 %d，最多 %d" % (min(hl), sorted(hl)[len(hl) // 2], max(hl)))


if __name__ == "__main__":
    main()
