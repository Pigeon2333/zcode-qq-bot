# -*- coding: utf-8 -*-
"""GPT-SoVITS 微调模型管理（方案A：社区训练包导入）。
模型存放：引擎 GPT_SoVITS/pretrained_models/ft_models/ft_<id>/ 内
  model.ckpt + model.pth + ref.wav；索引在 DATA_DIR/gsv_ft_models.json。
切模型 = 改引擎 tts_infer.yaml 权重路径 + 重启引擎加载。
安全边界：源目录只读；写入全部限定引擎 ft_models 子树与引擎 yaml；
模型 id 白名单 ft+hex8；目录级删除按 dirname 精确匹配受控根。"""

import glob as _glob
import io as _io
import json
import os
import re as _re
import shutil as _sh
import time
import uuid
import wave as _wave

import qq_common as C
from qq_tts import gsv_dir, gsv_start, gsv_stop, save_voice_cfg, voice_cfg

FT_DIR = "ft_models"
FT_JSON = os.path.join(C.DATA_DIR, "gsv_ft_models.json")
FT_ID_RE = _re.compile(r"ft[0-9a-f]{8}\Z")
PRESET_T2S = ("GPT_SoVITS/pretrained_models/gsv-v2final-pretrained/"
              "s1bert25hz-5kh-longer-epoch=12-step=369668.ckpt")
PRESET_VITS = "GPT_SoVITS/pretrained_models/gsv-v2final-pretrained/s2G2333k.pth"


def engine_paths(cfg):
    root = gsv_dir(cfg)
    return {
        "engine": root,
        "yaml": root + "/GPT_SoVITS/configs/tts_infer.yaml",
        "models": root + "/GPT_SoVITS/pretrained_models/" + FT_DIR,
    }


def load_index():
    try:
        d = json.loads(open(FT_JSON, encoding="utf-8").read())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_index(d):
    # FT_JSON 是模块常量（DATA_DIR 下固定名），无外部输入；用 pathlib 校验后写
    import pathlib as _pl
    p = _pl.Path(C.DATA_DIR) / "gsv_ft_models.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def ft_model_list(cfg=None):
    cfg = cfg or voice_cfg()
    cur = str(cfg.get("gsv_ft_model") or "")
    out = []
    for mid, m in sorted(load_index().items()):
        ref = str(m.get("ref") or "")
        out.append({
            "id": mid, "name": m.get("name", ""),
            "created": m.get("created", 0),
            "has_ref": bool(ref) and os.path.isfile(ref),
            "prompt_text": m.get("prompt_text", ""),
            "prompt_lang": m.get("prompt_lang", "zh"),
            "active": mid == cur,
        })
    return {"models": out, "active": cur, "using_ft": bool(cur)}


def _wav_seconds(path):
    try:
        raw = open(path, "rb").read()
        w = _wave.open(_io.BytesIO(raw))
        secs = w.getnframes() / max(1, w.getframerate())
        w.close()
        return secs
    except Exception:
        return -1.0


def _dir_wavs(folder):
    """folder 内（仅本层）的 wav 文件列表。"""
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return [folder + "/" + n for n in sorted(names) if n.lower().endswith(".wav")]


def _pick_ref_wav(src_dir):
    """源目录（含单层子目录）里找一条 3-10 秒 wav 作默认参考。"""
    cands = _dir_wavs(src_dir)
    for sub in sorted(os.listdir(src_dir)) if os.path.isdir(src_dir) else []:
        subp = src_dir + "/" + sub
        if os.path.isdir(subp):
            cands += _dir_wavs(subp)
    for cand in cands:
        if 3.0 <= _wav_seconds(cand) <= 10.0:
            return cand
    return None


def _find_model_pair(src):
    """src 目录本层里的 (.ckpt, .pth) 各一个（sorted 取首）。"""
    try:
        names = os.listdir(src)
    except OSError:
        return [], []
    cks = [src + "/" + n for n in sorted(names) if n.lower().endswith(".ckpt")]
    pts = [src + "/" + n for n in sorted(names) if n.lower().endswith(".pth")]
    return cks, pts


