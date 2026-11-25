# -*- coding: utf-8 -*-
"""管理台修复第二批冒烟（离线：不连任何服务端口、不写用户数据目录）。

覆盖（对应 2026-11-25 施工单 B1 七项）：
  1 群名单管理台：GET /api/groups 六种 wl_state 的返回；文件缺失回 daemon 实际
    生效状态（default_all/default_groups）；daemon 可选容错字段 wl_groups /
    wl_allow_all 有则用、没有不炸；页面六种状态说明文案；stale/broken 横幅；
    stale/broken 编辑器不置灰。
  2 聊天导入调用方：store() 抛 ValueError（source 非法）→ 400；正常来源照常 200。
  3 text_img_merge_enabled：缺键读出 True；保存 False/True；同批保存其它项不丢。
  4 状态页 10 秒整页重写不清空向导输入（值/焦点/光标尽量模拟，见末尾说明）。
  5 备份恢复目录分支：正常换名；tmp 复制失败原目录原样；dst 改名失败走覆盖式
    并带 overlay 标注；文件分支行为不变。
  6 卸载：先写 bat（utf-8，路径含中文不炸）成功才摘自启；写失败不动自启返 500。
  7 表情包缩略图地址带 tk 参数。

隔离方式：import 被测模块前把 QQBOT_DATA_DIR 指到 tempfile.mkdtemp()；服务端
处理器用 UiHandler.__new__ 直接调（不起服务、不连端口，proxy_get 一律打桩）；
页面脚本在 node 沙箱里跑（照 _smoke_settings_tabs 的做法）。跑完删临时目录。

用法：python BOT_REPORTS/_smoke_uifix2.py
"""
import contextlib
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="qqbot_uifix2_iso_")
os.environ["QQBOT_DATA_DIR"] = _TMP
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "plugins" / "qq-bot" / "scripts"
SRC = SCRIPTS / "qq_admin_ui.py"
sys.path.insert(0, str(SCRIPTS))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import qq_admin_ui as U  # noqa: E402  （import 前 QQBOT_DATA_DIR 已指向临时目录）

fails = []


def chk(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (" | " + str(extra)[:180] if extra else ""))
    if not cond:
        fails.append(name)


_MISSING = object()


@contextlib.contextmanager
def patched(obj, **names):
    """临时替换模块属性（缺席属性用哨兵记录，退出时补删）。"""
    old = {k: getattr(obj, k, _MISSING) for k in names}
    for k, v in names.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is _MISSING:
                try:
                    delattr(obj, k)
                except AttributeError:
                    pass
            else:
                setattr(obj, k, v)


def ui_call(method, path, bodyq=None):
    """进程内直调 UiHandler（照 _smoke_arch 18 组的写法），不打桩的回调全穿透。"""
    h = U.UiHandler.__new__(U.UiHandler)
    h.path = path
    raw = json.dumps(bodyq).encode("utf-8") if bodyq is not None else b""
    h.headers = {"Content-Length": str(len(raw))}
    h.rfile = io.BytesIO(raw)
    replies = []
    h._reply = lambda code, obj: replies.append((code, obj))
    h._authed = lambda u, q: True
    getattr(h, method)()
    return replies


