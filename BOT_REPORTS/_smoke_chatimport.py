# -*- coding: utf-8 -*-
"""冒烟：qq_chatimport.store 的 source 白名单校验与落盘越界保险。
离线跑：qq_common 以桩模块注入 sys.modules（DATA_DIR→系统临时目录），
不读不写生产数据目录 ~/.zcode-qq-bot，不连 ws、不起服务。"""
import json
import os
import pathlib
import sys
import tempfile
import types

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TMP = tempfile.mkdtemp(prefix="smoke_chatimport_")
_stub = types.ModuleType("qq_common")
_stub.DATA_DIR = TMP                      # 打桩：BASE_DIR 在 import 时读它
sys.modules["qq_common"] = _stub
sys.path.insert(0, os.path.join(ROOT, "plugins", "qq-bot", "scripts"))
import qq_chatimport as CI  # noqa: E402

fails = []
_total = [0]


def chk(name, cond):
    _total[0] += 1
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def snap():
    return sorted(os.listdir(TMP))


def expect_reject(name, source, session="会话一"):
    """非法 source：必须抛 ValueError，且临时目录里不出现任何新目录/文件。"""
    before = snap()
    raised = None
    try:
        CI.store(source, session, str(EXPORT))
    except ValueError as e:
        raised = e
    except Exception as e:  # noqa: BLE001
        raised = e
    chk(name + " 抛异常", raised is not None)
    chk(name + " 抛的是 ValueError 实得 %s" % type(raised).__name__,
        isinstance(raised, ValueError))
    chk(name + " 未产生任何新目录/文件", snap() == before)


# 造一份合法导出文件（qq-cli md 格式：**时间** 发送人: 内容），放系统临时目录
EXPORT = pathlib.Path(TMP) / "export.md"
EXPORT.write_text("**2026-01-02 15:30:12** 张三: 你好世界\n"
                  "**2026-01-02 15:31:00** 李四: 收到\n", encoding="utf-8")
BASE = pathlib.Path(CI.BASE_DIR)
ABS_ESC = str(pathlib.Path(TMP) / "abs_escape")   # 绝对路径，BASE_DIR 之外

# --- 场景 1：source 合法（"qq"）→ 照常写入 BASE_DIR/qq/<会话>/ ---
r = CI.store("qq", "会话一", str(EXPORT))
chk("source=qq 合法返回 ok", r.get("ok") is True)
chk("source=qq 返回 session 原样", r.get("session") == "会话一")
mj = BASE / "qq" / "会话一" / "messages.json"
mt = BASE / "qq" / "会话一" / "meta.json"
chk("messages.json 落在 BASE_DIR/qq/会话一/", mj.is_file())
chk("meta.json 落在 BASE_DIR/qq/会话一/", mt.is_file())
msgs = json.loads(mj.read_text(encoding="utf-8"))
meta = json.loads(mt.read_text(encoding="utf-8"))
chk("messages.json 内容=解析出的 2 条（张三/李四）",
    isinstance(msgs, list) and len(msgs) == 2
    and msgs[0]["sender"] == "张三" and msgs[1]["sender"] == "李四")
chk("meta.json 结构与改动前一致(count/range/senders/source_file, count=2)",
    all(k in meta for k in ("count", "range", "senders", "source_file"))
    and meta.get("count") == 2
    and meta.get("source_file") == "export.md")

# --- 场景 2：source 合法（"wx"）→ 照常写入 BASE_DIR/wx/<会话>/ ---
r2 = CI.store("wx", "群A", str(EXPORT))
chk("source=wx 合法返回 ok", r2.get("ok") is True)
chk("两个 json 都在 BASE_DIR/wx/群A/ 下",
    (BASE / "wx" / "群A" / "messages.json").is_file()
    and (BASE / "wx" / "群A" / "meta.json").is_file())

# --- 场景 3-6：非法 source → ValueError，目录零新增 ---
expect_reject("source='../escape'", "../escape")
expect_reject("source=绝对路径", ABS_ESC)
expect_reject("source='qq/../../escape'", "qq/../../escape")
expect_reject("source='foobar'", "foobar")

# --- 场景 7：session 含 ".." → 仍被 _safe_name 清洗挡住（回归）---
r3 = CI.store("qq", "../evil", str(EXPORT))
chk("session='../evil' 仍 ok（清洗不拒绝）", r3.get("ok") is True)
chk("session 清洗成 'evil'", r3.get("session") == "evil")
chk("落盘在 BASE_DIR/qq/evil/ 下", (BASE / "qq" / "evil" / "messages.json").is_file())
chk("TMP 顶层没有逃逸出的 evil 目录", not (pathlib.Path(TMP) / "evil").exists())

# --- 保险二（设计第 2 条）：白名单被改坏时 resolve 越界检查兜底 ---
_orig = CI.SOURCES
try:
    CI.SOURCES = dict(_orig)
    CI.SOURCES["../escape"] = _orig["qq"]     # 人为放进一个越界 source
    before = snap()
    raised = None
    try:
        CI.store("../escape", "会话一", str(EXPORT))
    except ValueError as e:
        raised = e
    chk("越界保险：路径解析后越出 BASE_DIR 即拒", isinstance(raised, ValueError))
    chk("越界保险：未产生任何新目录/文件", snap() == before)
finally:
    CI.SOURCES = _orig                        # 原样还原

print()
print("%d 项通过，%d 项失败" % (_total[0] - len(fails), len(fails)))
import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)   # 只删本次 mkdtemp 精确路径
sys.exit(1 if fails else 0)
