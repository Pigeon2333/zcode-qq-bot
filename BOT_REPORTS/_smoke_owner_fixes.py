# -*- coding: utf-8 -*-
"""离线冒烟：C5 三件小修的钉子（Z-20 裸「安静」、Z-21 日志 0、OWNER_HELP 补定时）。

Z1 手打裸「安静」命中 owner_command、默认 10 分钟（Z-20 之前正则
   分钟? 的量词只管「钟」，裸「安静」不命中）；带分钟数两种写法照常；
   非数字参数仍不命中；daemon=None 时仍不命中（原守卫不变）。
Z2 _tail_log_lines 条数不大于 0 时按默认 12 取（Z-21 之前 [-0:] 会把
   整个文件都取回来）；正数照常、空行滤掉、超长行截 150、超顶取全部。
H1 OWNER_HELP 在「开启/关闭」行后补了定时三条；H2 开关关时「帮助」回复
   逐字等于 OWNER_HELP（无附加句）；H3 开关开时末尾加
   「（这些用平常说话的说法也行）」；H4 关回去又逐字一致。

隔离：import 被测模块前把 QQBOT_DATA_DIR 指到一次性临时目录（照
_smoke_commands.py 的做法）；_ui_api/_tail_log_lines 之外的执行体在本文件
用不到，_ui_api、daemon_status_local、_cron_*、_zcode_handoff 全打桩防误触；
改 reply_config 用真实文件（隔离目录内）验证 _owner_intent_on 三态。
Z2 的日志文件是临时隔离目录里自造的，不读生产日志。跑完删临时目录。
用法：python BOT_REPORTS/_smoke_owner_fixes.py
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
_SMOKE_OWN_DATA_DIR = tempfile.mkdtemp(prefix="qqbot_owner_fixes_iso_")
os.environ["QQBOT_DATA_DIR"] = _SMOKE_OWN_DATA_DIR

import qq_bot_agent as A  # noqa: E402

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

QUIET_OK = "好，安静 %d 分钟～期间群里不主动说话，有人@或回复我照常回答"


class QuietDaemon(object):
    """只记 set_quiet 的 daemon 桩。"""

    def __init__(self):
        self.quiet_calls = []

    def set_quiet(self, minutes):
        self.quiet_calls.append(minutes)


def oc(text, daemon):
    return A.owner_command({"text": text}, daemon)


# ---- Z1 裸「安静」（Z-20） ----

d = QuietDaemon()
hit, reply = oc("安静", d)
chk("Z1a 裸「安静」命中：默认 10 分钟、回复与省略写法一致",
    hit is True and d.quiet_calls == [10]
    and reply == QUIET_OK % 10, (hit, d.quiet_calls, reply))

d2 = QuietDaemon()
hit2, reply2 = oc("安静 30 分钟", d2)
chk("Z1b 「安静 30 分钟」照常：30 分钟", hit2 is True and d2.quiet_calls == [30]
    and reply2 == QUIET_OK % 30, (hit2, d2.quiet_calls, reply2))

d3 = QuietDaemon()
hit3, reply3 = oc("安静30", d3)
chk("Z1c 「安静30」（不带分/钟）照常：30 分钟",
    hit3 is True and d3.quiet_calls == [30], (hit3, d3.quiet_calls, reply3))

d4 = QuietDaemon()
hit4, _ = oc("安静 abc 分钟", d4)
chk("Z1d 「安静 abc 分钟」仍不命中", hit4 is False and d4.quiet_calls == [],
    (hit4, d4.quiet_calls))

d5 = QuietDaemon()
hit5, _ = oc("安静", None)
chk("Z1e daemon=None 时「安静」不命中（原守卫不变）",
    hit5 is False and d5.quiet_calls == [], (hit5,))

# ---- Z2 _tail_log_lines 条数兜底（Z-21） ----

logs = pathlib.Path(A.C.logs_dir())
logs.mkdir(parents=True, exist_ok=True)
# 20 行：L2 空行（滤空用）、L7 超长行（截断用）。空行放头部，让它不落在
# 「最后 12 行原始行」的窗口里，Z2b 的条数才好对账（函数是先取尾 n 行原始
# 行、再滤空行——这是既有语义，本单不动）。
lines = ["L%d" % i for i in range(1, 21)]
lines[6] = "L7 " + "x" * 180
lines[1] = ""
(logs / "agent.log").write_text("\n".join(lines) + "\n", encoding="utf-8")

got3 = A._tail_log_lines("agent.log", 3)
chk("Z2a n=3 取最后 3 行", len(got3) == 3 and got3 == ["L18", "L19", "L20"],
    got3)

got0 = A._tail_log_lines("agent.log", 0)
got_neg = A._tail_log_lines("agent.log", -5)
chk("Z2b n=0 与 n=-5 都按默认 12 取（不再拿到整个文件）",
    len(got0) == 12 and got0[0] == "L9" and got0[-1] == "L20"
    and got_neg == got0, (len(got0), got0[:1], got_neg == got0))

got_all = A._tail_log_lines("agent.log", 200)
chk("Z2c n 超过行数：取全部非空行（19 条，空行 L2 滤掉）",
    len(got_all) == 19 and "" not in got_all and "L2" not in got_all,
    len(got_all))

long_line = [x for x in got_all if x.startswith("L7 ")][0]
chk("Z2d 超长行截到 150 字符", len(long_line) == 150, len(long_line))

# ---- H1/H2/H3/H4 帮助清单 ----

help_reply_off = A.owner_command({"text": "帮助"}, None)[1]
hl = A.OWNER_HELP.split("\n")
i_toggle = next(i for i, x in enumerate(hl) if x.startswith("开启/关闭"))
chk("H1 「开启/关闭」行后紧跟定时三条（风格、分隔符同上下文）",
    hl[i_toggle + 1] == "定时 HH:MM 要做的事（每天到点执行，结果私聊发你）"
    "| 定时列表 | 删定时 编号", hl[i_toggle + 1])

RCPATH = A.REPLY_CONFIG_PATH
_RCPAD = [0]


def write_reply_cfg(obj):
    _RCPAD[0] += 1    # 追加不同数量换行：保证 mtime+size 签名必变
    pathlib.Path(RCPATH).write_text(json.dumps(obj, ensure_ascii=False)
                                    + ("\n" * _RCPAD[0]), encoding="utf-8")


def rm_reply_cfg():
    try:
        os.remove(RCPATH)
    except OSError:
        pass


chk("H2 开关关（默认）：「帮助」回复逐字等于 OWNER_HELP、无附加句",
    help_reply_off == A.OWNER_HELP
    and "（这些用平常说话" not in help_reply_off, "")

write_reply_cfg({"owner_intent_enabled": True})
help_reply_on = A.owner_command({"text": "帮助"}, None)[1]
chk("H3 开关开：末尾多一行「（这些用平常说话的说法也行）」",
    help_reply_on == A.OWNER_HELP + "\n（这些用平常说话的说法也行）",
    help_reply_on[-40:])

rm_reply_cfg()
help_reply_back = A.owner_command({"text": "帮助"}, None)[1]
chk("H4 关回去：又逐字等于 OWNER_HELP",
    help_reply_back == A.OWNER_HELP, "")

print("\n%d 项通过，%d 项失败" % (n_pass, len(fails))
      + (": " + "; ".join(fails) if fails else "（全部通过）"))

# 一次性临时数据目录：先关日志句柄（Windows 下打开的文件删不掉），再尽量删掉
try:
    import logging
    logging.shutdown()
    shutil.rmtree(_SMOKE_OWN_DATA_DIR, ignore_errors=True)
except Exception:
    pass
sys.exit(1 if fails else 0)
