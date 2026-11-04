# -*- coding: utf-8 -*-
"""qq_chatimport.py — 聊天记录导入与学习引擎（宿主模块，QQ/微信共用）。

安全与隔离模型：
  - 只读用户主动选择的导出文件（md/txt/json）；解析结果存
    DATA_DIR/chat_import/<source>/<会话>/messages.json —— 原料库，
    不进记忆、不进上下文，任何注入都不会引用
  - 「学习」由 agent 侧 LLM 消化采样片段，产出人设草稿与记忆候选；
    是否采纳由用户在插件页确认后经既有 /api/personas/save 与
    /api/memory/add 落盘（覆盖人设走 UI danger 二次确认）
  - 文件名/会话名白名单清洗，杜绝路径穿越
"""

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

import qq_common as C

BASE_DIR = os.path.join(C.DATA_DIR, "chat_import")
SOURCES = {
    "qq": {"label": "QQ", "hint": "用 qq-cli 导出（md/json），或任何「时间 发送人： 内容」格式的文本"},
    "wx": {"label": "微信", "hint": "用 wechat-cli 的 export_markdown.py 导出，或任何「时间 发送人： 内容」格式的文本"},
}

# qq-cli 技能脚本（本机已装时可直接调用导出）
_QQCLI_SKILL = os.path.join(
    os.environ.get("USERPROFILE", ""), ".zcode", "skills", "qq-cli",
    "scripts", "qq_cli.py")


def qq_cli_available():
    return os.path.isfile(_QQCLI_SKILL)


CREATE_NO_WINDOW = 0x08000000  # 后台子进程，避免控制台窗口闪现

_PY_EXE_CACHE = None


def _py_exe():
    """跑外部 python 脚本用的解释器（qq-cli / wechat-cli 均为纯标准库）。

    打包版里 sys.executable 是本应用 exe 本身——直接用它跑脚本会误启
    第二个应用实例（单实例互斥弹「应用已在运行」，exe 加载托盘图标还
    打印 libpng 噪音），所以冻结环境必须找系统 Python。"""
    global _PY_EXE_CACHE
    if _PY_EXE_CACHE is not None:
        return list(_PY_EXE_CACHE) if _PY_EXE_CACHE else None
    cands = []
    if not getattr(sys, "frozen", False):
        cands.append([sys.executable])
    else:
        for name in ("py", "python", "python3"):
            w = shutil.which(name)
            if w:
                cands.append([w, "-3"] if name == "py" else [w])
    for cmd in cands:
        try:
            r = subprocess.run(cmd + ["-c", "import sys"],
                               capture_output=True, timeout=20,
                               creationflags=CREATE_NO_WINDOW)
            if r.returncode == 0:
                _PY_EXE_CACHE = cmd
                return list(cmd)
        except (OSError, subprocess.TimeoutExpired):
            continue
    _PY_EXE_CACHE = []
    return None


_NOISE_MARKS = ("libpng", "iccp", "deprecat", "userwarning",
                "deprecationwarning", "futurewarning")


def _clean_noise(*streams):
    """合并子进程输出并滤掉无害告警（libpng iCCP / Deprecation 等），
    取尾部几行作为可读的错误详情。"""
    lines = []
    for s in streams:
        try:
            txt = (s or b"").decode("utf-8", "replace")
        except AttributeError:
            txt = str(s or "")
        for ln in txt.splitlines():
            t = ln.strip()
            if t and not any(m in t.lower() for m in _NOISE_MARKS):
                lines.append(t)
    return " | ".join(lines[-4:])


