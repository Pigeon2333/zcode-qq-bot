# -*- coding: utf-8 -*-
"""冒烟：WP-07 A2b-1 四个带对外副作用的内置工具——send_group（含回读核对）、
draw、cron_add、cron_list（只读），以及 _sendgrp_execute 抽出共用路径后行为
不变（对应派单验收 T1–T11）。

  T1  四个工具 risk/scope/参数名与派单表一致；register_builtin 不传新参数
      不登记这四个、传了才登记；连调两次不抛；send_group 带 verify。
  T2  send_group：白名单群按群名唯一对上 → 群发送桩被调 1 次、参数正确；
      回读 evidence 写「发送计数 +1」与「历史里有这条」；data 带 gid。
  T3  目标群不在白名单 → 不发送、ok=False、说明是现成提示语（与 [发到群]
      同一条校验），提示也照旧发回当前会话。
  T4  群名对不上 → 不发送；模糊匹配到多个群 → 拒发并列出全部对上的群名；
      与某群名完全相等且唯一时算命中（另有名字只是包含它不算歧义）；
      完全同名的群有两个 → 仍拒发。
  T5  发送桩返回失败 → ok=False、说明带失败原因；verify 直接调不抛异常。
  T6  tool_confirm_write 开 → send_group/draw/cron_add 只回「先复述确认」
      不执行；关 → 直接执行；cron_list 是 read 不受开关影响。
  T7  draw 的执行体桩被调 1 次、参数是 prompt。
  T8  cron_add：8:30/08:30 通过并存入（存入桩收到任务、私聊发主人）；
      25:00/abc/8/8:5 拒且不调存入函数；内容为空拒。
  T9  cron_list 返回定时清单文字；无任务时回「暂无」。
  T10 _sendgrp_execute 重构后行为不变（主人拒绝只记日志不发送 / 找不到群
      发现有提示 / 发成功不打扰当前会话 / 发送失败回会话 / 旧路径多匹配
      仍取第一个）+ _sendgrp_execute 与工具包装调同一个解析发送函数（运行
      时桩 + 源码级各一条）。
  T11 非主人 tools_for 拿不到这四个工具；run_tool 权限闸也拒。
  T-C 定时时间补零（bug 修复）：存成 8:30 的任务在 08:30 触发、08:30 照常、
      脏时间值不抛异常不触发；旧命令「定时 9:0 …」仍不匹配，「定时 9:05 …」
      存为补零 09:05 且列表照此显示；工具路径找不到群不重复打扰主人、
      旧标记路径仍直接提示（T3c/T3d）。

隔离：import 被测模块前把 QQBOT_DATA_DIR 指到一次性临时目录（照
_smoke_toolloop_run.py 的做法）；群白名单指到临时目录里的临时文件；群发送、
画图执行体、定时存入/读取全部打桩——零真实发送、零真实画图、零真实写定时。
跑完删临时目录。
用法：python BOT_REPORTS/_smoke_toolloop_tools.py
"""
import contextlib
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.join("plugins", "qq-bot", "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 数据目录隔离（必须在 import 被测模块之前生效）
_SMOKE_OWN_DATA_DIR = tempfile.mkdtemp(prefix="qqbot_toolloop_tools_iso_")
os.environ["QQBOT_DATA_DIR"] = _SMOKE_OWN_DATA_DIR

import qq_daemon as D  # noqa: E402,F401  (与其它冒烟保持同一导入面)
import qq_bot_agent as A  # noqa: E402
import qq_tools as T  # noqa: E402
import qq_tools_builtin as TB  # noqa: E402

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
NOT_OWNER = "99999"
FOUR = ("send_group", "draw", "cron_add", "cron_list")

ISO_DIR = pathlib.Path(A.DATA_DIR)


def write_wl(name, obj):
    """把群白名单写进隔离数据目录里的临时文件（不走真实数据）。"""
    p = ISO_DIR / name
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return str(p)


def owner_msg(**kw):
    m = {"id": 1, "ts": 0, "chat_type": "private", "chat_id": OWNER_QQ,
         "chat_name": "主人", "message_id": "m1", "sender_id": OWNER_QQ,
         "sender_name": "主人", "triggered": True, "trigger_reason": "chat",
         "text": "在吗", "at_me": False, "at_users": [], "image_urls": [],
         "has_reply": False, "reply_to": None, "reply_text": "",
         "face_count": 0, "has_record": False, "reply_id": "",
         "record_file": "", "record_url": "", "forward_lines": []}
    m.update(kw)
    return m


class GrpDaemon(object):
    """群发送记录桩：group 发送记账（可造失败）；status/history 供回读核对。"""

    def __init__(self, names=None, hist=None, fail=False):
        self._names = dict(names or {})
        self._hist = list(hist or [])
        self._fail = fail
        self.group_sends = []
        self.chat_sends = []
        self.sent_count = 100

    def group_names(self):
        return dict(self._names)

    def send(self, chat_type, chat_id, texts, reply_message_id=None):
        if chat_type == "group":
            self.group_sends.append((chat_type, chat_id, list(texts)))
            if self._fail:
                return {"ok": False, "error": "模拟发送失败"}
            self.sent_count += 1
            return {"ok": True}
        self.chat_sends.append((chat_type, chat_id, list(texts)))
        return {"ok": True}

    def status(self):
        return {"stats": {"sent": self.sent_count}}

    def history(self, chat_type, chat_id, limit=30):
        return self._hist[:limit] if limit else list(self._hist)


def ctx_of(dm, m):
    return {"daemon": dm, "msg": m, "is_owner": True}


def spec_named(name):
    return {"name": name, "desc": "d", "params": [], "risk": "read",
            "scope": "all", "returns": "r", "run": lambda d, m, a: None}


def _raises_dup(name):
    try:
        T.register(spec_named(name))
        return False
    except ValueError as e:
        return "已登记" in str(e)


def fake_search(q):
    return ("x 测试条目（example.com）", "bing")


# ============ T1 登记：不传不登记、传了才登记、幂等 ============

chk("T1a 初始登记表里没有这四个",
    all(T.get_tool(n) is None for n in FOUR),
    [n for n in FOUR if T.get_tool(n) is not None])

TB.register_builtin(fake_search)
chk("T1b 只传 search 时不登记这四个（web_search/react 照常）",
    all(T.get_tool(n) is None for n in FOUR)
    and T.get_tool("web_search") is not None and T.get_tool("react") is not None,
    [n for n in FOUR if T.get_tool(n) is not None])

try:
    TB.register_builtin(
        fake_search,
        send_group_fn=A._sendgrp_tool_send,
        draw_fn=A._draw_tool,
        cron_add_fn=A._cron_add_tool,
        cron_list_fn=A._cron_list_tool)
    reg1_ok = True
except Exception as exc:
    reg1_ok = False
    print("register_builtin 异常：%r" % exc)

sg, dr, ca, cl = (T.get_tool(n) for n in FOUR)
chk("T1c 传了才登记；risk/scope/参数名与派单表一致",
    reg1_ok
    and sg and (sg["risk"], sg["scope"]) == ("write", "owner")
    and [p["name"] for p in sg["params"]] == ["target", "text"]
    and all(p["required"] for p in sg["params"])
    and dr and (dr["risk"], dr["scope"]) == ("write", "owner")
    and [p["name"] for p in dr["params"]] == ["prompt"]
    and dr["params"][0]["required"]
    and ca and (ca["risk"], ca["scope"]) == ("write", "owner")
    and [p["name"] for p in ca["params"]] == ["time", "content"]
    and all(p["required"] for p in ca["params"])
    and cl and (cl["risk"], cl["scope"]) == ("read", "owner")
    and cl["params"] == [],
    [(n, T.get_tool(n) and (T.get_tool(n)["risk"], T.get_tool(n)["scope"]))
     for n in FOUR])

dup_ok = False
try:
    TB.register_builtin(
        fake_search,
        send_group_fn=A._sendgrp_tool_send,
        draw_fn=A._draw_tool,
        cron_add_fn=A._cron_add_tool,
        cron_list_fn=A._cron_list_tool)
    dup_ok = all(_raises_dup(n) for n in FOUR)
except Exception as exc:
    print("register_builtin 二次调用异常：%r" % exc)
chk("T1d 连调两次不抛异常；手工重复登记每个都报同名",
    dup_ok, [n for n in FOUR if not _raises_dup(n)])

chk("T1e send_group 带 verify，其余三个没有",
    callable(sg.get("verify")) and "verify" not in dr
    and "verify" not in ca and "verify" not in cl,
    (callable(sg.get("verify")), [n for n in ("draw", "cron_add", "cron_list")
                                  if T.get_tool(n).get("verify")]))

SRC_A = pathlib.Path("plugins", "qq-bot", "scripts",
                     "qq_bot_agent.py").read_text(encoding="utf-8")
chk("T1f main() 把四个薄包装接进 register_builtin",
    "send_group_fn=_sendgrp_tool_send" in SRC_A
    and "draw_fn=_draw_tool" in SRC_A
    and "cron_add_fn=_cron_add_tool" in SRC_A
    and "cron_list_fn=_cron_list_tool" in SRC_A, "")


# ============ T11 非主人拿不到 ============

names_non = [s["name"] for s in T.tools_for({"is_owner": False})]
names_owner = [s["name"] for s in T.tools_for({"is_owner": True})]
chk("T11a 非主人 tools_for 清单里没有这四个",
    not set(FOUR) & set(names_non), names_non)
chk("T11b 主人 tools_for 清单里有这四个",
    set(FOUR) <= set(names_owner), names_owner)

m_non = owner_msg(sender_id=NOT_OWNER, chat_id=NOT_OWNER, sender_name="路人")
dm11 = GrpDaemon(names={"555001": "冒烟群"})
r11 = T.run_tool(T.get_tool("send_group"), {"target": "冒烟群", "text": "hi"},
                 {"daemon": dm11, "msg": m_non, "is_owner": False}, {})
chk("T11c run_tool 权限闸拒非主人：不发送",
    r11["ok"] is False and "主人" in r11["summary"] and not dm11.group_sends
    and not dm11.chat_sends, r11["summary"])


# ============ T2 send_group 唯一群名命中 + 回读核对 ============

WL2 = write_wl("wl_t2.json",
               {"enabled": True, "groups": ["555001"], "allow_all": False})
dm2 = GrpDaemon(names={"555001": "冒烟群"},
                hist=[{"ts": 1, "text": "测试消息", "is_bot": True,
                       "sender_name": "机器人"}])
m2 = owner_msg()
with patched(A, GROUP_WL_PATH=WL2, owner_qq=lambda: OWNER_QQ):
    r2 = T.run_tool(T.get_tool("send_group"),
                    {"target": "冒烟群", "text": "测试消息"},
                    ctx_of(dm2, m2), {})
chk("T2a 群发送桩被调 1 次、参数正确",
    dm2.group_sends == [("group", "555001", ["测试消息"])], dm2.group_sends)
chk("T2b ok=True；evidence 写了发送计数 +1",
    r2["ok"] is True and "发送计数 +1" in r2["evidence"], r2["evidence"])
chk("T2c evidence 写了历史里有这条",
    "历史里有这条消息" in r2["evidence"] and "确认发出去了" in r2["evidence"],
    r2["evidence"])
chk("T2d data 带回读用的群号",
    r2["data"].get("gid") == "555001"
    and r2["data"].get("sent_before") == 100, r2["data"])


# ============ T3 目标群不在白名单 ============

WL3 = write_wl("wl_t3.json",
               {"enabled": True, "groups": ["555001"], "allow_all": False})
dm3 = GrpDaemon(names={"555001": "冒烟群", "888888": "外面的群"})
with patched(A, GROUP_WL_PATH=WL3, owner_qq=lambda: OWNER_QQ):
    r3 = T.run_tool(T.get_tool("send_group"),
                    {"target": "外面的群", "text": "hi"},
                    ctx_of(dm3, m2), {})
chk("T3a 不发送（无群发送）", dm3.group_sends == [], dm3.group_sends)
chk("T3b ok=False、说明是现成的提示语（与 [发到群] 同一条校验）",
    r3["ok"] is False
    and "没找到这个群（或它不在我的群白名单里）" in r3["summary"], r3["summary"])
chk("T3c 工具路径不重复打扰主人：当前会话 0 条提示（说明已走工具结果回喂）",
    dm3.chat_sends == [], dm3.chat_sends)

dm3b = GrpDaemon(names={"555001": "冒烟群", "888888": "外面的群"})
with patched(A, GROUP_WL_PATH=WL3, owner_qq=lambda: OWNER_QQ):
    A._sendgrp_execute(dm3b, m2, "外面的群", "hi")
chk("T3d 旧标记路径同一输入仍然直接发提示（行为不变）",
    dm3b.group_sends == [] and dm3b.chat_sends
    and "没找到这个群（或它不在我的群白名单里）" in "".join(dm3b.chat_sends[-1][2]),
    (dm3b.group_sends, dm3b.chat_sends))


# ============ T4 群名对不上 / 歧义 / 完全同名唯一 ============

WL4 = write_wl("wl_t4.json", {"enabled": True,
                              "groups": ["555001", "555002", "555003"],
                              "allow_all": False})
with patched(A, GROUP_WL_PATH=WL4, owner_qq=lambda: OWNER_QQ):
    dm4a = GrpDaemon(names={"555001": "冒烟群"})
    r4a = T.run_tool(T.get_tool("send_group"),
                     {"target": "不存在的群", "text": "hi"},
                     ctx_of(dm4a, m2), {})

    dm4b = GrpDaemon(names={"555001": "测试群", "555002": "测试二群",
                            "555003": "无关群"})
    r4b = T.run_tool(T.get_tool("send_group"), {"target": "测试", "text": "hi"},
                     ctx_of(dm4b, m2), {})

    dm4c = GrpDaemon(names={"555001": "测试群", "555002": "测试群2号"})
    r4c = T.run_tool(T.get_tool("send_group"),
                     {"target": "测试群", "text": "hi"},
                     ctx_of(dm4c, m2), {})

    dm4d = GrpDaemon(names={"555001": "测试群", "555002": "测试群"})
    r4d = T.run_tool(T.get_tool("send_group"),
                     {"target": "测试群", "text": "hi"},
                     ctx_of(dm4d, m2), {})

chk("T4a 群名对不上：不发送、ok=False",
    dm4a.group_sends == [] and r4a["ok"] is False, (r4a["ok"], r4a["summary"]))
chk("T4b 模糊匹配到多个群：拒发、列出全部对上的 群名(群号)",
    dm4b.group_sends == [] and r4b["ok"] is False
    and "叫这个名字的群有多个" in r4b["summary"]
    and "测试群(555001)" in r4b["summary"] and "测试二群(555002)" in r4b["summary"]
    and "请告诉我群号" in r4b["summary"], r4b["summary"])
chk("T4c 拒发提示不直接进当前会话（走工具结果回喂）",
    dm4b.chat_sends == [], dm4b.chat_sends)
chk("T4d 完全同名唯一算命中（另有名字只是包含它）",
    dm4c.group_sends == [("group", "555001", ["hi"])] and r4c["ok"] is True,
    (dm4c.group_sends, r4c["summary"]))
chk("T4e 完全同名的群有两个：仍拒发",
    dm4d.group_sends == [] and r4d["ok"] is False
    and "叫这个名字的群有多个" in r4d["summary"], r4d["summary"])


# ============ T5 发送桩失败 ============

dm5 = GrpDaemon(names={"555001": "冒烟群"}, fail=True)
with patched(A, GROUP_WL_PATH=WL2, owner_qq=lambda: OWNER_QQ):
    r5 = T.run_tool(T.get_tool("send_group"), {"target": "冒烟群", "text": "hi"},
                    ctx_of(dm5, m2), {})
chk("T5a 发送桩失败：ok=False、说明带现成的失败文案、不直接打扰会话",
    r5["ok"] is False and "发到群里失败：模拟发送失败" in r5["summary"]
    and dm5.chat_sends == [],
    (r5["summary"], dm5.chat_sends))
ev5 = ""
try:
    res5 = T.make_result(False, r5["summary"],
                         data={"gid": "555001", "sent_before": dm5.sent_count})
    ev5 = T.get_tool("send_group")["verify"](dm5, m2,
                                             {"target": "冒烟群", "text": "hi"},
                                             res5)
except Exception as exc:
    ev5 = "EXC:%r" % exc
chk("T5b verify 直接调不抛异常、返回文字", not ev5.startswith("EXC:")
    and isinstance(ev5, str), ev5)
ev_empty = T.get_tool("send_group")["verify"](
    dm2, m2, {"target": "冒烟群", "text": "hi"},
    T.make_result(True, "x", data={}))
chk("T5c 群号取不到就不回读：evidence 留空", ev_empty == "", ev_empty)


# ============ T6 复述确认开关 ============

draw_rec = []
save_rec = []
ALOG.clear()
with patched(A, _draw_execute=lambda d, m, p: draw_rec.append(p),
             _cron_save=lambda ts: save_rec.append(list(ts))):
    r6a = T.run_tool(T.get_tool("send_group"),
                     {"target": "冒烟群", "text": "hi"},
                     ctx_of(dm2, m2), {"confirm_write": True})
    r6b = T.run_tool(T.get_tool("draw"), {"prompt": "一只猫"},
                     ctx_of(dm2, m2), {"confirm_write": True})
    r6c = T.run_tool(T.get_tool("cron_add"),
                     {"time": "8:30", "content": "提醒喝水"},
                     ctx_of(dm2, m2), {"confirm_write": True})
chk("T6a confirm 开：三个 write 工具只回「先复述确认」不执行",
    all(not r["ok"] and "复述" in r["summary"] and "还没有执行" in r["summary"]
        for r in (r6a, r6b, r6c))
    and dm2.group_sends == [("group", "555001", ["测试消息"])]
    and draw_rec == [] and save_rec == [],
    (r6a["summary"], r6b["summary"], r6c["summary"]))

with patched(A, _draw_execute=lambda d, m, p: draw_rec.append(p),
             _cron_save=lambda ts: save_rec.append(list(ts)),
             owner_qq=lambda: OWNER_QQ,
             _cron_tasks=lambda: [{"id": 1, "time": "07:00", "text": "早报"}]):
    r6d = T.run_tool(T.get_tool("draw"), {"prompt": "一只猫"},
                     ctx_of(dm2, m2), {})
    r6e = T.run_tool(T.get_tool("cron_add"),
                     {"time": "8:30", "content": "提醒喝水"},
                     ctx_of(dm2, m2), {})
    r6f = T.run_tool(T.get_tool("cron_list"), {}, ctx_of(dm2, m2),
                     {"confirm_write": True})
chk("T6b confirm 关：draw/cron_add 直接执行（桩被调）",
    r6d["ok"] is True and draw_rec == ["一只猫"]
    and r6e["ok"] is True and len(save_rec) == 1,
    (draw_rec, save_rec))
chk("T6c cron_list 是 read，confirm 开也照常执行",
    r6f["ok"] is True and "#1" in r6f["summary"], r6f["summary"])


# ============ T7 draw 执行体被调一次、参数是 prompt ============

draw_rec7 = []
with patched(A, _draw_execute=lambda d, m, p: draw_rec7.append((m, p))):
    r7 = T.run_tool(T.get_tool("draw"), {"prompt": "一只在窗台看雨的橘猫"},
                    ctx_of(dm2, m2), {})
chk("T7 draw 执行体桩被调 1 次、参数是 prompt",
    len(draw_rec7) == 1 and draw_rec7[0][1] == "一只在窗台看雨的橘猫"
    and r7["ok"] is True, (draw_rec7, r7["summary"]))


# ============ T8 cron_add 时间格式校验 ============

def cron_add_case(tool_args):
    """每次用干净桩跑一遍 cron_add 工具，返回 (结果, 存入桩收到的任务表)。"""
    saved = []
    with patched(A, owner_qq=lambda: OWNER_QQ,
                 _cron_tasks=lambda: [{"id": 1, "time": "07:00",
                                       "text": "早报"}],
                 _cron_save=lambda ts: saved.append(list(ts))):
        r = T.run_tool(T.get_tool("cron_add"), tool_args, ctx_of(dm2, m2), {})
    return r, saved


r8a, s8a = cron_add_case({"time": "8:30", "content": "提醒我喝水"})
chk("T8a 8:30 通过并存入（补零成 08:30）：新任务 id=2、私聊发主人",
    r8a["ok"] is True and len(s8a) == 1 and s8a[0][1] == {
        "id": 2, "time": "08:30", "text": "提醒我喝水",
        "chat_type": "private", "chat_id": OWNER_QQ},
    (r8a["summary"], s8a))
r8b, s8b = cron_add_case({"time": "08:30", "content": "早安"})
chk("T8b 08:30 通过并存入（原样保存时间）",
    r8b["ok"] is True and s8b[0][1]["time"] == "08:30", (r8b["summary"], s8b))
r8c, s8c = cron_add_case({"time": "25:00", "content": "x"})
r8d, s8d = cron_add_case({"time": "abc", "content": "x"})
r8e, s8e = cron_add_case({"time": "8", "content": "x"})
r8f, s8f = cron_add_case({"time": "8:5", "content": "x"})
r8g, s8g = cron_add_case({"time": "8:30", "content": "   "})
chk("T8c 25:00 拒且不调存入函数",
    r8c["ok"] is False and s8c == [] and "时间格式" in r8c["summary"],
    (r8c["summary"], s8c))
chk("T8d abc 拒且不调存入函数",
    r8d["ok"] is False and s8d == [] and "时间格式" in r8d["summary"],
    (r8d["summary"], s8d))
chk("T8e 8 拒且不调存入函数",
    r8e["ok"] is False and s8e == [] and "时间格式" in r8e["summary"],
    (r8e["summary"], s8e))
chk("T8f 8:5 拒（分钟要两位，与旧命令同一格式）",
    r8f["ok"] is False and s8f == [], (r8f["summary"], s8f))
chk("T8g 内容为空拒且不调存入函数",
    r8g["ok"] is False and s8g == [] and "内容" in r8g["summary"],
    (r8g["summary"], s8g))


# ============ T9 cron_list 清单文字 ============

with patched(A, _cron_tasks=lambda: [
        {"id": 2, "time": "08:30", "text": "收天气"},
        {"id": 5, "time": "21:05", "text": "晚安"}]):
    r9 = T.run_tool(T.get_tool("cron_list"), {}, ctx_of(dm2, m2), {})
    with patched(A, _cron_tasks=lambda: []):
        r9b = T.run_tool(T.get_tool("cron_list"), {}, ctx_of(dm2, m2), {})
chk("T9 cron_list 返回定时清单文字",
    r9["ok"] is True and "#2" in r9["summary"] and "08:30" in r9["summary"]
    and "#5" in r9["summary"] and "收天气" in r9["summary"], r9["summary"])
chk("T9b 无任务时回「暂无」", r9b["ok"] is True and "暂无" in r9b["summary"],
    r9b["summary"])


# ============ T10 _sendgrp_execute 重构后行为不变 + 同一解析发送函数 ============

rec_calls = []


def fake_resolve(daemon, msg, target, text, notify=True):
    rec_calls.append((target, text, notify))
    return True, "已发到群 555001（1 段）", "555001"


with patched(A, _sendgrp_resolve_and_send=fake_resolve):
    A._sendgrp_execute(dm2, m2, "旧标记目标", "旧标记内容")
    A._sendgrp_tool_send(dm2, m2, "工具目标", "工具内容")
chk("T10a _sendgrp_execute 与工具包装调的是同一个解析发送函数（运行时桩）",
    len(rec_calls) == 2
    and rec_calls[0] == ("旧标记目标", "旧标记内容", True)
    and rec_calls[1] == ("工具目标", "工具内容", False), rec_calls)
chk("T10b 源码级：两者都直接调 _sendgrp_resolve_and_send；ambiguous_deny 已移除",
    SRC_A.count("def _sendgrp_resolve_and_send") == 1
    and "_sendgrp_resolve_and_send(daemon, msg, target, text)\n" in SRC_A
    and "_sendgrp_resolve_and_send(daemon, msg, target, text, notify=False)" in SRC_A
    and "ambiguous_deny" not in SRC_A,
    "")

WL10 = write_wl("wl_t10.json", {"enabled": True,
                                "groups": ["555001", "555002"],
                                "allow_all": False})
with patched(A, GROUP_WL_PATH=WL10, owner_qq=lambda: OWNER_QQ):
    dm10a = GrpDaemon(names={"555001": "测试群", "555002": "测试二群"})
    A._sendgrp_execute(dm10a, m2, "测试", "hi")

    dm10b = GrpDaemon(names={"555001": "冒烟群"})
    A._sendgrp_execute(dm10b, m_non, "冒烟群", "hi")

    dm10c = GrpDaemon(names={})
    A._sendgrp_execute(dm10c, m2, "不存在的群", "hi")

    dm10d = GrpDaemon(names={"555001": "冒烟群"})
    A._sendgrp_execute(dm10d, m2, "冒烟群", "第一句\n第二句\n第三句")

    dm10e = GrpDaemon(names={"555001": "冒烟群"}, fail=True)
    A._sendgrp_execute(dm10e, m2, "冒烟群", "hi")

chk("T10c 旧路径对上多个群也一律拒发（用户定的：不再挑第一个）",
    dm10a.group_sends == [] and dm10a.chat_sends
    and "叫这个名字的群有多个" in "".join(dm10a.chat_sends[-1][2])
    and any("AGENT_SENDGRP_AMBIG" in l for l in ALOG),
    (dm10a.group_sends, dm10a.chat_sends))
chk("T10d 旧路径主人判定不变：非主人只记日志、什么都不发",
    dm10b.group_sends == [] and dm10b.chat_sends == []
    and any("AGENT_SENDGRP_DENY" in l for l in ALOG),
    (dm10b.group_sends, dm10b.chat_sends))
chk("T10e 旧路径找不到群：发现成提示回当前会话、不发群",
    dm10c.group_sends == [] and dm10c.chat_sends
    and "没找到这个群（或它不在我的群白名单里）" in "".join(dm10c.chat_sends[-1][2])
    and any("AGENT_SENDGRP_BAD_TARGET" in l for l in ALOG),
    (dm10c.group_sends, dm10c.chat_sends))
chk("T10f 旧路径发成功：群发送带换行拆句、不打扰当前会话",
    dm10d.group_sends == [("group", "555001", ["第一句", "第二句"])]
    and dm10d.chat_sends == []
    and any("AGENT_SENDGRP_OK" in l for l in ALOG),
    (dm10d.group_sends, dm10d.chat_sends))
chk("T10g 旧路径发送失败：失败文案回当前会话",
    len(dm10e.group_sends) == 1 and dm10e.chat_sends
    and "发到群里失败：模拟发送失败" in "".join(dm10e.chat_sends[-1][2])
    and any("AGENT_SENDGRP_FAIL" in l for l in ALOG),
    (dm10e.group_sends, dm10e.chat_sends))


# ============ T-C 定时时间补零（bug 修复：存成 8:30 的任务从前永不触发） ============

class _FakeTime(object):
    """_cron_tick 的时间桩：固定「今天」与时分。"""

    def __init__(self, hh, mm):
        self._hh, self._mm = hh, mm

    def strftime(self, fmt):
        if fmt == "%Y-%m-%d %H:%M":
            return "2026-11-27 %02d:%02d" % (self._hh, self._mm)
        if fmt == "%Y-%m-%d":
            return "2026-11-27"
        return ""

    def localtime(self):
        return types.SimpleNamespace(tm_hour=self._hh, tm_min=self._mm)


def cron_tick_case(tasks, hh, mm, expect_fire):
    """把时间/开关/存取全部打桩跑一遍 _cron_tick，等线程把执行体桩喂满，
    返回 (执行体桩收到的 fake 列表, 存入桩收到的表列表, tick 异常文本)。"""
    run_rec, saved_rec = [], []
    with patched(A, time=_FakeTime(hh, mm),
                 feature_enabled=lambda name: True,
                 _cron_tasks=lambda: [dict(x) for x in tasks],
                 _cron_run_task=lambda daemon, fake: run_rec.append(fake),
                 _cron_save=lambda ts: saved_rec.append(list(ts))):
        tick_err = ""
        try:
            A._cron_tick(dm2)
        except Exception as exc:
            tick_err = repr(exc)
    deadline = time.time() + 2
    while len(run_rec) < expect_fire and time.time() < deadline:
        time.sleep(0.05)
    return run_rec, saved_rec, tick_err


TASK_TC = {"id": 1, "time": "8:30", "text": "早报",
           "chat_type": "private", "chat_id": OWNER_QQ}
rc1, sv1, err1 = cron_tick_case([TASK_TC], 8, 30, 1)
chk("TC1 存成 8:30 的任务在 08:30 触发（补零比较修复）",
    err1 == "" and len(rc1) == 1 and rc1[0].get("text") == "早报"
    and any("AGENT_CRON_FIRE" in l for l in ALOG) and len(sv1) == 1,
    (err1, rc1, [len(x) for x in sv1]))
rc2, sv2, err2 = cron_tick_case([dict(TASK_TC, id=2, time="08:30")], 8, 30, 1)
chk("TC2 已补零的 08:30 照常触发", err2 == "" and len(rc2) == 1, (err2, rc2))
rc3, sv3, err3 = cron_tick_case(
    [dict(TASK_TC, id=3, time="abc"), dict(TASK_TC, id=4, time=None)],
    8, 30, 0)
chk("TC3 脏时间值（abc/None）不抛异常、不触发、不回写",
    err3 == "" and rc3 == [] and sv3 == [], (err3, rc3, sv3))

save5 = []
with patched(A, owner_qq=lambda: OWNER_QQ,
             _cron_tasks=lambda: [],
             _cron_save=lambda ts: save5.append(list(ts))):
    hit5a, rep5a = A.owner_command({"text": "定时 9:0 提醒我喝水",
                                    "sender_id": OWNER_QQ}, daemon=None)
    saved_after_9_0 = len(save5)
    hit5b, rep5b = A.owner_command({"text": "定时 9:05 提醒我喝水",
                                    "sender_id": OWNER_QQ}, daemon=None)
chk("TC4 旧命令「定时 9:0 …」不匹配、不保存（分钟要两位，与从前一致）",
    hit5a is False and saved_after_9_0 == 0, (hit5a, rep5a, saved_after_9_0))
chk("TC5 旧命令「定时 9:05 …」保存为补零 09:05，回复也显示 09:05",
    hit5b is True and save5 and save5[0][0]["time"] == "09:05"
    and "09:05" in rep5b, (rep5b, save5))
with patched(A, _cron_tasks=lambda: save5[0]):
    hit6, rep6 = A.owner_command({"text": "定时列表"}, daemon=None)
chk("TC6 定时列表显示补零时间 09:05",
    hit6 is True and "09:05" in rep6, rep6)


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
