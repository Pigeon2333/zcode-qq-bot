# -*- coding: utf-8 -*-
"""设置页分页冒烟（改动 qq_admin_ui.py 的设置页必跑）。

1. 页面脚本在 node 里跑：只取设置页相关的函数，配最小的桩，逐个分页拼出 HTML。
   查四件事——每个卡片函数的 <div>/</div> 闭合（少一个闭合标签，后面的卡片会被
   套进去、小标题消失，语法检查查不出来）；五个分页的卡片数；拼出来的 HTML 里
   没有未渲染的 ${；每个配置项的保存入口在五个分页里恰好出现一次（不丢不重）。
   不连任何服务，不读写数据目录。
2. 「记录注入上下文」开关真的写进配置。这个开关 2026-11-08 引入后保存一直 500
   （写文件的函数只收字符串，那里传了字典），页面却照样提示「已开启/已停止」。
   用临时数据目录起一个管理台实例来验，端口和数据目录都显式指定，跑完即停。
"""
import http.client
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "plugins" / "qq-bot" / "scripts"
SRC = SCRIPTS / "qq_admin_ui.py"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

fails = []


def chk(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (" | " + str(extra)[:160] if extra else ""))
    if not cond:
        fails.append(name)


# ---------------------------------------------------------------- 第 1 部分
def page_script():
    return re.search(r"<script>(.*?)</script>", SRC.read_text(encoding="utf-8"), re.S).group(1)


def grab(script, pattern):
    m = re.search(pattern, script, re.M)
    if not m:
        raise SystemExit("页面脚本里找不到：%s" % pattern)
    return m.group(0)


