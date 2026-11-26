# -*- coding: utf-8 -*-
"""识图后台探测判定（2026-11-26 改版）冒烟：离线、不联网、不写生产数据目录。

覆盖：128×128 纯红探测图与提问、max_tokens=64、「答不出红色＝纯文本」三步判定、
出错诊断尾巴 (状态码,原因)/+think/(exc)、冷却中的模型跳过、日志行格式、
_caption_call 跳过已判定的纯文本模型。

用法：python BOT_REPORTS/_smoke_visionprobe.py
"""
import base64
import contextlib
import os
import shutil
import struct
import sys
import tempfile
import time

sys.path.insert(0, os.path.join("plugins", "qq-bot", "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 数据目录隔离（必须在 import 被测模块之前生效）：import qq_bot_agent 就会打开
# usage_log.jsonl 句柄——默认把 QQBOT_DATA_DIR 指到一次性临时目录，绝不读写生产
# 数据 ~/.zcode-qq-bot。外部已显式设置 QQBOT_DATA_DIR 时尊重原值。
_SMOKE_OWN_DATA_DIR = None
if not os.environ.get("QQBOT_DATA_DIR"):
    _SMOKE_OWN_DATA_DIR = tempfile.mkdtemp(prefix="qqbot_vp_smoke_iso_")
    os.environ["QQBOT_DATA_DIR"] = _SMOKE_OWN_DATA_DIR

import qq_bot_agent as A  # noqa: E402

fails = []


def chk(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name
          + (" | " + str(extra)[:130] if extra else ""))
    if not cond:
        fails.append(name)


@contextlib.contextmanager
def patched(obj, **names):
    """临时替换模块属性，退出时恢复（仿 _smoke_arch.py 的做法）。"""
    old = {k: getattr(obj, k) for k in names}
    for k, v in names.items():
        setattr(obj, k, v)
    try:
        yield old
    finally:
        for k, v in old.items():
            setattr(obj, k, v)


LOG = []


def _no_net(*a, **k):
    raise AssertionError("离线冒烟不允许调用模型（未打桩的联网路径）")


def _probe0():
    return {"cls": {}, "sig": "", "ts": 0.0, "running": False}


class _RTs(object):
    """假 RT：只带探测用到的 cooldowns。"""

    def __init__(self, cool=None):
        self.cooldowns = cool or {}


def _glm_by_model(preset, calls):
    """按模型名返回预设结果的 glm_chat 桩：值是 (status, data)；放异常就原样抛。"""

    def _glm(model, messages, timeout=40, extra_body=None, key=None):
        calls.append({"model": model, "timeout": timeout,
                      "extra_body": dict(extra_body or {}), "key": key,
                      "msgs": messages})
        item = preset[model]
        if isinstance(item, Exception):
            raise item
        return item

    return _glm


def _probe_case(models, preset, chain, cool=None, glm=None):
    """隔离状态下（临时 RT / _VISION_PROBE / 两个模型表、打桩链与 glm_chat）
    跑一遍 _vision_probe，把探到的结论、模型表和新增日志行带出来。"""
    calls = []
    if glm is None:
        glm = _glm_by_model(preset, calls)
    rec = {"calls": calls}
    with patched(A, glm_chat=glm, out=lambda line: LOG.append(str(line)),
                 RT=_RTs(cool), _vision_pref=lambda: "",
                 _chain_candidates=lambda: list(chain), econ_on=lambda: False,
                 _NO_VISION_MODELS=set(), _VISION_OK_MODELS=set(),
                 _VISION_PROBE=_probe0()):
        n0 = len(LOG)
        A._vision_probe(models)
        rec["cls"] = dict(A._VISION_PROBE["cls"])
        rec["no_vision"] = set(A._NO_VISION_MODELS)
        rec["ok"] = set(A._VISION_OK_MODELS)
        rec["lines"] = LOG[n0:]
    return rec


MV, MT, ME, M4, M5, MX, MC = ("vp-vision", "vp-text", "vp-empty",
                              "vp-429", "vp-500", "vp-exc", "vp-cool")

with patched(A, glm_chat=_no_net, out=lambda line: LOG.append(str(line)),
             log_usage=lambda *a, **k: None):

    # ---------- S1：400「不支持图片输入」→ 纯文本模型 ----------
    _r1 = _probe_case([MT], {MT: (400, {"error": {"message": "不支持图片输入"}})},
                      [("main", {"key": "M"}, MT, 0, 0)])
    chk("S1 400「不支持图片输入」→ 记 text，进 _NO_VISION_MODELS、不进 _VISION_OK_MODELS",
        _r1["cls"] == {MT: "text"} and MT in _r1["no_vision"] and MT not in _r1["ok"]
        and _r1["lines"] == ["AGENT_VISION_PROBE vision= text=%s error=" % MT],
        (_r1["cls"], _r1["no_vision"], _r1["lines"]))

    # ---------- S2：200 正文「红色」→ 能看图；顺带钉住 glm_chat 的调用参数 ----------
    _r2 = _probe_case([MV], {MV: (200, {"choices": [{"message": {"content": "红色"},
                                                     "finish_reason": "stop"}]})},
                      [("main", {"key": "M"}, MV, 0, 0)])
    chk("S2 200 正文「红色」→ 记 vision，进 _VISION_OK_MODELS",
        _r2["cls"] == {MV: "vision"} and MV in _r2["ok"] and MV not in _r2["no_vision"]
        and _r2["lines"] == ["AGENT_VISION_PROBE vision=%s text= error=" % MV],
        (_r2["cls"], _r2["ok"], _r2["lines"]))
    _c2 = _r2["calls"][0] if _r2["calls"] else {}
    chk("S2' 调 glm_chat：max_tokens=64、thinking disabled、timeout=30、只带一张探测图",
        _c2.get("extra_body") == {"thinking": {"type": "disabled"}, "max_tokens": 64}
        and _c2.get("timeout") == 30 and len(_c2.get("msgs") or []) == 1,
        _c2)

    # ---------- S3：200 正文「我无法查看图片」→ 答不出红色＝纯文本 ----------
    _r3 = _probe_case([MT], {MT: (200, {"choices": [{"message": {"content": "我无法查看图片"},
                                                     "finish_reason": "stop"}]})},
                      [("main", {"key": "M"}, MT, 0, 0)])
    chk("S3 200 正文「我无法查看图片」→ 记 text，进 _NO_VISION_MODELS、不进 _VISION_OK_MODELS",
        _r3["cls"] == {MT: "text"} and MT in _r3["no_vision"] and MT not in _r3["ok"],
        (_r3["cls"], _r3["no_vision"], _r3["ok"]))

    # ---------- S4：200 正文「Red」→ 不分大小写 ----------
    _r4 = _probe_case([MV], {MV: (200, {"choices": [{"message": {"content": "Red"},
                                                     "finish_reason": "stop"}]})},
                      [("main", {"key": "M"}, MV, 0, 0)])
    chk("S4 200 正文「Red」→ 不分大小写，记 vision",
        _r4["cls"] == {MV: "vision"} and MV in _r4["ok"], (_r4["cls"], _r4["ok"]))

    # ---------- S5：200 正文空（思考占满预算）→ error，日志带 (200,len) / (200,len+think) ----------
    _r5a = _probe_case([ME], {ME: (200, {"choices": [{"message": {"content": ""},
                                                      "finish_reason": "length"}]})},
                        [("main", {"key": "M"}, ME, 0, 0)])
    _r5b = _probe_case([ME], {ME: (200, {"choices": [{"message": {
        "content": "", "reasoning_content": "这张图应该是纯色的……"},
        "finish_reason": "length"}]})},
        [("main", {"key": "M"}, ME, 0, 0)])
    chk("S5 200 正文空 → 记 error、不进两个表；日志 (200,len)；reasoning_content 非空 → (200,len+think)",
        _r5a["cls"] == {ME: "error"} and ME not in _r5a["no_vision"] and ME not in _r5a["ok"]
        and _r5a["lines"] == ["AGENT_VISION_PROBE vision= text= error=%s(200,len)" % ME]
        and _r5b["cls"] == {ME: "error"}
        and _r5b["lines"] == ["AGENT_VISION_PROBE vision= text= error=%s(200,len+think)" % ME],
        (_r5a["lines"], _r5b["lines"]))

    # ---------- S6：429 / 500 / 抛异常 → error，日志带状态码或 (exc) ----------
    _r6a = _probe_case([M4], {M4: (429, {"error": {"message": "该模型当前访问量过大，请您稍后再试"}})},
                        [("main", {"key": "M"}, M4, 0, 0)])
    _r6b = _probe_case([M5], {M5: (500, {"error": {"message": "服务器内部错误"}})},
                        [("main", {"key": "M"}, M5, 0, 0)])
    _r6c = _probe_case([MX], {MX: TimeoutError("探测超时")},
                        [("main", {"key": "M"}, MX, 0, 0)])
    chk("S6 429/500/抛超时异常 → 都记 error、都不进纯文本表；日志分别带 (429)、(500)、(exc)",
        _r6a["cls"] == {M4: "error"} and M4 not in _r6a["no_vision"]
        and _r6b["cls"] == {M5: "error"} and _r6c["cls"] == {MX: "error"}
        and _r6a["lines"] == ["AGENT_VISION_PROBE vision= text= error=%s(429)" % M4]
        and _r6b["lines"] == ["AGENT_VISION_PROBE vision= text= error=%s(500)" % M5]
        and _r6c["lines"] == ["AGENT_VISION_PROBE vision= text= error=%s(exc)" % MX],
        (_r6a["lines"], _r6b["lines"], _r6c["lines"]))

    # ---------- S7：冷却中的模型不被调用 ----------
    _r7 = _probe_case([MC], {}, [("main", {"key": "M"}, MC, 0, 0)],
                      cool={"main:%s" % MC: time.time() + 999})
    chk("S7 冷却中的模型不被调用：不撞接口、cls 留空、日志三段都空",
        _r7["cls"] == {} and not _r7["calls"] and MC not in _r7["no_vision"]
        and _r7["lines"] == ["AGENT_VISION_PROBE vision= text= error="],
        (_r7["cls"], _r7["calls"], _r7["lines"]))

    # ---------- S8：日志行格式 vision=… text=… error=模型(状态码,原因),… ----------
    _chain8 = [("main", {"key": "M"}, MV, 0, 0), ("k1", {"key": "A"}, ME, 0, 0),
               ("k2", {"key": "B"}, M4, 0, 0)]
    _preset8 = {MV: (200, {"choices": [{"message": {"content": "红色"},
                                        "finish_reason": "stop"}]}),
                ME: (200, {"choices": [{"message": {"content": ""},
                                        "finish_reason": "length"}]}),
                M4: (429, {"error": {"message": "该模型当前访问量过大，请您稍后再试"}})}
    _r8 = _probe_case([MV, ME, M4], _preset8, _chain8)
    chk("S8 日志行格式：AGENT_VISION_PROBE vision=… text=… error=模型(状态码,原因),…",
        _r8["lines"] == ["AGENT_VISION_PROBE vision=%s text= error=%s(200,len),%s(429)"
                         % (MV, ME, M4)], (_r8["lines"],))

    # ---------- S9：探测图是合法 PNG、宽高 128、提问改成只要颜色名 ----------
    _msgs9 = A._probe_msgs()
    _url9 = _msgs9[0]["content"][0]["image_url"]["url"]
    _png9 = base64.b64decode(_url9.split(",", 1)[1])
    _w9, _h9 = struct.unpack(">II", _png9[16:24])
    chk("S9 探测图是合法 PNG：文件头 8 字节正确、IHDR 宽高都是 128、提问只答颜色名",
        _png9[:8] == b"\x89PNG\r\n\x1a\n" and (_w9, _h9) == (128, 128)
        and _msgs9[0]["content"][1]["text"] == "这张图主要是什么颜色？只回答颜色的名字，不要解释。",
        (_png9[:8], _w9, _h9, _msgs9[0]["content"][1]["text"]))

    # ---------- S10：探测记成 text 后，_caption_call 跳过该模型（其余候选顺序不变） ----------
    _chain10 = [("main", {"key": "M"}, MT, 0, 0), ("k1", {"key": "A"}, MV, 0, 0)]
    _att10, _calls10 = [], []

    def _attempt10(model, kv, msgs, th, mt):
        _att10.append(model)
        return (200, {"choices": [{"message": {"content": "一只橘猫"},
                                   "finish_reason": "stop"}]})

    with A._CAP["lock"]:
        _cap_stat0, _cap_cool0 = dict(A._CAP["stat"]), dict(A._CAP["cool"])
    try:
        with patched(A, glm_chat=_glm_by_model(
                         {MT: (400, {"error": {"message": "不支持图片输入"}})}, _calls10),
                     out=lambda line: LOG.append(str(line)),
                     _vision_pref=lambda: "", _chain_candidates=lambda: list(_chain10),
                     econ_on=lambda: False, _NO_VISION_MODELS=set(),
                     _VISION_OK_MODELS=set(), _VISION_PROBE=_probe0(),
                     _img_to_datauri=lambda u, daemon=None: "data:image/png;base64,AA",
                     vision_caption_model=lambda: "", _vision_attempt=_attempt10):
            A._vision_probe([MT])           # 探测把 MT 记成 text
            _cand10 = A._vision_candidates("")
            _t10 = A._caption_call(object(), "http://example.invalid/pic.jpg")
            _in10 = MT in A._NO_VISION_MODELS
        chk("S10 探测记成 text 后 _caption_call 跳过它：候选表顺序不变、速写只调下一个模型",
            _in10 and _cand10 == _chain10 and _att10 == [MV] and _t10 == "一只橘猫"
            and _calls10 and _calls10[0]["model"] == MT,
            (_in10, _cand10, _att10, _t10))
    finally:
        with A._CAP["lock"]:
            A._CAP["stat"].clear()
            A._CAP["stat"].update(_cap_stat0)
            A._CAP["cool"].clear()
            A._CAP["cool"].update(_cap_cool0)

print("\n%d 项失败" % len(fails) + (": " + "; ".join(fails) if fails else "（全部通过）"))

# 一次性临时数据目录：先关日志句柄（Windows 下打开的文件删不掉），再尽量删掉
if _SMOKE_OWN_DATA_DIR:
    try:
        import logging
        logging.shutdown()
        shutil.rmtree(_SMOKE_OWN_DATA_DIR, ignore_errors=True)
    except Exception:
        pass
sys.exit(1 if fails else 0)