# ----------------------------------------------------------- 1 群名单接口六状态
def t_groups_api(tmp):
    gp = pathlib.Path(tmp) / "wl_api_group_whitelist.json"

    def daemon_ret(state, err="", groups=None, allow_all=None, ok=True):
        d = {"ok": ok, "wl_state": state, "wl_error": err}
        if groups is not None:
            d["wl_groups"] = groups
        if allow_all is not None:
            d["wl_allow_all"] = allow_all
        return d

    def once(state, exists, content=None, daemon=None):
        gp.unlink(missing_ok=True)
        if exists:
            gp.write_text(content, encoding="utf-8")
        with patched(U, GROUPS_PATH=str(gp)), \
                patched(U, proxy_get=lambda *a, **k: daemon):
            r = ui_call("do_GET", "/api/groups")
        code, body = r[0]
        return code, body

    code, b = once("file", True, '{"enabled": true, "allow_all": false, "groups": ["123", "456"]}',
                   daemon_ret("file"))
    chk("1a 群名单 file：按文件返回并带 wl_state",
        code == 200 and b["enabled"] is True and b["allow_all"] is False
        and b["groups"] == ["123", "456"] and b["wl_state"] == "file" and b["wl_error"] == "", b)

    code, b = once("default_all", False, None, daemon_ret("default_all"))
    chk("1b 文件缺失 default_all：回 daemon 实际状态（所有群生效）",
        code == 200 and b["enabled"] is True and b["allow_all"] is True and b["groups"] == []
        and b["wl_state"] == "default_all", b)

    code, b = once("default_groups", False, None,
                   daemon_ret("default_groups", groups=["111", "222"], allow_all=False))
    chk("1c 文件缺失 default_groups：带回 daemon 的群列表（容错字段在）",
        code == 200 and b["groups"] == ["111", "222"] and b["allow_all"] is False, b)

    code, b = once("default_groups", False, None, daemon_ret("default_groups"))
    chk("1c' 文件缺失 default_groups：daemon 没给容错字段也不炸、不给假值",
        code == 200 and b["groups"] == [] and b["allow_all"] is False, b)

    bad = '{"oops'
    code, b = once("stale", True, bad, daemon_ret("stale", err="白名单文件读不出来: x"))
    chk("1d stale：wl_state/wl_error 原样透传",
        code == 200 and b["wl_state"] == "stale" and b["wl_error"] == "白名单文件读不出来: x", b)

    code, b = once("broken", True, bad, daemon_ret("broken", err="结构不对"))
    chk("1e broken：同样透传", code == 200 and b["wl_state"] == "broken"
        and b["wl_error"] == "结构不对", b)

    code, b = once("disabled", True, '{"enabled": false, "allow_all": true, "groups": ["1"]}',
                   daemon_ret("disabled"))
    chk("1f disabled：enabled=False 如实返回",
        code == 200 and b["enabled"] is False and b["wl_state"] == "disabled", b)

    code, b = once("file", True, '{"enabled": true, "allow_all": false, "groups": []}',
                   daemon_ret("file"))
    chk("1g file 且名单为空且未勾响应所有群：接口给出该组合（页面据此提示）",
        code == 200 and b["wl_state"] == "file" and b["allow_all"] is False and b["groups"] == [], b)

    code, b = once("", False, None, {"ok": False, "error": "refused"})
    chk("1h daemon 没跑：维持旧行为（不硬编 allow_all）且 wl_state 为空",
        code == 200 and b["enabled"] is True and b["allow_all"] is False and b["groups"] == []
        and b["wl_state"] == "", b)


# ----------------------------------------------------------- 2 聊天导入
def t_chatimport(tmp):
    r = ui_call("do_POST", "/api/chatimport/store",
                {"source": "nope", "session": "s1", "path": ""})
    chk("2a 聊天导入非法来源 → 400 且 error 是「source 非法」",
        bool(r) and r[0][0] == 400 and r[0][1].get("error") == "source 非法", r[:1])

    msgfile = pathlib.Path(tmp) / "export_b1.json"
    msgfile.write_text(json.dumps(
        [{"time": "2026-11-25 10:00:00", "sender": "甲", "text": "你好"}],
        ensure_ascii=False), encoding="utf-8")
    r = ui_call("do_POST", "/api/chatimport/store",
                {"source": "qq", "session": "临时B1", "path": str(msgfile)})
    ok = bool(r) and r[0][0] == 200 and r[0][1].get("ok") is True
    disk = pathlib.Path(U.DATA_DIR, "chat_import", "qq")
    chk("2b 正常来源不受影响：200 且落盘在临时数据目录",
        ok and r[0][1].get("count") == 1 and disk.is_dir(),
        (r[:1], str(disk)))
    chk("2c 落盘没有碰到生产数据目录",
        str(disk).startswith(str(pathlib.Path(_TMP).resolve())) or str(disk).startswith(_TMP),
        str(disk))


