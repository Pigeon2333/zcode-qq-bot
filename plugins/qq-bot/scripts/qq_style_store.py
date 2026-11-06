# -*- coding: utf-8 -*-
"""风格卡按人设存储（0.5.10）：风格卡从全局单文件升级为人设附属。
- 存储：personas/collection.json 的 "_style:<人设名>" 键；"_style:_global_"=全局风格
- 兼容：无 per-persona 风格时回退旧 styles/current_style.md（老用户无缝）
- 副本：复制人设/风格做调试副本（"xxx 副本"），污染可快速切回
消费点统一走 persona_style(name)；UI 保存走 /api/style/save。"""

import json
import os
import re as _re

import qq_common as C

DATA_DIR = C.DATA_DIR
PERSONA_COLLECTION_PATH = os.path.join(DATA_DIR, "personas", "collection.json")
STYLE_PATH = os.path.join(DATA_DIR, "styles", "current_style.md")   # 旧版兼容读
_STYLE_KEY_PREFIX = "_style:"


def _read_const(path, default=""):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return default


def _load_coll():
    try:
        with open(PERSONA_COLLECTION_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_coll(data):
    import pathlib
    p = pathlib.Path(PERSONA_COLLECTION_PATH + ".tmp")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(p), PERSONA_COLLECTION_PATH)


def persona_style(persona_name):
    """某人设生效的风格卡文本：per-persona 覆盖 > 旧全局文件 > 空。
    全局风格（_style:_global_）不在此返回——它由 build_messages 叠加。"""
    data = _load_coll()
    v = data.get(_STYLE_KEY_PREFIX + str(persona_name or ""))
    if isinstance(v, str) and v.strip():
        return v
    return None


def global_style():
    """全局风格卡文本（叠加到所有人格；空串=未设置）。"""
    data = _load_coll()
    v = data.get(_STYLE_KEY_PREFIX + "_global_")
    if isinstance(v, str):
        return v
    # 兼容旧版：全局唯一风格文件视为全局风格（只在从未写过新格式时）
    if not any(k.startswith(_STYLE_KEY_PREFIX) for k in data.keys()):
        return _read_const(STYLE_PATH)
    return ""


def set_style(target, text):
    """写风格卡：target 为人设名或 "_global_"。空文本=清除该条（回退旧文件）。"""
    data = _load_coll()
    key = _STYLE_KEY_PREFIX + str(target or "_global_")
    if str(text or "").strip():
        data[key] = str(text)[:20000]
    else:
        data.pop(key, None)
    _save_coll(data)


def style_targets(coll=None):
    """所有风格目标：[("_global_", "全局通用设定"), (人设名, 人设名)...]。"""
    data = coll if isinstance(coll, dict) else _load_coll()
    out = [("_global_", "全局通用设定")]
    for k in sorted(data.keys()):
        if k.startswith("_"):
            continue
        out.append((k, k))
    return out


def duplicate_persona(name, new_name):
    """创建人设副本（含其 per-persona 风格）。返回 (ok, error)。"""
    name, new_name = str(name or "").strip(), str(new_name or "").strip()
    if not name or not new_name:
        return False, "源与副本名都要填"
    if new_name.startswith("_") or len(new_name) > 40 or "\n" in new_name:
        return False, "副本名非法（禁下划线开头，<=40 字，不含换行）"
    data = _load_coll()
    if new_name in data:
        return False, "「%s」已存在" % new_name
    if name not in data:
        return False, "源人设「%s」不存在" % name
    data[new_name] = data[name]
    src_style = data.get(_STYLE_KEY_PREFIX + name)
    if isinstance(src_style, str):
        data[_STYLE_KEY_PREFIX + new_name] = src_style
    _save_coll(data)
    return True, ""


def duplicate_style(target):
    """把旧全局风格文件初始化为新目标的 per-persona 风格（迁移辅助）。"""
    text = _read_const(STYLE_PATH)
    if str(text or "").strip():
        set_style(target, text)
    return True
