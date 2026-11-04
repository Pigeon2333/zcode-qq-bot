# -*- coding: utf-8 -*-
"""lark_bridge：QQ 群 ↔ 飞书群互通（引擎随插件包自带 engine.py）。

QQ→飞书：纯 REST 即时转发，零第三方依赖
飞书→QQ：飞书 WebSocket 长连接（需 pip install lark-oapi，免公网；重启机器人后接入）
"""

import importlib.util as _ilu
import os as _os

_dir = _os.path.dirname(_os.path.abspath(__file__))
_spec = _ilu.spec_from_file_location("qq_lark_engine", _os.path.join(_dir, "engine.py"))
qq_lark = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(qq_lark)


def register(api):
    def _on_msg(msg):
        try:
            if qq_lark.cfg().get("enabled"):
                qq_lark.on_qq_message(msg)
        except Exception:
            pass

    def _on_tick(daemon):
        try:
            if qq_lark.cfg().get("enabled"):
                qq_lark.start_ws_bridge(daemon)
        except Exception:
            pass

    api.on_message(_on_msg)
    api.on_tick(_on_tick)
    api.log("ready（引擎自带；插件页配置应用凭证与桥接映射）")