# ----------------------------------------------------------- 3 text_img_merge_enabled
def t_text_img_merge(tmp):
    rc = pathlib.Path(tmp) / "reply_config.json"
    with patched(U, REPLY_CONFIG_PATH=str(rc)):
        g = ui_call("do_GET", "/api/reply-config")[0][1]
        chk("3a 缺键读出 True（text_img_merge_enabled / burst_merge_enabled 都是）",
            g.get("text_img_merge_enabled") is True and g.get("burst_merge_enabled") is True,
            {k: g.get(k) for k in ("text_img_merge_enabled", "burst_merge_enabled")})
        r = ui_call("do_POST", "/api/reply-config",
                    {"text_img_merge_enabled": 0, "burst_merge_enabled": 0})
        g = ui_call("do_GET", "/api/reply-config")[0][1]
        disk = json.loads(rc.read_text(encoding="utf-8"))
        chk("3b 保存 False 后读出 False，磁盘同样",
            bool(r) and r[0][0] == 200 and g.get("text_img_merge_enabled") is False
            and disk.get("text_img_merge_enabled") is False, (g.get("text_img_merge_enabled"), disk))
        ui_call("do_POST", "/api/reply-config", {"chat_max_len": 99})
        g = ui_call("do_GET", "/api/reply-config")[0][1]
        chk("3c 同一份配置里保存其它设置后新开关不丢",
            g.get("text_img_merge_enabled") is False and g.get("burst_merge_enabled") is False, g)
        r = ui_call("do_POST", "/api/reply-config",
                    {"text_img_merge_enabled": True, "burst_merge_enabled": True})
        g = ui_call("do_GET", "/api/reply-config")[0][1]
        chk("3d 与 burst_merge_enabled 在同一份保存里再打开",
            bool(r) and r[0][0] == 200 and g.get("text_img_merge_enabled") is True
            and g.get("burst_merge_enabled") is True, g)


