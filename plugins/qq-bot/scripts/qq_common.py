#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qq_common.py — 公共基础：路径推导、环境配置、端口约定

发布适配原则：
  - 一切路径由本模块动态推导，任何业务文件禁止硬编码绝对路径
  - 环境差异（QQNT 安装目录、机器人账号、端口）集中在 app_config.json，
    不存在时自动生成默认值（QQNT 目录用注册表探测）
  - 数据目录 ~/.zcode-qq-bot/（跟随用户家目录，跨用户可用）
"""

import json
import os
import pathlib
import time
import winreg

# 脚本目录（本文件所在目录），所有业务脚本与其同级
SCRIPTS_DIR = os.path.dirname(os.path.realpath(__file__))
# 数据目录（用户家目录下）
DATA_DIR = os.path.join(os.path.expanduser("~"), ".zcode-qq-bot")
DATA_ROOT = pathlib.Path(DATA_DIR)

APP_CONFIG_PATH = DATA_ROOT / "app_config.json"

DEFAULT_CONFIG = {
    "qqnt_dir": "",            # 空 = 自动探测注册表；探测失败由用户在设置页填写
    "bot_qq": "",              # 机器人 QQ 号
    "owner_qq": "",            # 主人 QQ（任务模式与私聊遥控的唯一白名单，空=禁用）
    "task_workspace": "",      # 任务沙箱根目录（空=~/.zcode-qq-bot/tasks；可指向 ZCode 工作区子目录）
    "napcat_ws_port": 3001,
    "daemon_port": 39763,
    "agent_admin_port": 39764,
    "ui_port": 39765,
    "autostart_with_windows": False,
    "auto_power_on": False,    # 打开应用窗口后自动启动机器人组件
    "close_action": "tray",    # tray=最小化到托盘 | exit_app=退出应用(机器人保持) | exit_all=退出并停止机器人
    "review_time": "03:30",    # 夜间复盘触发时间（HH:MM）
    "backup_enabled": True,    # 每日数据快照开关
    "backup_time": "04:00",    # 备份触发时间（HH:MM）
    "backup_dir": "",          # 备份目录（空=~/.zcode-qq-bot/backups）
    "backup_keep_days": 30,    # 备份保留天数（超过自动清理）
    "backup_keep_copies": 30,  # 每个备份保留份数（超过自动清理最旧的）
    "timezone": "Asia/Shanghai",  # 机器人时区（IANA 名，影响 AI 感知的时间）
    "device_ctrl_enabled": False,  # 设备操控（高危，默认关）
    "device_ctrl_2fa": True,       # 设备操控动态授权码（每次需验证）
    "device_ctrl_protected_dirs": [],  # 额外保护目录（不可搜索/发送）
    "sched_pause_enabled": False,  # 定时暂停（时段内停止一切回复+零消耗）
    "sched_pause_start": "23:00",  # 暂停开始时间
    "sched_pause_end": "07:00",    # 暂停结束时间（支持跨午夜）
    "watchdog_enabled": True,      # NapCat 活性看门狗（半死连接检测）
    "watchdog_mode": "auto",       # auto=自动恢复（重连+重启 NapCat）| alert=仅告警
    "watchdog_interval": 120,      # 探测间隔秒（60-600）
}

# 备份/恢复统一文件清单（basename 冲突无；子目录文件扁平存放）
BACKUP_FILES = [
    "styles/current_style.md",   # 风格卡真实路径（根目录 style.md 是 0 字节占位）
    "styles/evolved.json",       # 自进化风格（同上，根目录同名文件非真实数据）
    "memory_stream.jsonl",
    "group_reply_modes.json", "reply_config.json",
    "private_config.json", "group_whitelist.json",
    "model_chain.json", "app_config.json",
    "stickers.json",            # 表情包三库索引+视觉描述缓存
    "device_ctrl_flow.json",    # 设备操控流程自定义
    "cron_tasks.json",          # 定时任务
    "proactive_state.json",     # 主动消息发送状态
    "personas/collection.json", # 人设合集（含激活项）
    "usage_log.jsonl",          # token 用量流水（统计）
]
# 密钥类（api_keys.json / zhipu_api_key / token / ws_token）刻意不备份：备份目录
# 可能被用户指向网盘同步盘，避免明文密钥外泄。


def detect_qqnt_dir():
    """从注册表探测 QQNT 安装目录（NapCat launcher 同款逻辑）。"""
    for root in ("SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\QQ",
                 "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\QQ"):
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root)
            try:
                uninstall, _ = winreg.QueryValueEx(key, "UninstallString")
            finally:
                winreg.CloseKey(key)
        except OSError:
            continue
        qq_dir = os.path.dirname(os.path.abspath(uninstall.strip('"')))
        if os.path.isfile(os.path.join(qq_dir, "QQ.exe")):
            return qq_dir
    return ""


def load_app_config():
    """读 app_config.json；不存在或格式异常时返回默认值（不回写，写由显式保存触发）。"""
    cfg = dict(DEFAULT_CONFIG)
    try:
        data = json.loads(APP_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    if isinstance(data, dict):
        for k in DEFAULT_CONFIG:
            if k in data:
                cfg[k] = data[k]
    return cfg


def save_app_config(cfg):
    """写 app_config.json（仅数据目录内；pathlib 常量路径）。

    Windows 下 os.replace 需要目标文件没有其他进程持有句柄——agent/daemon
    高频读取本文件时原子替换会偶发 WinError 5。策略：
    ① 最多重试 6 次 os.replace（每次间隔递增）；② 仍失败则退化为就地写入。
    """
    merged = load_app_config()
    for k in DEFAULT_CONFIG:
        if k in cfg:
            merged[k] = cfg[k]
    text = json.dumps(merged, ensure_ascii=False, indent=2)
    tmp = DATA_ROOT / "app_config.json.tmp"
    tmp.write_text(text, encoding="utf-8")
    last = None
    for attempt in range(6):
        try:
            os.replace(str(tmp), str(APP_CONFIG_PATH))
            return merged
        except PermissionError as e:
            last = e
            time.sleep(0.08 * (attempt + 1))
    # 兜底：就地覆盖（放弃原子性，保证保存成功）；失败才向上抛
    try:
        APP_CONFIG_PATH.write_text(text, encoding="utf-8")
        os.remove(str(tmp)) if tmp.exists() else None
        return merged
    except OSError:
        raise last if last else OSError("保存 app_config 失败")


def qqnt_dir_resolved(cfg=None):
    """返回可用的 QQNT 目录：配置优先，否则注册表探测。"""
    cfg = cfg or load_app_config()
    d = str(cfg.get("qqnt_dir") or "").strip()
    if d and os.path.isfile(os.path.join(d, "QQ.exe")):
        return d
    return detect_qqnt_dir()


# ---------------- 高峰降耗（econ）：daemon 与 agent 共用的生效判定 ----------------

def _hhmm_to_min(s, default):
    try:
        h, m = str(s).strip().split(":")
        v = int(h) * 60 + int(m)
        return v if 0 <= v < 24 * 60 else default
    except (ValueError, AttributeError):
        return default


def econ_active(cfg):
    """高峰降耗此刻是否生效（reply_config 的 econ_* 字段）。
    econ_enabled 总开关关闭 → 不生效；econ_manual=true 立即开启（不限时）；
    否则按时间表：econ_days 命中当天（0=周一…6=周日，空=每天）+ start<=now<end（支持跨午夜）。
    兼容旧字段：econ_mode_override="on" / econ_workdays_only。异常一律 False。"""
    try:
        if not cfg.get("econ_enabled"):
            return False
        if cfg.get("econ_manual"):
            return True
        if str(cfg.get("econ_mode_override") or "") == "on":  # 旧三态兼容
            return True
        lt = time.localtime()
        days = cfg.get("econ_days")
        if isinstance(days, list) and days:
            try:
                allowed = [int(d) for d in days]
            except (TypeError, ValueError):
                allowed = []
            if allowed and lt.tm_wday not in allowed:
                return False
        elif days is None and cfg.get("econ_workdays_only", True) and lt.tm_wday >= 5:
            return False  # 旧仅工作日开关兼容
        cur = lt.tm_hour * 60 + lt.tm_min
        s = _hhmm_to_min(cfg.get("econ_start", "14:00"), 14 * 60)
        e = _hhmm_to_min(cfg.get("econ_end", "18:00"), 18 * 60)
        if s <= e:
            return s <= cur < e
        return cur >= s or cur < e  # 跨午夜时段（如 22:00-06:00）
    except Exception:
        return False


def tz_now_info():
    """按配置时区返回结构化当前时间（agent 注入 system prompt 用）。
    时区名无效时回退系统本地时间。"""
    import zoneinfo
    tz_name = str(load_app_config().get("timezone") or "Asia/Shanghai").strip()
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except (ValueError, KeyError, OSError):
        tz = None
    import datetime
    now = datetime.datetime.now(tz) if tz else datetime.datetime.now()
    wd = ["一", "二", "三", "四", "五", "六", "日"][now.weekday()]
    ampm = "上午" if now.hour < 12 else ("下午" if now.hour < 18 else "晚上")
    h12 = now.hour if 0 < now.hour < 13 else (now.hour - 12 if now.hour >= 13 else 12)
    return {
        "iso": now.strftime("%Y-%m-%d"),
        "weekday": "周%s" % wd,
        "clock24": now.strftime("%H:%M"),
        "clock12": "%s%d:%02d" % (ampm, h12, now.minute),
        "hour": now.hour,
        "tz": tz_name,
    }


def gen_auth_code():
    """生成 6 位数字动态授权码（设备操控用）。"""
    import secrets
    return str(secrets.randbelow(900000) + 100000)
