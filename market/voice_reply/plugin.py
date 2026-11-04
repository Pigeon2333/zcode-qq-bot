# -*- coding: utf-8 -*-
"""voice_reply：语音回复（引擎随插件包自带 engine.py；发送走 api.send_voice → daemon 队列）。

引擎（TTS 合成+策略判定）与页面（voicePage）都在插件包内，完全独立于主程序版本。
插件注册一个自定义端点 refresh_hooks 供宿主调用刷新策略缓存。"""
import importlib.util as _ilu
import os as _os

_dir = _os.path.dirname(_os.path.abspath(__file__))
_spec = _ilu.spec_from_file_location("qq_tts_engine", _os.path.join(_dir, "engine.py"))
qq_tts = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(qq_tts)


def register(api):
    # 定期同步宿主的 daemon 句柄（agent 启动时才连上）
    api.on_tick(lambda daemon: _sync_daemon(daemon))

    # 自定义端点：宿主 agent 在发送判定点调用（替代旧的 import qq_tts）
    api.route("should_voice", lambda args: {"ok": True,
                                            "hit": qq_tts.should_voice(args.get("msg") or {})})
    api.route("send_voice_for", lambda args: {"ok": qq_tts.send_voice(
        None, str(args.get("chat_type")), str(args.get("chat_id")),
        args.get("texts") or [])})
    api.route("voice_with_text", lambda args: {"ok": True,
                                               "enabled": qq_tts.voice_with_text()})
    api.log("ready（引擎自带；发送走宿主 daemon 队列）")


def _sync_daemon(daemon):
    if daemon is not None:
        import sys as _s
        try:
            import qq_plugins as _qp
            if getattr(_qp, "_ACTIVE_HOST", None) is not None:
                _qp._ACTIVE_HOST.daemon = daemon
        except Exception:
            pass
