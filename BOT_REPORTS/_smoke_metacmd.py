# -*- coding: utf-8 -*-
"""元指令句剥离+追聊回声扩词冒烟（脚本文件跑，避开引号坑）。

数据目录隔离：import 被测模块前把 QQBOT_DATA_DIR 指到一次性临时目录，不读
生产 ~/.zcode-qq-bot。断言针对的剥离/回声逻辑是纯字符串规则、与配置无关，
隔离只为让 Daemon 构造时的启动读取（ws_token/互聊判定）也落在临时目录，
跑完删临时目录。"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, r"plugins/qq-bot/scripts")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 数据目录隔离（必须在 import 前生效：DATA_DIR 系列模块常量在 import 时定死，
# 事后设环境变量不回填）
_SMOKE_OWN_DATA_DIR = tempfile.mkdtemp(prefix="qqbot_metacmd_iso_")
os.environ["QQBOT_DATA_DIR"] = _SMOKE_OWN_DATA_DIR

import qq_daemon as D  # noqa: E402
import qq_bot_agent as A  # noqa: E402

fails = []


def chk(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (" | " + repr(extra)[:70] if extra else ""))
    if not cond:
        fails.append(name)


d = D.Daemon(ws_url="", token="", cfg={}, ctl_port=0)
S = d._strip_control_marks
leak = "分析过程不外显，仅直接给出最终回复。宝宝别打字啦，先去把午饭吃了嘛～"
chk("1 截图句首元指令剥离", S(leak) == "宝宝别打字啦，先去把午饭吃了嘛～", S(leak))
chk("2 变体：思考过程", S("思考过程不外显，仅直接给出最终回复。在干嘛呢") == "在干嘛呢")
chk("3 整条只有指令→丢弃", S("分析过程不外显，仅直接给出最终回复。") is None)
chk("4 正常话不误杀", S("分析下来这个版本确实强") == "分析下来这个版本确实强")
chk("5 正常口语句不误杀", S("这种情况不要出现第二次哦") == "这种情况不要出现第二次哦")
chk("6 nudge回声命中新词", bool(A._NUDGE_ECHO_RE.search("分析过程不外显")))
chk("7 正常追话不误杀", not A._NUDGE_ECHO_RE.search("午饭吃了吗，别饿着"))
print()
print("失败 %d 项" % len(fails) if fails else "全部通过 ✓")
for _h in list(A._usage_log.handlers):
    _h.close()                     # 放掉 usage_log.jsonl 句柄，Windows 才删得净
    A._usage_log.removeHandler(_h)
shutil.rmtree(_SMOKE_OWN_DATA_DIR, ignore_errors=True)
if os.path.isdir(_SMOKE_OWN_DATA_DIR):
    print("注意：隔离目录没能删净 %s" % _SMOKE_OWN_DATA_DIR)
else:
    print("隔离目录已删净（os.path.isdir=False）：%s" % _SMOKE_OWN_DATA_DIR)
sys.exit(1 if fails else 0)