# ----------------------------------------------------------- 5 备份恢复
def t_backup_restore(tmp):
    bdir = pathlib.Path(tmp) / "backups"
    name = "bk1"
    src_dir = bdir / name
    (src_dir / "gsv_voices").mkdir(parents=True)
    (src_dir / "gsv_voices" / "new.wav").write_text("NEW", encoding="utf-8")
    (src_dir / "reply_config.json").write_text("k=v", encoding="utf-8")
    (src_dir / "voice_index.bin").write_bytes(b"\x00\x01\xff")

    def entries(d):
        return sorted(p.name for p in d.iterdir())

    with patched(U.C, load_app_config=lambda: {"backup_dir": str(bdir)},
                 backup_files_for=lambda cfg: ["gsv_voices", "reply_config.json", "voice_index.bin"]):
        # (a) 正常换名成功
        dst = pathlib.Path(U.DATA_DIR) / "gsv_voices"
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "old.wav").write_text("OLD", encoding="utf-8")
        (dst / "extra_only_in_dst.txt").write_text("EXTRA", encoding="utf-8")
        r = ui_call("do_POST", "/api/backup-restore", {"name": name})[0]
        leftovers = [p.name for p in pathlib.Path(U.DATA_DIR).iterdir()
                     if ".restore." in p.name]
        chk("5a 正常恢复 200、restored 含目录与两个文件、无 overlay、无 tmp/bak 残留",
            r[0] == 200 and "gsv_voices" in r[1]["restored"]
            and "reply_config.json" in r[1]["restored"] and "voice_index.bin" in r[1]["restored"]
            and r[1]["overlay"] == [] and not leftovers, (r, leftovers))
        chk("5a' 换名语义：dst 与备份内容一致（dst 独有的多余文件没了，不是覆盖式）",
            entries(dst) == ["new.wav"], entries(dst))
        chk("5a'' 文件分支行为不变：文本与二进制文件内容照常恢复",
            (pathlib.Path(U.DATA_DIR) / "reply_config.json").read_text(encoding="utf-8") == "k=v"
            and (pathlib.Path(U.DATA_DIR) / "voice_index.bin").read_bytes() == b"\x00\x01\xff")

        # (b) tmp 复制失败：原目录原样
        real_copytree = shutil.copytree

        def bad_copytree(s, d, *a, **k):
            if str(d).endswith(".restore.tmp"):
                raise OSError("smoke: copy fail")
            return real_copytree(s, d, *a, **k)

        (dst / "sentinel.txt").write_text("KEEP", encoding="utf-8")
        before = entries(dst)
        with patched(shutil, copytree=bad_copytree):
            r = ui_call("do_POST", "/api/backup-restore", {"name": name})[0]
        leftovers = [p.name for p in pathlib.Path(U.DATA_DIR).iterdir() if ".restore." in p.name]
        chk("5b tmp 复制失败：记入 failed、原目录原样、无 tmp/bak 残留",
            r[0] == 200 and "gsv_voices" in r[1]["failed"] and entries(dst) == before
            and not leftovers, (r[1].get("failed"), entries(dst), leftovers))

        # (c) dst 改名失败：走覆盖式复制并带 mode overlay
        real_replace = os.replace

        def bad_replace_bak(s, d):
            if str(d).endswith(".restore.bak"):
                raise PermissionError("smoke: busy")
            return real_replace(s, d)

        (dst / "extra_busy.txt").write_text("BUSY", encoding="utf-8")
        with patched(os, replace=bad_replace_bak):
            r = ui_call("do_POST", "/api/backup-restore", {"name": name})[0]
        leftovers = [p.name for p in pathlib.Path(U.DATA_DIR).iterdir() if ".restore." in p.name]
        chk("5c dst 改名失败：overlay 复制成功并标注，dst 原有文件保留",
            r[0] == 200 and "gsv_voices" in r[1]["restored"]
            and "gsv_voices" in r[1]["overlay"]
            and entries(dst) == ["extra_busy.txt", "new.wav", "sentinel.txt"]
            and not leftovers, (r[1], entries(dst), leftovers))

        # (d) tmp→dst 换名失败：bak 还原、tmp 删掉、记入 failed
        def bad_replace_tmp(s, d):
            if str(s).endswith(".restore.tmp"):
                raise PermissionError("smoke: busy swap")
            return real_replace(s, d)

        before = entries(dst)
        with patched(os, replace=bad_replace_tmp):
            r = ui_call("do_POST", "/api/backup-restore", {"name": name})[0]
        leftovers = [p.name for p in pathlib.Path(U.DATA_DIR).iterdir() if ".restore." in p.name]
        chk("5d tmp→dst 换名失败：记入 failed、bak 改回原名（原目录原样）、tmp 清掉",
            r[0] == 200 and "gsv_voices" in r[1]["failed"] and entries(dst) == before
            and not leftovers and ".restore.bak" not in ",".join(leftovers),
            (r[1].get("failed"), entries(dst), leftovers))


