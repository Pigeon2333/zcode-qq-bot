#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qq_app.py — ZCode QQ机器人 主应用（窗口 + 托盘）

行为：
  - 启动 = 确保管理台在运行并打开窗口（默认不启动机器人组件）
  - 设置「打开应用自动启动机器人」开启时，开窗后自动 power_on（异步）
  - 关闭行为三模式（设置页选择）：
      tray      最小化到系统托盘（默认；托盘菜单可显示窗口/退出）
      exit_app  退出应用（机器人组件保持运行）
      exit_all  退出应用并停止机器人（含 NapCat，需二次确认）
  - 与「开机自启」组合时顺序天然正确：Windows 启动本应用 → 应用内再按
    auto_power_on 决定是否启动机器人（同一进程内先后执行）

依赖：pywebview（窗口）、pystray+Pillow（托盘）
"""

import os
import sys
import threading

try:
    import webview
except ImportError:
    print("缺少 pywebview：请先执行  pip install pywebview")
    sys.exit(1)

import qq_common as C
import qq_services as S

APP_NAME = "ZCode QQ机器人"
# 图标唯一真源在插件仓库 assets/（与插件市场图标同图）；数据目录作回退
_BUNDLED_ICON = os.path.join(os.path.dirname(C.SCRIPTS_DIR), "assets")
APP_ICON = os.path.join(_BUNDLED_ICON, "app.ico") if os.path.exists(
    os.path.join(_BUNDLED_ICON, "app.ico")) else os.path.join(
    C.DATA_DIR, "assets", "app.ico")
APP_PNG = os.path.join(_BUNDLED_ICON, "icon.png") if os.path.exists(
    os.path.join(_BUNDLED_ICON, "icon.png")) else os.path.join(
    C.DATA_DIR, "assets", "app_icon.png")


def _confirm_exit_all():
    """exit_all 模式的二次确认（tkinter 本地弹窗，不依赖浏览器）。"""
    import tkinter.messagebox as mb
    try:
        from tkinter import Tk
        root = Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        ok = mb.askyesnocancel(
            "ZCode QQ机器人",
            "确定要关闭应用并停止机器人吗？\n\n"
            "「是」= 停止 NapCat/daemon/agent 并退出\n"
            "「否」= 仅退出应用，机器人保持运行\n"
            "「取消」= 什么都不做，回到应用")
        root.destroy()
        return ok
    except Exception:
        return None


def main():
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    os.makedirs(os.path.join(C.DATA_DIR, "logs"), exist_ok=True)
    cfg = C.load_app_config()
    msg = S.ensure_ui(cfg)  # 管理台是窗口的内容源，必须先就绪
    print(msg, flush=True)

    # 设置了「打开应用自动启动机器人」：开窗后异步拉起（不阻塞窗口）
    if cfg.get("auto_power_on"):
        def _auto():
            print("auto_power_on: 启动中…", flush=True)
            for note in S.power_on(C.load_app_config()):
                print("auto_power_on:", note, flush=True)
        threading.Thread(target=_auto, daemon=True).start()

    window = webview.create_window(
        APP_NAME,
        "http://127.0.0.1:%d/" % cfg["ui_port"],
        width=1180, height=820, min_size=(900, 620))
    del window

    def _ui_watchdog():
        """UI 服务重启恢复后自动刷新窗口（管理台升级/重启时窗口自愈，
        不会停留在旧页面或「加载失败」）。"""
        import time as _t
        import socket
        was_up = True
        while True:
            _t.sleep(5)
            try:
                s = socket.create_connection(("127.0.0.1", cfg["ui_port"]), timeout=2)
                s.close()
                up = True
            except OSError:
                up = False
            if up and not was_up:
                for w in webview.windows:
                    try:
                        w.evaluate_js("location.reload()")
                    except Exception:
                        pass
            was_up = up

    threading.Thread(target=_ui_watchdog, daemon=True).start()

    # 关闭行为处理：pywebview 6 用 closing 事件拦截
    close_state = {"action": None}

    def on_closing():
        cfg_now = C.load_app_config()
        action = str(cfg_now.get("close_action", "tray"))
        if action == "tray":
            _start_tray()
            return False  # 返回 False 阻止默认关闭？pywebview closing 返回 True 才允许关闭
        if action == "exit_all":
            ans = _confirm_exit_all()
            if ans is None:
                return False      # 取消：不关
            if ans is False:
                return True       # 仅退应用
            for note in S.power_off(cfg_now, include_napcat=True):
                print("exit_all:", note, flush=True)
            return True
        return True  # exit_app

    # pywebview: closing 回调返回 True 允许关闭，False 取消
    try:
        window_ref = webview.windows[-1] if webview.windows else None
        if window_ref is not None:
            window_ref.events.closing += on_closing
    except Exception:
        pass

    kwargs = {}
    if os.path.isfile(APP_ICON):
        kwargs["icon"] = APP_ICON
    webview.start(**kwargs)


_tray_started = {"flag": False}


def _start_tray():
    """启动系统托盘（幂等）：显示窗口 / 彻底退出（不动机器人）。"""
    if _tray_started["flag"]:
        return
    _tray_started["flag"] = True
    try:
        from PIL import Image
        import pystray
        from pystray import Menu, MenuItem

        img = Image.open(APP_PNG if os.path.isfile(APP_PNG) else APP_ICON)

        def show_window(icon=None, item=None):
            for w in webview.windows:
                try:
                    w.show()
                except Exception:
                    pass

        def quit_app(icon=None, item=None):
            try:
                icon.stop()
            except Exception:
                pass
            for w in webview.windows:
                try:
                    w.destroy()
                except Exception:
                    pass
            os._exit(0)

        tray = pystray.Icon(
            "ZCodeQQBot", img, APP_NAME,
            menu=Menu(
                MenuItem("显示窗口", show_window, default=True),
                MenuItem("退出应用（机器人保持运行）", quit_app),
            ))
        threading.Thread(target=tray.run, daemon=True).start()
        for w in webview.windows:
            try:
                w.hide()
            except Exception:
                pass
    except Exception as e:
        print("tray unavailable: %r" % e, flush=True)
        # 托盘不可用时退化为直接隐藏（进程保留，可从任务管理器/再次双击找回）
        for w in webview.windows:
            try:
                w.hide()
            except Exception:
                pass


if __name__ == "__main__":
    main()
