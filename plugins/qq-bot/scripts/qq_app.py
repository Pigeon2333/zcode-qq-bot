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

import atexit
import os
import sys
import threading
import time

import qq_common as C
import qq_services as S

APP_NAME = "ZCode QQ机器人"


def _fatal(title, message):
    """启动期致命错误：windowed 模式没有 stdout，必须写日志 + 弹窗，
    否则用户看到的现象就是「双击 exe 完全没反应」。"""
    text = "%s\n\n%s" % (title, message)
    try:
        log_dir = os.path.join(C.DATA_DIR, "logs")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "app.log"), "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
    except OSError:
        pass
    try:
        print(text, flush=True)
    except Exception:
        pass
    if not os.environ.get("QQBOT_NO_DIALOG"):
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text,
                                             APP_NAME + " 启动失败", 0x10)
        except Exception:
            pass


try:
    import webview
except Exception as e:
    _fatal("缺少/无法加载 pywebview",
           "打包版必须把 pywebview 一起打进去。\n"
           "请先执行：pip install pywebview pystray pillow\n"
           "然后重新运行 dev/build-exe.bat。\n\n详细错误：%r" % e)
    sys.exit(1)


def _asset_dir():
    """打包后 assets 在 _internal/assets（PyInstaller 的 _MEIPASS），源码在插件根。"""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", None) or os.path.dirname(
            os.path.abspath(sys.executable))
        return os.path.join(base, "assets")
    return os.path.join(os.path.dirname(C.SCRIPTS_DIR), "assets")


