# -*- coding: utf-8 -*-
"""冒烟：WP-08 统一授权开关 owner_auth_code_enabled（危险操作要不要授权码）。

对应施工单 B1 的场景表 S1–S9 与不变量 I1–I4（S10 由跑法保证，见文末说明）：
  S1 新键没设（None／配置文件里缺键）→ 行为只由旧键 device_ctrl_2fa 决定
     （旧键缺省＝开；旧键 False＝关——用户原有设置不丢）。
  S2 新键显式 True/False → 直接生效，不再看旧键（旧键 True 也关、False 也开）。
  S3 新键脏值（字符串、数字、容器）→ 按旧键回落。
  S4 cfg_2fa_on() 与 _owner_auth_code_on() 在每种配置下返回值相同（布尔）。
  S5 开关关：_handle_devctrl 走「不需要授权码直接授权」分支（不索码、不要求
     6 位数、进工具循环）；开关开：仍走索码／比对授权码分支；非主人永远不行。
  S6 /admin/devctrl-code 返回里的 twofa 字段跟随统一开关。
  S7 管理台：复选框换成「危险操作需要授权码」绑定新键（勾选状态＝生效值：
     新键 null 看旧键；保存写新键、不再写旧键；旧复选框文案不再出现）；
     /api/app-config POST 真实登记保存并回读（不模拟 DEFAULT_CONFIG）。
  S8 qq_common.DEFAULT_CONFIG 里新键存在且值是 None。
  S9 README.md 与 README_EN.md 的设备操控段都提到这个开关。
  不变量：I1＝S1 全组；I2＝S5 非主人用例 + 复选框文案仍写「别人无论如何都
     不能操控」；I3＝界面红色二次确认（toggleDevCtrl 的 dlgConfirm）原样在、
     且新复选框不经过它；I4＝cfg_2fa_on 恰好 4 处调用点逐字未动、函数体只
     剩 return _owner_auth_code_on()。
  S10（通过数不比改前少）由跑法保证：本文件与 _smoke_uisec/_smoke_arch/
     _smoke_toolloop_confirm/_smoke_tools 各自单跑，与改前基线
     （78/281/56/41）比对，不在本脚本内复跑。

隔离：import 被测模块前把 QQBOT_DATA_DIR 指到一次性临时目录（照
_smoke_routes.py 开头的做法），所有配置读写都落在这个临时目录；
_handle_devctrl 只打桩 devctrl_agent_loop / owner_qq / out，daemon.send
只记录不发送——零联网、零真实设备操控、零真实发送。跑完删临时目录。
用法：python BOT_REPORTS/_smoke_authswitch.py
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
import time

_TMP = tempfile.mkdtemp(prefix="qqbot_authswitch_iso_")
os.environ["QQBOT_DATA_DIR"] = _TMP
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "plugins" / "qq-bot" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import qq_common as C    # noqa: E402  （import 前 QQBOT_DATA_DIR 已指向临时目录）
import qq_bot_agent as A  # noqa: E402
import qq_admin_ui as U   # noqa: E402

fails = []


def chk(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name
          + (" | " + str(extra)[:200] if extra else ""))
    if not cond:
        fails.append(name)


@contextlib.contextmanager
def patched(obj, **kw):
    old = {k: getattr(obj, k) for k in kw}
    for k, v in kw.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(obj, k, v)


def reset_cfg():
    """清掉临时目录里的 app_config.json = 两个键都回到出厂（新键 None/旧键 True）。"""
    pathlib.Path(C.APP_CONFIG_PATH).unlink(missing_ok=True)


def set_cfg(**kw):
    """真实登记验证：走 save_app_config 写文件、load_app_config 读文件，不模拟。"""
    C.save_app_config(kw)
    return C.load_app_config()


class NullDaemon:
    def __init__(self):
        self.sent = []

    def send(self, chat_type, chat_id, texts, reply_message_id=None):
        self.sent.extend(list(texts))
        return True


def ui_call(method, path, bodyq=None):
    """进程内直调 UiHandler（照 _smoke_uisec 的写法），不起服务。"""
    h = U.UiHandler.__new__(U.UiHandler)
    h.path = path
    data = json.dumps(bodyq).encode("utf-8") if bodyq is not None else b""
    h.headers = {"Content-Length": str(len(data))}
    h.rfile = io.BytesIO(data)
    replies = []
    h._reply = lambda code, obj: replies.append((code, obj))
    h._authed = lambda u, q: True
    getattr(h, method)()
    return replies


def agent_call(method, path, bodyq=None):
    """进程内直调 qq_bot_agent.AdminHandler（/admin/devctrl-code 用）。"""
    h = A.AdminHandler.__new__(A.AdminHandler)
    h.path = path
    data = json.dumps(bodyq).encode("utf-8") if bodyq is not None else b""
    h.headers = {"Content-Length": str(len(data))}
    h.rfile = io.BytesIO(data)
    replies = []
    h._reply = lambda code, obj: replies.append((code, obj))
    h._authed = lambda: True
    getattr(h, method)()
    return replies


def dev_run(text, cfg, *, ask_open=False, code="654321", sender="888001"):
    """按给定配置跑一遍 _handle_devctrl，返回 (handled, sent, loops, ok_until, ask_until)。
    配置走真实文件（set_cfg），行为分支由 _owner_auth_code_on 真读配置决定。"""
    reset_cfg()
    set_cfg(**cfg)
    set_cfg(device_ctrl_enabled=True)
    dm = NullDaemon()
    loops = []
    A._DEVCTRL_AUTH["ok_until"] = 0.0
    A._DEVCTRL_ASK["until"] = time.time() + 300 if ask_open else 0.0
    A._DEVCTRL_CODE.update({"code": code, "expires": time.time() + 300})
    A._DEVCTRL_SESSION.update({"msgs": [], "noticed": False})
    msg = {"id": "m1", "ts": time.time(), "chat_type": "private",
           "chat_id": sender, "sender_id": sender, "sender_name": "t",
           "text": text}
    with patched(A, owner_qq=lambda: "888001", out=lambda line: None,
                 devctrl_agent_loop=lambda d, m, r, f: loops.append(str(m.get("text")))):
        handled = A._handle_devctrl(dm, msg)
    return handled, dm.sent, loops, A._DEVCTRL_AUTH["ok_until"], A._DEVCTRL_ASK["until"]


SRC_AGENT = (SCRIPTS / "qq_bot_agent.py").read_text(encoding="utf-8")
SRC_UI = (SCRIPTS / "qq_admin_ui.py").read_text(encoding="utf-8")
UI_SCRIPT = re.search(r"<script>(.*?)</script>", SRC_UI, re.S).group(1)


@contextlib.contextmanager
def cleanup_tmp():
    """失败跑也不留隔离目录（收尾进 finally；只删本测试自己的 _TMP）。

    qq_bot_agent 在 import 时给 usage_log.jsonl 挂了进程级 FileHandler，
    句柄不关 Windows 不让删文件——先关自己进程里的这个 handler 再删目录。"""
    try:
        yield
    finally:
        for _h in list(getattr(A, "_usage_log", None) and A._usage_log.handlers or []):
            try:
                _h.close()
                A._usage_log.removeHandler(_h)
            except Exception:
                pass
        pathlib.Path(C.APP_CONFIG_PATH).unlink(missing_ok=True)
        shutil.rmtree(_TMP, ignore_errors=True)


# ----------------------------------------------------------- S8 默认表登记
v = C.DEFAULT_CONFIG.get("owner_auth_code_enabled", "MISS")
chk("S8 DEFAULT_CONFIG 里 owner_auth_code_enabled 存在且值是 None",
    "owner_auth_code_enabled" in C.DEFAULT_CONFIG and v is None, v)
chk("S8b 旧键 device_ctrl_2fa 仍在默认表（没被删，缺省 True）",
    C.DEFAULT_CONFIG.get("device_ctrl_2fa") is True)

# ----------------------------------------------------------- S1 新键没设 → 跟随旧键
reset_cfg()
chk("S1a 新键没设（None）+ 旧键缺省 → 开", A._owner_auth_code_on() is True)
set_cfg(device_ctrl_2fa=False)
chk("S1b 新键没设 + 旧键显式 False → 关（回落生效，用户原有设置不丢）",
    A._owner_auth_code_on() is False)
set_cfg(device_ctrl_2fa=True)
chk("S1c 新键没设 + 旧键显式 True → 开", A._owner_auth_code_on() is True)
# 升级场景：旧版配置文件里根本没有新键这个字段
pathlib.Path(C.APP_CONFIG_PATH).write_text(
    json.dumps({"device_ctrl_2fa": False}), encoding="utf-8")
chk("S1d 升级场景：旧版配置文件缺新键字段 → 跟随旧键 False",
    A._owner_auth_code_on() is False)

# ----------------------------------------------------------- S2 新键显式 → 不再看旧键
set_cfg(owner_auth_code_enabled=True, device_ctrl_2fa=False)
chk("S2a 新键显式 True（旧键 False）→ 开", A._owner_auth_code_on() is True)
set_cfg(owner_auth_code_enabled=False, device_ctrl_2fa=True)
chk("S2b 新键显式 False（旧键 True 也关）→ 关", A._owner_auth_code_on() is False)
set_cfg(owner_auth_code_enabled=False, device_ctrl_2fa=False)
chk("S2c 新键显式 False（旧键 False）→ 关", A._owner_auth_code_on() is False)
set_cfg(owner_auth_code_enabled=True, device_ctrl_2fa=True)
chk("S2d 新键显式 True（旧键 True）→ 开", A._owner_auth_code_on() is True)

# ----------------------------------------------------------- S3 新键脏值 → 按旧键
for dirty in ("yes", 1, 0, "false", [], {}):
    set_cfg(owner_auth_code_enabled=dirty, device_ctrl_2fa=False)
    on = A._owner_auth_code_on()
    chk("S3 脏值 %r → 按旧键 False（回落）" % (dirty,), on is False, on)
set_cfg(owner_auth_code_enabled="yes", device_ctrl_2fa=True)
chk("S3b 脏值字符串 + 旧键 True → 开", A._owner_auth_code_on() is True)
set_cfg(owner_auth_code_enabled=None, device_ctrl_2fa=True)
chk("S3c 显式 None（文件里是 null）→ 按旧键 True", A._owner_auth_code_on() is True)

# ----------------------------------------------------------- S4 两个函数同值
cfgs = [
    {},
    {"device_ctrl_2fa": False},
    {"device_ctrl_2fa": True},
    {"owner_auth_code_enabled": True},
    {"owner_auth_code_enabled": False},
    {"owner_auth_code_enabled": None},
    {"owner_auth_code_enabled": True, "device_ctrl_2fa": False},
    {"owner_auth_code_enabled": False, "device_ctrl_2fa": True},
    {"owner_auth_code_enabled": False, "device_ctrl_2fa": False},
    {"owner_auth_code_enabled": "yes", "device_ctrl_2fa": False},
    {"owner_auth_code_enabled": 1, "device_ctrl_2fa": True},
]
bad, details = True, []
for extra in cfgs:
    reset_cfg()
    set_cfg(**extra)
    a, b = A.cfg_2fa_on(), A._owner_auth_code_on()
    if a != b or not isinstance(a, bool):
        bad = False
        details.append((extra, a, b))
chk("S4 %d 种配置下 cfg_2fa_on() 与 _owner_auth_code_on() 返回值相同且为布尔" % len(cfgs),
    bad, details)

# ----------------------------------------------------------- S5 _handle_devctrl 行为分支
_h, _sent, _loops, _ok, _ask = dev_run("设备操控", {"owner_auth_code_enabled": False})
chk("S5a 开（新键 False）：主人请求直接授权——ok_until 写入、进工具循环、不索码、不要求 6 位数",
    _h and _ok > time.time() and _loops == ["设备操控"] and _ask == 0.0
    and all("授权码" not in t for t in _sent)
    and any("连线成功" in t for t in _sent),
    (_h, _sent, _loops, _ask))
_h, _sent, _loops, _ok, _ask = dev_run("设备操控",
                                       {"owner_auth_code_enabled": False,
                                        "device_ctrl_2fa": True})
chk("S5b 新键 False 压过旧键 True：照样直通（开关只看统一键）",
    _h and _ok > time.time() and _loops == ["设备操控"] and _ask == 0.0,
    (_h, _loops, _ask))
_h, _sent, _loops, _ok, _ask = dev_run("设备操控", {"device_ctrl_2fa": False})
chk("S5c 关（回落旧键 False）：同样直通不索码",
    _h and _ok > time.time() and _loops == ["设备操控"] and _ask == 0.0,
    (_h, _loops, _ask))
_h, _sent, _loops, _ok, _ask = dev_run("设备操控", {"owner_auth_code_enabled": False},
                                       sender="777002")
chk("S5d/I2 关：非主人照样不能用（入口限定主人）",
    not _h and not _sent and not _loops and _ok == 0.0, (_h, _sent, _loops))
_h, _sent, _loops, _ok, _ask = dev_run("设备操控", {})
chk("S5e 开（默认）：仍先索授权码（不直接授权）",
    _h and _ok == 0.0 and not _loops and _ask > time.time()
    and any("授权码" in t for t in _sent), (_h, _sent, _loops, _ask))
_h, _sent, _loops, _ok, _ask = dev_run("654321", {}, ask_open=True)
chk("S5f 开：码正确 → 授权窗口打开（比对授权码分支仍走）",
    _h and _ok > time.time() and not _loops
    and any("连线成功" in t for t in _sent), (_h, _sent, _loops))
_h, _sent, _loops, _ok, _ask = dev_run("设备操控",
                                       {"owner_auth_code_enabled": True,
                                        "device_ctrl_2fa": False})
chk("S5g 新键 True 压过旧键 False：仍然索码",
    _h and _ok == 0.0 and not _loops and _ask > time.time()
    and any("授权码" in t for t in _sent), (_h, _sent, _loops, _ask))

# ----------------------------------------------------------- S6 /admin/devctrl-code 跟随统一开关
def twofa_of(cfg):
    reset_cfg()
    set_cfg(**cfg)
    set_cfg(device_ctrl_enabled=True)
    code, resp = agent_call("do_GET", "/admin/devctrl-code")[0]
    return resp.get("twofa"), resp.get("enabled"), resp.get("code")

reset_cfg()
set_cfg(device_ctrl_enabled=True)
_r6 = agent_call("do_GET", "/admin/devctrl-code")[0][1]
_t, _e, _c = _r6.get("twofa"), _r6.get("enabled"), _r6.get("code")
chk("S6a 缺省（跟随旧键 True）：twofa=True、enabled=True、给出授权码",
    _t is True and _e is True and _c, (_t, _e, _c))
_t, _e, _c = twofa_of({"owner_auth_code_enabled": False})
chk("S6b 新键 False：twofa=False、enabled 仍 True、不给码",
    _t is False and _e is True and _c == "", (_t, _e, _c))
_t, _e, _c = twofa_of({"device_ctrl_2fa": False})
chk("S6c 新键没设 + 旧键 False：twofa 跟随旧键=False",
    _t is False and _e is True and _c == "", (_t, _e, _c))

# ----------------------------------------------------------- S7 管理台复选框与保存
chk("S7a 页面有「危险操作需要授权码」复选框，旧的「动态授权码（2FA）」不再出现",
    "危险操作需要授权码" in UI_SCRIPT and "动态授权码（2FA）" not in UI_SCRIPT, "")
chk("S7b 保存写新键 {owner_auth_code_enabled:this.checked?1:0}，不再写旧键",
    "{owner_auth_code_enabled:this.checked?1:0}" in UI_SCRIPT
    and "{device_ctrl_2fa:this.checked?1:0}" not in UI_SCRIPT, "")
chk("S7c 勾选状态＝生效值：新键 null 看旧键（?? 表达式在页面脚本里）",
    "c.owner_auth_code_enabled??c.device_ctrl_2fa" in UI_SCRIPT, "")
chk("S7d/I3 界面红色二次确认不受影响：toggleDevCtrl 的 dlgConfirm 原样在",
    "dlgConfirm({title:'开启设备操控'" in UI_SCRIPT
    and "saveKV('/api/app-config',{device_ctrl_enabled:on}" in UI_SCRIPT, "")
chk("S7e 文案不写说教：没有「极不推荐」也没有「极危险」",
    "极不推荐" not in UI_SCRIPT and "极危险" not in UI_SCRIPT, "")

# 勾选状态表达式（从源码里抠出来）在 node 里按各配置核一遍
m = re.search(r"const on2fa=([^;]+);", UI_SCRIPT)
node_cases = [
    ({"owner_auth_code_enabled": None, "device_ctrl_2fa": True}, True),
    ({"owner_auth_code_enabled": None, "device_ctrl_2fa": False}, False),
    ({"owner_auth_code_enabled": False, "device_ctrl_2fa": True}, False),
    ({"owner_auth_code_enabled": True, "device_ctrl_2fa": False}, True),
    ({"owner_auth_code_enabled": None}, True),
    ({}, True),
]
node_ok, node_detail = False, ""
_jsdir = pathlib.Path(tempfile.mkdtemp(prefix="qqbot_authswitch_js_"))
try:
    if m:
        js = ("const f=(c)=>(%s);\n"
              "const cases=%s;\n"
              "console.log(JSON.stringify(cases.map(([c,w])=>[f(c),w])));\n"
              % (m.group(1), json.dumps([[c, w] for c, w in node_cases])))
        jf = _jsdir / "on2fa_cases.js"
        jf.write_text(js, encoding="utf-8")
        r = subprocess.run(["node", str(jf)], capture_output=True,
                           text=True, cwd=str(ROOT), timeout=60)
        got = json.loads(r.stdout.strip()) if r.returncode == 0 else []
        node_ok = (r.returncode == 0 and len(got) == len(node_cases)
                   and all(g[0] == w for g, (_c2, w) in zip(got, node_cases)))
        node_detail = (r.stdout[:120] or r.stderr[:120])
    else:
        node_detail = "源码里没找到 const on2fa="
finally:
    shutil.rmtree(_jsdir, ignore_errors=True)
chk("S7f 页面勾选状态表达式（node 实跑）6 种配置全部等于生效值",
    node_ok, node_detail)

# POST 真实登记：保存 → 落文件 → 读回（先回到出厂配置，隔离前面用例对旧键的改写）
reset_cfg()
r = ui_call("do_POST", "/api/app-config", {"owner_auth_code_enabled": 1})
d = C.load_app_config()
chk("S7g POST owner_auth_code_enabled=1 → 200 且文件里读回 True",
    r[0][0] == 200 and d.get("owner_auth_code_enabled") is True,
    (r[0][0] if r else None, d.get("owner_auth_code_enabled")))
r = ui_call("do_POST", "/api/app-config", {"owner_auth_code_enabled": 0})
d = C.load_app_config()
chk("S7h POST owner_auth_code_enabled=0 → 读回 False",
    r[0][0] == 200 and d.get("owner_auth_code_enabled") is False,
    (r[0][0] if r else None, d.get("owner_auth_code_enabled")))
b = ui_call("do_GET", "/api/app-config")[0][1]
chk("S7i GET /api/app-config 回读：新键在（False）、旧键也在且不受影响",
    b.get("owner_auth_code_enabled") is False and b.get("device_ctrl_2fa") is True,
    {k: b.get(k) for k in ("owner_auth_code_enabled", "device_ctrl_2fa")})
d = json.loads(pathlib.Path(C.APP_CONFIG_PATH).read_text(encoding="utf-8"))
chk("S7j 落盘文件里写的是布尔（不是 1/0）",
    d.get("owner_auth_code_enabled") is False, d.get("owner_auth_code_enabled"))

# ----------------------------------------------------------- S9 两份 README
rd_cn = (ROOT / "README.md").read_text(encoding="utf-8")
rd_en = (ROOT / "README_EN.md").read_text(encoding="utf-8")
chk("S9a README.md 设备操控段提到「危险操作需要授权码」开关及关掉后的边界",
    "危险操作需要授权码" in rd_cn and "仅限主人" in rd_cn
    and "二次确认" in rd_cn, "")
chk("S9b README_EN.md 设备操控段提到同一开关",
    "Dangerous actions need an auth code" in rd_en
    and "owner-only" in rd_en, "")

# ----------------------------------------------------------- 不变量 I4：调用点与函数体
calls = re.findall(r"cfg_2fa_on\(\)", SRC_AGENT)
chk("I4a cfg_2fa_on() 恰好 5 处带括号出现（1 处 def + 4 处调用，一个不多一个不少）",
    len(calls) == 5, len(calls))
chk("I4b 4 处调用点逐字未动",
    "if m_code and cfg_2fa_on():" in SRC_AGENT
    and "if not cfg_2fa_on() and _devctrl_should_ask(" in SRC_AGENT
    and SRC_AGENT.count("if not cfg_2fa_on():") == 1
    and "twofa = cfg_2fa_on()" in SRC_AGENT, "")
mdef = re.search(r"def cfg_2fa_on\(\):\n(.*?)\n\n\ndef ", SRC_AGENT, re.S)
chk("I4c cfg_2fa_on 函数体只剩 return _owner_auth_code_on()（别名）",
    bool(mdef) and "return _owner_auth_code_on()" in mdef.group(1)
    and "device_ctrl_2fa" not in mdef.group(1),
    mdef.group(1) if mdef else None)

# ----------------------------------------------------------- 收尾
with cleanup_tmp():
    print()
    if fails:
        print("失败 %d 项：" % len(fails))
        for f in fails:
            print("  - " + f)
        sys.exit(1)
    print("全部通过 ✓")