RV_FIX = {
    "reply_mode": "chat", "prompt_v2": True, "reply_v2": True, "reply_json": True,
    "vision_caption": True, "vision_caption_model": "glm-4.6v-flash", "vision_caption_daily_cap": 600,
    "direct_must_reply": True, "len_follow_group": True, "react_only": True,
    "proactive_mode": "reason", "nudge_mode": "restrained", "hist_lines": 50, "hist_budget_chars": 5000,
    "econ_enabled": True, "econ_active": True, "econ_manual": False,
    "econ_start": "14:00", "econ_end": "18:00", "econ_days": [0, 1, 2, 3, 4],
    "wait_gate_enabled": True, "burst_merge_enabled": True, "botloop_guard_enabled": True,
    "botloop_cooldown_min": 60, "abuse_guard_enabled": True, "vision_enabled": True,
    "vision_mode": "all_prob", "vision_prob": 0.3, "chat_max_len": 80, "web_search_enabled": True,
    "task_bridge_enabled": True, "remote_control_enabled": True, "draw_enabled": True,
    "daily_review_enabled": True, "lean_ctx_enabled": True, "dyn_ctx_enabled": True,
    "risk_limit_image_fetch": True, "risk_proactive_downscale": False, "risk_human_pace": True,
    "context_log_enabled": True, "member_profiles_enabled": False, "env_pulse_enabled": False,
}
C_FIX = {
    "bot_qq": "100000001", "owner_qq": "100000002", "bot_name_enabled": True, "bot_name_value": "测试",
    "owner_name_enabled": False, "owner_name_value": "", "napcat_ws_port": 3001, "daemon_port": 1,
    "agent_admin_port": 2, "ui_port": 3, "timezone": "Asia/Shanghai", "logs_dir": "", "logs_keep_days": 30,
    "api_proxy": "", "user_blacklist": ["100000003"], "review_time": "03:30", "backup_time": "04:00",
    "backup_keep_days": 30, "backup_keep_copies": 30, "backup_dir": "", "watchdog_interval": 120,
    "autostart_with_windows": False, "auto_power_on": False, "close_action": "tray",
}
KEYS_FIX = {"keys": [{"id": "k1", "label": "测试", "mask": "sk-***", "provider": "zhipu", "primary": True}]}
# model 分页 2026-11-25 起多了一张「识图模型」卡（识图模型与每日上限从「聊天风格」搬来）；
# 2026-11-27 起又多一张「同时处理」卡（WP-04 主循环并行的并发数输入）；
# 同日再多一张「按用途选模型」卡（WP-05 路由表）；
# style 分页 2026-11-27 起多一张「工具循环（实验性）」卡（WP-07 A2a-2）
EXPECT_CARDS = {"general": 8, "model": 8, "style": 9, "safe": 4, "maint": 4}
EXPECT_SECTIONS = {
    "general": ["身份与称呼", "运行环境", "功能开关", "应用行为"],
    "model": ["API Key 与代理", "识图模型（后台识图与看图用）",
              "上下文（每次带多少历史和记忆）", "同时处理", "省额度"],
    "style": ["活跃程度", "对话架构（怎么生成回复）", "风格与活跃程度", "接话时机", "看图"],
    "safe": ["防滥用", "账号风控", "隐私与数据"],
    "maint": ["自动化", "维护"],
}
# 每个配置项（或它的保存函数）在五个分页合起来的 on* 属性里出现的次数：
# 默认 1 次，不丢不重；单选按钮每个选项各写一次，所以是 2 或 3
SAVE_KEYS = {
    "owner_qq": 1, "bot_name_enabled": 1, "bot_name_value": 1, "owner_name_enabled": 1,
    "owner_name_value": 1, "napcat_ws_port": 1, "daemon_port": 1, "agent_admin_port": 1,
    "ui_port": 1, "timezone": 1, "logs_dir": 1, "logs_autoclean_enabled": 1, "logs_keep_days": 1,
    "api_proxy": 1, "dyn_ctx_enabled": 1, "hist_lines": 1,
    "hist_budget_chars": 1, "econ_enabled": 1, "econ_manual": 2, "econ_start": 1, "econ_end": 1,
    "prompt_v2": 1, "reply_v2": 1, "reply_json": 1, "direct_must_reply": 1, "len_follow_group": 1,
    "react_only": 1, "proactive_mode": 1, "nudge_mode": 1, "chat_max_len": 1,
    "wait_gate_enabled": 1, "burst_merge_enabled": 1, "botloop_guard_enabled": 1,
    "botloop_cooldown_min": 1, "tool_loop_enabled": 1, "tool_confirm_write": 1,
    "tool_web_fetch_enabled": 1, "tool_cross_chat_search": 1,
    "tool_group_files_enabled": 1,
    "owner_intent_enabled": 1,
    "vision_caption": 1, "vision_caption_model": 1,
    "vision_caption_daily_cap": 1, "vision_prob": 1, "risk_limit_image_fetch": 1,
    "risk_proactive_downscale": 1, "risk_human_pace": 1, "task_bridge_enabled": 1,
    "remote_control_enabled": 1, "draw_enabled": 1, "daily_review_enabled": 1, "review_time": 1,
    "backup_enabled": 1, "backup_time": 1, "backup_keep_days": 1, "backup_keep_copies": 1,
    "backup_dir": 1, "watchdog_enabled": 1, "watchdog_mode": 2, "watchdog_interval": 1,
    "saveCloseAction": 3,
    "saveVision": 1, "visionModePick": 3, "saveAbuseGuard": 1, "saveWebSearch": 1, "ctxToggle": 1,
    "blAdd": 2, "blDel": 1, "setPrimary": 1, "renameKey": 1, "delKey": 1, "addKey": 1,
    "saveAuto": 1, "savePowerOn": 1, "doUninstall": 1, "pickBackupDir": 1, "backupNow": 1,
    "restorePicker": 1, "cacheReload": 1, "cacheClean": 1, "botQqSwitch": 1, "saveLeanCtx": 1,
    "saveEconDays": 7, "goTab('groups')": 1, "goTab('private')": 1,
}

