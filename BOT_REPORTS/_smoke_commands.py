# -*- coding: utf-8 -*-
"""冒烟：C1 主人指令表 qq_commands.py（纯逻辑模块）+ 渲染必命中。

  T1 表完整：正好 18 条、名字唯一、字段齐、risk/intent/subset 合法、每条
      至少三条互不相同的例句；write 九条 intent 全 False，read 与 soft 全
      True；INTENT_MIN_CONF 与派单一致；by_name 查得到/查不到返回 None。
  T2 渲染必命中（本单最重要）：每条命令的三条例句各配一组合理参数 →
      validate 通过 → render 出的文本逐字等于预期 → 喂给真实的
      owner_command（执行体全打桩）返回 (True, 非空字符串)；再抽一批细看
      桩收到的参数：cron_add 时间补零后存入、quiet 超顶钳 720、quiet/log
      省略参数走默认、feature_toggle/pause/persona_switch 的 POST 体、
      group_add/group_remove/cron_del 的写结果、zcode_handoff 的任务原文。
  T3 参数校验：缺必填、gid 位数不对、hm 格式错/24:00/12:60、int 越界、
      enum 不在选项、参数含换行、subset 不在原话（在原话但空格不同则过）、
      渲染超 400 字、多了不认识的参数、未知命令名——全拒；len>400 的原文
      owner_command 确实不中（400 字杠的依据）。
  T4 intent_doc 三态（closed=hide/mark/plain）：hide 不含任何 intent=False
      的命令名；hide 与 mark 均不超过 2500 字；两次调用输出相同（默认＝
      mark）；mark 18 条全列且未开放九条行尾带「暂不支持」标注、plain 全列
      无标注；非法 closed 值拒绝；预算压小时按条截断并注明。
  T5 render 对没过 validate 的参数（缺必填键、类型不对、越界、未知命令）
      抛 ValueError，不拼半截命令。

隔离：import 被测模块前把 QQBOT_DATA_DIR 指到一次性临时目录（照
_smoke_toolloop_tools.py 的做法）；_ui_api、daemon_status_local、
_tail_log_lines、load_json_const、_cron_tasks/_cron_save、_zcode_handoff、
owner_qq 全部打桩，daemon 用只记 set_quiet 的桩——零真实 API 调用、零真实
写定时、零真实转交、零真实数据读写、不调模型、不发消息。跑完删临时目录。
用法：python BOT_REPORTS/_smoke_commands.py
"""
import contextlib
import os
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join("plugins", "qq-bot", "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 数据目录隔离（必须在 import 被测模块之前生效）
_SMOKE_OWN_DATA_DIR = tempfile.mkdtemp(prefix="qqbot_commands_iso_")
os.environ["QQBOT_DATA_DIR"] = _SMOKE_OWN_DATA_DIR

import qq_bot_agent as A  # noqa: E402
import qq_commands as K   # noqa: E402

fails = []
n_pass = 0


def chk(name, cond, extra=""):
    global n_pass
    print(("PASS " if cond else "FAIL ") + name
          + (" | " + str(extra)[:200] if extra else ""))
    if cond:
        n_pass += 1
    else:
        fails.append(name)


@contextlib.contextmanager
def patched(obj, **names):
    old = {k: getattr(obj, k) for k in names}
    for k, v in names.items():
        setattr(obj, k, v)
    try:
        yield old
    finally:
        for k, v in old.items():
            setattr(obj, k, v)


ALOG = []
A.out = lambda line: ALOG.append(str(line))

OWNER_QQ = "10086"

# ---- 桩：owner_command 的全部执行体 ----

HANDOFF = []          # _zcode_handoff 收到的任务原文
UI_CALLS = []         # _ui_api 收到的 (path, method, body)
TAIL_CALLS = []       # _tail_log_lines 收到的 (文件名, 条数)
CRON_SAVED = []       # _cron_save 收到的任务表（每次一份）
CRON_NOW = [{"id": 1, "time": "08:30", "text": "早报"},
            {"id": 3, "time": "21:05", "text": "晚安"}]   # _cron_tasks 的固定返回


class OwnerDaemon(object):
    """owner_command 的 daemon 桩：只记 set_quiet；群名表给群列表用。"""

    def __init__(self, names=None):
        self.quiet_calls = []
        self._names = dict(names or {})

    def set_quiet(self, minutes):
        self.quiet_calls.append(minutes)

    def group_names(self):
        return dict(self._names)


def fake_handoff(task, msg):
    HANDOFF.append(str(task))
    return "收到！任务已经放进 ZCode 收件箱（测试桩）"


def fake_ui_api(path, method="GET", body=None, timeout=6):
    UI_CALLS.append((path, method, body))
    return {"ok": True, "names": ["小雪", "毒舌", "助理"], "active": "小雪",
            "groups": ["555001"], "enabled": True, "allow_all": False,
            "paused": False, "today_tokens": 1, "today_calls": 1,
            "d7_tokens": 2, "d7_calls": 2, "total_tokens": 3,
            "current_model": "链路桩", "processed": 1}


BASE_STUBS = dict(
    _ui_api=fake_ui_api,
    daemon_status_local=lambda: {"ok": True, "ws_connected": True,
                                 "login": {"nickname": "测试号",
                                           "user_id": OWNER_QQ},
                                 "stats": {"received": 1, "triggered": 2,
                                           "sent": 3}},
    _tail_log_lines=lambda name, n: (TAIL_CALLS.append((name, n))
                                     or ["%s 日志行 %d" % (name, i + 1)
                                         for i in range(max(int(n), 1))]),
    load_json_const=lambda path, default=None: {"enabled": True,
                                                "allow_all": False,
                                                "groups": ["555001"]},
    _cron_tasks=lambda: [dict(x) for x in CRON_NOW],
    _cron_save=lambda tasks: CRON_SAVED.append(list(tasks)),
    _zcode_handoff=fake_handoff,
    owner_qq=lambda: OWNER_QQ,
)


def run_owner(text, group_names=None):
    """打桩跑一遍真实的 owner_command，返回 (hit, reply, dm)。每次调用前
    清掉上一轮的桩记录，dm 的 quiet_calls 是新的。"""
    del HANDOFF[:], UI_CALLS[:], TAIL_CALLS[:], CRON_SAVED[:]
    dm = OwnerDaemon(names=group_names or {"555001": "冒烟群"})
    with patched(A, **BASE_STUBS):
        hit, reply = A.owner_command({"text": text, "sender_id": OWNER_QQ},
                                     daemon=dm)
    return hit, reply, dm


def v_case(name, args, utter):
    """validate + render 一条，返回 (ok, norm, 渲染文本, 原因)。"""
    ok, norm, why = K.validate(name, args, utter)
    return ok, norm, (K.render(name, norm) if ok else ""), why


# ============ T1 表完整 ============

RISK_OF = {"help": "read", "cron_list": "read", "status": "read",
           "usage": "read", "persona_list": "read", "group_list": "read",
           "log": "read", "quiet": "soft", "unquiet": "soft",
           "zcode_handoff": "write", "cron_add": "write", "cron_del": "write",
           "group_add": "write", "group_remove": "write",
           "feature_toggle": "write", "pause": "write", "resume": "write",
           "persona_switch": "write"}
SUBSET_OF = [("cron_del", "id"), ("group_add", "gid"), ("group_remove", "gid"),
             ("persona_switch", "name"), ("zcode_handoff", "task"),
             ("cron_add", "text")]

names = [c["name"] for c in K.COMMANDS]
chk("T1a 正好 18 条、名字唯一、顺序与派单一致",
    len(K.COMMANDS) == 18 and len(set(names)) == 18
    and set(names) == set(RISK_OF), names)

fields_ok = True
risk_ok = intent_ok = ex_ok = param_ok = True
for c in K.COMMANDS:
    for key in ("name", "risk", "intent", "desc", "examples", "params",
                "render"):
        if key not in c:
            fields_ok = False
    if c["risk"] != RISK_OF[c["name"]] or c["risk"] not in K.RISKS:
        risk_ok = False
    if c["risk"] == "write":
        if c["intent"] is not False:
            intent_ok = False
    elif c["intent"] is not True:
        intent_ok = False
    if (len(c["examples"]) < 3 or len(set(c["examples"])) != len(c["examples"])
            or not all(isinstance(x, str) and x.strip() for x in c["examples"])):
        ex_ok = False
    for p in c["params"]:
        if (not all(k in p for k in ("name", "type", "required", "subset"))
                or p["type"] not in K.PARAM_TYPES
                or not isinstance(p["required"], bool)
                or not isinstance(p["subset"], bool)):
            param_ok = False
subset_ok = True
for n, pn in SUBSET_OF:
    pmap = dict((p["name"], p) for p in K.by_name(n)["params"])
    if pmap[pn]["subset"] is not True or pmap[pn]["required"] is not True:
        subset_ok = False
nonsubset_ok = all(
    p["subset"] is False
    for c in K.COMMANDS for p in c["params"]
    if (c["name"], p["name"]) not in SUBSET_OF)
chk("T1b 每条字段齐（name/risk/intent/desc/examples/params/render）", fields_ok, "")
chk("T1c risk 归类与派单一致、全部合法", risk_ok, "")
chk("T1d write 九条 intent=False；read 与 soft 全 True", intent_ok, "")
chk("T1e 每条至少三条互不相同的非空例句", ex_ok, "")
chk("T1f 每个参数带 name/type/required/subset 且类型合法", param_ok, "")
chk("T1g 六个 subset 参数正是派单点名的六个（且必填）；其余全 False",
    subset_ok and nonsubset_ok, "")
chk("T1h INTENT_MIN_CONF = read 0.7 / soft 0.7 / write 0.85",
    K.INTENT_MIN_CONF == {"read": 0.7, "soft": 0.7, "write": 0.85},
    K.INTENT_MIN_CONF)
chk("T1i ask_missing：有必填的九条都有；ask_extra 只有删定时/切换人设两条且"
    "指向 cron_list/persona_list",
    all(K.by_name(n).get("ask_missing") for n in
        ("zcode_handoff", "cron_add", "cron_del", "persona_switch",
         "group_add", "group_remove", "feature_toggle"))
    and not any(c.get("ask_missing") for c in K.COMMANDS if not c["params"]
                or c["name"] in ("quiet", "log"))
    and K.by_name("cron_del").get("ask_extra") == "cron_list"
    and K.by_name("persona_switch").get("ask_extra") == "persona_list"
    and sum(1 for c in K.COMMANDS if "ask_extra" in c) == 2, "")
chk("T1j by_name 查得到返回条目本身、查不到返回 None",
    K.by_name("status") is K.COMMANDS[1] and K.by_name("nope") is None
    and K.by_name(None) is None, "")
def _missing_nope():
    try:
        K.missing("nope", {})
        return "没抛"
    except ValueError:
        return "ValueError"


chk("T1k missing：只看有没有给；未知命令名抛 ValueError（与 render 一致）",
    K.missing("cron_add", {"time": "08:30"}) == ["text"]
    and K.missing("cron_add", {}) == ["time", "text"]
    and K.missing("status", {}) == []
    and K.missing("cron_add", None) == ["time", "text"]
    and _missing_nope() == "ValueError", "")

# ============ T2 渲染必命中（例句 × 参数 → validate → render → owner_command） ============

# 每条命令三条例句各配（参数, 预期渲染文本）；例句必须与表里的完全一致。
CASES = {
    "help": [("你都会些啥指令", {}, "帮助"), ("看看帮助", {}, "帮助"),
             ("把菜单发我看看", {}, "帮助")],
    "status": [("给我报个状态", {}, "状态"), ("现在什么状态", {}, "状态"),
               ("看看你的运行状态", {}, "状态")],
    "usage": [("今天用了多少 token", {}, "用量"), ("看看最近的用量", {}, "用量"),
              ("模型用量统计一下", {}, "用量")],
    "pause": [("先暂停，别回复任何人", {}, "暂停"),
              ("暂停一下所有自动回复", {}, "暂停"),
              ("现在开始停一停，全部暂停", {}, "暂停")],
    "resume": [("好了，恢复回复吧", {}, "恢复"), ("可以恢复了", {}, "恢复"),
               ("解除暂停，继续干活", {}, "恢复")],
    "quiet": [("安静半小时", {"minutes": 30}, "安静 30 分钟"),
              ("别在群里说话了，安静俩小时", {"minutes": 120}, "安静 120 分钟"),
              ("先安静个五分钟吧", {"minutes": 5}, "安静 5 分钟")],
    "unquiet": [("好了，可以说话了", {}, "继续"), ("继续吧", {}, "继续"),
                ("出来说话", {}, "继续")],
    "log": [("看看最近的日志", {}, "日志"), ("把最近 30 条日志发我", {"n": 30}, "日志 30"),
            ("拉 5 条日志给我瞅瞅", {"n": 5}, "日志 5")],
    "cron_add": [("每天早上八点半提醒我给花浇水", {"time": "08:30", "text": "给花浇水"},
                  "定时 08:30 给花浇水"),
                 ("每晚十点整提醒我写日报", {"time": "22:00", "text": "提醒我写日报"},
                  "定时 22:00 提醒我写日报"),
                 ("晚上十一点半的时候，记得提醒我关窗", {"time": "23:30", "text": "提醒我关窗"},
                  "定时 23:30 提醒我关窗")],
    "cron_list": [("现在都有哪些定时任务", {}, "定时列表"), ("看看定时列表", {}, "定时列表"),
                  ("我把哪些定时都列一下", {}, "定时列表")],
    "cron_del": [("把 3 号定时删了", {"id": 3}, "删定时 3"),
                 ("把定时 5 那条删了吧", {"id": 5}, "删定时 5"),
                 ("2 号那个定时不要了", {"id": 2}, "删定时 2")],
    "persona_list": [("现在有哪些人设", {}, "人设列表"), ("看看人设列表", {}, "人设列表"),
                     ("把人设都列出来", {}, "人设列表")],
    "persona_switch": [("切换到小雪这个人设", {"name": "小雪"}, "切换人设 小雪"),
                       ("换成人设「毒舌」", {"name": "毒舌"}, "切换人设 毒舌"),
                       ("用「助理」那个人设回我消息", {"name": "助理"}, "切换人设 助理")],
    "group_add": [("把 555001 这个群加进白名单", {"gid": "555001"}, "加群 555001"),
                  ("加个群 555002", {"gid": "555002"}, "加群 555002"),
                  ("555003 也加进去", {"gid": "555003"}, "加群 555003")],
    "group_list": [("你都在哪些群里", {}, "群列表"), ("看看群列表", {}, "群列表"),
                   ("列一下白名单里的群", {}, "群列表")],
    "group_remove": [("把 555002 移出白名单", {"gid": "555002"}, "移除群 555002"),
                     ("移除群 555003", {"gid": "555003"}, "移除群 555003"),
                     ("555001 不用了，从白名单里撤了吧", {"gid": "555001"}, "移除群 555001")],
    "feature_toggle": [("把看图关掉吧", {"action": "关闭", "feature": "看图"}, "关闭 看图"),
                       ("联网搜索给我打开", {"action": "开启", "feature": "联网搜索"}, "开启 联网搜索"),
                       ("防滥用先关一会儿", {"action": "关闭", "feature": "防滥用"}, "关闭 防滥用")],
    "zcode_handoff": [("把这个任务转交给 ZCode：写一个爬虫脚本", {"task": "写一个爬虫脚本"},
                       "转交ZCode 写一个爬虫脚本"),
                      ("转给 ZCode 处理一下：整理周报数据", {"task": "整理周报数据"},
                       "转交ZCode 整理周报数据"),
                      ("让 ZCode 帮我修一下登录的bug", {"task": "修一下登录的bug"},
                       "转交ZCode 修一下登录的bug")],
}

chk("T2-0 CASES 覆盖全部 18 条且例句与表一致",
    sorted(CASES) == sorted(RISK_OF)
    and all([u for u, _a, _w in CASES[c["name"]]] == list(c["examples"])
            for c in K.COMMANDS), sorted(set(RISK_OF) ^ set(CASES)))

for c in K.COMMANDS:
    name = c["name"]
    bad = []
    for utter, args, want in CASES[name]:
        ok, norm, got, why = v_case(name, args, utter)
        hit, reply = (None, "")
        if ok:
            hit, reply, _dm = run_owner(got)
        if not (ok and got == want and hit is True and isinstance(reply, str)
                and reply):
            bad.append((utter, ok, why or got, hit, reply[:40]))
    chk("T2 %s 三条例句渲染必命中 owner_command（返回 True+非空回复）" % name,
        not bad, bad)


# ---- T2 细看：桩收到的参数 ----

ok, norm, got, why = v_case("cron_add", {"time": "8:30", "text": "提醒我给花浇水"},
                            "明早八点半提醒我给花浇水")
hit, reply, _dm = run_owner(got)
chk("T2x cron_add 8:30 补零成 08:30 存入：id=4、内容原样、私聊发主人",
    ok and hit and len(CRON_SAVED) == 1
    and CRON_SAVED[0][-1] == {"id": 4, "time": "08:30",
                              "text": "提醒我给花浇水",
                              "chat_type": "private", "chat_id": OWNER_QQ},
    (norm, CRON_SAVED))

for mins, utter, want_call in ((1440, "安静一天吧，1440 分钟那种", 720),
                               (999, "安静 999 分钟", 720),
                               (None, "安静一会儿吧", 10)):
    args = {} if mins is None else {"minutes": mins}
    ok, norm, got, why = v_case("quiet", args, utter)
    hit, reply, dm = run_owner(got)
    chk("T2x quiet %s → set_quiet(%d)（超顶钳 720、省略默认 10）"
        % (mins if mins is not None else "省略", want_call),
        ok and hit and dm.quiet_calls == [want_call]
        and ("安静 %d 分钟" % want_call) in reply,
        (norm, got, dm.quiet_calls, reply[:60]))

ok, norm, got, why = v_case("unquiet", {}, "好了，可以说话了")
hit, reply, dm = run_owner(got)
chk("T2x unquiet → set_quiet(0)，回复与手打一致",
    ok and hit and got == "继续" and dm.quiet_calls == [0]
    and reply == "好啦，恢复正常聊天～", (got, dm.quiet_calls, reply))

for n_arg, utter, want_n in ((None, "看看最近的日志", 12),
                             (999, "把日志拉 999 条给我看看", 40),
                             (30, "把最近 30 条日志发我", 30)):
    args = {} if n_arg is None else {"n": n_arg}
    ok, norm, got, why = v_case("log", args, utter)
    hit, reply, _dm = run_owner(got)
    chk("T2x log %s → _tail_log_lines(…, %d)（省略默认 12、超顶钳 40）"
        % (n_arg if n_arg is not None else "省略", want_n),
        ok and hit and TAIL_CALLS == [("agent.log", want_n)]
        and "日志行 1" in reply,
        (norm, got, TAIL_CALLS))

ok, norm, got, why = v_case("feature_toggle",
                            {"action": "关闭", "feature": "看图"}, "把看图关掉吧")
hit, reply, _dm = run_owner(got)
chk("T2x 关闭看图 → POST reply-config {vision_enabled: False}",
    ok and hit and UI_CALLS[-1] == ("/api/reply-config", "POST",
                                    {"vision_enabled": False})
    and reply == "看图已关闭", (got, UI_CALLS[-1], reply))
ok, norm, got, why = v_case("feature_toggle",
                            {"action": "开启", "feature": "联网搜索"},
                            "联网搜索给我打开")
hit, reply, _dm = run_owner(got)
chk("T2x 开启联网搜索 → POST {web_search_enabled: True}",
    ok and hit and UI_CALLS[-1] == ("/api/reply-config", "POST",
                                    {"web_search_enabled": True})
    and reply == "联网搜索已开启", (got, UI_CALLS[-1], reply))

ok, norm, got, why = v_case("pause", {}, "先暂停，别回复任何人")
hit, reply, _dm = run_owner(got)
chk("T2x 暂停 → POST pause {paused: True, src: remote}",
    ok and hit and UI_CALLS[-1] == ("/api/pause", "POST",
                                    {"paused": True, "src": "remote"})
    and reply == "已暂停回复", (got, UI_CALLS[-1], reply))
ok, norm, got, why = v_case("resume", {}, "好了，恢复回复吧")
hit, reply, _dm = run_owner(got)
chk("T2x 恢复 → POST pause {paused: False}",
    ok and hit and UI_CALLS[-1] == ("/api/pause", "POST",
                                    {"paused": False, "src": "remote"})
    and reply == "已恢复回复", (got, UI_CALLS[-1], reply))

ok, norm, got, why = v_case("persona_switch", {"name": "毒舌"}, "换成人设「毒舌」")
hit, reply, _dm = run_owner(got)
chk("T2x 切换人设 → POST personas/switch {name: 毒舌}",
    ok and hit and UI_CALLS[-1] == ("/api/personas/switch", "POST",
                                    {"name": "毒舌"})
    and "已切换到「毒舌」" in reply, (got, UI_CALLS[-1], reply))

ok, norm, got, why = v_case("group_add", {"gid": 555002}, "加个群 555002")
hit, reply, _dm = run_owner(got)
chk("T2x 加群（模型给数字也行）→ POST groups 白名单带 555002",
    ok and norm.get("gid") == "555002" and hit
    and UI_CALLS[-1] == ("/api/groups", "POST",
                         {"enabled": True, "allow_all": False,
                          "groups": ["555001", "555002"]})
    and "已加群 555002" in reply, (norm, UI_CALLS[-1], reply))

ok, norm, got, why = v_case("group_remove", {"gid": "555001"},
                            "555001 不用了，从白名单里撤了吧")
hit, reply, _dm = run_owner(got)
chk("T2x 移除群 → POST groups 白名单变空",
    ok and hit and UI_CALLS[-1] == ("/api/groups", "POST",
                                    {"enabled": True, "allow_all": False,
                                     "groups": []})
    and reply == "已移除群 555001", (got, UI_CALLS[-1], reply))

ok, norm, got, why = v_case("cron_del", {"id": 3}, "把 3 号定时删了")
hit, reply, _dm = run_owner(got)
chk("T2x 删定时 3 → 存回的表只剩 #1，回复与手打一致",
    ok and hit and len(CRON_SAVED) == 1
    and [x["id"] for x in CRON_SAVED[0]] == [1] and reply == "已删除定时 #3",
    (got, CRON_SAVED, reply))
ok, norm, got, why = v_case("cron_del", {"id": 13}, "把 13 号定时删了")
hit, reply, _dm = run_owner(got)
chk("T2x 删定时带数字边界：13 整体命中（渲染必中、删的是 #13）",
    ok and got == "删定时 13" and hit is True and reply == "已删除定时 #13",
    (ok, why, got, hit, reply))

ok, norm, got, why = v_case("zcode_handoff", {"task": "整理周报数据"},
                            "转给 ZCode 处理一下：整理周报数据")
hit, reply, _dm = run_owner(got)
chk("T2x 转交 ZCode → 转交桩收到任务原文，回复带收件箱",
    ok and hit and HANDOFF == ["整理周报数据"] and "收件箱" in reply,
    (got, HANDOFF, reply[:60]))

hit, reply, _dm = run_owner("定时列表")
chk("T2x 定时列表（只读）→ 列出 #1/#3 与时间",
    hit is True and "#1" in reply and "#3" in reply and "08:30" in reply,
    reply[:80])
hit, reply, _dm = run_owner("状态")
chk("T2x 状态（只读）→ 管道/账号/模型齐",
    hit is True and "管道：运行" in reply and "测试号" in reply, reply[:80])
hit, reply, _dm = run_owner("用量")
chk("T2x 用量（只读）→ 今日/近7天/总量",
    hit is True and "今日 1 tokens / 1 次" in reply and "近7天" in reply,
    reply[:80])
hit, reply, _dm = run_owner("帮助")
chk("T2x 帮助 → 逐字等于 OWNER_HELP",
    hit is True and reply == A.OWNER_HELP, "")
hit, reply, _dm = run_owner("人设列表")
chk("T2x 人设列表 → 三个人设与当前激活",
    hit is True and "毒舌" in reply and "当前激活：小雪" in reply, reply[:60])
hit, reply, _dm = run_owner("群列表")
chk("T2x 群列表 → 555001（冒烟群）",
    hit is True and "555001（冒烟群）" in reply, reply[:60])

# ============ T3 参数校验 ============

REJECTS = [
    ("缺必填 time", "cron_add", {"text": "提醒我喝水"}, "每天八点半提醒我喝水", "缺少必填"),
    ("缺必填 task", "zcode_handoff", {}, "转交给 ZCode", "缺少必填"),
    ("缺必填 gid", "group_add", {}, "加个群", "缺少必填"),
    ("gid 太短", "group_add", {"gid": "123"}, "加个群 123", "5–12 位"),
    ("gid 太长", "group_add", {"gid": "1234567890123"}, "群 1234567890123", "5–12 位"),
    ("gid 带字母", "group_add", {"gid": "5550a1"}, "加群 5550a1", "5–12 位"),
    ("hm 分钟一位", "cron_add", {"time": "8:5", "text": "提醒我喝水"}, "八点半提醒我喝水", "HH:MM"),
    ("hm 24:00", "cron_add", {"time": "24:00", "text": "提醒我喝水"}, "提醒我喝水", "00:00–23:59"),
    ("hm 12:60", "cron_add", {"time": "12:60", "text": "提醒我喝水"}, "提醒我喝水", "00:00–23:59"),
    ("hm 不是文本", "cron_add", {"time": 830, "text": "提醒我喝水"}, "提醒我喝水", "HH:MM"),
    ("int 越界（quiet 20000）", "quiet", {"minutes": 20000}, "安静 20000 分钟", "最大 10080"),
    ("int 越界（log 1000）", "log", {"n": 1000}, "拉一千条日志", "最大 999"),
    ("int 非整数", "log", {"n": "abc"}, "看看日志", "要整数"),
    ("int 布尔不算", "quiet", {"minutes": True}, "安静", "true/false"),
    ("enum 不在选项（action）", "feature_toggle", {"action": "打开", "feature": "看图"},
     "把看图打开", "开启/关闭"),
    ("enum 不在选项（feature）", "feature_toggle", {"action": "开启", "feature": "画视频"},
     "开个画视频", "看图/联网搜索/防滥用"),
    ("task 含换行", "zcode_handoff", {"task": "第一行\n第二行"},
     "转交给 ZCode 第一行第二行", "换行"),
    ("定时内容含换行", "cron_add", {"time": "08:30", "text": "提醒我\n喝水"},
     "提醒我喝水", "换行"),
    ("task 太短", "zcode_handoff", {"task": "修它"}, "转交给 ZCode 修它", "长度"),
    ("task 太长（301 字超 cron_add）", "cron_add", {"time": "08:30", "text": "字" * 301},
     "提醒我喝水", "长度"),
    ("人设名太长（81 字）", "persona_switch", {"name": "雪" * 81}, "切换到雪这个人设", "长度"),
    ("subset 不在原话（gid）", "group_add", {"gid": "555001"}, "把隔壁那个群加上", "不在原话"),
    ("subset 不在原话（人设名）", "persona_switch", {"name": "小雪"}, "切换人设", "不在原话"),
    ("subset 不在原话（定时内容）", "cron_add", {"time": "08:30", "text": "给花浇水"},
     "八点半提醒我浇花", "不在原话"),
    ("subset 编号不在原话", "cron_del", {"id": 7}, "把那个定时删了", "不在原话"),
    ("数字边界：13 里截 3", "cron_del", {"id": 3}, "把 13 号定时删了", "不在原话"),
    ("数字边界：1555001 里截 555001", "group_add", {"gid": "555001"},
     "把 1555001 这个群也拉进来", "不在原话"),
    ("全角数字群号", "group_add", {"gid": "５５５００１"},
     "把５５５００１加进白名单", "5–12 位"),
    ("全角数字时间", "cron_add", {"time": "８:３０", "text": "提醒我喝水"},
     "提醒我喝水", "HH:MM"),
    ("log 0 会倒出整个日志尾巴", "log", {"n": 0}, "看下日志", "最小 1"),
    ("渲染超 400 字", "zcode_handoff", {"task": "字" * 395}, "转交ZCode " + "字" * 395,
     "超过 400"),
    ("多了不认识的参数", "quiet", {"minutes": 5, "zzz": 1}, "安静 5 分钟", "不认识"),
    ("args 不是 dict", "quiet", None, "安静", "JSON 对象"),
    ("未知命令名", "reboot", {}, "重启一下", "没有这条指令"),
]
bad_rejects = []
for label, name, args, utter, want in REJECTS:
    ok, norm, why = K.validate(name, args, utter)
    if not (ok is False and norm == {} and want in why):
        bad_rejects.append((label, ok, norm, why))
chk("T3a %d 个非法输入全部拒绝（norm 为空、原因点名）" % len(REJECTS),
    not bad_rejects, bad_rejects)

PASS_OK = [
    ("subset 空格不同（gid）", "group_add", {"gid": "555001"},
     "把 5 5 5 0 0 1 这个群加进白名单"),
    ("subset 空格不同（人设名）", "persona_switch", {"name": "小雪"},
     "帮我切换 到 小雪 这个人设"),
    ("subset 空格不同（定时内容）", "cron_add", {"time": "08:30", "text": "给花浇水"},
     "每天早上八点半 提醒我 给花浇水"),
    ("int 数字文本", "log", {"n": "30"}, "把最近 30 条日志发我"),
    ("int 999 越过钳顶线（quiet）", "quiet", {"minutes": 999}, "安静 999 分钟"),
    ("数字边界：13 整体命中", "cron_del", {"id": 13}, "把 13 号定时删了"),
    ("渲染恰好 400 字", "zcode_handoff", {"task": "字" * 392},
     "转交ZCode " + "字" * 392),
]
bad_pass = []
len400 = -1
for label, name, args, utter in PASS_OK:
    ok, norm, why = K.validate(name, args, utter)
    if ok is not True:
        bad_pass.append((label, ok, why))
    if label == "渲染恰好 400 字":
        try:
            len400 = len(K.render(name, norm))
        except ValueError:
            len400 = -1
chk("T3b 六个边界合法输入全过（含空格不同子串、999 钳顶、400 字整放行）",
    not bad_pass and len400 == 400, bad_pass + [len400])

big = "转交ZCode " + "字" * 500
hit, reply, _dm = run_owner(big)
chk("T3c owner_command 对 >400 字的原文确实不中（400 字杠的依据）",
    hit is False and reply == "", (hit, reply[:40]))

# ============ T4 intent_doc ============

doc = K.intent_doc(closed="hide")   # T4a 原语义不变：只改调用方式（closed="hide"）
doc_m = K.intent_doc()              # 默认 closed="mark"
doc_p = K.intent_doc(closed="plain")
write_names = [c["name"] for c in K.COMMANDS if not c["intent"]]
read_soft_names = [c["name"] for c in K.COMMANDS if c["intent"]]
names_all = [c["name"] for c in K.COMMANDS]
chk("T4a hide 不含任何 intent=False 的命令名（read/soft 的都在）",
    all(n not in doc for n in write_names)
    and all(n in doc for n in read_soft_names),
    [n for n in write_names if n in doc])
chk("T4b hide 总长不超过 2500 字", len(doc) <= 2500, len(doc))
chk("T4b2 默认（mark）总长不超过 2500 字", len(doc_m) <= 2500, len(doc_m))
chk("T4c 两次调用输出相同（纯函数、确定；默认＝closed=\"mark\"）",
    doc_m == K.intent_doc() and K.intent_doc() == K.intent_doc(closed="mark"), "")


def _note_per_line_ok():
    """mark 文档逐行核对：未开放命令的行带「暂不支持」，已开放的行不带。"""
    for ln in doc_m.splitlines():
        if not ln.startswith("- "):
            continue
        nm = ln[2:].split("：", 1)[0]
        if (nm in write_names) != ("暂不支持" in ln):
            return False
    return True


chk("T4e mark：18 条全列、未开放九条行尾带「暂不支持」标注（恰好九处、逐行"
    "对得上）",
    all(n in doc_m for n in names_all)
    and doc_m.count("暂不支持") == len(write_names)
    and _note_per_line_ok(), "")
chk("T4f plain：18 条全列、无任何标注、与 mark 不同、非法 closed 值拒绝",
    all(n in doc_p for n in names_all) and "暂不支持" not in doc_p
    and doc_p != doc_m, "")
try:
    K.intent_doc(closed="nope")
    _ve = "没抛"
except ValueError:
    _ve = "ValueError"
chk("T4g 非法 closed 值抛 ValueError", _ve == "ValueError", _ve)
doc2 = K.intent_doc(max_chars=200)   # mark 模式下预算压小：标注计入长度
chk("T4d 预算压小时按条截断并注明",
    len(doc2) < len(doc_m) and "没列出" in doc2
    and doc2.splitlines()[0] == doc_m.splitlines()[0]
    and doc2 == K.intent_doc(max_chars=200), len(doc2))

# ============ T5 render 对没过 validate 的参数抛 ValueError ============

def raises_ve(name, args):
    try:
        K.render(name, args)
        return False
    except ValueError:
        return True
    except Exception:
        return False


chk("T5a 缺必填键 / 类型不对 / 越界 / 未知命令 / 非 dict 都抛 ValueError",
    raises_ve("cron_add", {"text": "提醒我喝水"})
    and raises_ve("cron_add", {"time": "25:00", "text": "提醒我喝水"})
    and raises_ve("quiet", {"minutes": "abc"})
    and raises_ve("log", {"n": True})
    and raises_ve("group_add", {"gid": "123"})
    and raises_ve("feature_toggle", {"action": "打开", "feature": "看图"})
    and raises_ve("nope", {})
    and raises_ve("status", None)
    and not raises_ve("quiet", {})          # 缺省参数合法：渲染裸「安静」
    and not raises_ve("quiet", {"minutes": 1440}),  # 超顶是 validate 的钳顶
    "")
ok, norm, why = K.validate("quiet", {"minutes": 1440}, "安静一天")
chk("T5b validate 钳顶后的 norm_args 一定能 render",
    ok and K.render("quiet", norm) == "安静 720 分钟", (norm, why))

# ============ 收尾与汇总 ============

print("=" * 60)
print("共 %d 项：PASS %d，FAIL %d" % (n_pass + len(fails), n_pass, len(fails)))
if fails:
    print("失败项：")
    for f in fails:
        print("  - %s" % f)
# 收尾：先关掉 import 时打开的常驻文件句柄（usage 的 FileHandler 锁着
# usage_log.jsonl，不关的话 Windows 下 rmtree 删不掉隔离目录）
for _h in list(A._usage_log.handlers):
    try:
        _h.close()
    except Exception:
        pass
    A._usage_log.removeHandler(_h)
shutil.rmtree(_SMOKE_OWN_DATA_DIR, ignore_errors=True)
if os.path.isdir(_SMOKE_OWN_DATA_DIR):
    print("注意：隔离目录没能删净 %s" % _SMOKE_OWN_DATA_DIR)
else:
    print("隔离目录已删净（os.path.isdir=False）：%s" % _SMOKE_OWN_DATA_DIR)
sys.exit(1 if fails else 0)