def _ensure_std_streams():
    """windowed 模式 stdout/stderr 可能为 None：挂到 app.log，避免 print 直接炸。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        log_dir = os.path.join(C.DATA_DIR, "logs")
        os.makedirs(log_dir, exist_ok=True)
        f = open(os.path.join(log_dir, "app.log"), "a", encoding="utf-8",
                 buffering=1)
    except OSError:
        return
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f


# 图标唯一真源在插件仓库 assets/（与插件市场图标同图）；数据目录作回退
_BUNDLED_ICON = _asset_dir()
APP_ICON = os.path.join(_BUNDLED_ICON, "app.ico") if os.path.exists(
    os.path.join(_BUNDLED_ICON, "app.ico")) else os.path.join(
    C.DATA_DIR, "assets", "app.ico")
APP_PNG = os.path.join(_BUNDLED_ICON, "icon.png") if os.path.exists(
    os.path.join(_BUNDLED_ICON, "icon.png")) else os.path.join(
    C.DATA_DIR, "assets", "app_icon.png")

# 退出收尾状态（窗口关闭与托盘退出共用；幂等）
_exit_state = {"action": None, "finalized": False}


def _finalize_exit():
    """桌面应用真正退出时的统一收尾，确保不留下本程序的服务进程。

    - exit_all：停机器人（agent/daemon/NapCat）+ 停控制台；
    - exit_app：只停控制台（它只服务于窗口），机器人组件保持运行。
    控制台是独立子进程，不会随窗口销毁而退出——这是此前「退出后仍有残留」
    的主要原因，必须在退出路径里显式停止并等端口释放。
    """
    if _exit_state["finalized"]:
        return
    _exit_state["finalized"] = True
    action = _exit_state["action"] or "exit_app"
    try:
        cfg = C.load_app_config()
    except Exception as e:
        print("exit: 读取配置失败 %r" % e, flush=True)
        return

    def _stop_console():
        try:
            print("exit: %s" % S.stop_ui(cfg), flush=True)
        except Exception as e:
            print("exit: 停止控制台失败 %r" % e, flush=True)

    if action == "exit_all":
        # 先停控制台：它的看门狗/启动线程可能在机器人停止后又把组件拉起来。
        _stop_console()
        try:
            for note in S.power_off(cfg, include_napcat=True):
                print("exit_all: %s" % note, flush=True)
            # 二次确认：覆盖「退出瞬间启动线程刚拉起组件」的极短竞态窗口
            time.sleep(0.5)
            for note in S.power_off(cfg, include_napcat=False):
                print("exit_all(confirm): %s" % note, flush=True)
        except Exception as e:
            print("exit_all cleanup failed: %r" % e, flush=True)
    else:
        # exit_app：控制台只服务于窗口，随应用退出；机器人组件保持运行。
        _stop_console()


def _tray_exit(action):
    """托盘菜单退出：先收尾，再硬退进程（避免 pystray 线程拖住解释器退出）。"""
    _exit_state["action"] = action
    _finalize_exit()
    os._exit(0)


def _confirm_exit_all():
    """exit_all 模式的二次确认（tkinter 本地弹窗，不依赖浏览器）。"""
    import tkinter.messagebox as mb
    try:
        from tkinter import Tk
        root = Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        ok = mb.askyesnocancel(
            APP_NAME,
            "关闭本应用时是否连机器人一起停止？\n\n"
            "是：停止机器人并退出（下次启动需重新登录）\n"
            "否：仅退出本应用，机器人保持运行\n"
            "取消：不关闭")
        root.destroy()
        return ok
    except Exception:
        return None


# ---------------- 单实例锁：命名 Mutex + 唤醒事件（防止托盘堆叠） ----------------
_MUTEX_NAME = "ZCodeQQBot::single-instance"
_WAKE_NAME = "ZCodeQQBot::wake"
_ERROR_ALREADY_EXISTS = 183
_mutex_handle = None

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateMutexW.restype = wintypes.HANDLE
    _k32.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR]
    _k32.CreateEventW.restype = wintypes.HANDLE
    _k32.CreateEventW.argtypes = [wintypes.LPCVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    _k32.OpenEventW.restype = wintypes.HANDLE
    _k32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    _k32.SetEvent.argtypes = [wintypes.HANDLE]
    _k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]


def _acquire_single_instance():
    """获取全局唯一锁；已有实例在运行返回 False（锁机制异常时放行，避免误杀）。"""
    global _mutex_handle
    if sys.platform != "win32":
        return True
    try:
        h = _k32.CreateMutexW(None, False, _MUTEX_NAME)
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            if h:
                _k32.CloseHandle(h)
            return False
        _mutex_handle = h  # 保活到进程结束
        return True
    except Exception:
        return True


def _signal_existing():
    """唤醒已运行实例的窗口（旧版本实例无唤醒事件时返回 False）。"""
    if sys.platform != "win32":
        return False
    try:
        h = _k32.OpenEventW(0x0002, False, _WAKE_NAME)  # EVENT_MODIFY_STATE
        if not h:
            return False
        _k32.SetEvent(h)
        _k32.CloseHandle(h)
        return True
    except Exception:
        return False


def _wake_listener(show_fn):
    """常驻线程：二次启动的实例触发唤醒事件后，把窗口带回前台。"""
    if sys.platform != "win32":
        return
    try:
        h = _k32.CreateEventW(None, False, False, _WAKE_NAME)
        if not h:
            return
        while True:
            if _k32.WaitForSingleObject(h, 1000) == 0:  # WAIT_OBJECT_0
                try:
                    show_fn()
                except Exception:
                    pass
    except Exception:
        pass


def _notify_already_running(woke):
    try:
        import tkinter.messagebox as mb
        from tkinter import Tk
        root = Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        mb.showinfo(APP_NAME, "应用已在运行，已为你唤出原窗口。" if woke else
                    "应用已在运行：请从系统托盘找回窗口（本次重复启动已阻止）。")
        root.destroy()
    except Exception:
        pass


def _run_service(args):
    """frozen（exe）模式的服务自举：qq_app.exe --service <daemon|agent|ui> …
    打包后各组件进程由本 exe 分身承载，参数与脚本模式一一对应。"""
    if len(args) < 2 or args[0] != "--service":
        return False
    which = args[1]
    rest = args[2:]
    if which == "ui":
        import qq_admin_ui as ui
        sys.argv = [sys.argv[0]] + rest
        ui.main()
    elif which == "daemon":
        import qq_daemon as dm
        sys.argv = [sys.argv[0]] + rest
        dm.main()
    elif which == "agent":
        import qq_bot_agent as ag
        sys.argv = [sys.argv[0]] + rest
        ag.main()
    elif which == "worker":
        import qq_task_worker as wk
        sys.argv = [sys.argv[0]] + rest
        wk.main()
    else:
        print("unknown service: %s" % which, flush=True)
    return True


def main():
    # windowed 打包没有控制台：先把 stdout/stderr 挂到 app.log，后续 print 不会炸
    _ensure_std_streams()
    # 构建产物自检：只验证 frozen 环境里 webview 能导入，不启动窗口/服务。
    # 打包脚本会以 QQBOT_NO_DIALOG=1 调用，失败时用退出码判断，不弹窗。
    if "--self-test" in sys.argv[1:]:
        print("self-test ok: webview=%s" % getattr(webview, "__file__", "?"),
              flush=True)
        sys.exit(0)
    # frozen 服务自举（打包后 spawn 的分身进程从这里进入对应组件）
    if getattr(sys, "frozen", False) and _run_service(sys.argv[1:]):
        return
    # 单实例锁：已有实例运行时唤出原窗口并退出，杜绝托盘图标堆叠
    if not _acquire_single_instance():
        _notify_already_running(_signal_existing())
        return
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    os.makedirs(os.path.join(C.DATA_DIR, "logs"), exist_ok=True)
    cfg = C.load_app_config()
    # 把自身 PID 传给控制台：窗口进程异常消失时控制台自动退出（防无主残留）
    msg = S.ensure_ui(cfg, parent_pid=os.getpid())  # 管理台是窗口的内容源，必须先就绪
    print(msg, flush=True)
    # 先确保令牌存在再拼 URL：首次安装时 ui_token 尚未生成会导致窗口落锁
    ui_tk = S.ensure_ui_token()
    # 任何正常退出路径（含未捕获异常）都走统一收尾
    atexit.register(_finalize_exit)

    # 设置了「打开应用自动启动机器人」：开窗后异步拉起（不阻塞窗口）
    if cfg.get("auto_power_on"):
        def _auto():
            print("auto_power_on: 启动中…", flush=True)
            for note in S.power_on(C.load_app_config()):
                print("auto_power_on:", note, flush=True)
        threading.Thread(target=_auto, daemon=True).start()

    window = webview.create_window(
        APP_NAME,
        "http://127.0.0.1:%d/?tk=%s" % (cfg["ui_port"], ui_tk),
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

    def _show_all_windows():
        for w in webview.windows:
            try:
                w.show()
            except Exception:
                pass

    threading.Thread(target=_wake_listener, args=(_show_all_windows,),
                     daemon=True).start()

    # 关闭行为处理：pywebview 6 用 closing 事件拦截
    def on_closing():
        cfg_now = C.load_app_config()
        action = str(cfg_now.get("close_action", "tray"))
        if action == "tray":
            if _start_tray():
                return False  # 隐藏到托盘，应用继续运行
            # 托盘不可用（缺 pystray/Pillow 等）：绝不把窗口藏起来——那会变成
            # 无窗口、无托盘、只能任务管理器结束的残留进程。退化为「退出应用」。
            print("托盘不可用：直接退出应用（机器人保持运行）", flush=True)
            action = "exit_app"
        elif action == "exit_all":
            ans = _confirm_exit_all()
            if ans is None:
                return False      # 取消：不关
            if ans is False:
                action = "exit_app"  # 仅退出应用，机器人保持
        # 实际停谁由 _finalize_exit 在窗口关闭后执行：
        # 避免在 closing 回调里同步停控制台（窗口内容源）造成卡死/半退出。
        _exit_state["action"] = action
        return True  # pywebview: 返回 True 允许关闭

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
    # 窗口已关闭：执行真正的进程收尾（幂等；托盘退出路径也会调用）
    _finalize_exit()
    # 兜底硬退：避免 pywebview/托盘残留的非守护线程把进程吊住
    os._exit(0)


_tray_started = {"flag": False}


def _start_tray():
    """启动系统托盘（幂等）。成功返回 True，失败返回 False（调用方不得隐藏窗口）。"""
    if _tray_started["flag"]:
        return True
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

        tray = pystray.Icon(
            "ZCodeQQBot", img, APP_NAME,
            menu=Menu(
                MenuItem("显示窗口", show_window, default=True),
                MenuItem("退出应用（机器人保持运行）",
                         lambda i, it: _tray_exit("exit_app")),
                MenuItem("退出并停止机器人",
                         lambda i, it: _tray_exit("exit_all")),
            ))
        threading.Thread(target=tray.run, daemon=True).start()
        _tray_started["flag"] = True
        # 只有托盘真正创建成功后才隐藏窗口
        for w in webview.windows:
            try:
                w.hide()
            except Exception:
                pass
        return True
    except Exception as e:
        print("tray unavailable: %r" % e, flush=True)
        return False


if __name__ == "__main__":
    main()