def probe(source):
    """环境探测：客户端进程 + 常见聊天数据目录候选（供向导展示）。"""
    src = source if source in SOURCES else "qq"
    exe = {"qq": "QQ.exe", "wx": "Weixin.exe"}.get(src, "")
    running = False
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq %s" % exe,
                              "/NH"], capture_output=True, timeout=15,
                             creationflags=CREATE_NO_WINDOW)
        running = exe.lower() in out.stdout.decode("gbk", "replace").lower()
    except (OSError, subprocess.TimeoutExpired):
        pass
    user = os.environ.get("USERPROFILE", "")
    appdata = os.environ.get("APPDATA", "")
    docs_tf = os.path.join(user, "Documents", "Tencent Files")
    xw = os.path.join(appdata, "Tencent", "xwechat_files")
    if src == "qq":
        cands = [
            ("新版 QQ 数据目录（nt_qq）",
             [os.path.join(appdata, "Tencent", "QQ", "nt_qq", d)
              for d in (os.listdir(os.path.join(appdata, "Tencent", "QQ",
                                                "nt_qq"))
                        if os.path.isdir(os.path.join(
                            appdata, "Tencent", "QQ", "nt_qq")) else [])]),
            ("旧版 QQ 数据目录（Documents\\Tencent Files）",
             [os.path.join(docs_tf, d) for d in
              (os.listdir(docs_tf) if os.path.isdir(docs_tf) else [])]),
        ]
    else:
        cands = [
            ("微信 4.x 数据目录（xwechat_files）",
             [os.path.join(xw, d) for d in
              (os.listdir(xw) if os.path.isdir(xw) else [])]),
            ("微信 3.x 数据目录（Documents\\WeChat Files）",
             [os.path.join(user, "Documents", "WeChat Files", d) for d in
              (os.listdir(os.path.join(user, "Documents", "WeChat Files"))
               if os.path.isdir(os.path.join(user, "Documents",
                                             "WeChat Files")) else [])]),
        ]
    found = []
    for label, dirs in cands:
        for d in dirs:
            if os.path.isdir(d):
                found.append({"label": label, "path": d})
    out = {"client_running": running, "data_dirs": found}
    if src == "qq":
        out["qq_cli_available"] = qq_cli_available()
        out["python_ok"] = _py_exe() is not None
        out["qq_key_ready"] = qq_key_configured()
    else:
        out["wx_skill_installed"] = wx_export_script() is not None
        out["wx_cli_cmd"] = wx_cli_cmd() or ""
    return out


def qq_key_configured():
    """qq-cli 是否已配置过密钥与数据目录（只查配置存在，不验证有效性）。"""
    try:
        p = pathlib.Path.home() / ".qq-cli" / "config.json"
        cfg = json.loads(p.read_text(encoding="utf-8"))
        return bool(cfg.get("key")) and bool(cfg.get("db_dir"))
    except (OSError, ValueError):
        return False


_INIT_HINT = ("首次使用需提取一次 QQ 数据库密钥：终端运行 "
              "python %s init （保持 QQ 登录状态，按提示在 QQ 搜索框"
              "输入任意词触发提取；自定义数据目录加 --db-dir 指到 "
              "nt_db 所在目录）" % _QQCLI_SKILL)


def qq_cli_export(chat, on_progress=None):
    """调用本机 qq-cli 技能导出指定会话（json）到收件区并入库。
    需要先手工运行过 qq_cli.py init（密钥提取）。on_progress(phase,pct,msg)。"""
    prog = on_progress or (lambda *a, **k: None)
    if not qq_cli_available():
        return {"ok": False,
                "error": "本机未安装 qq-cli 技能"
                         "（skillhub.cn 搜 xujianpeng/qq-cli 安装后重试）"}
    py = _py_exe()
    if not py:
        return {"ok": False,
                "error": "打包版调用 qq-cli 需要系统装有 Python 3"
                         "（python.org 下载安装后重试）"}
    inbox = pathlib.Path(BASE_DIR) / "qq" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    outp = str(inbox / ("export_%d.json" % int(time.time())))
    prog("export", 15, "调用 qq-cli 导出「%s」（最长 5 分钟，大库较久）"
         % str(chat)[:24])
    try:
        r = subprocess.run(
            py + [_QQCLI_SKILL, "export", str(chat)[:40],
                  "--format", "json", "--output", outp],
            capture_output=True, timeout=300,
            creationflags=CREATE_NO_WINDOW)
        if r.returncode != 0 or not os.path.isfile(outp):
            detail = _clean_noise(r.stderr, r.stdout)
            low = detail.lower()
            if "crypto" in low or "pycryptodome" in low:
                detail = ("系统 Python 缺少 pycryptodome——终端执行："
                          "pip install pycryptodome 后重试")
            elif ("init" in low or "解密" in detail or "明文" in detail):
                detail = ("QQ 数据库密钥未就绪——" + _INIT_HINT
                          + "（原始输出：" + detail[:80] + "）")
            return {"ok": False, "error": detail or
                    ("导出失败且无输出。" + _INIT_HINT)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "导出超时（300 秒）"}
    prog("store", 85, "导出完成，解析并入库…")
    res = store("qq", str(chat)[:40], outp)
    try:
        os.remove(outp)
    except OSError:
        pass
    if res.get("ok"):
        res["sessions"] = sessions("qq")
    return res


