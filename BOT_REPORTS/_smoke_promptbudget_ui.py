# -*- coding: utf-8 -*-
"""提示词预算（实验）设置卡冒烟（离线：不起进程、不连端口、不写用户数据目录）。

覆盖（对应 2026-11-25 施工单 P3）：
  1 /api/reply-config 四个新键：缺键读出三个 False 和 6；POST 保存后 GET 读回
    一致（布尔三个；整数夹 1-14，非整数按 6）；落盘同样；和 text_img_merge_enabled
    同一次保存互不覆盖；既有键（burst_merge_enabled）读写不受影响。
  2 页面：设置页「模型与消耗」分页「动态上下文预算」卡里的「提示词预算（实验）」
    分区——四个控件都在、默认状态正确（三个不勾、数字框 6 且不可编辑）、勾上后
    数字框可编辑、说明文字齐全、onchange 保存入口各一次、div 闭合平衡。

隔离方式：import 被测模块前把 QQBOT_DATA_DIR 指到 tempfile.mkdtemp()（照
_smoke_uifix2 的做法）；服务端处理器用 UiHandler.__new__ 直接调；页面脚本在
node 里跑（照 _smoke_settings_tabs 的做法）。跑完删临时目录。

用法：python BOT_REPORTS/_smoke_promptbudget_ui.py
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

_TMP = tempfile.mkdtemp(prefix="qqbot_promptbudget_iso_")
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
    """进程内直调 UiHandler（照 _smoke_uifix2 的写法），不起服务。"""
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


BOOL_KEYS = ("prompt_hard_rules_compact", "prompt_dedupe_legacy_tags", "gate_v2_lean_history")
INT_KEY = "gate_v2_hist_lines"
RC = pathlib.Path(_TMP) / "reply_config.json"


def get_cfg():
    with patched(U, REPLY_CONFIG_PATH=str(RC)):
        return ui_call("do_GET", "/api/reply-config")[0][1]


def post_cfg(body):
    with patched(U, REPLY_CONFIG_PATH=str(RC)):
        return ui_call("do_POST", "/api/reply-config", body)


# ----------------------------------------------------------- 1 接口：缺键默认值
def t_defaults():
    RC.unlink(missing_ok=True)
    g = get_cfg()
    chk("1a 缺键：GET 返回三个 False",
        all(g.get(k) is False for k in BOOL_KEYS),
        {k: g.get(k) for k in BOOL_KEYS})
    chk("1a' 缺键：GET 返回 6", g.get(INT_KEY) == 6, g.get(INT_KEY))
    chk("1a'' 缺键时不动磁盘（没有凭空写出配置文件）", not RC.exists(), str(RC))


# ----------------------------------------------------------- 2 接口：布尔保存读回
def t_bools():
    RC.unlink(missing_ok=True)
    body = {"prompt_hard_rules_compact": True, "prompt_dedupe_legacy_tags": False,
            "gate_v2_lean_history": True}
    r = post_cfg(body)
    g = get_cfg()
    disk = json.loads(RC.read_text(encoding="utf-8")) if RC.is_file() else {}
    chk("2a POST 三个布尔 → 200 ok",
        bool(r) and r[0][0] == 200 and r[0][1].get("ok") is True, r[:1])
    chk("2b GET 读回与保存一致",
        all(g.get(k) is body[k] for k in BOOL_KEYS), {k: g.get(k) for k in BOOL_KEYS})
    chk("2c 磁盘同样", all(disk.get(k) is body[k] for k in BOOL_KEYS), disk)
    # 反向再存一遍：全关
    post_cfg({k: False for k in BOOL_KEYS})
    g = get_cfg()
    chk("2d 全关再存后 GET 读回三个 False",
        all(g.get(k) is False for k in BOOL_KEYS), {k: g.get(k) for k in BOOL_KEYS})


# ----------------------------------------------------------- 3 接口：整数夹取
def t_int_clamp():
    cases = [(0, 1), (99, 14), ("abc", 6), (8, 8), (1, 1), (14, 14)]
    for sent, want in cases:
        RC.unlink(missing_ok=True)
        r = post_cfg({INT_KEY: sent})
        g = get_cfg()
        ok = bool(r) and r[0][0] == 200 and g.get(INT_KEY) == want
        chk("3 %s 存进去读出 %s" % (json.dumps(sent), want), ok,
            (r[:1], g.get(INT_KEY)))


# ----------------------------------------------------------- 4 接口：与 text_img 同存互不覆盖
def t_coexist():
    RC.unlink(missing_ok=True)
    r = post_cfg({"prompt_hard_rules_compact": True, "prompt_dedupe_legacy_tags": True,
                  "gate_v2_lean_history": True, INT_KEY: 7,
                  "text_img_merge_enabled": False})
    g = get_cfg()
    chk("4a 同一次保存：四个新键与 text_img 各存各的",
        bool(r) and r[0][0] == 200 and g.get("text_img_merge_enabled") is False
        and g.get("prompt_hard_rules_compact") is True
        and g.get("prompt_dedupe_legacy_tags") is True
        and g.get("gate_v2_lean_history") is True and g.get(INT_KEY) == 7, g)
    post_cfg({"text_img_merge_enabled": True})
    g = get_cfg()
    chk("4b 只动 text_img 后：新键一个不丢",
        g.get("text_img_merge_enabled") is True
        and g.get("prompt_hard_rules_compact") is True
        and g.get("prompt_dedupe_legacy_tags") is True
        and g.get("gate_v2_lean_history") is True and g.get(INT_KEY) == 7, g)
    post_cfg({INT_KEY: 3, "burst_merge_enabled": False})
    g = get_cfg()
    chk("4c 只动条数与既有键后：布尔与 text_img 不丢",
        g.get(INT_KEY) == 3 and g.get("burst_merge_enabled") is False
        and g.get("prompt_hard_rules_compact") is True
        and g.get("text_img_merge_enabled") is True, g)
    post_cfg({"burst_merge_enabled": True})
    g = get_cfg()
    chk("4d 既有键（burst_merge_enabled）关了再开，读写如常、新键不受影响",
        g.get("burst_merge_enabled") is True and g.get(INT_KEY) == 3, g)


# ----------------------------------------------------------- 5 页面（node 沙箱）
def page_script():
    return re.search(r"<script>(.*?)</script>", SRC.read_text(encoding="utf-8"), re.S).group(1)


def grab_block(script, start_pat, end_pat):
    a = re.search(start_pat, script, re.M)
    b = re.search(end_pat, script, re.M)
    if not a or not b or b.start() < a.start():
        raise SystemExit("区块定位失败：%s" % start_pat)
    return script[a.start():b.start()].rstrip() + "\n"


HARNESS = r"""
const input = JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8'));
const mod = new Function('rv', input.code + '\nreturn stDyn(rv);');
const out = {};
out.off = mod({});                                   // 缺键：全关
out.on = mod({prompt_hard_rules_compact: true, prompt_dedupe_legacy_tags: true,
  gate_v2_lean_history: true, gate_v2_hist_lines: 9}); // 全开
