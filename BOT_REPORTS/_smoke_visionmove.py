# -*- coding: utf-8 -*-
"""识图设置搬家冒烟（473ab1a：识图模型与每日上限从「聊天风格」搬到「模型与消耗」）。

覆盖（对应 2026-11-26 施工单 T2）：
  a 「模型与消耗」分页 stVisionModel 卡：识图模型输入框（placeholder「留空＝按模型链」）、
    每日上限输入框（type=number、min=0、max=5000）、两句说明、状态行都在；
    上限输入框的值按「配置里有值」与「缺键」两种情况核对——两条走真实接口
    （GET /api/reply-config 缺键时兜 ARCH_V2_DEFAULTS=600），拿接口返回的 rv 渲染；
    另按 2026-11-26 裁决扩到页面一处（stVisionModel 上限输入框 value 缺键回 600、
    不许用 ||600 吃掉合法的 0），直接拿 rv 渲染断言三条：缺键→600、0→0、77→77。
  b 「聊天风格」分页看图卡 stCaption：不再有识图模型输入框与上限输入框，换成引导
    文字加 __stGo('model') 链接；提取出的表达式先与字面量逐字比对，再在模块命名
    空间里直接调用具名的 __stGo('model')，断言 __ST_TAB 变成 model（不动态执行字符串）。
  c id="vision-state-line" 恰好一个（model、style 各 1，其他分页 0）；
    visionStatePaint 在两种分页下都把状态行填上（/api/agent-status 桩）；
    识图不可用（no_vision_model）时是「到「模型与消耗」里填…」新文案，
    旧文案「在上面填一个能看图的识图模型」整份页面源码里搜不到。
  d 上限输入框 onchange 表达式把 this.value 换成 0、6、-5、99999、"abc"、""（空）
    逐个实际执行，提交给 saveKVLoad 的 vision_caption_daily_cap 必须是
    0、6、0、5000、0、0；表达式里不含「0=不识」字样。
  e 卡片数与分区标题：model 分页 8 张卡（2026-11-27 起多一张「同时处理」卡——
    WP-04 主循环并行的并发数输入；同日再多一张「按用途选模型」卡——WP-05 路由表）、
    分区标题依次是「API Key 与代理」「识图模型
    （后台识图与看图用）」「上下文（每次带多少历史和记忆）」「同时处理」「省额度」
    「按用途选模型」；style 分页 9 张卡（2026-11-27 起多一张「工具循环
    （实验性）」卡——WP-07 A2a-2）。

隔离方式：import 被测模块前把 QQBOT_DATA_DIR 指到 tempfile.mkdtemp()（照
_smoke_promptbudget_ui 的做法）；服务端处理器用 UiHandler.__new__ 直接调；
页面脚本在 node 里跑（照 _smoke_settings_tabs 的做法）。不起进程、不连端口。
跑完删临时目录。

用法：python BOT_REPORTS/_smoke_visionmove.py
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

_TMP = tempfile.mkdtemp(prefix="qqbot_visionmove_iso_")
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


@contextlib.contextmanager
def patched(obj, **names):
    old = {k: getattr(obj, k) for k in names}
    for k, v in names.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(obj, k, v)


def ui_call(method, path, bodyq=None):
    """进程内直调 UiHandler（照 _smoke_promptbudget_ui 的写法），不起服务。"""
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


RC = pathlib.Path(_TMP) / "reply_config.json"


def get_rv():
    with patched(U, REPLY_CONFIG_PATH=str(RC)):
        r = ui_call("do_GET", "/api/reply-config")
    return r[0][1] if r else {}


# ---------------------------------------------------- 1 接口：识图两个键的缺键/有值
def t_api():
    RC.unlink(missing_ok=True)
    g_def = get_rv()
    chk("1a 缺键：GET 兜默认 每日上限 600",
        g_def.get("vision_caption_daily_cap") == 600, g_def.get("vision_caption_daily_cap"))
    chk("1a' 缺键：GET 兜默认 识图模型为空串（按模型链）",
        g_def.get("vision_caption_model") == "", g_def.get("vision_caption_model"))
    with patched(U, REPLY_CONFIG_PATH=str(RC)):
        r = ui_call("do_POST", "/api/reply-config", {"vision_caption_daily_cap": 77})
    rv77 = get_rv()
    chk("1b 有值：POST 77 → GET 读回 77",
        bool(r) and r[0][0] == 200 and rv77.get("vision_caption_daily_cap") == 77,
        (r[:1], rv77.get("vision_caption_daily_cap")))
    return rv77, g_def


# ---------------------------------------------------- 2 页面（node 沙箱）
def page_script():
    return re.search(r"<script>(.*?)</script>", SRC.read_text(encoding="utf-8"), re.S).group(1)


def grab(script, pattern):
    m = re.search(pattern, script, re.M)
    if not m:
        raise SystemExit("页面脚本里找不到：%s" % pattern)
    return m.group(0)


# 与 _smoke_settings_tabs 同一份夹具（页面其余卡片靠它们渲染才不缺键）
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
# /api/agent-status 桩：识图可用 / 识图不可用（链里的模型都不能看图）
VISION_OK = {"vision": {"state": "ok", "model": "glm-4.6v-flash", "preferred": "",
                        "vision_models": ["glm-4.6v-flash"], "text_only_models": ["gpt-4o"],
                        "caption": True, "probing": False,
                        "stat": {"ok": 3, "fail": 1, "wait_timeout": 0,
                                 "by_model": [["glm-4.6v-flash", 3]]}}}
VISION_BAD = {"vision": {"state": "no_vision_model", "model": "", "preferred": "",
                         "vision_models": [], "text_only_models": ["gpt-4o"],
                         "caption": True, "probing": False,
                         "stat": {"ok": 0, "fail": 2, "wait_timeout": 1, "by_model": []}}}

JS = r"""
const input = JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8'));
const helpers = input.helpers, settings = input.settings;
const fix = input.fix;
const view = {innerHTML: ''};
// visionStatePaint 用 getElementById('vision-state-line') 找状态行，这里给一个可写的桩
const stateEl = {innerHTML: ''};
const store = {data: {}, fail: false};
const fakeLS = {
  getItem: function (k) { if (store.fail) throw new Error('denied'); return (k in store.data) ? store.data[k] : null; },
  setItem: function (k, v) { if (store.fail) throw new Error('denied'); store.data[k] = String(v); },
};
const saves = [];
const sandbox = {
  TAB: 'settings',
  window: {localStorage: fakeLS},
  document: {getElementById: id => id === 'vision-state-line' ? stateEl : null,
             querySelector: () => null, querySelectorAll: () => []},
  $: () => view,
  api: p => Promise.resolve(JSON.parse(JSON.stringify(fix[p] || {}))),
  saveKVLoad: (p, body, msg) => { saves.push({p: p, body: body, msg: msg}); return Promise.resolve({ok: true}); },
  goTab: () => {}, load: () => {}, bkGroupsRender: () => {}, ncdlPoll: () => {}, rtPaint: () => {},
};
const names = Object.keys(sandbox);
const mod = new Function(...names, helpers + '\n' + settings +
  '\nreturn {getTab: () => __ST_TAB, vSettings: vSettings, goSettings: goSettings, __stGo: __stGo,' +
  ' stVisionModel: stVisionModel, stCaption: stCaption, visionStatePaint: visionStatePaint};')(...Object.values(sandbox));