# ---- 微信：wechat-cli 技能（skillhub user_514405d2）+ py-wechat-cli 命令 ----
def _wx_skill_dirs():
    """wechat-cli 技能可能装到的位置（ZCode 用户目录 / 应用旁目录）。"""
    dirs = [os.path.join(os.environ.get("USERPROFILE", ""),
                         ".zcode", "skills", "wechat-cli")]
    if getattr(sys, "frozen", False):
        # skillhub 默认装到「当前目录」，打包版工作目录在 exe 旁
        base = os.path.dirname(os.path.abspath(sys.executable))
        dirs.append(os.path.join(os.path.dirname(base), "skills",
                                 "@user_514405d2", "wechat-cli"))
        dirs.append(os.path.join(base, "skills", "wechat-cli"))
    else:
        dirs.append(os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))), "skills",
            "@user_514405d2", "wechat-cli"))
    return dirs


def wx_export_script():
    for d in _wx_skill_dirs():
        p = os.path.join(d, "scripts", "export_markdown.py")
        if os.path.isfile(p):
            return p
    return None


def wx_cli_cmd():
    """定位 py-wechat-cli 的 wechat-cli 命令（which + SKILL.md 约定路径）。"""
    w = shutil.which("wechat-cli")
    if w:
        return w
    user = os.environ.get("USERPROFILE", "")
    for c in (os.path.join(user, ".workbuddy", "binaries", "python",
                           "envs", "default", "Scripts", "wechat-cli.exe"),
              os.path.join(user, "AppData", "Local", "Programs", "Python",
                           "Python313", "Scripts", "wechat-cli.exe")):
        if os.path.isfile(c):
            return c
    return None


def wx_cli_export(chat, on_progress=None):
    """调用 wechat-cli 技能（export_markdown.py 清洗版）导出会话并入库。
    on_progress(phase,pct,msg)。"""
    prog = on_progress or (lambda *a, **k: None)
    script = wx_export_script()
    cli = wx_cli_cmd()
    missing = []
    if not script:
        missing.append("①skillhub 安装 wechat-cli 技能")
    if not cli:
        missing.append("②源码安装 py-wechat-cli 命令")
    if missing:
        return {"ok": False,
                "error": "微信导出未就绪，缺：" + "、".join(missing) +
                         "（插件页第②步有完整命令）"}
    py = _py_exe()
    if not py:
        return {"ok": False,
                "error": "打包版调用 wechat-cli 需要系统装有 Python 3"
                         "（python.org 下载安装后重试）"}
    inbox = pathlib.Path(BASE_DIR) / "wx" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    outp = str(inbox / ("wx_%d.md" % int(time.time())))
    prog("export", 15, "调用 wechat-cli 导出「%s」（最长 5 分钟）"
         % str(chat)[:24])
    try:
        r = subprocess.run(
            py + [script, str(chat)[:40], "--cli", cli,
                  "--output", outp, "--no-raw"],
            capture_output=True, timeout=300,
            creationflags=CREATE_NO_WINDOW)
        if r.returncode != 0 or not os.path.isfile(outp):
            detail = _clean_noise(r.stderr, r.stdout)
            return {"ok": False, "error": detail or
                    "导出失败且无输出——通常是还没提取密钥"
                    "（微信登录状态下跑一次技能里的 extract_keys.py）"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "导出超时（300 秒）"}
    prog("store", 85, "导出完成，解析并入库…")
    res = store("wx", str(chat)[:40], outp)
    try:
        os.remove(outp)
    except OSError:
        pass
    if res.get("ok"):
        res["sessions"] = sessions("wx")
        if not res.get("count"):
            res = dict(res)
            res["ok"] = False
            res["error"] = ("导出的文件里没认出消息行（格式不认识）——"
                            "请把文件发作者看看格式")
    return res

_MAX_FILE = 80 * 1024 * 1024       # 80MB 导出文件上限
_MAX_MSGS = 200000                 # 单会话消息上限

