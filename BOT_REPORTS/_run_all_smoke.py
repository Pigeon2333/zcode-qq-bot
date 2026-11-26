# -*- coding: utf-8 -*-
"""一键跑全部离线冒烟，并核对跑前跑后生产数据目录里的配置文件一字未变。

用法：python BOT_REPORTS/_run_all_smoke.py [脚本名片段…]
      （不带参数 = 全部 _smoke_*.py；带片段 = 只跑文件名含该片段的）

冒烟脚本读的是真实的 ~/.zcode-qq-bot（人设、白名单等，为的是提示词真实），
所以任何一处误写都会直接落到线上数据上——2026-11-24 _smoke_gadmin_chat.py
就把 group_admin.json 覆盖成了空配置。这个入口在跑前跑后给数据目录顶层和
plugins_data/ 下的 *.json 做指纹比对，有变化就报出来并非零退出。

退出码：0 全部通过且配置未变；1 有用例失败或脚本异常；2 配置文件被改动。
"""
import hashlib
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
HERE = ROOT / "BOT_REPORTS"
DATA = pathlib.Path.home() / ".zcode-qq-bot"
# 线上机器人自己会周期性写的状态文件。机器人在线时它们本来就会变，不算冒烟的副作用，
# 这时跳过比对；机器人不在线时全部严格比对（任何变化都报）。
LIVE_WRITTEN = {"proactive_state.json", "daily_word.json",
                "plugins_data/bili_sub.json", "plugins_data/style_learn.json"}
# 个别脚本按自己所在的目录找源码
CWD_OVERRIDE = {"_smoke_vision_merge2.py": HERE}
TIMEOUT = 300


def daemon_online():
    """daemon 控制端口能不能连上（只连不发）。"""
    import socket
    try:
        s = socket.create_connection(("127.0.0.1", 39763), timeout=1.5)
    except OSError:
        return False
    s.close()
    return True


def sigs(strict):
    out = {}
    pats = ("*.json", "plugins_data/*.json")
    for pat in pats:
        for p in sorted(DATA.glob(pat)):
            rel = str(p.relative_to(DATA)).replace("\\", "/")
            if not strict and rel in LIVE_WRITTEN:
                continue
            try:
                out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
            except OSError as e:
                out[rel] = "unreadable:%r" % e
    return out


def main():
    want = [a for a in sys.argv[1:] if not a.startswith("--")]
    scripts = sorted(p for p in HERE.glob("_smoke_*.py")
                     if not want or any(w in p.name for w in want))
    if not scripts:
        raise SystemExit("没有匹配的冒烟脚本")
    if not DATA.is_dir():
        raise SystemExit("找不到数据目录：%s" % DATA)
    strict = not daemon_online()
    print("线上 daemon %s：配置核对%s" % ("在线" if not strict else "不在线",
                                   "跳过机器人自己会写的 %d 个文件" % len(LIVE_WRITTEN) if not strict else "覆盖全部文件"))
    before = sigs(strict)
    bad, rows = 0, []
    t0 = time.time()
    for s in scripts:
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
        try:
            r = subprocess.run([sys.executable, str(s)], cwd=str(CWD_OVERRIDE.get(s.name, ROOT)),
                               capture_output=True, timeout=TIMEOUT, env=env)
            text = r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")
            code = r.returncode
        except subprocess.TimeoutExpired:
            text, code = "", "超时"
        n_pass = sum(1 for l in text.splitlines() if l.startswith("PASS"))
        n_fail = sum(1 for l in text.splitlines() if l.startswith("FAIL"))
        ok = code == 0 and n_pass > 0 and n_fail == 0
        bad += 0 if ok else 1
        rows.append((s.name, code, n_pass, n_fail, ok))
        if not ok:
            tail = "\n".join("      " + l[:200] for l in text.strip().splitlines()[-8:])
            print("✗ %s\n%s" % (s.name, tail))
    print()
    for name, code, n_pass, n_fail, ok in rows:
        print("%s %-28s 退出=%s  通过=%d  失败=%d" % ("✓" if ok else "✗", name, code, n_pass, n_fail))
    print("合计：%d 个脚本，%d 项通过，%d 项失败，用时 %.0f 秒" % (
        len(rows), sum(r[2] for r in rows), sum(r[3] for r in rows), time.time() - t0))

    after = sigs(strict)
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    if changed:
        print("\n⚠ 数据目录里的配置文件在冒烟期间被改动（%d 个）：" % len(changed))
        for k in changed:
            print("   ", k, "(新增)" if k not in before else "(被删除)" if k not in after else "(内容变化)")
        print("可能是线上机器人自己在写（例如群成员画像、风格学习、各组件日志）。核对方法：看该文件的修改时间是否和线上日志里某一行是同一秒。")
        print("确认是线上进程写的，可以把本次结果按『测试全过』处理，并在汇报里写明是哪一个文件。")
        return 2
    print("配置文件核对：%d 个文件跑前跑后一致" % len(before))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