const tick = () => new Promise(r => setTimeout(r, 0));   // 排干 vSettings 末尾自己触发的 paint
const bal = h => (h.match(/<div\b/g) || []).length - (h.match(/<\/div>/g) || []).length;
const out = {tabs: {}, lines: {}, cards: {}, paint: {}, d: [], rvRenders: {}};
(async () => {
  for (const t of ['model', 'style', 'general']) {
    mod.goSettings(t);
    await mod.vSettings();
    await tick();
    const h = view.innerHTML;
    const body = h.slice(h.indexOf('</div>', h.indexOf('hint-save')) + 6);
    out.lines[t] = (h.match(/id="vision-state-line"/g) || []).length;
    out.cards[t] = (body.match(/<div class="card/g) || []).length;
    out.bal = out.bal || {};
    out.bal[t] = bal(body);
    if (t === 'model' || t === 'style') out.tabs[t] = body;
    if (t === 'model') {
      const m = body.match(/<input type="number" min="0" max="5000"[^>]*onchange="([^"]*)"/);
      out.capExpr = m ? m[1] : null;
    } else if (t === 'style') {
      const m = body.match(/<a href="javascript:void\(0\)" onclick="([^"]*)"[^>]*>模型与消耗<\/a>/);
      out.navExpr = m ? m[1] : null;
    }
  }
  // (c) ok 样例：model 与 style 两种分页下 visionStatePaint 都填状态行
  fix['/api/agent-status'] = input.visionOk;
  for (const t of ['model', 'style']) {
    mod.goSettings(t);
    await mod.vSettings();
    await tick();
    stateEl.innerHTML = '';
    await mod.visionStatePaint();
    out.paint[t + 'Ok'] = stateEl.innerHTML;
  }
  // (c) no_vision_model：新文案
  fix['/api/agent-status'] = input.visionBad;
  mod.goSettings('model');
  await mod.vSettings();
  await tick();
  stateEl.innerHTML = '';
  await mod.visionStatePaint();
  out.paint.bad = stateEl.innerHTML;
  // (a) 四种 rv 直渲 stVisionModel：接口缺键兜 600 / 接口有值 77 / 直接缺键 / 合法 0
  out.rvRenders.def = mod.stVisionModel(input.rvDef);
  out.rvRenders.cap77 = mod.stVisionModel(input.rv77);
  out.rvRenders.miss = mod.stVisionModel({});
  out.rvRenders.zero = mod.stVisionModel(input.rvZero);
  // (b) 引导链接：表达式与字面量逐字比对（Python 侧同样断言）；跳转用具名函数直接调，不执行字符串
  mod.goSettings('style');
  const before = mod.getTab();
  mod.__stGo('model');
  out.nav = {before: before, after: mod.getTab()};
  // (d) 上限 onchange 表达式逐个真跑（this.value 换成各输入值）
  if (out.capExpr) {
    const f = new Function('saveKVLoad', out.capExpr);
    for (const raw of ['0', '6', '-5', '99999', 'abc', '']) {
      saves.length = 0;
      f.call({value: raw}, (p, body, msg) => { saves.push({p: p, body: body, msg: msg}); });
      out.d.push({raw: raw,
        sent: saves.length && saves[0].body ? saves[0].body.vision_caption_daily_cap : null,
        url: saves.length ? saves[0].p : null,
        msg: saves.length ? saves[0].msg : null});
    }
  }
  process.stdout.write(JSON.stringify(out));
})().catch(e => { process.stdout.write(JSON.stringify({error: String(e && e.stack || e)})); });
"""


def cap_input(html):
    m = re.search(r'<input type="number" min="0" max="5000" value="([^"]*)"', html)
    return m


def t_page(rv77, rv_def):
    if not shutil.which("node"):
        chk("2 页面渲染（需要 node）", False, "找不到 node")
        return
    script = page_script()
    helpers = "\n".join(grab(script, p) for p in (
        r"^function esc\(s\)\{.*\}$", r"^function head\(sub\)\{.*\}$",
        r"^function saveHint\(text\)\{.*\}$", r"^const _SAVE_HINT=.*$", r"^const TITLES=\{.*\};?$"))
    a = script.index("// ---- 设置页的子分页")
    settings = script[a:script.index("let _rtTimer=null;")]
    work = pathlib.Path(tempfile.mkdtemp(prefix="qqbot_visionmove_work_"))
    try:
        jf = work / "visionmove.js"
        jf.write_text(JS, encoding="utf-8")
        inp = work / "input.json"
        inp.write_text(json.dumps(
            {"helpers": helpers, "settings": settings,
             "fix": {"/api/app-config": C_FIX, "/api/reply-config": RV_FIX, "/api/keys": KEYS_FIX},
             "visionOk": VISION_OK, "visionBad": VISION_BAD,
             "rvDef": rv_def, "rv77": rv77,
             "rvZero": dict(RV_FIX, vision_caption_daily_cap=0)}, ensure_ascii=False), encoding="utf-8")
        r = subprocess.run(["node", str(jf), str(inp)], capture_output=True, text=True,
                           encoding="utf-8", timeout=120)
        if r.returncode != 0:
            chk("2 页面能在 node 里跑起来", False, (r.stderr or "")[:400])
            return
        out = json.loads(r.stdout)
        if "error" in out:
            chk("2 页面能在 node 里跑起来", False, out["error"][:400])
            return
        chk("2 页面能在 node 里跑起来", True)
        model, style = out.get("tabs", {}).get("model") or "", out.get("tabs", {}).get("style") or ""

        # ---- a 模型分页：stVisionModel 的控件与两句说明
        chk("a1 模型分页：识图模型输入框（placeholder 留空＝按模型链，值来自配置）",
            '<input value="glm-4.6v-flash" style="width:220px" placeholder="留空＝按模型链"' in model,
            "没找到该输入框")
        mcap = cap_input(model)
        chk("a2 模型分页：每日上限输入框 type=number min=0 max=5000 值=600（有值的 rv）",
            mcap is not None and mcap.group(1) == "600", mcap.group(0) if mcap else "没找到")
        texts = ["留空就是不指定：按上面模型链的顺序，用第一个能看图的模型。哪个模型能看图是实际试出来的，不看名字。模型链里一个能看图的都没有时，识图相关功能不可用，需要在这里填一个。",
                 "每天上限填 0 表示不限制。想完全关掉识图，到「聊天风格」里关「看图模式」或「后台识图」，不必靠上限来关。"]
        miss = [t for t in texts if t not in model]
        chk("a3 模型分页：两句说明齐全", not miss, miss)
        dcap, c77 = cap_input(out.get("rvRenders", {}).get("def") or ""), cap_input(out.get("rvRenders", {}).get("cap77") or "")
        chk("a4 缺键（接口兜 600）：渲染出 value=600、min=0、max=5000",
            dcap is not None and dcap.group(1) == "600", dcap.group(0) if dcap else "没找到")
        chk("a4' 有值 77：渲染出 value=77", c77 is not None and c77.group(1) == "77",
            c77.group(0) if c77 else "没找到")
        mmiss, mzero = cap_input(out.get("rvRenders", {}).get("miss") or ""), cap_input(out.get("rvRenders", {}).get("zero") or "")
        chk("a5 经裁决扩到页面一处：直接拿缺键 rv 渲染 → value=600（不再是 undefined）",
            mmiss is not None and mmiss.group(1) == "600", mmiss.group(0) if mmiss else "没找到")
        chk("a5' 合法值 0 原样渲染 value=0（不许 ||600 把 0 吃成 600）",
            mzero is not None and mzero.group(1) == "0", mzero.group(0) if mzero else "没找到")

        # ---- b 聊天风格分页：两个输入框搬走了，换成引导
        chk("b1 聊天风格：不再有识图模型输入框（placeholder 那个）",
            'placeholder="留空＝按模型链"' not in style, "")
        chk("b1' 聊天风格：不再有每日上限输入框（type=number 且 onchange 含 vision_caption_daily_cap）",
            not re.search(r'<input type="number"[^>]*onchange="[^"]*vision_caption_daily_cap', style)
            and "vision_caption_daily_cap" not in style and "vision_caption_model" not in style, "")
        chk("b2 聊天风格：引导文字与「模型与消耗」链接都在",
            "识图用哪个模型、每天最多识多少条，都在" in style and 'onclick="__stGo(\'model\')"' in style
            and ">模型与消耗</a>" in style and "里设置。" in style, "")
        chk("b2' 链接表达式就是 __stGo('model')（逐字）", out.get("navExpr") == "__stGo('model')",
            out.get("navExpr"))
        nav = out.get("nav") or {}
        chk("b3 直接调 __stGo('model') 后 __ST_TAB 从 style 变成 model",
            nav.get("before") == "style" and nav.get("after") == "model", nav)
        chk("b3' 聊天风格：状态行还在（stCaption 里保留）",
            'id="vision-state-line"' in style, "")

        # ---- c 状态行：每页恰好一个、两种分页都能刷、新文案、旧文案绝迹
        lines = out.get("lines", {})
        chk("c1 每个分页 #vision-state-line 恰好一个（model=1 style=1，general=0 无残留副本）",
            lines.get("model") == 1 and lines.get("style") == 1 and lines.get("general") == 0, lines)
        ok_m, ok_s = out.get("paint", {}).get("modelOk") or "", out.get("paint", {}).get("styleOk") or ""
        chk("c2 model 分页 visionStatePaint 后状态行被填充（识图可用）",
            "识图可用" in ok_m and "读取中" not in ok_m, ok_m[:120])
        chk("c2' style 分页同样被填充", "识图可用" in ok_s and "读取中" not in ok_s, ok_s[:120])
        bad = out.get("paint", {}).get("bad") or ""
        chk("c3 no_vision_model：新文案（到「模型与消耗」里填…或加进模型链）",
            "识图不可用" in bad
            and "到「模型与消耗」里填一个能看图的识图模型，或把能看图的模型加进模型链" in bad, bad[:150])
        old = "在上面填一个能看图的识图模型"
        chk("c3' 旧文案绝迹：整份页面源码与渲染结果里都搜不到",
            old not in script and old not in model and old not in style and old not in bad, "")

        # ---- d 上限 onchange 表达式：逐个真跑
        expr = out.get("capExpr") or ""
        want = [("0", 0), ("6", 6), ("-5", 0), ("99999", 5000), ("abc", 0), ("", 0)]
        got = [(x.get("raw"), x.get("sent")) for x in out.get("d", [])]
        chk("d1 六个输入值实际执行结果 0/6/0/5000/0/0", got == want, got)
        allok = all(x.get("url") == "/api/reply-config"
                    and x.get("msg") == "每日识图上限已保存" for x in out.get("d", [])) and len(out.get("d", [])) == 6
        chk("d1' 每次都走 /api/reply-config、提示「每日识图上限已保存」", allok, out.get("d"))
        chk("d2 表达式里不含「0=不识」字样", "0=不识" not in expr and "0＝不识" not in expr, expr[:120])

        # ---- e 卡片数与分区标题（2026-11-27 起 model 分页多一张「同时处理」卡，
        #      同日再多一张「按用途选模型」卡——WP-05 路由表）
        cards = out.get("cards", {})
        chk("e1 model 分页 8 张卡、div 闭合平衡", cards.get("model") == 8 and out.get("bal", {}).get("model") == 0,
            {k: cards.get(k) for k in cards})
        secs = re.findall(r'letter-spacing:1px">(.*?)</div>', model)
        chk("e2 model 分区标题依次是 API Key 与代理 / 识图模型 / 上下文 / 同时处理 / 省额度 / 按用途选模型",
            secs == ["API Key 与代理", "识图模型（后台识图与看图用）",
                     "上下文（每次带多少历史和记忆）", "同时处理", "省额度", "按用途选模型"], secs)
        # 2026-11-27 起 style 分页多一张「工具循环（实验性）」卡（WP-07 A2a-2）
        chk("e3 style 分页 9 张卡（原 8 + 工具循环）", cards.get("style") == 9, cards.get("style"))
    finally:
        shutil.rmtree(str(work), ignore_errors=True)


def main():
    rv77, rv_def = t_api()
    t_page(rv77, rv_def)
    shutil.rmtree(str(_TMP), ignore_errors=True)
    print()
    print("失败 %d 项" % len(fails) if fails else "全部通过 ✓")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