# ----------------------------------------------------------- 6 卸载
def t_uninstall(tmp):
    appdir = pathlib.Path(tmp) / "中文目录应用"
    appdir.mkdir()
    fake_exe = appdir / "ZCodeQQBot.exe"
    bat = pathlib.Path(tmp) / "uninstall-qqbot.bat"
    calls = {"autostart": [], "popen": []}

    with patched(sys, frozen=True, executable=str(fake_exe)), \
            patched(tempfile, gettempdir=lambda: str(tmp)), \
            patched(U.S, set_autostart=lambda on: calls["autostart"].append(on)), \
            patched(subprocess, Popen=lambda *a, **k: calls["popen"].append(a)):
        r = ui_call("do_POST", "/api/uninstall", {"mode": "keep-data"})[0]
    text = bat.read_text(encoding="utf-8") if bat.is_file() else ""
    chk("6a 路径含中文：bat 写成功且能按 utf-8 读回（含中文路径与 chcp 65001）",
        bool(r) and r[0] == 200 and r[1].get("ok") is True
        and "中文目录应用" in text and text.startswith("@echo off") and "chcp 65001" in text,
        (r[:1], text[:120]))
    chk("6a' 写成功后才摘自启：set_autostart(False) 恰一次、Popen 恰一次",
        calls["autostart"] == [False] and len(calls["popen"]) == 1, calls)

    blocker = pathlib.Path(tmp) / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    calls2 = {"autostart": [], "popen": []}
    with patched(sys, frozen=True, executable=str(fake_exe)), \
            patched(tempfile, gettempdir=lambda: str(blocker)), \
            patched(U.S, set_autostart=lambda on: calls2["autostart"].append(on)), \
            patched(subprocess, Popen=lambda *a, **k: calls2["popen"].append(a)):
        r = ui_call("do_POST", "/api/uninstall", {"mode": "keep-data"})[0]
    chk("6b bat 写失败：500 带原因，且 set_autostart 与 Popen 都没被调",
        bool(r) and r[0] == 500 and "卸载脚本写失败" in str(r[1].get("error"))
        and calls2["autostart"] == [] and calls2["popen"] == [], (r[:1], calls2))


# ----------------------------------------------------------- 页面脚本（node 沙箱）
def page_script():
    return re.search(r"<script>(.*?)</script>", SRC.read_text(encoding="utf-8"), re.S).group(1)


def grab_line(script, pattern):
    m = re.search(pattern, script, re.M)
    if not m:
        raise SystemExit("页面脚本里找不到：%s" % pattern)
    return m.group(0)


def grab_block(script, start_pat, end_pat):
    a = re.search(start_pat, script, re.M)
    b = re.search(end_pat, script, re.M)
    if not a or not b or b.start() < a.start():
        raise SystemExit("区块定位失败：%s" % start_pat)
    return script[a.start():b.start()].rstrip() + "\n"