JS = r"""
// 入参走文件：Windows 的命令行有长度上限，装不下整段页面脚本
const input = JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8'));
const settings = input.settings;
const helpers = input.helpers;
const fix = input.fix;
const expectations = input.expectations;
const view = {innerHTML: ''};
// 浏览器本地存储的桩：记下写了什么；fail=true 时模拟隐私模式下的「存不了」
const store = {data: {}, fail: false};
const fakeLS = {
  getItem: function (k) { if (store.fail) throw new Error('denied'); return (k in store.data) ? store.data[k] : null; },
  setItem: function (k, v) { if (store.fail) throw new Error('denied'); store.data[k] = String(v); },
};
const sandbox = {
  TAB: 'settings',
  window: {localStorage: fakeLS},
  document: {getElementById: () => null, querySelector: () => null, querySelectorAll: () => []},
  $: () => view,
  api: p => Promise.resolve(JSON.parse(JSON.stringify(fix[p] || {}))),
  goTab: () => {}, load: () => {}, bkGroupsRender: () => {}, ncdlPoll: () => {}, rtPaint: () => {},
};
const names = Object.keys(sandbox);
const mod = new Function(...names, helpers + '\n' + settings + '\nreturn {getTab: () => __ST_TAB, ' +
  ['vSettings', 'goSettings', '__stRecall', '__stRemember', '__stGo', 'stIdentity', 'stEnv', 'stKeys',
   'stLean', 'stDyn', 'stHist', 'stEcon',
   'stArchV2', 'stStyle', 'stLen', 'stTiming', 'stVision', 'stActivityGuide', 'stAbuse', 'stBlack',
   'stRisk', 'stPrivacy', 'stReview', 'stBackup', 'stCache', 'stWatchdog', 'stWebSearch',
   'stOwnerFeatures', 'stApp', 'stCaption'].map(n => n + ':' + n).join(',') + '};')(...Object.values(sandbox));
const c = fix['/api/app-config'], rv = fix['/api/reply-config'], keysL = fix['/api/keys'].keys;
const bal = h => (h.match(/<div\b/g) || []).length - (h.match(/<\/div>/g) || []).length;
const out = {funcs: {}, tabs: {}, fallback: null, missing: []};
const args = {stIdentity: [c], stEnv: [c], stKeys: [c, keysL], stLean: [rv], stDyn: [rv], stHist: [rv],
  stEcon: [rv], stArchV2: [rv], stStyle: [rv], stLen: [rv], stTiming: [rv], stVision: [rv],
  stActivityGuide: [c, rv], stAbuse: [rv], stBlack: [c], stRisk: [rv], stPrivacy: [rv],
  stReview: [c, rv], stBackup: [c], stCache: [], stWatchdog: [c], stWebSearch: [rv],
  stOwnerFeatures: [rv], stApp: [c], stCaption: [rv]};
(async () => {
  for (const n of Object.keys(args)) out.funcs[n] = bal(String(mod[n](...args[n])));
  let union = '';
  for (const t of Object.keys(expectations.cards)) {
    mod.goSettings(t);
    if (mod.getTab() !== t) out.missing.push('分页没切到 ' + t);
    await mod.vSettings();
    const h = view.innerHTML;
    union += h;
    // 正文从保存提示那个 div 之后算起（从中间切会少一个开标签）
    const hs = h.indexOf('hint-save');
    const body = h.slice(h.indexOf('</div>', hs) + 6);
    out.tabs[t] = {cards: (body.match(/<div class="card/g) || []).length, bal: bal(body),
      raw: body.includes('${'),
      sections: expectations.sections[t].filter(s => !body.includes('>' + s + '<'))};
  }
  const attrs = [];
  for (const m of union.matchAll(/\son(?:change|click|keydown|input)="([^"]*)"/g)) attrs.push(m[1]);
  const tok = (hay, k) => hay.split(new RegExp('(?<![A-Za-z0-9_])' + k.replace(/[()']/g, m => '\\' + m) + '(?![A-Za-z0-9_])')).length - 1;
  for (const k of Object.keys(expectations.keys)) {
    const n = attrs.reduce((s, a) => s + tok(a, k), 0);
    if (n !== expectations.keys[k]) out.missing.push(k + '×' + n + '（应为 ' + expectations.keys[k] + '）');
  }
  mod.goSettings('no-such-tab');
  out.fallback = mod.getTab();
  // 分页状态记住：存过就回到上次的分页；存的是不认识的值 / 没存过 / 存储不可用 → 「通用」
  out.memory = {};
  store.data['zbk_st_tab'] = 'style';
  out.memory.stored = mod.__stRecall();
  store.data['zbk_st_tab'] = 'no-such';
  out.memory.junk = mod.__stRecall();
  delete store.data['zbk_st_tab'];
  out.memory.none = mod.__stRecall();
  store.fail = true;
  out.memory.broken = mod.__stRecall();
  mod.__stRemember('model');            // 存不了也不能抛
  store.fail = false;
  mod.__stGo('model');
  out.memory.go = [mod.getTab(), store.data['zbk_st_tab']];
  mod.goSettings('safe');
  out.memory.goSettings = [mod.getTab(), store.data['zbk_st_tab']];
  mod.goSettings('no-such-tab');
  out.memory.goJunk = [mod.getTab(), store.data['zbk_st_tab']];
  process.stdout.write(JSON.stringify(out));
})().catch(e => { process.stdout.write(JSON.stringify({error: String(e && e.stack || e)})); });
"""


