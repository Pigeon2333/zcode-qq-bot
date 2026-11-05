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
        "label": "Edge-TTS（免费）",
        "billing": "免费。走微软 Edge 朗读的公共接口（非官方通道，无需任何 Key；"
                   "绝大多数时候稳定，偶发不可用时自动回退发文字）。",
    },
    "zhipu": {
        "label": "智谱 GLM-TTS（收费，支持克隆）",
        "billing": "收费：按合成字符数计费，价格见 bigmodel.cn/pricing（新账号通常有赠送额度）。"
                   "「音色复刻」用 3 秒样本克隆专属音色，复刻与用克隆音色合成都计费。"
                   "Key 可复用你已配置的智谱 API Key。",
    },
    "siliconflow": {
        "label": "硅基流动（部分模型免费额度）",
        "billing": "CosyVoice2-0.5B 等模型有免费额度，超出或 FishSpeech 按量收费"
                   "（siliconflow.cn 查看价格与免费模型清单）。需硅基流动 API Key。",
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
        files = sorted((f for f in d.glob("*.mp3")),
                       key=lambda f: f.stat().st_mtime, reverse=True)
        for f in files[keep:]:
            try:
                f.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _post_audio_speech(base_url, key, model, voice, text, timeout=90):
    """OpenAI 兼容 POST <base>/audio/speech → mp3 字节。base_url 只允许 https 域名。"""
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
    payload = {"model": str(model), "input": str(text), "voice": str(voice),
               "response_format": "mp3"}
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


def _synth_edge(text, cfg):
    import asyncio
    import edge_tts
    voice = str(cfg.get("edge_voice") or "zh-CN-XiaoxiaoNeural")
    out = _out_path()

    async def _run():
        await edge_tts.Communicate(text, voice).save(str(out))
    asyncio.run(_run())
    return out


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
                                  key, "cogtts", voice, text)
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
            res = sender.send_record(chat_type, chat_id, str(mp3))
            if res:
                ok += 1
            try:
                mp3.unlink()
            except OSError:
                pass
        except Exception:
            break
    _cleanup_old()
    return ok > 0
