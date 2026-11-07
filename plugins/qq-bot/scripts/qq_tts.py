# -*- coding: utf-8 -*-
"""qq_tts.py — 语音回复引擎（宿主模块；设置页由「语音回复」插件承载）。

设计：
  - 多供应商合成，统一输出 mp3 到 DATA_DIR/cache/tts/（NapCat 收 file:// 直接转 silk 发送）
  - 计费透明：每档供应商在 PROVIDERS 里带 billing 文案，设置页原样展示
  - Edge-TTS 免费零配置（走微软公共接口，非官方通道，可能偶发不可用）；
    智谱 GLM-TTS 收费（支持音色复刻克隆 speaker_id）；硅基流动部分模型有免费额度；
    自定义 = 任意 OpenAI 兼容 /audio/speech 服务（OpenAI/Groq/MiniMax 等）
  - 合成失败/超时一律回退发文字，绝不因语音故障丢回复
"""

import json
import os
import pathlib
import re
import secrets
import time
import uuid

import qq_common as C

TTS_DIR = os.path.join(C.DATA_DIR, "cache", "tts")
_MAX_TEXT = 220          # 单条语音文本上限（约 35-40 秒；QQ 长语音上游有已知问题）

# ---------------- GPT-SoVITS 本地引擎管理 ----------------
# 集成包默认目录（用户可用 gsv_dir 覆盖）：E:\GPTSoVITS\<包名>
_GSV_DIR_CANDIDATES = [
    r"E:\GPTSoVITS\GPT-SoVITS-v3lora-20250228",
    r"D:\GPTSoVITS\GPT-SoVITS-v3lora-20250228",
]
_GSV_PORT = 9880
_GSV_PROC = {"p": None}      # runtime/python.exe 子进程句柄（仅本进程内有效）
_GSV_VOICES_DIR = os.path.join(C.DATA_DIR, "gsv_voices")
_GSV_VOICES_JSON = os.path.join(C.DATA_DIR, "gsv_voices.json")


def gsv_env_check():
    """安装前环境体检（不强制）：NVIDIA GPU 型号/显存。"""
    import subprocess
    has, gpu, vram = False, "", 0
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader"], capture_output=True, text=True, timeout=6)
        line = (r.stdout or "").strip().splitlines()[0] if r.stdout else ""
        if "," in line:
            gpu, vram_s = [x.strip() for x in line.split(",", 1)]
            vram = int("".join(ch for ch in vram_s if ch.isdigit()) or 0)
            has = True
    except Exception:
        pass
    notes = []
    if not has:
        notes.append("未检测到 NVIDIA 显卡——本地引擎基本无法使用（CPU 模式极慢，"
                     "不建议）。可直接用云端的智谱 GLM-TTS（收费但免部署）。")
    elif vram and vram < 6000:
        notes.append("显存 %dMB 偏小（推荐 6GB+）：合成可用但可能偶尔显存不足回退。" % vram)
    return {"has_nvidia": has, "gpu": gpu, "vram_mb": vram,
            "ok": has and (not vram or vram >= 6000), "notes": notes}


