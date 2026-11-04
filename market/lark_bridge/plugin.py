# -*- coding: utf-8 -*-
"""lark_bridge：QQ 群 ↔ 飞书群互通（引擎在宿主 qq_lark，本插件为载体与设置入口）。

QQ→飞书：纯 REST 即时转发，零第三方依赖
飞书→QQ：飞书 WebSocket 长连接（需 pip install lark-oapi，免公网；重启机器人后接入）
"""


def register(api):
    import qq_lark

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
    api.log("ready（插件页配置应用凭证与桥接映射）")