def run_page_checks():
    if not shutil.which("node"):
        chk("设置页分页检查（需要 node）", False, "找不到 node")
        return
    script = page_script()
    helpers = "\n".join(grab(script, p) for p in (
        r"^function esc\(s\)\{.*\}$", r"^function head\(sub\)\{.*\}$",
        r"^function saveHint\(text\)\{.*\}$", r"^const _SAVE_HINT=.*$", r"^const TITLES=\{.*\};?$"))
    a = script.index("// ---- 设置页的子分页")
    settings = script[a:script.index("let _rtTimer=null;")]
    exp = {"cards": EXPECT_CARDS, "sections": EXPECT_SECTIONS, "keys": SAVE_KEYS}
    work = pathlib.Path(tempfile.mkdtemp(prefix="qqbot_ui_smoke_"))
    try:
        jf = work / "check.js"
        jf.write_text(JS, encoding="utf-8")

        def evaluate(settings_text):
            inp = work / "input.json"
            inp.write_text(json.dumps({"settings": settings_text, "helpers": helpers,
                                       "fix": {"/api/app-config": C_FIX, "/api/reply-config": RV_FIX,
                                               "/api/keys": KEYS_FIX},
                                       "expectations": exp}, ensure_ascii=False), encoding="utf-8")
            r = subprocess.run(["node", str(jf), str(inp)], capture_output=True, text=True,
                               encoding="utf-8", timeout=120)
            if r.returncode != 0:
                return {"error": r.stderr}
            got = json.loads(r.stdout)
            if "error" in got:
                got["error"] = got["error"][:1500]
            return got

        res = evaluate(settings)
        if "error" in res:
            print(res["error"][:1500])
            chk("页面脚本能在 node 里跑起来", False)
            return
        chk("页面脚本能在 node 里跑起来", True)
        bad = {k: v for k, v in res["funcs"].items() if v != 0}
        chk("每个卡片函数的 <div>/</div> 闭合（%d 个函数）" % len(res["funcs"]), not bad, bad)
        for t, want in EXPECT_CARDS.items():
            got = res["tabs"][t]
            chk("分页「%s」卡片数 = %d" % (t, want), got["cards"] == want,
                "实际 %d" % got["cards"] if got["cards"] != want else "")
            chk("分页「%s」小标题齐全、无嵌套、无未渲染占位" % t,
                not got["sections"] and got["bal"] == 0 and not got["raw"], got)
        chk("每个配置项的保存入口次数符合预期（%d 项）" % len(SAVE_KEYS), not res["missing"],
            res["missing"])
        chk("不认识的分页名退回「通用」", res["fallback"] == "general", res["fallback"])
        _m = res.get("memory") or {}
        chk("分页状态记住：存过 → 回到上次的分页；不认识的值 / 没存过 → 通用",
            _m.get("stored") == "style" and _m.get("junk") == "general" and _m.get("none") == "general",
            _m)
        chk("分页状态记住：浏览器不让存（隐私模式）→ 回到通用，且不报错", _m.get("broken") == "general", _m)
        chk("分页状态记住：点分页条和「去填 Key」都会记下当前分页",
            _m.get("go") == ["model", "model"] and _m.get("goSettings") == ["safe", "safe"]
            and _m.get("goJunk") == ["general", "general"], _m)
        # 自检：故意从「风格与活跃程度」卡片去掉一个闭合标签，闭合检查必须能查出来
        # （这个错误 2026-11-24 真出过，语法检查查不出来）
        needle = "决定</div></div></div>`}"
        res2 = evaluate(settings.replace(needle, "决定</div></div>`}", 1)) if needle in settings else {}
        chk("自检：故意去掉一个闭合标签时能查出", res2.get("funcs", {}).get("stStyle") == 1,
            (res2.get("funcs") or {}).get("stStyle"))
    finally:
        shutil.rmtree(str(work), ignore_errors=True)


