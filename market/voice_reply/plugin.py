# -*- coding: utf-8 -*-
"""voice_reply：语音回复（引擎在宿主 qq_tts + daemon.send_record，本插件为市场载体与设置入口）。"""


def register(api):
    api.log("ready（插件页配置语音来源/音色/策略；合成失败自动回退文字）")
