# -*- coding: utf-8 -*-
"""电脑状态感知插件：仅主人私聊注入电脑实况（逐项可开关，5 分钟缓存）。
采集项：开机时长 / CPU / 内存 / 各盘剩余 / 电池供电 / 局域网 IP / 前台窗口（默认关）。
全部 ctypes+stdlib 直读，不落子进程；只进主人私聊，不进群聊。"""

import ctypes
import string
import time

_CACHE = {"key": "", "ts": 0.0, "lines": []}


def register(api):
    api.inject_context(lambda ct, cid: _line(ct, cid, api))


def _owner_qq():
    try:
        import qq_bot_agent as A
        return str(A.owner_qq() or "")
    except Exception:
        return ""


def _on(api, key):
    return bool(api.setting("item_" + key, key != "active_window"))


# ---------------- 采集项（每个返回一行字符串，失败返回 ""） ----------------

def _k32():
    return ctypes.windll.kernel32


def _item_uptime():
    ms = ctypes.c_uint64(_k32().GetTickCount64()).value
    days = ms / 86400000.0
    if days >= 1:
        return "已连续开机 %.1f 天" % days
    return "已开机 %d 小时" % max(1, int(ms // 3600000))


def _item_cpu():
    # GetSystemTimes 双采样 300ms：CPU% = 1 - Δ空闲/Δ总
    class FT(ctypes.Structure):
        _fields_ = [("idle", ctypes.c_uint64), ("user", ctypes.c_uint64),
                    ("kernel", ctypes.c_uint64)]
    a = FT()
    if not _k32().GetSystemTimes(ctypes.byref(a), None, None):
        return ""
    time.sleep(0.3)
    b = FT()
    if not _k32().GetSystemTimes(ctypes.byref(b), None, None):
        return ""
    d_idle = b.idle - a.idle
    d_total = (b.idle + b.user + b.kernel) - (a.idle + a.user + a.kernel)
    if d_total <= 0:
        return ""
    return "CPU 占用 %d%%" % round(100 * (1 - d_idle / d_total))


def _item_memory():
    class MEM(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                    ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                    ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                    ("ullAvailExtendedVirtual", ctypes.c_uint64)]
    m = MEM()
    m.dwLength = ctypes.sizeof(MEM)
    if not _k32().GlobalMemoryStatusEx(ctypes.byref(m)):
        return ""
    return "内存占用 %d%%（可用 %.1fGB / 共 %.1fGB）" % (
        int(m.dwMemoryLoad), m.ullAvailPhys / 2**30, m.ullTotalPhys / 2**30)


def _item_disk():
    # 所有存在的盘符各一行，合成一条
    rows = []
    free = ctypes.c_uint64(0)
    total = ctypes.c_uint64(0)
    for letter in string.ascii_uppercase:
        root = u"%s:\\" % letter
        if ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                ctypes.c_wchar_p(root), ctypes.byref(free), ctypes.byref(total), None):
            rows.append("%s盘剩余 %.0fGB" % (letter, free.value / 2**30))
    return "；".join(rows) if rows else ""


def _item_battery():
    class PWR(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_byte),
                    ("BatteryFlag", ctypes.c_byte), ("BatteryLifePercent", ctypes.c_byte),
                    ("Reserved1", ctypes.c_byte), ("BatteryLifeTime", ctypes.c_ulong),
                    ("BatteryFullLifeTime", ctypes.c_ulong)]
    p = PWR()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(p)):
        return ""
    if p.BatteryLifePercent in (255, 0) and p.ACLineStatus == 1:
        return "台式机供电（无电池）"
    pct = p.BatteryLifePercent
    if pct > 100 or pct < 0:
        return ""
    src = "外接电源供电" if p.ACLineStatus == 1 else "使用电池"
    return "电池 %d%%（%s）" % (pct, src)


def _item_network():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # UDP connect 不发包，只选路由
        ip = s.getsockname()[0]
        s.close()
        return "局域网 IP %s" % ip
    except OSError:
        return ""


def _item_active_window():
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    if not hwnd:
        return ""
    n = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, n + 1)
    title = buf.value.strip()
    return ("前台窗口「%s」" % title[:60]) if title else ""


_ITEMS = [
    ("uptime", _item_uptime), ("cpu", _item_cpu), ("memory", _item_memory),
    ("disk", _item_disk), ("battery", _item_battery), ("network", _item_network),
    ("active_window", _item_active_window),
]


def _collect(api):
    key = "|".join(k for k, _ in _ITEMS if _on(api, k))
    now = time.time()
    if _CACHE["key"] == key and now - _CACHE["ts"] < 300:
        return _CACHE["lines"]
    lines = []
    for k, fn in _ITEMS:
        if not _on(api, k):
            continue
        try:
            t = fn()
        except Exception as e:
            api.log("item %s fail %r" % (k, e))
            t = ""
        if t:
            lines.append("- " + t)
    _CACHE.update(key=key, ts=now, lines=lines)
    return lines


def _line(chat_type, chat_id, api):
    if chat_type != "private" or str(chat_id) != _owner_qq():
        return ""
    lines = _collect(api)
    if not lines:
        return ""
    return ("\n\n## 主人的电脑环境（真实数据；主人聊到电脑状态时可自然引用，"
            "其他话题不必提）\n" + "\n".join(lines))