_TS_PATTERNS = [
    # **2026-01-02 15:30:12** 张三: 内容  （qq-cli md）
    re.compile(r"^\*\*(\d{4}[-/]\d{2}[-/]\d{2})[ T](\d{2}:\d{2}(?::\d{2})?)\*\*\s*([^:：]{1,32})[:：]\s*(.*)$"),
    # [2026-01-02 15:30] 张三: 内容 / [2026/01/02 15:30:12] 张三：内容
    re.compile(r"^\[(\d{4}[-/]\d{2}[-/]\d{2})[ T](\d{2}:\d{2}(?::\d{2})?)\]\s*([^:：]{1,32})[:：]\s*(.*)$"),
    # 2026-01-02 15:30:12 张三: 内容 / 2026/01/02 15:30 张三: 内容（裸时间开头）
    re.compile(r"^(\d{4}[-/]\d{2}[-/]\d{2})[ T](\d{2}:\d{2}(?::\d{2})?)\s+([^:：]{1,32})[:：]\s*(.*)$"),
]
_JSON_TIME_KEYS = ("time", "ts", "timestamp", "datetime", "date")
_JSON_SENDER_KEYS = ("sender", "sender_name", "name", "nick", "nickname", "from")
_JSON_TEXT_KEYS = ("text", "content", "msg", "message", "body")


def _safe_name(s, default="session"):
    s = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff-]+", "_", str(s or "").strip())
    return (s[:40] or default).strip("_") or default


def _norm_ts(date_s, time_s):
    d = re.sub(r"[/.]", "-", str(date_s))
    t = str(time_s or "00:00")
    if t.count(":") == 1:
        t += ":00"
    try:
        return int(time.mktime(time.strptime("%s %s" % (d, t), "%Y-%m-%d %H:%M:%S")))
    except ValueError:
        return 0


def _parse_json_list(rows):
    out = []
    for it in rows:
        if not isinstance(it, dict):
            continue
        t_raw = next((it[k] for k in _JSON_TIME_KEYS if it.get(k)), "")
        ts = 0
        if isinstance(t_raw, (int, float)):
            ts = int(t_raw) if t_raw > 10 ** 12 else int(t_raw)  # 秒/毫秒都接受
            if ts > 10 ** 12:
                ts //= 1000
        else:
            m = re.match(r"(\d{4}[-/]\d{2}[-/]\d{2})[ T](\d{2}:\d{2}(?::\d{2})?)", str(t_raw))
            if m:
                ts = _norm_ts(m.group(1), m.group(2))
        sender = str(next((it[k] for k in _JSON_SENDER_KEYS if it.get(k) is not None), "?"))
        text = str(next((it[k] for k in _JSON_TEXT_KEYS if it.get(k) is not None), "")).strip()
        if text:
            out.append({"ts": ts, "sender": sender[:32], "text": text[:2000]})
    return out


def parse_file(path):
    """解析导出文件 → {"ok", messages, count, range:[min,max], senders, error}。"""
    p = pathlib.Path(str(path or ""))
    if not p.is_file() or p.suffix.lower() not in (".md", ".txt", ".json"):
        return {"ok": False, "error": "请选择 .md / .txt / .json 聊天记录导出文件"}
    if p.stat().st_size > _MAX_FILE:
        return {"ok": False, "error": "文件过大（>80MB），请分批导出"}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"ok": False, "error": "读取失败：%r" % e}

    # ① JSON 数组
    st = text.lstrip()
    if st.startswith("[") or st.startswith("{"):
        try:
            data = json.loads(text)
            rows = data if isinstance(data, list) else (
                data.get("messages") if isinstance(data, dict) else None)
            if isinstance(rows, list):
                msgs = _parse_json_list(rows)[:_MAX_MSGS]
                if len(msgs) >= 1:
                    return _pack(msgs)
        except ValueError:
            pass

    # ② 文本行解析（四种时间前缀 + 续行归并 + md 头跳过）
    msgs = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        # 微信 export_markdown 的消息行带 markdown 列表前缀「- 」
        if line.startswith("- "):
            line = line[2:].rstrip()
        if line.startswith("#") and not any(pt.match(line) for pt in _TS_PATTERNS):
            continue  # md 标题
        if line.startswith(("```", "---", ">>")):
            continue
        hit = None
        for pt in _TS_PATTERNS:
            m = pt.match(line)
            if m:
                hit = m
                break
        if hit:
            sender = hit.group(3).strip()[:32]
            # 微信行是「[时间] [会话名] 发送者: 内容」——剥掉开头的会话名标签
            sender = re.sub(r"^\[[^\]]{1,24}\]\s*", "", sender) or sender
            msgs.append({"ts": _norm_ts(hit.group(1), hit.group(2)),
                         "sender": sender,
                         "text": hit.group(4).strip()[:2000]})
            if len(msgs) >= _MAX_MSGS:
                break
        elif msgs:
            extra = line.strip()[:500]
            if extra and msgs[-1]["text"]:
                msgs[-1]["text"] = (msgs[-1]["text"] + "\n" + extra)[:2000]
    if not msgs:
        return {"ok": False,
                "error": "没认出消息行——需要「时间 发送人： 内容」格式或 json 导出"}
    return _pack(msgs)


