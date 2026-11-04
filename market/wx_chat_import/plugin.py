# -*- coding: utf-8 -*-
"""wx_chat_import：导入微信聊天记录到隔离库，学习成人设/记忆。
引擎随插件包自带（engine.py，独立于宿主）。"""

import importlib.util as _ilu
import os as _os

_dir = _os.path.dirname(_os.path.abspath(__file__))
_spec = _ilu.spec_from_file_location("wx_chatimport_engine", _os.path.join(_dir, "engine.py"))
engine = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(engine)


def register(api):
    api.log("ready（引擎自带；在插件页导入与学习）")
