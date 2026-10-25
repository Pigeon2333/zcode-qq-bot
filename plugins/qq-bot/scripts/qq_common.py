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
}


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
    """原子写 app_config.json（仅数据目录内；pathlib 常量路径）。"""
    merged = load_app_config()
    for k in DEFAULT_CONFIG:
        if k in cfg:
            merged[k] = cfg[k]
    tmp = DATA_ROOT / "app_config.json.tmp"
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(APP_CONFIG_PATH))
    return merged


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