def _pack(msgs):
    tss = [m["ts"] for m in msgs if m["ts"]]
    fmt = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t))
    return {"ok": True, "messages": msgs, "count": len(msgs),
            "range": [fmt(min(tss)), fmt(max(tss))] if tss else ["", ""],
            "senders": sorted({m["sender"] for m in msgs})[:12]}


def _dir(source, session):
    if source not in SOURCES:
        raise ValueError("source 非法")
    return pathlib.Path(BASE_DIR) / source / _safe_name(session)


def store(source, session, path):
    """解析并落隔离库。返回 {ok, session(已清洗), count, range}。"""
    r = parse_file(path)
    if not r.get("ok"):
        return r
    safe = _safe_name(session)
    d = pathlib.Path(BASE_DIR) / source / safe
    d.mkdir(parents=True, exist_ok=True)
    (d / "messages.json").write_text(
        json.dumps(r["messages"], ensure_ascii=False), encoding="utf-8")
    (d / "meta.json").write_text(json.dumps(
        {"count": r["count"], "range": r["range"], "senders": r["senders"],
         "source_file": str(pathlib.Path(str(path)).name), "imported": time.time()},
        ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "session": safe, "count": r["count"],
            "range": r["range"], "senders": r["senders"]}


def sessions(source):
    """隔离库会话列表。"""
    root = pathlib.Path(BASE_DIR) / (source if source in SOURCES else "qq")
    out = []
    if root.is_dir():
        for d in sorted(root.iterdir()):
            meta_p = d / "meta.json"
            if d.is_dir() and meta_p.is_file():
                try:
                    m = json.loads(meta_p.read_text(encoding="utf-8"))
                except ValueError:
                    m = {}
                out.append({"name": d.name, "count": m.get("count", 0),
                            "range": m.get("range", ["", ""]),
                            "senders": (m.get("senders") or [])[:6],
                            "imported": m.get("imported", 0)})
    return out


def delete(source, session):
    d = _dir(source, session)
    # d.parent = chat_import/<source>——校验源目录合法，防误删库外内容
    if source not in SOURCES or not d.is_dir() or d.parent.name != source:
        return {"ok": False, "error": "会话不存在"}
    import shutil
    shutil.rmtree(d, ignore_errors=True)
    return {"ok": True}


def load_sample(source, session, budget=24000):
    """读取会话并均匀采样到 token 预算内（agent 学习用）。
    返回 (lines, meta) 或 (None, error)。"""
    d = _dir(source, session)
    mp = d / "messages.json"
    if not mp.is_file():
        return None, "会话不存在"
    try:
        msgs = json.loads(mp.read_text(encoding="utf-8"))
    except ValueError:
        return None, "messages.json 损坏"
    if not isinstance(msgs, list) or not msgs:
        return None, "会话为空"
    meta = {}
    try:
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    # 均匀采样：头尾各留，中间等距
    def line_of(m):
        t = time.strftime("%m-%d %H:%M", time.localtime(m.get("ts") or 0)) \
            if m.get("ts") else "?"
        return "%s %s: %s" % (t, m.get("sender", "?"),
                              str(m.get("text", ""))[:180])
    lines = [line_of(m) for m in msgs]
    total = sum(len(x) for x in lines)
    if total <= budget:
        return lines, meta
    step = max(1, int(len(lines) * 1.0 / (len(lines) * budget / total)) )
    # 按预算等距抽取
    picked, acc = [], 0
    i = 0
    while i < len(lines) and acc < budget:
        picked.append(lines[i])
        acc += len(lines[i]) + 1
        i += step
    if lines and (not picked or picked[-1] != lines[-1]):
        picked.append(lines[-1])
    return picked, meta