out.mixed = mod({gate_v2_lean_history: true});       // 只开「只带几条历史」
process.stdout.write(JSON.stringify(out));
"""


def checkbox_tag(html, key):
    """取某个保存键对应的复选框开标签（含 checked 与否）。"""
    m = re.search(r'<input type="checkbox"( checked)?\s*onchange="saveKVLoad\('
                  r"'/api/reply-config',\{" + re.escape(key) + r":", html)
    return m


def number_tag(html):
    m = re.search(r'<input type="number"[^>]*max="14"[^>]*>', html)
    return m.group(0) if m else ""


def t_page(work):
    if not shutil.which("node"):
        chk("5 页面渲染（需要 node）", False, "找不到 node")
        return
    code = grab_block(page_script(), r"^function stDyn\(rv\)\{", r"^function stArchV2\(rv\)\{")
    jf = work / "promptbudget_harness.js"
    jf.write_text(HARNESS, encoding="utf-8")
    inp = work / "promptbudget_input.json"
    inp.write_text(json.dumps({"code": code}, ensure_ascii=False), encoding="utf-8")
    r = subprocess.run(["node", str(jf), str(inp)], capture_output=True, text=True,
                       encoding="utf-8", timeout=120)
    if r.returncode != 0:
        chk("5 页面能在 node 里跑起来", False, (r.stderr or "")[:400])
        return
    try:
        out = json.loads(r.stdout)
    except ValueError:
        chk("5 页面能在 node 里跑起来", False, (r.stdout or "")[:400])
        return
    chk("5 页面能在 node 里跑起来", True)
    off, on, mixed = out.get("off") or "", out.get("on") or "", out.get("mixed") or ""

    # 5a 缺省：三个复选都不勾、数字框 6 且不可编辑
    nochecked = all(checkbox_tag(off, k) is not None and not checkbox_tag(off, k).group(1)
                    for k in BOOL_KEYS)
    chk("5a 缺省：三个复选都不勾", nochecked,
        {k: (checkbox_tag(off, k) or [None]) and (checkbox_tag(off, k).group(1) or "") for k in BOOL_KEYS})
    num_off = number_tag(off)
    chk("5a' 缺省：数字框值 6、带 disabled（复选没勾时不可编辑）",
        'value="6"' in num_off and "disabled" in num_off, num_off)
    chk("5a'' 缺省：数字框所在行带置灰样式", 'class=""' not in off and 'dimmed' in off, "")

    # 5b 全开：三个都勾、数字框可编辑且值来自配置
    allchecked = all(checkbox_tag(on, k) is not None and checkbox_tag(on, k).group(1) == " checked"
                     for k in BOOL_KEYS)
    chk("5b 全开：三个复选都勾", allchecked,
        {k: (checkbox_tag(on, k).group(1) if checkbox_tag(on, k) else "无标签") for k in BOOL_KEYS})
    num_on = number_tag(on)
    chk("5b' 全开：数字框无 disabled、值 9", "disabled" not in num_on and 'value="9"' in num_on, num_on)

    # 5c 只开「只带几条历史」：数字框可编辑、值回默认 6；另两个不勾
    num_mixed = number_tag(mixed)
    chk("5c 只开第三个：数字框可编辑且缺键时值 6",
        "disabled" not in num_mixed and 'value="6"' in num_mixed, num_mixed)
    others_off = all(checkbox_tag(mixed, k) is not None and not checkbox_tag(mixed, k).group(1)
                     for k in BOOL_KEYS[:2])
    chk("5c' 只开第三个：前两个复选不勾", others_off, "")

    # 5d 说明文字齐全（静态文案，任何状态都在）
    texts = ["提示词预算（实验）",
             "精简硬性规则", "用压缩版替换每条消息都带的那段固定规则，每条约省 900 token。可能影响说话方式。",
             "不再重复教旧标记的写法",
             "新版对话的输出格式已经写明了记忆、等待、安静这些字段，不再重复教旧标记怎么写。每条约省 300–400 token。",
             "判断该不该回时只带最近几条历史", "那次小调用不再带完整对话历史。用的是便宜模型。",
             "带几条历史", "条（1–14，默认 6）",
             "这些是实验性开关，关着时和以前完全一样。"]
    miss = [t for t in texts if t not in off]
    chk("5d 说明文字齐全（%d 段）" % len(texts), not miss, miss)

    # 5e 保存入口：四个键在 onchange 里各恰好一次、都走 /api/reply-config
    attrs = re.findall(r'\sonchange="([^"]*)"', off)
    counts = {}
    for k in BOOL_KEYS + (INT_KEY,):
        counts[k] = sum(('{' + k + ':') in a for a in attrs)
    chk("5e 四个键的保存入口各一次", all(v == 1 for v in counts.values()), counts)
    chk("5e' 保存入口都走 /api/reply-config 且 POST 通道",
        all(("saveKVLoad('/api/reply-config'," in a)
            for a in attrs if any(("{" + k + ":") in a for k in BOOL_KEYS + (INT_KEY,))), "")

    # 5f div 闭合平衡 + 原有「动态上下文预算」控件原样还在
    bal = len(re.findall(r"<div\b", off)) - len(re.findall(r"</div>", off))
    chk("5f 分区 div 闭合平衡", bal == 0, bal)
    chk("5f' 原有「动态上下文预算」开关还在且保存入口不变",
        "动态上下文预算" in off and "dyn_ctx_enabled:this.checked?1:0" in off, "")


def main():
    work = pathlib.Path(tempfile.mkdtemp(prefix="qqbot_promptbudget_work_"))
    try:
        chk("0 隔离生效：数据目录在临时目录",
            str(pathlib.Path(U.DATA_DIR).resolve()).startswith(str(pathlib.Path(_TMP).resolve())),
            U.DATA_DIR)
        t_defaults()
        t_bools()
        t_int_clamp()
        t_coexist()
        t_page(work)
    finally:
        shutil.rmtree(str(work), ignore_errors=True)
        shutil.rmtree(str(_TMP), ignore_errors=True)
    print()
    print("失败 %d 项" % len(fails) if fails else "全部通过 ✓")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