def _gsv_voices_load():
    try:
        d = json.loads(pathlib.Path(_GSV_VOICES_JSON).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and isinstance(d.get("voices"), list) \
            else {"voices": []}
    except (OSError, ValueError):
        return {"voices": []}


def _gsv_voices_save(d):
    pathlib.Path(C.DATA_DIR).mkdir(parents=True, exist_ok=True)
    pathlib.Path(_GSV_VOICES_JSON).write_text(
        json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def gsv_voice_list():
    out = []
    for v in _gsv_voices_load()["voices"]:
        dur = float(v.get("dur") or 0)
        out.append({"id": v.get("id"), "name": v.get("name"),
                    "prompt_text": v.get("prompt_text", ""),
                    "prompt_lang": v.get("prompt_lang", "zh"),
                    "dur": dur,
                    "warn": "" if 3 <= dur <= 10 else
                            ("参考 %.1f 秒超出引擎 3-10 秒限制，合成会失败——删掉重新截取" % dur) if dur else "",
                    "created": v.get("created", 0),
                    "exists": os.path.isfile(v.get("file") or "")})
    return out


def gsv_voice_get(voice_id):
    for v in _gsv_voices_load()["voices"]:
        if str(v.get("id")) == str(voice_id):
            return v
    return None


def gsv_voice_save(name, wav_bytes, prompt_text="", prompt_lang="zh"):
    """保存一个本地音色（参考音频 wav + 元信息）→ id。记录时长供列表警示
    （引擎限参考音频 3-10 秒，超长合成必失败）。"""
    if not wav_bytes or wav_bytes[:4] != b"RIFF":
        raise RuntimeError("音频必须是 WAV")
    name = str(name or "").strip()[:40]
    if not name:
        raise RuntimeError("先给音色起个名字")
    dur = _wav_duration(wav_bytes)
    pathlib.Path(_GSV_VOICES_DIR).mkdir(parents=True, exist_ok=True)
    vid = "v" + secrets.token_hex(4)
    fpath = os.path.join(_GSV_VOICES_DIR, vid + ".wav")
    pathlib.Path(fpath).write_bytes(wav_bytes)
    d = _gsv_voices_load()
    d["voices"].append({"id": vid, "name": name, "file": fpath,
                        "prompt_text": str(prompt_text or "")[:300],
                        "prompt_lang": str(prompt_lang or "zh")[:8],
                        "dur": round(dur, 1),
                        "created": time.time()})
    _gsv_voices_save(d)
    return vid


def gsv_voice_delete(voice_id):
    d = _gsv_voices_load()
    keep, removed = [], None
    for v in d["voices"]:
        if str(v.get("id")) == str(voice_id):
            removed = v
            try:
                os.remove(v.get("file") or "")
            except OSError:
                pass
        else:
            keep.append(v)
    if removed is None:
        return False
    d["voices"] = keep
    _gsv_voices_save(d)
    cfg = voice_cfg()
    if str(cfg.get("gsv_voice_id") or "") == str(voice_id):
        cfg["gsv_voice_id"] = ""
        save_voice_cfg(cfg)
    return True


def gsv_dir(cfg=None):
    """整合包目录：配置 gsv_dir 优先，其次候选路径里第一个存在的。"""
    d = str((cfg or {}).get("gsv_dir") or "").strip()
    if d and os.path.isdir(d):
        return d
    for c in _GSV_DIR_CANDIDATES:
        if os.path.isdir(c):
            return c
    return d   # 配置了但不存在 → 原样返回（状态页会提示路径无效）


def gsv_status(cfg=None):
    """引擎状态：包是否存在 / 服务是否在跑（探测 9880）/ 由谁启动。"""
    d = gsv_dir(cfg)
    py = os.path.join(d, "runtime", "python.exe") if d else ""
    api = os.path.join(d, "api_v2.py") if d else ""
    running = False
    try:
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", _GSV_PORT, timeout=2)
        c.request("GET", "/")
        r = c.getresponse()
        r.read()
        c.close()
        running = True     # 404 也算在跑（FastAPI 根路径无路由）
    except Exception:
        running = False
    return {
        "dir": d,
        "dir_ok": bool(d) and os.path.isdir(d) and os.path.isfile(py) and os.path.isfile(api),
        "running": running,
        "ours": bool(_GSV_PROC["p"] and _GSV_PROC["p"].poll() is None),
        "port": _GSV_PORT,
    }


def gsv_start(cfg=None):
    """启动本地引擎（api_v2.py，CUDA）。已在跑则直接返回。"""
    st = gsv_status(cfg)
    if st["running"]:
        return {"ok": True, "already": True, **st}
    if not st["dir_ok"]:
        return {"ok": False, "error": "未找到整合包（runtime/python.exe 或 api_v2.py 缺失）。"
                                      "请先在语音页按引导下载并解压到 E:\\GPTSoVITS", **st}
    import subprocess
    d = st["dir"]
    logf = open(os.path.join(C.DATA_DIR, "logs", "gsv_engine.log"), "ab")
    p = subprocess.Popen(
        [os.path.join(d, "runtime", "python.exe"), "api_v2.py",
         "-a", "127.0.0.1", "-p", str(_GSV_PORT),
         "-c", os.path.join("GPT_SoVITS", "configs", "tts_infer.yaml")],
        cwd=d, stdout=logf, stderr=logf,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    _GSV_PROC["p"] = p
    # 等端口就绪（模型加载 20-90 秒）
    import time
    import http.client
    for _ in range(60):
        time.sleep(2)
        try:
            c = http.client.HTTPConnection("127.0.0.1", _GSV_PORT, timeout=2)
            c.request("GET", "/")
            r = c.getresponse()
            r.read()
            c.close()
            return {"ok": True, "started": True, **gsv_status(cfg)}
        except Exception:
            if p.poll() is not None:
                return {"ok": False, "error": "引擎进程退出（详见 logs/gsv_engine.log）", **gsv_status(cfg)}
    return {"ok": False, "error": "启动超时（模型加载慢，稍后重试或直接用）", **gsv_status(cfg)}


def gsv_stop():
    """停止本应用启动的引擎（外部启动的不动）。"""
    p = _GSV_PROC.get("p")
    if p and p.poll() is None:
        p.terminate()
        _GSV_PROC["p"] = None
        return {"ok": True, "stopped": True}
    return {"ok": True, "stopped": False,
            "note": "没有由本应用启动的引擎（外部启动的进程不受管理）"}


def _gsv_request(text, ref, ptext, plang, cfg):
    """调本地引擎 /tts → WAV 字节。"""
    import http.client
    from urllib.parse import quote
    params = {
        "text": str(text)[:_MAX_TEXT], "text_lang": "zh",
        "ref_audio_path": ref,
        "prompt_text": ptext,
        "prompt_lang": plang,
        "text_split_method": "cut0",
        "media_type": "wav", "streaming_mode": False,
    }
    qs = "&".join("%s=%s" % (k, quote(str(v))) for k, v in params.items())
    port = int((cfg or {}).get("gsv_port") or _GSV_PORT)
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=300)
    c.request("GET", "/tts?" + qs)
    r = c.getresponse()
    ct = r.getheader("Content-Type") or ""
    if r.status != 200 or "audio" not in ct:
        body = r.read()[:200].decode("utf-8", "replace")
        c.close()
        raise RuntimeError("GPT-SoVITS 合成失败 HTTP %d: %s" % (r.status, body))
    data = b""
    while True:
        chunk = r.read(1 << 20)
        if not chunk:
            break
        data += chunk
    c.close()
    if len(data) < 1000:
        raise RuntimeError("GPT-SoVITS 返回音频过短")
    return data


def gsv_tts(text, cfg):
    """GPT-SoVITS 本地合成 → WAV 字节。失败抛异常（调用方回退）。
    参考音频优先级：gsv_voice_id（本地音色库）> gsv_ref_wav（手动路径）。
    gsv_prompt_text = 参考音频原文，gsv_prompt_lang = 参考语言。"""
    d = gsv_dir(cfg)
    ref, ptext, plang = "", "", "zh"
    vid = str(cfg.get("gsv_voice_id") or "").strip()
    if vid:
        v = gsv_voice_get(vid)
        if v and os.path.isfile(v.get("file") or ""):
            ref = v["file"]
            ptext = str(v.get("prompt_text") or "")
            plang = str(v.get("prompt_lang") or "zh")
    if not ref:
        ref = str(cfg.get("gsv_ref_wav") or "").strip()
        ptext = str(cfg.get("gsv_prompt_text") or "")
        plang = str(cfg.get("gsv_prompt_lang") or "zh")
    if not ref or not os.path.isfile(ref):
        raise RuntimeError("未设置参考音频（语音页「本地引擎」卡里克隆/选择一个音色）")
    return _gsv_request(text, ref, ptext, plang, cfg)


def gsv_preview_bytes(text, wav_bytes, prompt_text="", prompt_lang="zh", cfg=None):
    """克隆试听：用一段临时参考音频（页面截好、未入库）合成 → WAV 字节。
    试听不等于保存——满意后由调用方走 gsv_voice_save 入库。"""
    d = gsv_dir(cfg)
    if not d or not os.path.isdir(d):
        raise RuntimeError("本地引擎未安装")
    if not wav_bytes or wav_bytes[:4] != b"RIFF":
        raise RuntimeError("参考音频必须是 WAV")
    tmp = pathlib.Path(TTS_DIR)
    tmp.mkdir(parents=True, exist_ok=True)
    tref = tmp / ("gsvref_%s.wav" % secrets.token_hex(4))
    tref.write_bytes(wav_bytes)
    try:
        return _gsv_request(text, str(tref),
                            str(prompt_text or ""), str(prompt_lang or "zh"), cfg)
    finally:
        try:
            tref.unlink()
        except OSError:
            pass

# 各供应商预设音色（label 仅展示用；value 原样传 API）
EDGE_VOICES = [
    ("晓晓（女·温柔，最自然）", "zh-CN-XiaoxiaoNeural"),
    ("晓伊（女·活泼）", "zh-CN-XiaoyiNeural"),
    ("云希（男·阳光少年）", "zh-CN-YunxiNeural"),
    ("云健（男·浑厚沉稳）", "zh-CN-YunjianNeural"),
    ("云扬（男·播音腔）", "zh-CN-YunyangNeural"),
    ("晓北（女·东北味）", "zh-CN-liaoning-XiaobeiNeural"),
    ("晓妮（女·陕西味）", "zh-CN-shaanxi-XiaoniNeural"),
    ("曉曼（女·粤语）", "zh-HK-HiuMaanNeural"),
    ("曉臻（女·台湾腔）", "zh-TW-HsiaoChenNeural"),
]
ZHIPU_VOICES = [
    ("童童（女·默认）", "tongtong"),
]
SF_MODELS = [
    ("CosyVoice 2（有免费额度）", "FunAudioLLM/cosyvoice2-0.5B"),
    ("FishSpeech 1.5", "fishaudio/fish-speech-1-5"),
]
SF_VOICES = ["anna", "bella", "benjamin", "charles", "claire", "david", "diana"]

PROVIDERS = {
    "gsv": {
        "label": "GPT-SoVITS（本地 · 免费）",
        "billing": "完全免费：本机 GPU 推理，无字符计费、无限量。需按引导安装整合包"
                   "（约 6GB 下载 / 12GB 磁盘，RTX 显卡推荐）并保持引擎运行；"
                   "未启动时自动回退其他来源。中文参考音频效果最佳（日文样本说中文会发音混杂）。",
    },
    "edge": {
        "label": "Edge-TTS（免费 · 入门）",
        "billing": "免费。走微软 Edge 朗读的公共接口（非官方通道，无需任何 Key；"
                   "绝大多数时候稳定，偶发不可用时自动回退发文字）。"
                   "语音清晰但节奏均匀、无情感起伏——入门够用。",
    },
    "zhipu": {
        "label": "智谱 GLM-TTS（⭐ 推荐 · 支持克隆）",
        "billing": "收费：按合成字符计费（日常聊天一个月约几块钱）。"
                   "充值：open.bigmodel.cn → 控制台 → 费用中心 → 充值（10 元够用很久）。"
                   "新用户有赠送额度；Key 可复用模型管理里已配的智谱 Key。"
                   "有情感起伏、自然停顿、语气变化，远比免费方案更像真人。"
                   "「声音复刻」用 3-30 秒音频样本克隆专属音色（推荐走插件页引导），"
                   "克隆与用克隆音色合成都计费。",
    },
    "siliconflow": {
        "label": "硅基流动（部分模型免费额度）",
        "billing": "CosyVoice2-0.5B 等模型有免费额度，超出或 FishSpeech 按量收费"
                   "（siliconflow.cn 查看价格与免费模型清单）。需硅基流动 API Key。"
                   "充值：siliconflow.cn → 控制台 → 费用 → 充值。",
    },
    "custom": {
        "label": "自定义（任意 OpenAI 兼容 TTS）",
        "billing": "取决于你接入的服务商（OpenAI / Groq / MiniMax 等按各自价格计费，"
                   "部分服务商有免费档）。需填接口地址与 Key。",
    },
}


def _rc_path():
    return pathlib.Path(C.DATA_DIR) / "reply_config.json"


def _load_json(p, default):
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else dict(default)
    except (OSError, ValueError):
        return dict(default)


def voice_cfg():
    cfg = _load_json(_rc_path(), {})
    v = cfg.get("voice") if isinstance(cfg.get("voice"), dict) else {}
    return v


def save_voice_cfg(v):
    p = _rc_path()
    cfg = _load_json(p, {})
    cfg["voice"] = v
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def zhipu_key(cfg):
    k = str(cfg.get("zhipu_key", "")).strip()
    if k:
        return k
    try:
        import qq_bot_agent as A
        return A.glm_key("zhipu")
    except Exception:
        return ""


def zhipu_keys(cfg):
    """智谱语音 Key 候选序（去重保序）：插件页单独填的 → 主 Key →
    模型管理里的全部智谱备用 Key。多账户场景（实测：企业 Key 语音
    额度耗尽、个人 Key 有余额）逐个尝试，1113 余额类错误自动换下一个。"""
    seen, out = set(), []
    cands = [str(cfg.get("zhipu_key") or "").strip()]
    try:
        import qq_bot_agent as A
        cands.append(str(A.glm_key("zhipu") or "").strip())
        keys = A.load_api_keys()
        for e in (keys.get("keys") or []):
            if str(e.get("provider") or "zhipu") == "zhipu":
                cands.append(str(e.get("key") or "").strip())
    except Exception:
        pass
    for k in cands:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _out_path():
    d = pathlib.Path(TTS_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d / ("tts_%d_%s.mp3" % (int(time.time()), uuid.uuid4().hex[:6]))


def _cleanup_old(keep=12):
    """只保留最近 keep 个合成文件（试听/发送产生的缓存）。"""
    d = pathlib.Path(TTS_DIR)
    try:
        files = sorted(list(d.glob("*.mp3")) + list(d.glob("*.wav")),
                       key=lambda f: f.stat().st_mtime, reverse=True)
        for f in files[keep:]:
            try:
                f.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _post_audio_speech(base_url, key, model, voice, text, timeout=90,
                       audio_format="mp3"):
    """POST <base>/audio/speech → 音频字节。base_url 只允许 https 域名。
    audio_format: 传 None 则不发 response_format（默认 mp3）。"""
    import http.client
    import urllib.parse
    base = str(base_url or "").strip()
    if not base.startswith("https://"):
        raise RuntimeError("接口地址必须是 https:// 开头")
    if not base.endswith("/"):
        base += "/"
    u = urllib.parse.urlsplit(base + "audio/speech")
    if u.scheme != "https" or not u.hostname:
        raise RuntimeError("接口地址非法（必须 https 域名）")
    payload = {"model": str(model), "input": str(text), "voice": str(voice)}
    if audio_format:
        payload["response_format"] = audio_format
    conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout)
    conn.request("POST", u.path + ("?" + u.query if u.query else ""),
                 body=json.dumps(payload).encode("utf-8"),
                 headers={"Authorization": "Bearer " + str(key),
                          "Content-Type": "application/json"})
    r = conn.getresponse()
    data = r.read()
    conn.close()
    if r.status != 200:
        raise RuntimeError("HTTP %d: %s" % (r.status, data[:160].decode("utf-8", "replace")))
    return data


def _pcm_to_wav(pcm, sample_rate=16000, channels=1, bits=16):
    """raw PCM → WAV 字节（44 字节头 + PCM 数据）。"""
    import struct
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    data_size = len(pcm)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_size, b"WAVE",
        b"fmt ", 16,
        1,  # PCM
        channels, sample_rate, byte_rate, block_align, bits,
        b"data", data_size)
    return header + pcm


def clone_zhipu_voice(key, wav_bytes, voice_name, sample_text="", preview_text=""):
    """用一段 WAV 样本克隆智谱音色 → 返回 (voice_id, 预览音频 file_id)。

    流程（智谱开放平台）：
      1) POST /api/paas/v4/files  multipart，purpose=voice-clone-input → file_id
      2) POST /api/paas/v4/voice/clone {model, voice_name, file_id, input[, text]}
    限制：样本 ≤10MB，建议 3-30 秒人声；样本 WAV 由设置页在浏览器里截好再传上来。
    key 支持传单个或候选列表（zhipu_keys）：1113 余额类错误自动换下一个 Key；
    1234「网络错误」/HTTP 5xx 先同 Key 重试一次再换（实测平台偶发 500）。
    """
    import http.client
    keys = [str(k).strip() for k in (key if isinstance(key, (list, tuple)) else [key])
            if str(k or "").strip()]
    if not keys:
        raise RuntimeError("未找到智谱 API Key")
    if not wav_bytes or wav_bytes[:4] != b"RIFF":
        raise RuntimeError("样本必须是 WAV 音频")
    if len(wav_bytes) > 10 * 1024 * 1024:
        raise RuntimeError("样本超过 10MB，请缩短时长后重试")

    last_err = None
    for ki, k in enumerate(keys):
        for attempt in (1, 2):   # 1234/5xx 同 Key 隔 2 秒重试一次，仍败换下一个 Key
            try:
                r = _zhipu_clone_once(k, wav_bytes, voice_name,
                                      sample_text, preview_text)
                if ki or attempt > 1:
                    print("[ZHIPU_CLONE] ok key#%d attempt%d" % (ki, attempt),
                          flush=True)
                return r
            except RuntimeError as e:
                s = str(e)
                last_err = e
                print("[ZHIPU_CLONE] fail key#%d attempt%d: %.160s"
                      % (ki, attempt, s), flush=True)
                _balance = ("1113" in s or "余额不足" in s or "资源包" in s)
                _transient = ("1234" in s or "网络错误" in s
                              or "HTTP 5" in s or "HTTP 500" in s)
                if _balance:
                    break        # 这个 Key 没额度/权限，直接换下一个
                if _transient and attempt == 1:
                    time.sleep(2.0)
                    continue
                break           # 非余额非抖动（参数/样本类错误）或抖动二连败：换下一个 Key
    # 所有 Key 都试完仍失败：报最后一次错误（1113/1234 等文案原样透出）
    raise last_err or RuntimeError("没有可用的智谱语音 Key（克隆）")


def _zhipu_clone_once(key, wav_bytes, voice_name, sample_text, preview_text):
    """单 Key 一次完整克隆（上传 → /voice/clone，含 1214 同名自动改名）。"""
    import http.client
    boundary = "----qqbot" + secrets.token_hex(8)
    head = ("--%s\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\n"
            "voice-clone-input\r\n"
            "--%s\r\nContent-Disposition: form-data; name=\"file\"; "
            "filename=\"sample.wav\"\r\nContent-Type: audio/wav\r\n\r\n"
            % (boundary, boundary)).encode("utf-8")
    tail = ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    body = head + wav_bytes + tail
    conn = http.client.HTTPSConnection("open.bigmodel.cn", 443, timeout=60)
    conn.request("POST", "/api/paas/v4/files", body=body, headers={
        "Authorization": "Bearer " + key,
        "Content-Type": "multipart/form-data; boundary=" + boundary,
        "Content-Length": str(len(body))})
    r = conn.getresponse()
    raw = r.read()
    conn.close()
    if r.status != 200:
        raise RuntimeError("上传样本失败 HTTP %d: %s"
                           % (r.status, raw[:200].decode("utf-8", "replace")))
    try:
        file_id = str(json.loads(raw.decode("utf-8")).get("id") or "")
    except ValueError:
        file_id = ""
    if not file_id:
        raise RuntimeError("上传成功但没有拿到 file_id")

    payload = {
        "model": "glm-tts-clone",
        "voice_name": str(voice_name or "my_voice")[:40],
        "file_id": file_id,
        # input = 克隆后用于试听合成的目标文本（接口必填）
        "input": str(preview_text or "你好呀，我是你的语音助手，听听这个声音怎么样？")[:120],
    }
    if str(sample_text or "").strip():
        payload["text"] = str(sample_text).strip()[:300]   # 样本原文（可选，填了更像）
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    used_name = payload["voice_name"]
    for attempt in (1, 2):
        conn = http.client.HTTPSConnection("open.bigmodel.cn", 443, timeout=90)
        conn.request("POST", "/api/paas/v4/voice/clone", body=data, headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Content-Length": str(len(data))})
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        if r.status == 400 and ("1214" in raw.decode("utf-8", "replace")
                                or "已存在" in raw.decode("utf-8", "replace")):
            # 音色名重复：平台同一账号下名字唯一——自动加短后缀重试一次
            # （用户重传同名样本时不必手动改名；旧音色仍在平台上可继续用其 id）
            if attempt == 1:
                used_name = ("%s_%s" % (payload["voice_name"],
                                        secrets.token_hex(2)))[:40]
                payload["voice_name"] = used_name
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                continue
        break
    if r.status != 200:
        raise RuntimeError("克隆失败 HTTP %d: %s"
                           % (r.status, raw[:300].decode("utf-8", "replace")))
    try:
        obj = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise RuntimeError("克隆接口返回非 JSON")
    voice_id = str(obj.get("voice") or "")
    if not voice_id:
        raise RuntimeError("克隆接口没有返回 voice id: %s"
                           % raw[:200].decode("utf-8", "replace"))
    return voice_id, str(obj.get("file_id") or ""), used_name


def list_zhipu_voices(key):
    """拉取本账号的复刻音色列表（PRIVATE）→ [{id, name, created, url}]。"""
    import http.client
    if not str(key or "").strip():
        raise RuntimeError("未找到智谱 API Key")
    conn = http.client.HTTPSConnection("open.bigmodel.cn", 443, timeout=30)
    conn.request("GET", "/api/paas/v4/voice/list?voiceType=PRIVATE",
                 headers={"Authorization": "Bearer " + str(key).strip()})
    r = conn.getresponse()
    raw = r.read()
    conn.close()
    if r.status != 200:
        raise RuntimeError("拉取音色列表失败 HTTP %d: %s"
                           % (r.status, raw[:200].decode("utf-8", "replace")))
    try:
        d = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise RuntimeError("音色列表返回非 JSON")
    out = []
    for it in (d.get("voice_list") or []):
        out.append({
            "id": str(it.get("voice") or ""),
            "name": str(it.get("voice_name") or ""),
            "created": str(it.get("create_time") or ""),
            "url": str(it.get("download_url") or ""),
        })
    return out


def delete_zhipu_voice(key, voice_id):
    """删除一个复刻音色。返回 (被删 id, 删除时间)。"""
    import http.client
    vid = str(voice_id or "").strip()
    if not str(key or "").strip():
        raise RuntimeError("未找到智谱 API Key")
    if not vid:
        raise RuntimeError("缺少音色 ID")
    data = json.dumps({"voice": vid}).encode("utf-8")
    conn = http.client.HTTPSConnection("open.bigmodel.cn", 443, timeout=30)
    conn.request("POST", "/api/paas/v4/voice/delete", body=data, headers={
        "Authorization": "Bearer " + str(key).strip(),
        "Content-Type": "application/json",
        "Content-Length": str(len(data))})
    r = conn.getresponse()
    raw = r.read()
    conn.close()
    if r.status != 200:
        raise RuntimeError("删除音色失败 HTTP %d: %s"
                           % (r.status, raw[:300].decode("utf-8", "replace")))
    try:
        d = json.loads(raw.decode("utf-8"))
    except ValueError:
        return vid, ""
    return str(d.get("voice") or vid), str(d.get("update_time") or "")


def _synth_edge(text, cfg):
    import asyncio
    import edge_tts
    voice = str(cfg.get("edge_voice") or "zh-CN-XiaoxiaoNeural")
    out = _out_path()

    async def _run():
        await edge_tts.Communicate(text, voice).save(str(out))
    asyncio.run(_run())
    return out


def _wav_duration(data):
    """WAV 字节 → 时长秒（读 RIFF 头帧数；失败返回 0）。"""
    try:
        import wave
        import io as _io
        w = wave.open(_io.BytesIO(data), 'rb')
        dur = w.getnframes() / max(1, w.getframerate())
        w.close()
        return dur
    except Exception:
        return 0.0


def _trim_lead_noise(data):
    """智谱 GLM-TTS 输出偶发头部数秒低电平持续音（用户听感「嘟嘟声」，
    克隆音色更常见）。50ms 窗逐窗算 RMS，自适应阈值定位首个持续语音块
    （该窗超阈值且后续 0.5s 内至少还有 4 窗超阈值——容忍语音里的停顿），
    起音前的噪声整段剪掉、留 0.1s 预垫；起音在开头 0.3s 内或整体近静音
    时原样返回（对干净输出零影响）。只处理 16bit 单声道 PCM。"""
    try:
        import io as _io
        import math as _math
        import struct as _st
        import wave as _w
        w = _w.open(_io.BytesIO(data), "rb")
        ch, sw, rate, n = (w.getnchannels(), w.getsampwidth(),
                           w.getframerate(), w.getnframes())
        if ch != 1 or sw != 2 or n < rate:  # 仅 16bit 单声道；<1s 不剪
            w.close()
            return data
        frames = w.readframes(n)
        w.close()
        win = max(1, rate // 20)            # 50ms 窗
        cnt = len(frames) // 2
        vals = _st.unpack("%dh" % cnt, frames[:cnt * 2])
        nwin = cnt // win
        if nwin < 20:
            return data
        rms = []
        for i in range(nwin):
            seg = vals[i * win:(i + 1) * win]
            rms.append(int(_math.sqrt(sum(x * x for x in seg) / len(seg))))
        peak = max(rms)
        if peak < 400:                      # 整体近静音，不动
            return data
        thr = max(350, int(peak * 0.30))
        onset = None
        for i in range(nwin - 10):
            if rms[i] >= thr:
                hits = sum(1 for v in rms[i + 1:i + 11] if v >= thr)
                if hits >= 4:               # 后 0.5s 里还有实声=真起音
                    onset = i
                    break
        if onset is None or onset <= 6:     # 起音在前 0.3s 内=无杂音
            return data
        cut = max(0, onset - 2) * win       # 留 0.1s 预垫
        out = _io.BytesIO()
        o = _w.open(out, "wb")
        o.setnchannels(ch)
        o.setsampwidth(sw)
        o.setframerate(rate)
        o.writeframes(frames[cut * 2:])
        o.close()
        return out.getvalue()
    except Exception:
        return data


def synthesize(text, cfg):
    """合成 → 返回 mp3 路径；失败抛异常（调用方回退文字）。text 会被截断。"""
    text = str(text or "").strip().replace("\n", " ")[:_MAX_TEXT]
    if not text:
        raise RuntimeError("空文本")
    provider = str(cfg.get("provider") or "edge")
    if provider == "gsv":
        data = gsv_tts(text, cfg)   # 本地引擎直接返回 WAV 字节
        out = _out_path().with_suffix(".wav")
        out.write_bytes(data)
        return out
    if provider == "edge":
        return _synth_edge(text, cfg)
    if provider == "zhipu":
        voice = str(cfg.get("zhipu_voice") or "tongtong")
        if str(cfg.get("zhipu_speaker_id", "")).strip():
            voice = str(cfg["zhipu_speaker_id"]).strip()   # 克隆音色优先
        key = zhipu_key(cfg)
        if not key:
            raise RuntimeError("未找到智谱 API Key（模型管理里配置，或在语音设置里单独填）")
        data = None
        last_err = None
        good_key = key
        for k in zhipu_keys(cfg):
            try:
                data = _post_audio_speech("https://open.bigmodel.cn/api/paas/v4/",
                                           k, "cogtts", voice, text,
                                           audio_format="wav")  # 智谱支持 wav（mp3 报 1214）
                good_key = k
                break
            except RuntimeError as e:
                s = str(e)
                if "1113" in s or "余额不足" in s or "资源包" in s:
                    # 该账户语音额度不可用（多账户场景：主 Key 企业额度耗尽、
                    # 个人 Key 有余额）——换下一个候选 Key
                    last_err = e
                    continue
                raise
        if data is None:
            raise last_err or RuntimeError("没有可用的智谱语音 Key")
        # 头部杂音修剪：智谱 GLM-TTS 输出偶发开头数秒低电平持续音（嘟嘟声，
        # 实测克隆音色更常见、预设也有）——定位首个持续语音块，噪声段剪掉
        data = _trim_lead_noise(data)
        # 克隆音色「样本复现」兜底（实测：6 字文本合成出 19 秒——模型把
        # 克隆样本里的原文混进输出）。合成时长超过文本预估 2 倍且当前用的
        # 是克隆音色时，改用预设音色重合成一次，保证内容可听
        est = max(4.0, len(text) * 0.32 + 1.5)
        if str(cfg.get("zhipu_speaker_id") or "").strip() \
                and _wav_duration(data) > est * 2:
            fallback_voice = str(cfg.get("zhipu_voice") or "tongtong")
            data = _post_audio_speech("https://open.bigmodel.cn/api/paas/v4/",
                                      good_key, "cogtts", fallback_voice, text,
                                      audio_format="wav")
    elif provider == "siliconflow":
        key = str(cfg.get("sf_key", "")).strip()
        if not key:
            raise RuntimeError("未配置硅基流动 API Key")
        data = _post_audio_speech("https://api.siliconflow.cn/v1/", key,
                                  str(cfg.get("sf_model") or "FunAudioLLM/cosyvoice2-0.5B"),
                                  str(cfg.get("sf_voice") or "anna"), text)
    elif provider == "custom":
        data = _post_audio_speech(str(cfg.get("custom_base") or ""),
                                  str(cfg.get("custom_key", "")).strip(),
                                  str(cfg.get("custom_model") or "tts-1"),
                                  str(cfg.get("custom_voice") or "alloy"), text)
    else:
        raise RuntimeError("未知供应商: %s" % provider)
    out = _out_path()
    # WAV 格式用 .wav 扩展名（NapCat/Audio 元素按扩展名识别格式）
    if data[:4] == b"RIFF":
        out = out.with_suffix(".wav")
    out.write_bytes(data)
    return out


# ---------------------------------------------------------------------------
# 回复链路集成：agent 在发送文字前调 should_voice()；命中则 send_voice()
# ---------------------------------------------------------------------------

_RNG = secrets.SystemRandom()


def _kw_hit(msg, keywords):
    text = str(msg.get("text") or "")
    return any(k and k in text for k in keywords)


# 语音意图正则：消息明确要求发语音就触发（不管关键词配置怎么写）。
# 关键词子串匹配会漏口语变体（实测「发个语音给我听听」不含「发语音」——
# 差一个"个"字），模型又未必自发 [语音] 标记，双重 miss = 用户要语音
# 只得到文字。意图正则是确定性兜底，覆盖口语变体。
_VOICE_ASK_RE = re.compile(
    r"[发来录给想要点整].{0,3}语音|语音.{0,6}(给我|听听|呢|呀)|"
    r"说给我听|念给我听|读给我听|用语音|来段语音|语音回复|听你说话|"
    r"发条语音|语音消息")
# 否定语境排除：「不用语音/别发语音/语音就好（不用真发）」不是请求
_VOICE_NO_RE = re.compile(
    r"(不用|别|不要|无需|不想|免).{0,4}语音|语音.{0,4}(免了|就好|就行|算了)")


def should_voice(msg):
    """本条回复是否用语音发送。配置由插件设置页维护（reply_config.voice）。"""
    cfg = voice_cfg()
    if not cfg.get("enabled"):
        return False
    if msg.get("chat_type") == "group":
        if not cfg.get("group_enabled", False):
            return False
    elif not cfg.get("private_enabled", True):
        return False
    mode = str(cfg.get("mode") or "keyword")     # always / keyword / mixed
    if mode == "always":
        return True
    # 语音意图兜底：对方明确要求语音就必发（各模式一致；场景开关仍生效）；
    # 否定语境（不用语音/别发语音）不算请求
    _t = str(msg.get("text") or "")
    if _VOICE_ASK_RE.search(_t) and not _VOICE_NO_RE.search(_t):
        return True
    kws = [str(x).strip() for x in (cfg.get("keywords") or []) if str(x).strip()]
    if mode == "keyword":
        return bool(kws) and _kw_hit(msg, kws)
    if mode == "mixed":                          # 触发词必语音，否则按概率
        if kws and _kw_hit(msg, kws):
            return True
        try:
            return _RNG.random() < float(cfg.get("prob") or 0)
        except (TypeError, ValueError):
            return False
    return False


def voice_with_text():
    return bool(voice_cfg().get("with_text"))


def send_voice(daemon, chat_type, chat_id, texts):
    """把 texts（1-N 段）逐段合成语音发送。成功段数>0 返回 True。
    daemon 参数兼容旧调用方；优先从 agent 的 PLUGIN_HOST 拿 daemon。"""
    ok = 0
    sender = daemon
    if sender is None:
        try:
            import qq_plugins as _qp
            host = getattr(_qp, "_ACTIVE_HOST", None)
            if host and getattr(host, "daemon", None):
                sender = host.daemon
        except Exception:
            pass
    if sender is None:
        try:
            import qq_bot_agent as _A
            sender = _A.PLUGIN_HOST.daemon
        except Exception:
            pass
    if sender is None:
        return False
    for t in (texts or [])[:3]:
        try:
            mp3 = synthesize(t, voice_cfg())
            try:
                # text 会写进会话历史（模型才知道自己说过什么，防复读）
                res = sender.send_record(chat_type, chat_id, str(mp3), text=t)
            except TypeError:
                # 旧宿主 DaemonClient 没有 text 参数（插件更新先于应用时的兼容）
                res = sender.send_record(chat_type, chat_id, str(mp3))
            if res:
                ok += 1
            # 不立即删文件：发送队列是异步的，NapCat 真正发的时候文件必须还在。
            # 由 _cleanup_old() 统一清理（保留最近 12 个）
        except Exception:
            break
    _cleanup_old()
    return ok > 0
