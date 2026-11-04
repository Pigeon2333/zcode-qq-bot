#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""linux_headless.py — Linux 无 GUI 部署入口（服务器 7×24 模式）

架构与 Windows 版完全一致：admin UI(:39765) + daemon(:39763) + agent(:39764)
三个子进程，浏览器访问控制台。桌面外壳（pywebview/托盘/单实例锁）不参与。

启动方式：
  python3 linux_headless.py              # 前台运行（调试）
  systemd（见 deploy/zcode-qq-bot.service）# 服务器标准方式
  docker compose up -d（见 deploy/docker-compose.yml）# NapCat+bot 一键

首次启动后浏览器打开 http://<服务器IP>:39765/?tk=<令牌>
（令牌在 ~/.zcode-qq-bot/ui_token，或看启动日志输出的完整带令牌 URL）
"""

import argparse
import atexit
import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "plugins", "qq-bot", "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

_children = []


def _spawn(name, args):
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    p = subprocess.Popen([sys.executable, os.path.join(SCRIPTS, name)] + args,
                         env=env)
    _children.append(p)
    return p


def _shutdown(*_a):
    for p in _children:
        try:
            p.terminate()
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser(description="ZCode QQ Bot · Linux headless launcher")
    ap.add_argument("--no-agent", action="store_true", help="只跑 UI+daemon（调试）")
    ap.add_argument("--check", action="store_true", help="环境自检后退出")
    args = ap.parse_args()

    os.makedirs(os.path.expanduser("~/.zcode-qq-bot/logs"), exist_ok=True)

    if args.check:
        ok = True
        try:
            import NapCat  # noqa
        except ImportError:
            pass
        import socket
        for port in (39763, 39764, 39765):
            s = socket.socket()
            try:
                s.bind(("0.0.0.0", port))
            except OSError as e:
                print("[check] 端口 %d 被占用: %s" % (port, e))
                ok = False
            finally:
                s.close()
        py_ok = sys.version_info >= (3, 10)
        print("[check] python >=3.10:", "OK" if py_ok else "FAIL (%s)" % sys.version)
        try:
            import webview  # noqa
            print("[check] pywebview: 存在（headless 模式不需要，忽略）")
        except ImportError:
            print("[check] pywebview: 未装（headless 不需要）")
        print("[check] 结论:", "可启动" if ok else "有端口冲突，先处理")
        return

    import qq_services as S  # noqa: E402
    import qq_common as C    # noqa: E402

    cfg = C.load_app_config()
    print("[boot] 数据目录:", C.DATA_DIR, flush=True)
    msg = S.ensure_ui(cfg, parent_pid=os.getpid())
    print("[boot]", msg, flush=True)
    tk = S.ensure_ui_token()
    print("[boot] 控制台: http://0.0.0.0:%d/?tk=%s" % (cfg["ui_port"], tk), flush=True)
    print("[boot] 令牌文件: %s （浏览器打开控制台时用到）" % S.TOKEN_PATH
          if hasattr(S, "TOKEN_PATH") else "[boot] 令牌见 ui_token", flush=True)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    atexit.register(_shutdown)

    # daemon + agent（UI 已由 ensure_ui 拉起）
    _spawn("qq_daemon.py", ["--port", str(cfg.get("daemon_port", 39763))])
    if not args.no_agent:
        _spawn("qq_bot_agent.py", ["--port", str(cfg.get("daemon_port", 39763)),
                                   "--admin-port", str(cfg.get("agent_admin_port", 39764))])

    print("[boot] 全部组件已拉起。Ctrl+C 停止（systemd 下由管理信号自动停止）", flush=True)
    try:
        while True:
            time.sleep(5)
            # 子进程意外退出则整体退出（systemd 会重启整个单元）
            for p in _children:
                if p.poll() is not None:
                    print("[exit] 子进程 %s 退出（code=%s）——容器/systemd 将整体重启"
                          % (p.pid, p.returncode), flush=True)
                    _shutdown()
                    sys.exit(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
