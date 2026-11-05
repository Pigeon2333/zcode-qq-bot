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
import secrets
import time
import uuid

import qq_common as C

TTS_DIR = os.path.join(C.DATA_DIR, "cache", "tts")
_MAX_TEXT = 220          # 单条语音文本上限（约 35-40 秒；QQ 长语音上游有已知问题）

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
    """
    import http.client
    if not str(key or "").strip():
        raise RuntimeError("未找到智谱 API Key")
    if not wav_bytes or wav_bytes[:4] != b"RIFF":
        raise RuntimeError("样本必须是 WAV 音频")
    if len(wav_bytes) > 10 * 1024 * 1024:
        raise RuntimeError("样本超过 10MB，请缩短时长后重试")
    key = str(key).strip()
    boundary = "----qqbot" + secrets.token_hex(8)
    head = ("--%s\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\n"
            "voice-clone-input\r\n"
            "--%s\r\nContent-Disposition: form-data; name=\"file\"; "
            "filename=\"sample.wav\"\r\nContent-Type: audio/wav\r\n\r\n"
            % (boundary, boundary)).encode("utf-8")
    tail = ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    body = head + wav_bytes + tail
    conn = http.client.HTTPSConnection("open.bigmodel.cn", 443, timeout=120)
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
        conn = http.client.HTTPSConnection("open.bigmodel.cn", 443, timeout=180)
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


def synthesize(text, cfg):
    """合成 → 返回 mp3 路径；失败抛异常（调用方回退文字）。text 会被截断。"""
    text = str(text or "").strip().replace("\n", " ")[:_MAX_TEXT]
    if not text:
        raise RuntimeError("空文本")
    provider = str(cfg.get("provider") or "edge")
    if provider == "edge":
        return _synth_edge(text, cfg)
    if provider == "zhipu":
        voice = str(cfg.get("zhipu_voice") or "tongtong")
        if str(cfg.get("zhipu_speaker_id", "")).strip():
            voice = str(cfg["zhipu_speaker_id"]).strip()   # 克隆音色优先
        key = zhipu_key(cfg)
        if not key:
            raise RuntimeError("未找到智谱 API Key（模型管理里配置，或在语音设置里单独填）")
        data = _post_audio_speech("https://open.bigmodel.cn/api/paas/v4/",
                                   key, "cogtts", voice, text,
                                   audio_format="wav")  # 智谱支持 wav（mp3 报 1214）
        # 克隆音色「样本复现」兜底（实测：6 字文本合成出 19 秒——模型把
        # 克隆样本里的原文混进输出）。合成时长超过文本预估 2 倍且当前用的
        # 是克隆音色时，改用预设音色重合成一次，保证内容可听
        est = max(4.0, len(text) * 0.32 + 1.5)
        if str(cfg.get("zhipu_speaker_id") or "").strip() \
                and _wav_duration(data) > est * 2:
            fallback_voice = str(cfg.get("zhipu_voice") or "tongtong")
            data = _post_audio_speech("https://open.bigmodel.cn/api/paas/v4/",
                                      key, "cogtts", fallback_voice, text,
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