HARNESS = r"""
const input = JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8'));
const els = {};
let focusedId = null;
const KNOWN = ['wzbot', 'wzowner', 'wzqqnt'];
let FIX = input.fix || {};
function mkEl(id){
  return { id: id, value: '', selectionStart: 0, selectionEnd: 0, focusCalls: 0,
    focus: function(){ focusedId = id; this.focusCalls++; },
    setSelectionRange: function(a, b){ this.selectionStart = a; this.selectionEnd = b; } };
}
const view = { html: '',
  set innerHTML(v){ this.html = String(v);
    for (const id of KNOWN){
      if (this.html.indexOf('id="' + id + '"') >= 0) els[id] = mkEl(id); else delete els[id]; } },
  get innerHTML(){ return this.html; } };
const sandbox = {
  TAB: 'status',
  UI_TOKEN: 'tk-secret-值',
  window: {},
  document: {
    getElementById: function(id){ return els[id] || null; },
    querySelector: function(){ return null; },
    querySelectorAll: function(){ return []; },
    get activeElement(){ return focusedId ? (els[focusedId] || null) : null; },
  },
  $: function(sel){ if (sel === '#view') return view;
    return (sel && sel.charAt(0) === '#') ? {textContent: '', style: {}} : null; },
  api: function(p){ return Promise.resolve(JSON.parse(JSON.stringify(FIX[p] || {}))); },
  pwApi: function(p){ return Promise.resolve(JSON.parse(JSON.stringify(FIX[p] || {}))); },
  bootCardHtml: function(){ return '<div class="card" id="bootcard">BOOT</div>'; },
  manageBootTimer: function(){},
  ncFill: function(){},
  setInterval: function(){ return 0; },
  clearInterval: function(){},
};
const names = Object.keys(sandbox);
const mod = new Function(...names, input.helpers + '\n' + input.code +
  '\nreturn {vStatus: vStatus, vGroups: vGroups, stickerRows: stickerRows};')(...Object.values(sandbox));
const out = {};
const baseStatus = { ok: true, ws_connected: true, login: {nickname: 'n', user_id: '1'}, stats: {} };
const baseCfg = { bot_qq: '', sched_pause_enabled: false };
const baseAgent = { ok: true, ready: true };
function setFix(patch){
  FIX = Object.assign({
    '/api/daemon-status': baseStatus, '/api/agent-status': baseAgent, '/api/power/status': {},
    '/api/app-config': baseCfg, '/api/watchdog': {enabled: false}, '/api/app-info': {},
    '/api/qq-compat': {level: 'ok'}, '/api/keys': {keys: [{id: 'k'}]},
    '/api/napcat-login-info': {token: 'x'},
  }, patch || {});
}
async function renderStatus(patch){ setFix(patch); await mod.vStatus(); return view.html; }

(async () => {
  // ---- 向导输入保活：第一次渲染出现输入框 → 填值/焦点/光标 → 第二次渲染不被清空
  await renderStatus();
  if (!els.wzbot || !els.wzowner || !els.wzqqnt){
    out.wizard = {error: '第一次渲染没有出现向导输入框'};
  } else {
    els.wzbot.value = '123456789';
    els.wzowner.value = '987654321';
    focusedId = 'wzbot';
    els.wzbot.selectionStart = 3; els.wzbot.selectionEnd = 3;
    const oldBot = els.wzbot;
    const html2 = await renderStatus();   // 模拟 10 秒整页重写
    const nb = els.wzbot, no = els.wzowner, nq = els.wzqqnt;
    out.wizard = { bot: nb && nb.value, owner: no && no.value, qqnt: nq && nq.value,
      focusCalls: nb && nb.focusCalls, sel: nb && [nb.selectionStart, nb.selectionEnd],
      nodeReplaced: nb !== oldBot, wizardStillThere: html2.indexOf('id="wzbot"') >= 0 };
  }
  // ---- stale/broken 横幅 + 其它横幅不受影响
  out.banner = {
    stale: await renderStatus({ '/api/daemon-status': Object.assign({}, baseStatus,
      {wl_state: 'stale', wl_error: '白名单文件读不出来: 逗号'}) }),
    broken: await renderStatus({ '/api/daemon-status': Object.assign({}, baseStatus,
      {wl_state: 'broken', wl_error: ''}) }),
    file: await renderStatus({ '/api/daemon-status': Object.assign({}, baseStatus,
      {wl_state: 'file', wl_error: ''}) }),
    none: await renderStatus({ '/api/daemon-status': Object.assign({}, baseStatus) }),
  };
  // ---- 群名单卡片：六种状态说明文案
  const rv = { reply_mode: 'standard', keywords: [], chat_probability: 0.5,
    chat_min_interval_sec: 45, chat_interesting: false, chat_anti_interrupt: true,
    proactive_groups_enabled: false, nudge_group_enabled: false, proactive_hourly_cap: 2,
    reaction_enabled: false, reaction_emojis: [], burst_merge_enabled: true,
    text_img_merge_enabled: true, botloop_guard_enabled: true, botloop_cooldown_min: 60,
    wait_gate_enabled: true };
  const gfix = function(g){ return {
    '/api/groups': g, '/api/reply-config': rv, '/api/group-modes': {modes: {}},
    '/api/group-names': {names: {}} }; };
  const vG = async function(g){ setFix(gfix(g)); await mod.vGroups(); return view.html; };
  out.groups = {
    default_all: await vG({enabled: true, allow_all: true, groups: [], wl_state: 'default_all', wl_error: ''}),
    default_groups_n: await vG({enabled: true, allow_all: false, groups: ['111', '222'],
      wl_state: 'default_groups', wl_error: '', wl_groups: ['111', '222'], wl_allow_all: false}),
    default_groups_plain: await vG({enabled: true, allow_all: false, groups: [],
      wl_state: 'default_groups', wl_error: ''}),
    stale: await vG({enabled: true, allow_all: false, groups: [],
      wl_state: 'stale', wl_error: '白名单文件读不出来: x'}),
    stale_eff: await vG({enabled: true, allow_all: false, groups: [], wl_state: 'stale',
      wl_error: '', wl_allow_all: true, wl_groups: ['1', '2', '3']}),
    broken: await vG({enabled: true, allow_all: false, groups: [],
      wl_state: 'broken', wl_error: '结构不对'}),
    disabled: await vG({enabled: false, allow_all: false, groups: ['9'],
      wl_state: 'disabled', wl_error: ''}),
    file_empty: await vG({enabled: true, allow_all: false, groups: [], wl_state: 'file', wl_error: ''}),
    file_ok: await vG({enabled: true, allow_all: false, groups: ['555'], wl_state: 'file', wl_error: ''}),
    daemon_down: await vG({enabled: true, allow_all: false, groups: [], wl_state: '', wl_error: ''}),
  };
  // ---- 表情包缩略图带 tk
  out.sticker = mod.stickerRows([{id: 5, desc: 'd'}], '池A');
  out.stickerSrc = '/api/stickers/thumb?pool=' + encodeURIComponent('池A') +
    '&id=5&tk=' + encodeURIComponent('tk-secret-值');
  process.stdout.write(JSON.stringify(out));
})().catch(e => { process.stdout.write(JSON.stringify({fatal: String(e && e.stack || e)})); });
"""


