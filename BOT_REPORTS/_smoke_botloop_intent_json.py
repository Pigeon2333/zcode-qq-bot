# -*- coding: utf-8 -*-
"""离线冒烟：互聊改判判定调用走统一意图底座 _intent_json（C4）之后的
解析行为。_smoke_botloop_intent.py 的桩打在 call_glm_with_fallback 这一层，
_intent_json 调的是同一个模块级名字，桩照样生效；本文件补钉它没覆盖的
解析形态（全部单元级直调 _intent_botloop_verdict，不进 process）：

V1 模型输出带 ```json 围栏 → _intent_json 三步解析的第②步剥掉围栏，改判照常。
V2 输出前后带解释文字 → 第③步取首个 { 到最后一个 }，改判照常。
V3 输出是嵌套 JSON（verdict 外再挂一层对象）→ 旧写法取首个花括号块
   \\{[^{}]*\\} 会取到内层、丢掉 verdict；只有 _intent_json 的三步解析
   能取到外层。本条钉住「改判路径真实执行到的解析代码就是 _intent_json」。
V4 模型抛异常 → 恰好一条 AGENT_BOTLOOP_INTENT_FAIL、返回 None
   （与 _smoke_botloop_intent.py P9 的既有约定一致：异常被 _intent_json
   收成 None 后，走原失败路径统一记 FAIL 日志）。
V5 输出根本不是 JSON → 返回 None，同一条 FAIL 路径。
V6 开关 botloop_verdict_intent 默认仍关（直调不受开关影响，这里只钉默认值）。

不连 ws、不连 39763/39764/39765、不启停任何进程、不发任何消息、不调真实
模型（全部打桩）；QQBOT_DATA_DIR 在 import 被测模块之前指到一次性临时
目录，绝不读写生产数据 ~/.zcode-qq-bot。
用法：python BOT_REPORTS/_smoke_botloop_intent_json.py
"""
import contextlib
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join("plugins", "qq-bot", "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 数据目录隔离（必须在 import 被测模块之前生效）
_SMOKE_OWN_DATA_DIR = tempfile.mkdtemp(prefix="qqbot_botloop_intent_json_iso_")
os.environ["QQBOT_DATA_DIR"] = _SMOKE_OWN_DATA_DIR

import qq_bot_agent as A  # noqa: E402

fails = []
n_pass = 0


def chk(name, cond, extra=""):
    global n_pass
    print(("PASS " if cond else "FAIL ") + name
          + (" | " + str(extra)[:110] if extra else ""))
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
A.log_usage = lambda *x, **k: None
A._log_context_dump = lambda *x, **k: None


def alog_of(tag):
    return [l for l in ALOG if tag in l]


def glm_script(resps, calls):
    """按调用次序回放的 glm 桩；str→(模型名, 文本)；Exception→原样抛出。
    calls 记 (messages, route)，route 一并对账。"""
    def _glm(messages, text_ctx="", budget_sec=200, **kwargs):
        calls.append((messages, kwargs.get("route")))
        r = resps[min(len(calls) - 1, len(resps) - 1)]
        if isinstance(r, BaseException):
            raise r
        return ("m", r) if isinstance(r, str) else r
    return _glm


def register_one():
    """清空待改判表后登记一条「小张/777」。"""
    A._BOTLOOP_QUIET_PENDING.clear()
    A._botloop_quiet_register({"chat_type": "group", "chat_id": "555001",
                               "sender_id": "777", "sender_name": "小张",
                               "text": "x"}, 15)


def verdict_case(text, resp, calls):
    """登记一条 → 直调 _intent_botloop_verdict，返回 (结果, ALOG 快照长度)。"""
    register_one()
    cands = A._botloop_quiet_candidates()
    ALOG.clear()
    with patched(A, call_glm_with_fallback=glm_script(resp, calls)):
        r = A._intent_botloop_verdict(text, cands)
    return r


# ===== V1 ```json 围栏 =====
_calls1 = []
_r1 = verdict_case(
    "对小张是机器人",
    ['```json\n{"verdict": "bot", "target": "小张", "confidence": 0.93}\n```'],
    _calls1)
chk("V1 带 ```json 围栏的输出照常改判（_intent_json 第②步剥围栏）",
    isinstance(_r1, tuple) and _r1[0] == "bot"
    and _r1[1].get("sender_id") == "777"
    and len(_calls1) == 1 and _calls1[0][1] == "judge"
    and str(_calls1[0][0][0]["content"]).startswith("主人在私聊里对机器人说"),
    (_r1, _calls1 and (_calls1[0][1],)))

# ===== V2 前后带解释文字 =====
_calls2 = []
_r2 = verdict_case(
    "小张其实是真人",
    ['好的，判断结果如下：{"verdict": "human", "target": "小张", '
     '"confidence": 0.9} 以上。'],
    _calls2)
chk("V2 前后带解释文字照常改判（第③步取 {…} 子串）",
    isinstance(_r2, tuple) and _r2[0] == "human"
    and _r2[1].get("sender_id") == "777"
    and len(_calls2) == 1 and _calls2[0][1] == "judge",
    (_r2, _calls2 and (_calls2[0][1],)))

# ===== V3 嵌套 JSON：旧取首块写法拿不到，_intent_json 拿得到 =====
_calls3 = []
_r3 = verdict_case(
    "对小张是机器人",
    ['{"verdict": "bot", "target": "小张", "confidence": 0.95, '
     '"note": {"detail": "ok"}}'],
    _calls3)
chk("V3 嵌套 JSON 也改判（证明执行的是 _intent_json 三步解析，"
    "不是旧的取首个花括号块）",
    isinstance(_r3, tuple) and _r3[0] == "bot"
    and _r3[1].get("sender_id") == "777"
    and len(_calls3) == 1 and _calls3[0][1] == "judge",
    (_r3,))

# ===== V4 模型抛异常：一条 FAIL、返回 None =====
_calls4 = []
_r4 = verdict_case("小张其实是真人", [RuntimeError("模型炸了")], _calls4)
chk("V4 模型抛异常：None、恰好一条 AGENT_BOTLOOP_INTENT_FAIL、一次调用",
    _r4 is None and len(alog_of("AGENT_BOTLOOP_INTENT_FAIL")) == 1
    and len(_calls4) == 1 and _calls4[0][1] == "judge",
    (_r4, alog_of("AGENT_BOTLOOP_INTENT_FAIL")))

# ===== V5 输出不是 JSON：同一条 FAIL 路径 =====
_calls5 = []
_r5 = verdict_case("小张其实是真人", ["抱歉，我做不到。"], _calls5)
chk("V5 输出不是 JSON：None、恰好一条 AGENT_BOTLOOP_INTENT_FAIL",
    _r5 is None and len(alog_of("AGENT_BOTLOOP_INTENT_FAIL")) == 1
    and len(_calls5) == 1,
    (_r5, alog_of("AGENT_BOTLOOP_INTENT_FAIL")))

# ===== V6 开关默认关 =====
chk("V6 开关 botloop_verdict_intent 默认关", A._botloop_verdict_intent_on() is False)

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