def ft_model_import(cfg, name, src_dir, ref_wav=None, prompt_text="", prompt_lang="zh"):
    """导入一套微调模型：src_dir 含 .ckpt+.pth（社区包原样）。
    ref_wav 可显式指定参考音频；不指定则自动挑 3-10 秒的一条。
    模型复制进引擎 ft_models 子树；索引写 gsv_ft_models.json。"""
    root = gsv_dir(cfg)
    if not root or not os.path.isdir(root):
        return {"ok": False, "error": "本地引擎未安装（先按引导安装整合包）"}
    expanded = os.path.expanduser(str(src_dir or "").strip())
    src = os.path.normpath(os.path.abspath(expanded))
    if not src or not os.path.isdir(src):
        return {"ok": False, "error": "源文件夹不存在"}
    ck, pt = _find_model_pair(src)
    if not ck or not pt:
        return {"ok": False, "error": "文件夹里没找到 .ckpt 与 .pth 模型对"
                                      "（微调模型包应同时含这两个文件）"}
    if os.path.getsize(ck[0]) < 10 * 1024 * 1024:
        return {"ok": False, "error": ".ckpt 文件异常（小于 10MB，不像微调模型）"}
    ref = ""
    if ref_wav and os.path.isfile(str(ref_wav)):
        ref = os.path.normpath(os.path.abspath(str(ref_wav)))
    else:
        picked = _pick_ref_wav(src)
        ref = picked if picked else ""
    mid = "ft" + uuid.uuid4().hex[:8]
    dst = engine_paths(cfg)["models"] + "/" + mid
    os.makedirs(dst, exist_ok=True)
    try:
        _sh.copy2(ck[0], dst + "/model.ckpt")
        _sh.copy2(pt[0], dst + "/model.pth")
        if ref:
            _sh.copy2(ref, dst + "/ref.wav")
    except OSError as e:
        return {"ok": False, "error": "复制模型失败：%r" % e}
    data = load_index()
    data[mid] = {"name": str(name or "微调音色")[:40],
                 "dir": dst,
                 "ref": (dst + "/ref.wav") if ref else "",
                 "prompt_text": str(prompt_text or "")[:300],
                 "prompt_lang": str(prompt_lang or "zh")[:8],
                 "created": time.time()}
    save_index(data)
    return {"ok": True, "id": mid, "has_ref": bool(ref)}


def ft_model_write_yaml(cfg, mid):
    """引擎 tts_infer.yaml 的 t2s/vits 指向微调模型（mid 空=回退预设）。
    写入值由常量模板 + 白名单 id 拼出，不含任何用户路径输入。"""
    yaml_path = engine_paths(cfg)["yaml"]
    if not os.path.isfile(yaml_path):
        return {"ok": False, "error": "引擎配置文件不存在"}
    if mid:
        if not FT_ID_RE.fullmatch(str(mid)):
            return {"ok": False, "error": "模型 id 非法"}
        if mid not in load_index():
            return {"ok": False, "error": "微调模型不存在"}
        t2s = "GPT_SoVITS/pretrained_models/" + FT_DIR + "/" + mid + "/model.ckpt"
        vits = "GPT_SoVITS/pretrained_models/" + FT_DIR + "/" + mid + "/model.pth"
    else:
        t2s, vits = PRESET_T2S, PRESET_VITS
    content = open(yaml_path, encoding="utf-8").read()
    content = _re.sub(r"(t2s_weights_path:\s*)(\S+)",
                      lambda mm: mm.group(1) + t2s, content, count=1)
    content = _re.sub(r"(vits_weights_path:\s*)(\S+)",
                      lambda mm: mm.group(1) + vits, content, count=1)
    import pathlib as _pl2
    _pl2.Path(yaml_path).write_text(content, encoding="utf-8")
    return {"ok": True, "t2s": t2s, "vits": vits}


def ft_model_use(cfg, mid):
    """启用微调模型（写 yaml + 记配置 + 重启引擎加载）。mid 空=回退预设。"""
    r = ft_model_write_yaml(cfg, mid)
    if not r.get("ok"):
        return r
    c = dict(voice_cfg())
    c["gsv_ft_model"] = str(mid or "")
    c["gsv_ft_ts"] = time.time()
    save_voice_cfg(c)
    gsv_stop()
    st = gsv_start(c)
    m = load_index().get(mid) or {}
    return {"ok": True,
            "name": (m.get("name", "预设模型") if mid else "预设模型"),
            "engine": {"ok": st.get("ok"), "error": st.get("error", "")},
            "hint": "引擎正在重启加载模型（约 40-90 秒），就绪后即可合成"}


def ft_model_del(cfg, mid):
    """删除微调模型（当前启用时先回退预设并重启引擎）。
    只删 dirname 恰等于受控根的 ft_models/<mid> 一级目录。"""
    if not FT_ID_RE.fullmatch(str(mid or "")):
        return {"ok": False, "error": "模型 id 非法"}
    data = load_index()
    m = data.get(mid)
    if not m:
        return {"ok": False, "error": "微调模型不存在"}
    was_active = str(voice_cfg().get("gsv_ft_model") or "") == mid
    if was_active:
        ft_model_use(cfg, "")
    target = str(m.get("dir") or "")
    ctrl_root = engine_paths(cfg)["models"]
    if target and os.path.isdir(target) and os.path.dirname(target) == ctrl_root:
        _sh.rmtree(target, ignore_errors=True)
    data.pop(mid, None)
    save_index(data)
    return {"ok": True, "was_active": was_active}
