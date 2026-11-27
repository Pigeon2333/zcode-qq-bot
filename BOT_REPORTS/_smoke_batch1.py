# -*- coding: utf-8 -*-
"""第一批修复冒烟（离线：不联网、不写用户配置文件）。

覆盖：agent 管理端口鉴权 / daemon 令牌空即拒 / 内容安全不冷却 / 截断与空正文 /
视觉链降级与冷却键 / 识图概率 0 跳过（源码级）/ 定时禁言跨午夜 / 群管保存保留
开关 / 市场镜像地址与哈希 / 飞书转发方法名 / 管理台转义函数。

数据目录隔离：import 被测模块前把 QQBOT_DATA_DIR 指到一次性临时目录，不读
生产 ~/.zcode-qq-bot。里面只写一份最小 app_config.json：第 11 节任务桥用
A.owner_qq() 读主人号（缺省为空=任务桥全禁，11a 必假红），主人号取本套冒烟
通用的常量（同 _smoke_fwd/_smoke_gadmin_chat）；其余键缺省即默认（如
task_bridge_enabled 等开关缺文件时默认开启）。跑完删临时目录。

用法：python BOT_REPORTS/_smoke_batch1.py
"""
import datetime
import http.client
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join("plugins", "qq-bot", "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 数据目录隔离（必须在 import 前生效：DATA_DIR/REPLY_CONFIG_PATH/
# APP_CONFIG_PATH 等模块常量在 import 时定死，事后设环境变量不回填）
_SMOKE_OWN_DATA_DIR = tempfile.mkdtemp(prefix="qqbot_batch1_iso_")
os.environ["QQBOT_DATA_DIR"] = _SMOKE_OWN_DATA_DIR
pathlib.Path(_SMOKE_OWN_DATA_DIR, "app_config.json").write_text(
    json.dumps({"owner_qq": "3349525227"}), encoding="utf-8")

import qq_daemon as D  # noqa: E402
import qq_bot_agent as A  # noqa: E402
import qq_plugins as P  # noqa: E402

fails = []


def chk(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (" | " + str(extra)[:100] if extra else ""))
    if not cond:
        fails.append(name)


def read_src(name):
    return pathlib.Path(os.path.join("plugins", "qq-bot", "scripts", name)).read_text(
        encoding="utf-8")


# ---------- 1. 内容安全不冷却 ----------
SAFE_MSG = "系统检测到输入或生成内容可能包含不安全或敏感内容，请您避免输入易产生敏感内容的提示词，感谢您的配合。"
chk("1a 内容安全 400 → 不冷却", A._cooldown_sec(SAFE_MSG) == 0, A._cooldown_sec(SAFE_MSG))
chk("1b 平台过载 → 45s", A._cooldown_sec("该模型当前访问量过大，请您稍后再试") == 45)
chk("1c 套餐失效 → 30min", A._cooldown_sec("套餐已失效") == 1800)
chk("1d 其它 → 默认 300s", A._cooldown_sec("图片输入格式/解析错误") == A.COOLDOWN_SECONDS)

# ---------- 2. agent 管理端口鉴权 ----------
A._ADMIN_TOKEN["t"] = "smoke-token-123"
srv = A.AdminServer((A.DAEMON_HOST, 0), A.AdminHandler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


def _admin_get(headers=None, host=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    if host:
        c.putrequest("GET", "/admin/status", skip_host=True)
        c.putheader("Host", host)
        for k, v in (headers or {}).items():
            c.putheader(k, v)
        c.endheaders()
    else:
        c.request("GET", "/admin/status", headers=headers or {})
    r = c.getresponse()
    r.read()
    c.close()
    return r.status


try:
    chk("2a 无令牌 → 401", _admin_get() == 401)
    chk("2b 错令牌 → 401", _admin_get({"X-QQBot-Token": "nope"}) == 401)
    chk("2c 正确令牌 → 200", _admin_get({"X-QQBot-Token": "smoke-token-123"}) == 200)
    chk("2d 带 Origin（浏览器发起）→ 401",
        _admin_get({"X-QQBot-Token": "smoke-token-123", "Origin": "http://evil.example"}) == 401)
    chk("2e 伪造 Host（DNS 重绑定）→ 401",
        _admin_get({"X-QQBot-Token": "smoke-token-123"}, host="evil.example:39764") == 401)
    A._ADMIN_TOKEN["t"] = ""
    chk("2f 令牌为空 → 一律拒绝", _admin_get({"X-QQBot-Token": ""}) == 401)
finally:
    srv.shutdown()
    srv.server_close()
    A._ADMIN_TOKEN["t"] = ""

# ---------- 3. daemon 令牌空即拒 ----------
h = D.CtlHandler.__new__(D.CtlHandler)
_dm = type("Dm", (), {"ctl_token": "abc"})()
h.server = type("Srv", (), {"daemon": _dm})()   # CtlHandler.daemon 是 server.daemon 的只读属性
h.headers = {}
chk("3a daemon 无头 → 拒绝", h._check_token() is False)
h.headers = {"X-QQBot-Token": "abc"}
chk("3b daemon 正确 → 通过", h._check_token() is True)
h.headers = {"X-QQBot-Token": "abcd"}
chk("3c daemon 长度不同 → 拒绝", h._check_token() is False)
_dm.ctl_token = ""
h.headers = {"X-QQBot-Token": ""}
chk("3d daemon 令牌为空 → 拒绝（此前放行）", h._check_token() is False)

# ---------- 4. 群管保存保留对话群管开关 ----------
d = D.Daemon(ws_url="", token="", cfg={}, ctl_port=0)
saved = []
d._group_admin_save = lambda data: saved.append(json.loads(json.dumps(data)))
d._ga_cfg = {"chat_enabled": True,
             "groups": {"602323545": {"_state": {"muted_date": "2026-11-20"}}}}
d._ga_sig = D.file_sig(D.GROUP_ADMIN_PATH)   # 签名一致 → 直接用内存配置，不读盘
body = json.dumps({"groups": {"602323545": {"sched_ban": {
    "on": True, "mute_hhmm": "23:30", "unmute_hhmm": "07:30"}}}}).encode("utf-8")
h = D.CtlHandler.__new__(D.CtlHandler)
h.server = type("Srv", (), {"daemon": d})()
h.path = "/group-admin/save"
h.headers = {"Content-Length": str(len(body))}
h.rfile = io.BytesIO(body)
replies = []
h._reply = lambda code, obj: replies.append((code, obj))
h._check_token = lambda: True
h.do_POST()
chk("4a 保存返回 200", bool(replies) and replies[0][0] == 200, replies[:1])
chk("4b chat_enabled 被保留", bool(saved) and saved[0].get("chat_enabled") is True, saved[:1])
chk("4c groups 已更新且 _state 保留",
    bool(saved) and "602323545" in (saved[0].get("groups") or {})
    and saved[0]["groups"]["602323545"]["_state"] == {"muted_date": "2026-11-20"})

# ---------- 5. 定时禁言跨午夜 ----------
calls = []
d._call_action = lambda action, params, timeout=None: (calls.append((action, params)) or {})
d._group_admin_save = lambda data: None
REAL_STRFTIME, REAL_LOCALTIME, REAL_FILE_SIG = time.strftime, time.localtime, D.file_sig


class Clock(object):
    def __init__(self, *a):
        self.dt = datetime.datetime(*a)


clock = Clock(2026, 11, 24, 23, 35)


def fake_strftime(fmt="", *a):
    """只伪造本用到的格式（不能借 datetime.strftime——它内部会回调 time.strftime）。"""
    dt = clock.dt
    if fmt == "%Y-%m-%d":
        return "%04d-%02d-%02d" % (dt.year, dt.month, dt.day)
    if fmt == "%H:%M":
        return "%02d:%02d" % (dt.hour, dt.minute)
    if fmt == "%m-%d %H:%M:%S":
        return "%02d-%02d %02d:%02d:%02d" % (dt.month, dt.day, dt.hour, dt.minute, dt.second)
    return REAL_STRFTIME(fmt, *((a or (dt.timetuple(),))))


# 群管配置只驻内存：签名恒定 → _group_admin_cfg() 不会去读本机真实的 group_admin.json
time.strftime = fake_strftime
time.localtime = lambda *a, **k: clock.dt.timetuple()
D.file_sig = lambda p: ("smoke", str(p))
SIG = ("smoke", str(D.GROUP_ADMIN_PATH))


def set_sched(mute, unmute, state=None):
    d._ga_cfg = {"groups": {"1": {"sched_ban": {"on": True, "mute_hhmm": mute,
                                                "unmute_hhmm": unmute},
                                     "_state": dict(state or {})}}}
    d._ga_sig = SIG


def tick(*a):
    clock.dt = datetime.datetime(*a)
    calls[:] = []
    d._sched_ban_tick()
    return [c[1].get("enable") for c in calls if c[0] == "set_group_whole_ban"]


try:
    set_sched("23:30", "07:30")
    chk("5a 23:35 → 禁言", tick(2026, 11, 24, 23, 35) == [True])
    chk("5b 同日 23:59 → 不动作", tick(2026, 11, 24, 23, 59) == [])
    chk("5c 次日 00:05 → 保持禁言（此前在这里被解除）", tick(2026, 11, 25, 0, 5) == [])
    chk("5d 次日 07:29 → 保持禁言", tick(2026, 11, 25, 7, 29) == [])
    chk("5e 次日 07:31 → 解禁", tick(2026, 11, 25, 7, 31) == [False])
    chk("5f 次日 12:00 → 不重复解禁", tick(2026, 11, 25, 12, 0) == [])
    chk("5g 次日 23:31 → 再次禁言", tick(2026, 11, 25, 23, 31) == [True])
    set_sched("12:00", "13:30")
    chk("5h 同日窗口 12:05 → 禁言", tick(2026, 11, 25, 12, 5) == [True])
    chk("5i 同日窗口 13:35 → 解禁", tick(2026, 11, 25, 13, 35) == [False])
    set_sched("23:30", "07:30", {"muted_date": "2026-11-22"})
    chk("5j 停机两天后 09:00 → 补解禁", tick(2026, 11, 24, 9, 0) == [False])
finally:
    time.strftime, time.localtime = REAL_STRFTIME, REAL_LOCALTIME
    D.file_sig = REAL_FILE_SIG

# ---------- 6. 文本模型链：重试分支 / 空正文 / 截断 / 内容安全 ----------
A.log_usage = lambda *a, **k: None
KV = {"key": "dummy", "provider": "zhipu", "base_url": ""}
CANDS = [("k1", KV, "m1", 128, 0), ("k2", KV, "m2", 128, 0)]
A._chain_candidates = lambda: CANDS
seq = {}
log = []


def fake_glm_chat(model, messages, timeout=40, extra_body=None, key=None):
    log.append((model, extra_body))
    return seq[model].pop(0)


A.glm_chat = fake_glm_chat


def reset():
    seq.clear()
    log[:] = []
    with A.RT.lock:
        A.RT.cooldowns.clear()
        A.RT.pinned = None


reset()
seq["m1"] = [(400, {"error": {"message": "该模型不支持关闭思考"}}),
             (200, {"choices": [{"message": {"content": "hi"}}]})]
m, t = A.call_glm_with_fallback([], "")
chk("6a 「不支持关闭思考」重试分支可用（此前 NameError）", (m, t) == ("m1", "hi"), (m, t))
chk("6b 重试用 low 档思考",
    log[1][1].get("thinking") == {"type": "enabled", "level": "low"}, log[1][1])

reset()
seq["m1"] = [(200, {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})]
seq["m2"] = [(200, {"choices": [{"message": {"content": "ok"}}]})]
m, t = A.call_glm_with_fallback([], "")
chk("6c 空正文（被截断）→ 换下一个候选", (m, t) == ("m2", "ok"), (m, t))
chk("6d 空正文只短冷却 15s", 0 < A.RT.cooldowns.get("k1:m1", 0) - time.time() <= 15)

reset()
seq["m1"] = [(200, {"choices": [{"message": {"content": "", "reasoning_content": "我思考的过程"},
                                 "finish_reason": "stop"}]})]
seq["m2"] = [(200, {"choices": [{"message": {"content": "ok"}}]})]
m, t = A.call_glm_with_fallback([], "")
chk("6e 不再把思考内容当正文发出", t == "ok", (m, t))

reset()
seq["m1"] = [(400, {"error": {"message": SAFE_MSG}})]
seq["m2"] = [(200, {"choices": [{"message": {"content": "ok"}}]})]
m, t = A.call_glm_with_fallback([], "")
left = A.RT.cooldowns.get("k1:m1", 0) - time.time()
chk("6f 内容安全 400 后主模型不被冷却", (m, t) == ("m2", "ok") and left <= 0, left)

reset()
seq["m1"] = [(200, {"choices": [{"message": {"content": "被截断的话"}, "finish_reason": "length"}]})]
m, t = A.call_glm_with_fallback([], "")
chk("6g 非空截断正文照常返回", (m, t) == ("m1", "被截断的话"))

# ---------- 7. 视觉链 ----------
VM = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
# 先测真实函数（打桩之前）：QQ 图床链接在没有 daemon 时不联网、返回 None
chk("7a QQ 图床链接本地化失败 → None（不回落原始链接）",
    A._img_to_datauri("https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=y") is None)

A.vision_model = lambda: ""
A._NO_VISION_MODELS.clear()
A._img_to_datauri = lambda u, daemon=None: None
reset()
try:
    A.call_vision(VM, ["https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=x"])
    chk("7b 图片全部获取失败 → 不调用模型直接判失败", False)
except RuntimeError as e:
    chk("7b 图片全部获取失败 → 不调用模型直接判失败",
        "图片全部获取失败" in str(e) and not log, str(e))

A._img_to_datauri = lambda u, daemon=None: "data:image/png;base64,AAAA"
A._chain_candidates = lambda: [("k1", KV, "ma", 128, 0), ("k2", KV, "mv", 128, 0),
                               ("k3", KV, "mb", 128, 0)]
reset()
A.vision_model = lambda: "mv"
# 2026-11-25 复审改了语义：指定模型先用主 Key，再用链里给同一个模型绑定的 Key，所有的 Key
# 都不行才落到链上下一个模型（此前主 Key 一失败就跳过链上同名条目，等于只认主 Key）
seq["mv"] = [(429, {"error": {"message": "已达到 5 小时的使用上限。"}}),
             (200, {"choices": [{"message": {"content": "图里是一只猫"}}]})]
seq["ma"] = [(200, {"choices": [{"message": {"content": "不应该被调到"}}]})]
m, t = A.call_vision(VM, ["https://x/a.png"])
chk("7c 指定视觉模型主 Key 失败 → 接着用链里给同一个模型绑定的 Key",
    (m, t) == ("mv", "图里是一只猫"), (m, t))
chk("7d 顺序是 主 Key → 链上绑定的 Key，不落到别的模型", [x[0] for x in log] == ["mv", "mv"],
    [x[0] for x in log])

reset()
seq["mv"] = [(429, {"error": {"message": "已达到 5 小时的使用上限。"}}),
             (429, {"error": {"message": "该模型当前访问量过大，请您稍后再试"}})]
seq["ma"] = [(200, {"choices": [{"message": {"content": "图里是一只猫"}}]})]
m, t = A.call_vision(VM, ["https://x/a.png"])
chk("7d' 这个模型所有的 Key 都不行 → 才落到链上下一个模型",
    (m, t) == ("ma", "图里是一只猫") and [x[0] for x in log] == ["mv", "mv", "ma"], [x[0] for x in log])

reset()
A.vision_model = lambda: ""
seq["ma"] = [(429, {"error": {"message": "该模型当前访问量过大，请您稍后再试"}})]
seq["mv"] = [(200, {"choices": [{"message": {"content": "看到了"}}]})]
m, t = A.call_vision(VM, ["https://x/a.png"])
ck = A.RT.cooldowns.get("k1:ma", 0)
chk("7e 视觉冷却写入正确的键 kid:model",
    ck > time.time() and "ma" not in A.RT.cooldowns, round(ck - time.time()))

reset()
seq["ma"] = [(200, {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}),
             (200, {"choices": [{"message": {"content": "描述"}}]})]
m, t = A.call_vision(VM, ["https://x/a.png"])
chk("7f 视觉空正文且被截断 → 放大预算重试", (m, t) == ("ma", "描述"), (m, t))
chk("7g 重试预算 8192", log[-1][1].get("max_tokens") == 8192, log[-1][1])
A.vision_model = lambda: ""

# ---------- 8. 市场安装：镜像地址与哈希 ----------
ZIP = "https://github.com/o/r/releases/download/plugins/demo-1.0.0.zip"
got = []


def item(sha):
    return {"ok": True, "plugins": [
        {"name": "demo", "title": "Demo", "version": "1.0.0", "zip": ZIP, "size": 10,
         "sha256": sha}]}


def fake_get_fail_first(url, timeout=20, _hops=0, progress=None):
    got.append(url)
    if len(got) == 1:
        raise OSError("直连失败")
    return b"PK\x05\x06fake"


P.market_list = lambda url=None, force=False: item("")
P._http_get = fake_get_fail_first
P.install_from_zip = lambda p: {"ok": True}
r = P.market_install("demo")
chk("8a 直连失败 → 兜底到「插件包地址 + 代理前缀」",
    got == [ZIP, "https://gh-proxy.com/" + ZIP] and r.get("ok"), got)

got[:] = []
P.market_list = lambda url=None, force=False: item("00" * 32)


def fake_get_ok(url, timeout=20, _hops=0, progress=None):
    got.append(url)
    return b"PK\x05\x06fake"


P._http_get = fake_get_ok
r = P.market_install("demo")
chk("8b 清单带哈希且不符 → 放弃安装",
    r.get("ok") is False and "校验失败" in str(r.get("error")), r)

# ---------- 9. 源码级断言 ----------
a_src = read_src("qq_bot_agent.py")
chk("9a 任务桥入口带主人判断",
    'if owner and feature_enabled("task_bridge_enabled") and _is_task_request(msg):' in a_src)
chk("9b 已移除对不存在函数的调用", "_member_profile_block" not in a_src)
chk("9c 已移除从未生效的空占位降级",
    "blank_img_policy" not in a_src and "_blank_img" not in a_src)
chk("9d 识图概率为 0 时不调用视觉", "AGENT_VISION_ZERO_SKIP" in a_src)
chk("9e 戳一戳等非消息 id 不再尝试挂表情", 'in _rc_ems and mid0.isdigit():' in a_src)
for f in (os.path.join("plugins", "qq-bot", "scripts", "qq_lark.py"),
          os.path.join("market", "lark_bridge", "engine.py")):
    _s = pathlib.Path(f).read_text(encoding="utf-8")
    chk("9f 飞书转发改用 send：" + f, "_DAEMON.send_texts(" not in _s and '_DAEMON.send("group"' in _s)

# ---------- 10. 管理台转义函数（node 实跑） ----------
ui_src = read_src("qq_admin_ui.py")
esc_line = next(ln for ln in ui_src.splitlines() if ln.startswith("function esc(s){"))
fd, tp = tempfile.mkstemp(suffix=".js")
os.close(fd)
try:
    pathlib.Path(tp).write_text(
        esc_line + "\nconsole.log(JSON.stringify([esc('a\"b'), esc(\"it's\"), "
                   "esc('<b>&'), esc('plain')]));\n", encoding="utf-8")
    outp = subprocess.run(["node", str(tp)], capture_output=True, text=True, encoding="utf-8")
    val = json.loads(outp.stdout.strip())
    chk("10a esc 转义双引号", val[0] == "a&quot;b", val)
    chk("10b esc 转义单引号", val[1] == "it&#39;s", val)
    chk("10c esc 仍转义 < > &", val[2] == "&lt;b&gt;&amp;", val)
    chk("10d 普通文本不变", val[3] == "plain", val)
finally:
    pathlib.Path(tp).unlink(missing_ok=True)

# ---------- 11. 任务桥主人校验（真实 process()，假 daemon，不联网不发消息） ----------
class _Stop(Exception):
    pass


def _run_task_gate(sender):
    calls = []
    A.bridge_task = lambda dm, m: calls.append(1) or True

    def _boom(dm, m, ctx_k=32):
        raise _Stop()   # 走到提示词构建 = 已越过任务桥这道门

    A.build_messages = _boom
    # 主人路径上的其它入口与限额查询全部短路，只留任务桥这一个判断
    A._handle_devctrl = lambda dm, m: False
    A._handle_draw = lambda dm, m: False
    A.owner_command = lambda m, dm: (False, "")
    A.token_limit_state = lambda: (False, 0, 0, 1)
    fake = type("FakeDaemon", (), {"ack": lambda self, ids: None})()
    msg = {"id": 9001, "chat_type": "private", "chat_id": "1", "sender_id": sender,
           "sender_name": "x", "text": "任务：把桌面上的文件列出来", "trigger_reason": "at",
           "message_id": "1"}
    try:
        A.process(fake, msg)
        return calls, "returned"
    except _Stop:
        return calls, "passed the bridge gate"


_oq = A.owner_qq()
calls, where = _run_task_gate(_oq)
chk("11a 主人的「任务：」→ 进入任务桥", calls == [1] and where == "returned", (calls, where))
calls, where = _run_task_gate("10001")
chk("11b 非主人的「任务：」→ 不进入任务桥，走普通对话路径",
    calls == [] and where.startswith("passed"), (calls, where))

print("\n%d 项失败" % len(fails) + (": " + "; ".join(fails) if fails else "（全部通过）"))
for _h in list(A._usage_log.handlers):
    _h.close()                     # 放掉 usage_log.jsonl 句柄，Windows 才删得净
    A._usage_log.removeHandler(_h)
shutil.rmtree(_SMOKE_OWN_DATA_DIR, ignore_errors=True)
if os.path.isdir(_SMOKE_OWN_DATA_DIR):
    print("注意：隔离目录没能删净 %s" % _SMOKE_OWN_DATA_DIR)
else:
    print("隔离目录已删净（os.path.isdir=False）：%s" % _SMOKE_OWN_DATA_DIR)
sys.exit(1 if fails else 0)
