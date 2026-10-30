# -*- coding: utf-8 -*-
"""电脑状态感知插件：仅主人私聊注入开机时长/内存/磁盘实况（5 分钟缓存）。
自 agent 内联 _env_pulse_line 迁入（05c3ddd 前身 042b997）。"""


def register(api):
    api.inject_context(lambda ct, cid: _line(ct, cid, api))


def _owner_qq():
    try:
        import qq_bot_agent as A
        return str(A.owner_qq() or "")
    except Exception:
        return ""


_CACHE = {"ts": 0.0, "text": "", "line": ""}


def _line(chat_type, chat_id, api):
    if chat_type != "private" or str(chat_id) != _owner_qq():
        return ""
    import time
    now = time.time()
    if now - _CACHE["ts"] < 300 and _CACHE["text"]:
        return _CACHE["line"]
    try:
        import ctypes
        ms = ctypes.c_uint64(ctypes.windll.kernel32.GetTickCount64()).value

        class _MEM(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                        ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                        ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                        ("ullAvailExtendedVirtual", ctypes.c_uint64)]
        mem = _MEM()
        mem.dwLength = ctypes.sizeof(_MEM)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
        import shutil
        du = shutil.disk_usage(__import__("os").path.expanduser("~"))
        days = ms / 86400000.0
        boot = ("已连续开机 %.1f 天" % days) if days >= 1 else \
            ("已开机 %d 小时" % max(1, int(ms // 3600000)))
        text = "%s，内存占用 %d%%，C 盘剩余 %.0fGB（共 %.0fGB）" % (
            boot, int(mem.dwMemoryLoad), du.free / 2 ** 30, du.total / 2 ** 30)
        line = ("\n\n## 主人的电脑环境（真实数据；主人聊到电脑状态时可自然引用，"
                "其他话题不必提）\n- " + text)
        _CACHE.update(ts=now, text=text, line=line)
        return line
    except Exception as e:
        api.log("PULSE_FAIL %r" % e)
        return ""