# ---------------------------------------------------------------- 第 2 部分
def data_dir_under(env):
    """在给定环境变量下，qq_common 实际解析出的数据目录（起实例前先确认它指向临时目录）。"""
    r = subprocess.run([sys.executable, "-c", "import qq_common; print(qq_common.DATA_DIR)"],
                       cwd=str(SCRIPTS), env=env, capture_output=True, text=True, encoding="utf-8",
                       timeout=60)
    return (r.stdout or "").strip()


def call(port, token, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=6)
    try:
        payload = json.dumps(body) if body is not None else None
        conn.request(method, path, payload, {"Content-Type": "application/json",
                                             "X-UI-Token": token})
        raw = conn.getresponse().read()
        try:
            return json.loads(raw)
        except ValueError:
            return {"ok": False, "error": raw[:80].decode("utf-8", "replace")}
    finally:
        conn.close()


def run_ctx_toggle_check():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="qqbot_ctx_smoke_"))
    proc = None
    base = 49000 + (os.getpid() % 900) * 11
    ui_port, daemon_port, agent_port = base, base + 1, base + 2
    env = dict(os.environ, QQBOT_DATA_DIR=str(tmp), PYTHONIOENCODING="utf-8",
               PYTHONDONTWRITEBYTECODE="1")
    try:
        resolved = data_dir_under(env)
        if pathlib.Path(resolved).resolve() != tmp.resolve():
            chk("临时数据目录生效后才起实例", False, "qq_common 解析出的目录不是临时目录：%s" % resolved)
            return
        (tmp / "app_config.json").write_text(json.dumps({
            "bot_qq": "100000001", "owner_qq": "100000002", "ui_port": ui_port,
            "daemon_port": daemon_port, "agent_admin_port": agent_port,
            "watchdog_enabled": False, "update_check": False, "backup_enabled": False,
            "auto_power_on": False}), encoding="utf-8")
        proc = subprocess.Popen([sys.executable, str(SRC), "--port", str(ui_port),
                                 "--daemon-port", str(daemon_port), "--agent-port", str(agent_port)],
                                cwd=str(SCRIPTS), env=env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        token_file = tmp / "ui_token"
        token, up = "", False
        for _ in range(60):
            if proc.poll() is not None:
                break
            if token_file.is_file():
                token = token_file.read_text(encoding="utf-8").strip()
                cfg = call(ui_port, token, "GET", "/api/reply-config") if token else {}
                if isinstance(cfg, dict) and "context_log_enabled" in cfg:
                    up = True
                    break
            time.sleep(0.25)
        if not up:
            chk("临时管理台实例启动", False, "退出码 %r" % proc.poll())
            return
        for want in (True, False):
            r = call(ui_port, token, "POST", "/api/ctx-log-toggle", {"on": want})
            chk("开关设为 %s：接口返回成功" % want, r.get("ok") is True and r.get("enabled") is want, r)
            got = call(ui_port, token, "GET", "/api/reply-config").get("context_log_enabled")
            chk("开关设为 %s：读取接口返回一致" % want, got is want, got)
            disk = json.loads((tmp / "reply_config.json").read_text(encoding="utf-8"))
            chk("开关设为 %s：写进了配置文件" % want, disk.get("context_log_enabled") is want, disk)
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(str(tmp), ignore_errors=True)


run_page_checks()
run_ctx_toggle_check()
print()
print("失败 %d 项" % len(fails) if fails else "全部通过 ✓")
sys.exit(1 if fails else 0)
