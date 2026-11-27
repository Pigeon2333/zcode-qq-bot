# -*- coding: utf-8 -*-
"""冒烟：发到群按群号匹配（2026-11-27 用户定：群号唯一，不再猜）——
数字目标只当群号精确对、群名先完整相等再包含、对上多个两条路径一律拒发
（对应验收 G1–G7；旧行为「模糊对上多个挑第一个发」到此为止）。

  G1  数字目标只按群号精确对：即使某个群名里含有这串数字也不命中；
      在白名单内 → 命中该群号；allow_all 时以 group_names 的键为准。
  G2  数字目标不在白名单（未开 allow_all）→ 找不到，不发送。
  G3  完整群名恰好一个 → 命中（工具路径）；模糊包含恰好一个 → 命中（旧路径）。
  G4  模糊对上多个 → 两条路径都不发送；提示列出全部 群名(群号)；旧路径提示
      发到当前会话，工具路径当前会话 0 条（只在工具结果里）。
  G5  两个群完整群名相同 → 仍算歧义，不发送。
  G6  教学文案含「[发到群: 群号|」「优先写群号」「标记里写清单里对应的群号」，
      旧写法「[发到群: 群号或群名|」不再出现；「你在这些群」清单每项带
      括号里的群号（假 daemon 两个群）。
  G7  钉着 [发到群] 的既有测试（_smoke_arch / _smoke_batch1 /
      _smoke_toolloop_tools）由主会话复跑核对；本文件不重复跑它们。

隔离：import 被测模块前把 QQBOT_DATA_DIR 指到一次性临时目录（照
_smoke_toolloop_tools.py 的做法）；群白名单指到临时文件；发送全部打桩，
零真实发送。跑完删临时目录。
用法：python BOT_REPORTS/_smoke_sendgrp_gid.py
"""
import contextlib
import json
import os
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join("plugins", "qq-bot", "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 数据目录隔离（必须在 import 被测模块之前生效）
_SMOKE_OWN_DATA_DIR = tempfile.mkdtemp(prefix="qqbot_sendgrp_gid_iso_")
os.environ["QQBOT_DATA_DIR"] = _SMOKE_OWN_DATA_DIR

import qq_daemon as D  # noqa: E402,F401
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
ISO_DIR = pathlib.Path(A.DATA_DIR)


def write_wl(name, obj):
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
    """群发送记录桩：group 发送记账；不提供 status/history（回读不在本单）。"""

    def __init__(self, names=None):
        self._names = dict(names or {})
        self.group_sends = []
        self.chat_sends = []

    def group_names(self):
        return dict(self._names)

    def send(self, chat_type, chat_id, texts, reply_message_id=None):
        if chat_type == "group":
            self.group_sends.append((chat_type, chat_id, list(texts)))
        else:
            self.chat_sends.append((chat_type, chat_id, list(texts)))
        return {"ok": True}


def ctx_of(dm, m):
    return {"daemon": dm, "msg": m, "is_owner": True}


M2 = owner_msg()

TB.register_builtin(lambda q: ("x（example.com）", "bing"),
                    send_group_fn=A._sendgrp_tool_send,
                    draw_fn=A._draw_tool,
                    cron_add_fn=A._cron_add_tool,
                    cron_list_fn=A._cron_list_tool)
SG = T.get_tool("send_group")
chk("G0 send_group 已登记且 target 参数说明是「群号（优先）或完整群名」",
    SG is not None and SG["params"][0]["desc"] == "群号（优先）或完整群名",
    SG and SG["params"][0]["desc"])

# ============ G1 数字目标只按群号精确对 ============

WL1 = write_wl("wl_g1.json",
               {"enabled": True, "groups": ["555001"], "allow_all": False})
with patched(A, GROUP_WL_PATH=WL1, owner_qq=lambda: OWNER_QQ):
    dm1a = GrpDaemon(names={"555001": "群123"})
    r1a = T.run_tool(SG, {"target": "123", "text": "hi"},
                     ctx_of(dm1a, M2), {})
    dm1b = GrpDaemon(names={"555001": "群123"})
    r1b = T.run_tool(SG, {"target": "555001", "text": "hi"},
                     ctx_of(dm1b, M2), {})
chk("G1a 数字目标即使群名里含这串数字也不命中（不拿数字匹配群名）",
    dm1a.group_sends == [] and r1a["ok"] is False
    and "没找到这个群" in r1a["summary"], (r1a["ok"], r1a["summary"]))
chk("G1b 数字目标在白名单内 → 命中该群号",
    dm1b.group_sends == [("group", "555001", ["hi"])] and r1b["ok"] is True,
    (dm1b.group_sends, r1b["summary"]))

WL1B = write_wl("wl_g1b.json", {"enabled": True, "groups": [],
                                "allow_all": True})
with patched(A, GROUP_WL_PATH=WL1B, owner_qq=lambda: OWNER_QQ):
    dm1c = GrpDaemon(names={"555001": "群123"})
    r1c = T.run_tool(SG, {"target": "555001", "text": "hi"},
                     ctx_of(dm1c, M2), {})
    dm1d = GrpDaemon(names={"555001": "群123"})
    r1d = T.run_tool(SG, {"target": "123", "text": "hi"},
                     ctx_of(dm1d, M2), {})
chk("G1c allow_all 时数字按 group_names 的键精确对：是群号 → 命中",
    dm1c.group_sends == [("group", "555001", ["hi"])] and r1c["ok"] is True,
    (dm1c.group_sends, r1c["summary"]))
chk("G1d allow_all 时数字不是任何群号 → 找不到（不匹配群名）",
    dm1d.group_sends == [] and r1d["ok"] is False, r1d["summary"])

# ============ G2 数字不在白名单 ============

with patched(A, GROUP_WL_PATH=WL1, owner_qq=lambda: OWNER_QQ):
    dm2a = GrpDaemon(names={"999999": "别家的群"})
    r2a = T.run_tool(SG, {"target": "888888", "text": "hi"},
                     ctx_of(dm2a, M2), {})
    dm2b = GrpDaemon(names={})
    A._sendgrp_execute(dm2b, M2, "777777", "hi")
chk("G2 数字目标不在白名单：工具路径不发送",
    dm2a.group_sends == [] and r2a["ok"] is False, (r2a["ok"], r2a["summary"]))
chk("G2b 旧路径同样不发送（哪怕对方群里真有这个号的群）",
    dm2b.group_sends == [] and "没找到这个群" in "".join(dm2b.chat_sends[-1][2]),
    (dm2b.group_sends, dm2b.chat_sends))

# ============ G3 完整群名唯一 / 模糊包含唯一 ============

WL3 = write_wl("wl_g3.json",
               {"enabled": True, "groups": ["555001", "555002"],
                "allow_all": False})
with patched(A, GROUP_WL_PATH=WL3, owner_qq=lambda: OWNER_QQ):
    dm3a = GrpDaemon(names={"555001": "冒烟群", "555002": "别的群"})
    r3a = T.run_tool(SG, {"target": " 冒烟群 ", "text": "hi"},
                     ctx_of(dm3a, M2), {})
    dm3b = GrpDaemon(names={"555001": "冒烟主群", "555002": "别的群"})
    r3b = T.run_tool(SG, {"target": "烟主", "text": "hi"},
                     ctx_of(dm3b, M2), {})
chk("G3a 完整群名恰好一个 → 命中（首尾空白不影响）",
    dm3a.group_sends == [("group", "555001", ["hi"])] and r3a["ok"] is True,
    (dm3a.group_sends, r3a["summary"]))
chk("G3b 模糊包含恰好一个 → 命中",
    dm3b.group_sends == [("group", "555001", ["hi"])] and r3b["ok"] is True,
    (dm3b.group_sends, r3b["summary"]))

with patched(A, GROUP_WL_PATH=WL3, owner_qq=lambda: OWNER_QQ):
    dm3c = GrpDaemon(names={"555001": "冒烟主群", "555002": "别的群"})
    A._sendgrp_execute(dm3c, M2, "冒烟主群", "hi")
chk("G3c 旧标记路径完整群名唯一 → 照常发送",
    dm3c.group_sends == [("group", "555001", ["hi"])], dm3c.group_sends)

# ============ G4 模糊对上多个：两条路径都不发 ============

with patched(A, GROUP_WL_PATH=WL3, owner_qq=lambda: OWNER_QQ):
    dm4a = GrpDaemon(names={"555001": "测试群", "555002": "测试二群",
                            "555003": "无关群"})
    r4a = T.run_tool(SG, {"target": "测试", "text": "hi"},
                     ctx_of(dm4a, M2), {})
    dm4b = GrpDaemon(names={"555001": "测试群", "555002": "测试二群",
                            "555003": "无关群"})
    A._sendgrp_execute(dm4b, M2, "测试", "hi")
chk("G4a 工具路径：模糊对上多个 → 不发送",
    dm4a.group_sends == [] and r4a["ok"] is False, (r4a["ok"], r4a["summary"]))
chk("G4b 提示列出全部 群名(群号)，让主人报群号",
    "叫这个名字的群有多个" in r4a["summary"]
    and "测试群(555001)" in r4a["summary"] and "测试二群(555002)" in r4a["summary"]
    and "请告诉我群号" in r4a["summary"], r4a["summary"])
chk("G4c 工具路径当前会话 0 条提示（只在工具结果里）",
    dm4a.chat_sends == [], dm4a.chat_sends)
chk("G4d 旧路径：不发送、提示发到当前会话",
    dm4b.group_sends == [] and dm4b.chat_sends
    and "叫这个名字的群有多个" in "".join(dm4b.chat_sends[-1][2])
    and any("AGENT_SENDGRP_AMBIG" in l for l in ALOG),
    (dm4b.group_sends, dm4b.chat_sends))

# ============ G5 两个群完整群名相同 ============

with patched(A, GROUP_WL_PATH=WL3, owner_qq=lambda: OWNER_QQ):
    dm5a = GrpDaemon(names={"555001": "测试群", "555002": "测试群"})
    r5a = T.run_tool(SG, {"target": "测试群", "text": "hi"},
                     ctx_of(dm5a, M2), {})
    dm5b = GrpDaemon(names={"555001": "测试群", "555002": "测试群"})
    A._sendgrp_execute(dm5b, M2, "测试群", "hi")
chk("G5a 工具路径：两个群完整群名相同 → 歧义不发送",
    dm5a.group_sends == [] and r5a["ok"] is False
    and "测试群(555001)" in r5a["summary"] and "测试群(555002)" in r5a["summary"],
    (r5a["ok"], r5a["summary"]))
chk("G5b 旧路径同样拒发",
    dm5b.group_sends == [] and dm5b.chat_sends
    and "请告诉我群号" in "".join(dm5b.chat_sends[-1][2]),
    (dm5b.group_sends, dm5b.chat_sends))

# ============ G6 教学文案与「你在这些群」清单 ============

SRC_A = pathlib.Path("plugins", "qq-bot", "scripts",
                     "qq_bot_agent.py").read_text(encoding="utf-8")
chk("G6a 教学文案：标记写群号、优先写群号、信息齐了写清单里对应的群号",
    "[发到群: 群号|要发的话]" in SRC_A
    and "优先写群号" in SRC_A and "标记里写清单里对应的群号" in SRC_A,
    "")
chk("G6b 旧写法「[发到群: 群号或群名|」不再出现",
    "[发到群: 群号或群名|" not in SRC_A, "")
chk("G6c 「说某某群这种群名就行」那句保留（描述主人的说法）",
    "这种群名就行，清单里有" in SRC_A, "")
with patched(A, GROUP_WL_PATH=WL3, owner_qq=lambda: OWNER_QQ):
    line6 = A._owner_groups_line(GrpDaemon(names={"555001": "冒烟群",
                                                 "555002": "备用群"}))
chk("G6d 「你在这些群」清单每项带括号里的群号",
    "冒烟群(555001)" in line6 and "备用群(555002)" in line6, line6)

# ============ 收尾与汇总 ============

print("=" * 60)
print("共 %d 项：PASS %d，FAIL %d" % (n_pass + len(fails), n_pass, len(fails)))
if fails:
    print("失败项：")
    for f in fails:
        print("  - %s" % f)
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
