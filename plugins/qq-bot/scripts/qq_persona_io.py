#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qq_persona_io.py — 人设导入/迁移指令生成（独立模块，被 qq_admin_ui 调用）

导入：支持两类来源（自适应识别）
  A. JSON 文件：{"名字": "人设文本", ...} 或 [{"name":..,"prompt":..},...] 等
     常见形态（dsh/deepseek-harness 等框架的人设导出）都尽量归一化
  B. Markdown / 纯文本：文件名或首个一级标题作为人设名，全文作为人设文本

迁移指令：根据本机当前配置生成一段可发给其他 Agent 的指令，
让它把那边的人设/记忆按格式写到本应用数据目录（零格式猜测）。
"""

import json
import os
import pathlib
import re

import qq_common as C

PERSONA_PATH = pathlib.Path(C.DATA_DIR) / "personas" / "collection.json"
MEMORY_PATH = pathlib.Path(C.DATA_DIR) / "memory_stream.jsonl"


def _load_collection():
    try:
        return json.loads(PERSONA_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_collection(data):
    PERSONA_PATH.parent.mkdir(parents=True, exist_ok=True)
    PERSONA_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _valid_name(name):
    n = str(name or "").strip()
    if not n or n.startswith("_") or len(n) > 40 or "\n" in n:
        return ""
    if re.search(r'[/:*?"<>|\\]', n):
        return ""
    return n


def parse_import(path):
    """解析导入文件 → [(name, text), ...]（≤20 条）。失败返回 ([] , err)。"""
    p = pathlib.Path(str(path))
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return [], "读取失败：%r" % e
    out = []
    # A. JSON
    st = raw.lstrip()
    if st.startswith("{") or st.startswith("["):
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if isinstance(data, dict):
            skip = {"_active", "global"}
            for k, v in data.items():
                if k in skip:
                    continue
                if isinstance(v, str) and v.strip():
                    out.append((k, v))
                elif isinstance(v, dict):
                    t = str(v.get("prompt") or v.get("text") or
                            v.get("persona") or v.get("content") or "").strip()
                    if t:
                        nm = str(v.get("name") or k)
                        out.append((nm, t))
        elif isinstance(data, list):
            for it in data:
                if not isinstance(it, dict):
                    continue
                t = str(it.get("prompt") or it.get("text") or
                        it.get("persona") or it.get("content") or
                        it.get("system_prompt") or "").strip()
                nm = str(it.get("name") or it.get("title") or
                         it.get("id") or "")
                if t and _valid_name(nm):
                    out.append((nm, t))
    # B. 文本（md/txt）：整文件一个人设
    if not out and raw.strip():
        m = re.search(r"^#\s+(.+)$", raw, re.M)
        name = m.group(1).strip() if m else p.stem
        out.append((name, raw.strip()))
    # 过滤与截断
    out = [(_valid_name(n), t[:20000]) for n, t in out]
    out = [(n, t) for n, t in out if n and t]
    if not out:
        return [], "没解析出任何人设（支持 JSON 字典/数组 或 Markdown/文本）"
    return out[:20], ""


def do_import(path, replace_active=True):
    """导入文件里的所有人设到 collection；返回 (导入数, 消息)。"""
    items, err = parse_import(path)
    if err:
        return 0, err
    data = _load_collection()
    added, skipped = 0, []
    for name, text in items:
        if name in data:
            skipped.append(name)
            continue
        data[name] = text
        added += 1
    if added and replace_active:
        data["_active"] = items[0][0]
    if added:
        _save_collection(data)
    msg = "导入 %d 个人设" % added
    if skipped:
        msg += "（跳过同名：%s）" % "、".join(skipped[:5])
    if added:
        msg += "，已激活「%s」（即时生效）" % items[0][0]
    return added, msg


def migrate_command():
    """生成发给其他 Agent（如 dsh 会话里）的一键迁移指令文本。"""
    coll = _load_collection()
    active = coll.get("_active", "")
    names = [k for k in coll if not k.startswith("_") and k != "global"]
    mem_hint = ""
    try:
        n_mem = sum(1 for _ in MEMORY_PATH.open("r", encoding="utf-8"))
    except OSError:
        n_mem = 0
    if n_mem:
        mem_hint = "\n2. 记忆（memory_stream.jsonl，每行一条 JSON）共 %d 条，同样格式追加。" % n_mem
    return (
        "请把我在这边的机器人人设与记忆迁移到「ZCode QQ机器人」。步骤：\n"
        "1. 把我当前使用的人设/角色卡全文（system prompt 原文）按下述格式写入文件：\n"
        "   路径：%s\n"
        "   格式：JSON 字典，键=人设名（如「%s」），值=人设全文；"
        "并加 \"_active\": \"人设名\" 指定激活项。已有内容先读出来合并，别覆盖。%s\n"
        "3. 写完告诉我，我会让机器人重启生效。\n"
        "注意：只迁移人设文本与记忆，不要迁移 API Key 等凭据。"
        % (str(PERSONA_PATH), active or (names[0] if names else "人设名"), mem_hint))