def t_page_js(work):
    if not shutil.which("node"):
        chk("页面脚本 node 沙箱（需要 node）", False, "找不到 node")
        return
    script = page_script()
    helpers = "\n".join(grab_line(script, p) for p in (
        r"^const TITLES=\{.*\};?$", r"^function esc\(s\)\{.*\}$", r"^function head\(sub\)\{.*\}$",
        r"^function saveHint\(text\)\{.*\}$", r"^const _SAVE_HINT=.*$", r"^const _REACT_TABLE=.*$",
        r"^let _gmModes=.*$", r"^let rvState=\{\}$", r"^let _ncTok='';$", r"^let _ncTokTimer=null;$"))
    code = (grab_block(script, r"^async function vStatus\(\)\{", r"^let _ncTok='';$")
            + grab_block(script, r"^function proCard\(kind\)\{", r"^let rvState=\{\}$")
            + grab_block(script, r"^async function vGroups\(\)\{", r"^async function vPrivate\(\)\{")
            + grab_block(script, r"^function stickerRows\(items,pool\)\{", r"^async function loadStickerPool"))
    jf = work / "uifix2_harness.js"
    jf.write_text(HARNESS, encoding="utf-8")
    inp = work / "uifix2_input.json"
    inp.write_text(json.dumps({"helpers": helpers, "code": code},
                              ensure_ascii=False), encoding="utf-8")
    r = subprocess.run(["node", str(jf), str(inp)], capture_output=True, text=True,
                       encoding="utf-8", timeout=120)
    if r.returncode != 0:
        chk("页面脚本能在 node 里跑起来", False, r.stderr[:400])
        return
    try:
        out = json.loads(r.stdout)
    except ValueError:
        chk("页面脚本能在 node 里跑起来", False, r.stdout[:400])
        return
    if "fatal" in out:
        chk("页面脚本能在 node 里跑起来", False, out["fatal"][:800])
        return
    chk("页面脚本能在 node 里跑起来", True)

    w = out.get("wizard") or {}
    chk("4a 10 秒整页重写：三个向导输入框的值都保持",
        w.get("bot") == "123456789" and w.get("owner") == "987654321" and w.get("qqnt") == "", w)
    chk("4b 焦点与光标恢复：重写后 focus 被调、光标回到 3:3（node 模拟，真浏览器行为见终报说明）",
        (w.get("focusCalls") or 0) >= 1 and w.get("sel") == [3, 3], w)
    chk("4c 证明重写真的发生了：第二次渲染的输入框是新节点",
        w.get("nodeReplaced") is True and w.get("wizardStillThere") is True, w)

    b = out.get("banner") or {}
    chk("4d stale 横幅：常驻一条，带沿用说明与 wl_error",
        "群名单异常" in b.get("stale", "") and "正在沿用上一份正常的配置" in b.get("stale", "")
        and "白名单文件读不出来: 逗号" in b.get("stale", ""), "")
    chk("4e broken 横幅：不在任何群生效", "没有可用的旧配置" in b.get("broken", "")
        and "不在任何群生效" in b.get("broken", ""), "")
    chk("4f file / 无字段：没有横幅", "群名单异常" not in b.get("file", "")
        and "群名单异常" not in b.get("none", ""), "")
    chk("4g 其它横幅不受影响：ws/vision/up 横幅条件未被波及",
        "已经断了" not in b.get("stale", "") and "识图不可用" not in b.get("stale", "")
        and "有新版" not in b.get("stale", "") and "组件运行状态" in b.get("stale", ""), "")

    g = out.get("groups") or {}

    def has(state, text):
        return text in (g.get(state) or "")

    chk("4h default_all 说明", has("default_all", "还没有群名单配置，机器人当前在所有群生效"), "")
    chk("4i default_groups（daemon 给了群列表）：显示真实数量",
        has("default_groups_n", "只在启动时指定的 2 个群生效"), "")
    chk("4i' default_groups（没有容错字段）：只说状态不带数量",
        has("default_groups_plain", "只在启动时指定的群生效")
        and not has("default_groups_plain", "个群生效"), "")
    chk("4j stale 说明 + wl_error", has("stale", "群名单文件读取失败，正在沿用上一份正常的配置")
        and has("stale", "白名单文件读不出来: x"), "")
    chk("4j' stale（容错字段说当前生效所有群）",
        has("stale_eff", "当前实际生效：所有群"), "")
    chk("4k broken 说明 + wl_error",
        has("broken", "读取失败，且没有可用的旧配置") and has("broken", "不在任何群生效")
        and has("broken", "结构不对"), "")
    chk("4l disabled 说明", has("disabled", "群名单总开关已关闭，机器人不在任何群生效"), "")
    chk("4m file 且名单为空且未勾响应所有群：提示名单是空的",
        has("file_empty", "名单是空的：机器人不会在任何群生效"), "")
    chk("4m' file 且有名单：没有该提示", not has("file_ok", "名单是空的"), "")
    chk("4m'' daemon 没跑：不出提示行", not has("daemon_down", "名单是空的")
        and not has("daemon_down", "当前生效"), "")
    chk("4n stale/broken 编辑器不置灰（chain 无 opacity:.45），default_all 置灰（对照）",
        "opacity:.45" not in g.get("stale", "") and "opacity:.45" not in g.get("broken", "")
        and "opacity:.45" in g.get("default_all", ""), "")
    chk("4o stale/broken 下保存提示会换成「几秒内生效」（源码含该分支）",
        "已保存，几秒内生效" in page_script(), "")

    s = out.get("sticker") or ""
    chk("4p 缩略图地址带 tk 参数且 pool/token 都编码",
        out.get("stickerSrc") and out.get("stickerSrc") in s and "&tk=" in s, s[:200])


def main():
    work = pathlib.Path(tempfile.mkdtemp(prefix="qqbot_uifix2_work_"))
    try:
        chk("0 隔离生效：数据目录在临时目录",
            str(pathlib.Path(U.DATA_DIR).resolve()).startswith(str(pathlib.Path(_TMP).resolve())),
            U.DATA_DIR)
        t_groups_api(_TMP)
        t_chatimport(_TMP)
        t_text_img_merge(_TMP)
        t_backup_restore(_TMP)
        t_uninstall(_TMP)
        t_page_js(work)
    finally:
        shutil.rmtree(str(work), ignore_errors=True)
        shutil.rmtree(str(_TMP), ignore_errors=True)
    print()
    print("失败 %d 项" % len(fails) if fails else "全部通过 ✓")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
