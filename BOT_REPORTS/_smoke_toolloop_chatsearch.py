# -*- coding: utf-8 -*-
"""冒烟：D 批 D1——chat_search（聊天记录关键词检索）并入工具表。对应施工
单验收与不变量：

  C1  chat_search 的 risk/scope/参数与派单一致；register_builtin 不传
      chat_search_fn 不登记、传了才登记；连调两次不抛；main() 把
      _chat_search_tool 接进登记。
  C2  当前会话检索（不需要任何开关）：
      a 命中 → ok=True、摘要「最近 N 条里找到 M 条含「…」的消息」、
        data["content"] 是「月-日 时:分 发送者：内容」条目（内容截 200）；
      b 多词全部出现才算命中（只含其一的不算）；
      c 不分大小写；
      d 未命中 → ok=True、「最近 N 条里没有找到含「…」的消息」；
      e 空 query（纯空白）→ ok=False 的确定性说明；
      f 没有文本的消息（纯图片等）跳过；
      g 机器人自己的消息也列入，发送者后标「（机器人）」；
      h limit：缺省 100、上限 200（999→200）、下限 1（-5→1）；
      i 命中多于 20 条：取最近 20 条、按时间正序；
      j/k/l 取数失败 ≠ 没找到（主会话审查发回）：history 抛异常 / 返回
        None → ok=False「没取到」＋AGENT_CHAT_SEARCH_FAIL 日志（不含聊天
        内容）；正常返回空列表 → ok=True「最近 0 条里没有找到」。
  C3  跨会话（隐私定位）：
      a chat 指向别的群、tool_cross_chat_search 关 → 拒（那句说明）且
        没有取数据（history 零调用）；
      b 开 → 执行（history 收到目标群）；
      c chat 按群号 / 完整群名解析；数字只当群号；
      d 群名对上多个 → 歧义拒（不猜）；
      e 群号不在白名单 → 没找到；白名单外的名字同样不认；
      f chat 填的是当前会话自己的群 → 不算跨会话、不需要开关。
  C4  回喂里有条目：经真实 qq_tools.tool_loop 跑一轮，第二轮模型收到的
      回喂里有「内容：」通道与命中条目（内容里的 [TOOL] 一并改写）。
  C5  经真实 process() 驱动一轮完整循环：发到会话的文字正常；日志扫描
      ——整轮跑下来 ALOG 里没有任何一条聊天内容（种子文本逐一核对），
      循环日志只记工具名与成败。
  C6  管理台：「允许查别的群的聊天记录」复选框（绑定/保存写法）、说明
      文案、GET 默认值与 POST 白名单登记（web_fetch 的两处在
      _smoke_toolloop_webfetch.py 里钉）。

隔离：import 被测模块前把 QQBOT_DATA_DIR 指到一次性临时目录；daemon
（history 记账桩 + group_names）、模型链、qq_mcp 索引面、发送全部打桩
——零真实数据、零真实发送。聊天记录全部是造出来的测试数据（群号 5550xx）。
一次性临时数据目录，跑完删。
用法：python BOT_REPORTS/_smoke_toolloop_chatsearch.py
"""
import contextlib
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join("plugins", "qq-bot", "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 数据目录隔离（必须在 import 被测模块之前生效）
_SMOKE_OWN_DATA_DIR = tempfile.mkdtemp(prefix="qqbot_toolloop_chatsearch_iso_")
os.environ["QQBOT_DATA_DIR"] = _SMOKE_OWN_DATA_DIR
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

import qq_daemon as D  # noqa: E402,F401  (与其它冒烟保持同一导入面)
import qq_bot_agent as A  # noqa: E402
import qq_mcp as MQ  # noqa: E402
import qq_tools as T  # noqa: E402
import qq_tools_builtin as TB  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC_A = (ROOT / "plugins" / "qq-bot" / "scripts" / "qq_bot_agent.py").read_text(
    encoding="utf-8")
SRC_UI = (ROOT / "plugins" / "qq-bot" / "scripts" / "qq_admin_ui.py").read_text(
    encoding="utf-8")

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
A.log_usage = lambda *x, **k: None
A._log_context_dump = lambda *x, **k: None
A.PLUGIN_HOST.on_message_all = lambda msg: None
D.out = lambda line: None

OWNER_QQ = "10086"
NOT_OWNER = "99999"

# ---- 群白名单（临时数据目录里造：全部是测试群号 5550xx）----

WL_PATH = os.path.join(A.DATA_DIR, "group_whitelist.json")
pathlib.Path(WL_PATH).write_text(json.dumps(
    {"enabled": True, "groups": ["555001", "555002", "555003"],
     "allow_all": False}), encoding="utf-8")

GNAMES = {"555001": "冒烟一群", "555002": "冒烟二群", "555003": "冒烟三群二号"}

# ---- 聊天记录桩：history 记账（记每次调用的 ct/cid/limit）----

_NOW = time.time()
HIST_CALLS = []
STORE = {
    ("group", "555001"): [
        {"ts": _NOW - 400, "message_id": "c1", "sender_id": "70001",
         "sender_name": "小张", "is_bot": False, "text": "今天天气真不错呀周末",
         "n_images": 0, "image_urls": []},
        {"ts": _NOW - 300, "message_id": "c2", "sender_id": "70002",
         "sender_name": "小李", "is_bot": False,
         "text": "Weather Is GREAT today 说真的", "n_images": 0,
         "image_urls": []},
        {"ts": _NOW - 200, "message_id": "c3", "sender_id": "70001",
         "sender_name": "小张", "is_bot": False, "text": "",
         "n_images": 2, "image_urls": []},   # 纯图片：没有文本，检索要跳过
        {"ts": _NOW - 100, "message_id": "c4", "sender_id": "bot",
         "sender_name": "BOT", "is_bot": True, "text": "天气话题我插一句",
         "n_images": 0, "image_urls": []},
        {"ts": _NOW - 60, "message_id": "c5", "sender_id": "70003",
         "sender_name": "小王", "is_bot": False,
         "text": "长内容" * 150 + "结尾在两百字后", "n_images": 0,
         "image_urls": []},   # 超过 200 字，截断用
    ],
    ("group", "555002"): [
        {"ts": _NOW - 30, "message_id": "d1", "sender_id": "70004",
         "sender_name": "小赵", "is_bot": False, "text": "别的群的天气记一眼",
         "n_images": 0, "image_urls": []},
    ],
}


def rec_daemon():
    class _DM(object):
        def history(self, chat_type, chat_id, limit=30):
            HIST_CALLS.append((str(chat_type), str(chat_id), int(limit)))
            recs = STORE.get((str(chat_type), str(chat_id)), [])
            return recs[-limit:] if limit else list(recs)

        def group_names(self):
            return dict(GNAMES)

        def send(self, chat_type, chat_id, texts, reply_message_id=None):
            return {"ok": True, "queued": 1}

        def __getattr__(self, name):
            def _noop(*a, **k):
                return {"ok": False, "error": "smoke: no side effects"}
            return _noop
    return _DM()


def fake_search(q):
    return ("x 测试条目（example.com）", "bing")


def base_msg(**kw):
    m = {"id": 1, "ts": 0, "chat_type": "private", "chat_id": OWNER_QQ,
         "chat_name": "主人", "message_id": "m1", "sender_id": OWNER_QQ,
         "sender_name": "主人", "triggered": True, "trigger_reason": "chat",
         "text": "在吗", "at_me": False, "at_users": [], "image_urls": [],
         "has_reply": False, "reply_to": None, "reply_text": "",
         "face_count": 0, "has_record": False, "reply_id": "",
         "record_file": "", "record_url": "", "forward_lines": []}
    m.update(kw)
    return m


def owner_grp(**kw):
    return base_msg(chat_type="group", chat_id="555001", chat_name="冒烟一群",
                    sender_id=OWNER_QQ, sender_name="主人", at_me=False,
                    trigger_reason="chat", **kw)


class NullDaemon(object):

    def __init__(self):
        self.sent = []

    def history(self, chat_type, chat_id, limit=30):
        return []

    def group_names(self):
        return dict(GNAMES)

    def send(self, chat_type, chat_id, texts, reply_message_id=None):
        self.sent.append(list(texts))
        return {"ok": True, "queued": 1}

    def __getattr__(self, name):
        def _noop(*a, **k):
            return {"ok": False, "error": "smoke: no side effects"}
        return _noop


RC_PATH = os.path.join(A.DATA_DIR, "reply_config.json")
_RC_PAD = [0]


def write_reply_cfg(obj):
    _RC_PAD[0] += 1
    pathlib.Path(RC_PATH).write_text(
        json.dumps(obj, ensure_ascii=False, indent=2) + ("\n" * _RC_PAD[0]),
        encoding="utf-8")


def set_cfg(loop_on=False, cross_on=False):
    write_reply_cfg({"tool_loop_enabled": bool(loop_on),
                     "tool_cross_chat_search": bool(cross_on)})


set_cfg(loop_on=False, cross_on=False)

_CTX_GRP = {"is_owner": True, "is_gadmin": False, "daemon": rec_daemon(),
            "msg": owner_grp()}


def cs(args):
    """直跑 chat_search（当前会话＝群 555001；daemon/ctx 每次 fresh，避免
    HIST_CALLS 记账串场）。"""
    HIST_CALLS.clear()
    return T.run_tool(T.get_tool("chat_search"), dict(args), dict(
        _CTX_GRP, daemon=rec_daemon()), {})


# ============ C1 登记 ============

TB.register_builtin(fake_search)
chk("C1a 不传 chat_search_fn：不登记（web_search 照常）",
    T.get_tool("chat_search") is None and T.get_tool("web_search") is not None, "")

TB.register_builtin(fake_search, chat_search_fn=A._chat_search_tool)
_spec = T.get_tool("chat_search")
chk("C1b 传了才登记；risk=read、scope=owner",
    _spec is not None and _spec["risk"] == "read" and _spec["scope"] == "owner",
    (_spec and (_spec["risk"], _spec["scope"])))
_pnames = [(p["name"], p["type"], p["required"])
           for p in (_spec or {}).get("params", [])]
chk("C1c 参数表：query 必填、chat/limit 可选",
    _pnames == [("query", "str", True), ("chat", "str", False),
                ("limit", "int", False)], _pnames)
_dup_exc = ""
try:
    TB.register_builtin(fake_search, chat_search_fn=A._chat_search_tool)
except Exception as exc:
    _dup_exc = repr(exc)
chk("C1d 连调两次不抛、登记原样保留",
    _dup_exc == "" and T.get_tool("chat_search") is not None, _dup_exc)
chk("C1e main() 把 _chat_search_tool 接进 register_builtin",
    "chat_search_fn=_chat_search_tool" in SRC_A, "")
chk("C1f _tool_available 对 chat_search 恒可用（当前会话检索不需要开关）",
    A._tool_available("chat_search") is True
    and "chat_search" in [s["name"] for s in
                          T.tools_for({"is_owner": True, "is_gadmin": False})], "")

# ============ C2 当前会话检索 ============

set_cfg(loop_on=False, cross_on=False)   # 跨会话开关关着——当前会话不受影响

_r = cs({"query": "天气"})
_lines = str((_r.get("data") or {}).get("content") or "").splitlines()
chk("C2a 命中：ok=True、摘要「最近 N 条里找到 M 条含「…」的消息」",
    _r["ok"] is True and _r["summary"] == "最近 5 条里找到 2 条含「天气」的消息",
    _r["summary"])
chk("C2a2 条目格式「月-日 时:分 发送者：内容」（纯图片那条跳过）",
    len(_lines) == 2
    and all(re.match(r"\d{2}-\d{2} \d{2}:\d{2} .+：", ln) for ln in _lines)
    and not any("小王" in ln for ln in _lines)
    and any(ln.endswith("小张：今天天气真不错呀周末") for ln in _lines),
    _lines)

_r2 = cs({"query": "天气 周末"})
_chk2 = _r2["ok"] is True and _r2["summary"].startswith("最近 5 条里找到 1 条")
chk("C2b 多词全部出现才算命中（只含「天气」的不算）", _chk2, _r2["summary"])

_r3 = cs({"query": "WEATHER is great"})
chk("C2c 不分大小写（query 大写也命中）",
    _r3["ok"] is True and "找到 1 条" in _r3["summary"], _r3["summary"])

_r4 = cs({"query": "不存在的词组"})
chk("C2d 未命中：ok=True、「最近 N 条里没有找到含「…」的消息」",
    _r4["ok"] is True
    and _r4["summary"] == "最近 5 条里没有找到含「不存在的词组」的消息"
    and not (_r4.get("data") or {}).get("content"), _r4["summary"])

_r5 = cs({"query": "   "})
chk("C2e 空白 query：ok=False 的确定性说明",
    _r5["ok"] is False and "关键词不能是空" in _r5["summary"], _r5["summary"])

_r6 = cs({"query": "天气"})
_content6 = str((_r6.get("data") or {}).get("content") or "")
chk("C2g 机器人自己的消息列入并标「（机器人）」",
    "（机器人）：天气话题我插一句" in _content6, _content6)

_r7 = cs({"query": "结尾在两百字后"})
_line7 = str((_r7.get("data") or {}).get("content") or "")
_body7 = _line7.split("：", 1)[1] if "：" in _line7 else ""
chk("C2a3 条目内容截 200 字（超长的按 200 截）",
    _r7["ok"] is True and len(_body7) == 200, (len(_body7),))

HIST_CALLS.clear()
cs({"query": "天气"})
chk("C2h limit 缺省 100（history 收到 100）",
    HIST_CALLS == [("group", "555001", 100)], HIST_CALLS)
HIST_CALLS.clear()
T.run_tool(T.get_tool("chat_search"), {"query": "天气", "limit": 999},
           dict(_CTX_GRP, daemon=rec_daemon()), {})
T.run_tool(T.get_tool("chat_search"), {"query": "天气", "limit": -5},
           dict(_CTX_GRP, daemon=rec_daemon()), {})
chk("C2h2 limit 上限 200、下限 1",
    HIST_CALLS[0][2] == 200 and HIST_CALLS[1][2] == 1, HIST_CALLS)

# 取数失败 ≠ 没找到（主会话审查发回）：history 抛异常 / 返回形状不对 →
# ok=False 的「没取到」＋FAIL 日志；「最近 N 条里没有找到」只在接口正常
# 返回（含正常空列表）且确实没命中时出现。
class _RaiserDM(object):
    def history(self, chat_type, chat_id, limit=30):
        raise RuntimeError("history 炸了")

    def group_names(self):
        return dict(GNAMES)


class _NoneDM(object):
    def history(self, chat_type, chat_id, limit=30):
        return None

    def group_names(self):
        return dict(GNAMES)


class _EmptyDM(object):
    def history(self, chat_type, chat_id, limit=30):
        return []

    def group_names(self):
        return dict(GNAMES)


ALOG.clear()
_r15 = T.run_tool(T.get_tool("chat_search"), {"query": "天气"},
                  dict(_CTX_GRP, daemon=_RaiserDM()), {})
_fail15 = [l for l in ALOG if l.startswith("AGENT_CHAT_SEARCH_FAIL")]
_seed_hit15 = [l for l in _fail15
               if "今天天气真不错呀周末" in l or "天气话题我插一句" in l]
chk("C2j history 抛异常：ok=False、摘要不是「没有找到」、日志有 FAIL（含异常原因、不含种子聊天文本）",
    _r15["ok"] is False and "没有找到" not in _r15["summary"]
    and "没取到" in _r15["summary"] and len(_fail15) == 1
    and "history 炸了" in _fail15[0] and not _seed_hit15,
    (_r15["summary"], _fail15))

ALOG.clear()
_r16 = T.run_tool(T.get_tool("chat_search"), {"query": "天气"},
                  dict(_CTX_GRP, daemon=_NoneDM()), {})
_fail16 = [l for l in ALOG if l.startswith("AGENT_CHAT_SEARCH_FAIL")]
_seed_hit16 = [l for l in _fail16
               if "今天天气真不错呀周末" in l or "天气话题我插一句" in l]
chk("C2k history 返回 None：同样按「没取到」（ok=False、FAIL 日志只记类型）",
    _r16["ok"] is False and "没有找到" not in _r16["summary"]
    and "没取到" in _r16["summary"] and len(_fail16) == 1
    and "type=NoneType" in _fail16[0] and not _seed_hit16,
    (_r16["summary"], _fail16))

ALOG.clear()
_r17 = T.run_tool(T.get_tool("chat_search"), {"query": "天气"},
                  dict(_CTX_GRP, daemon=_EmptyDM()), {})
chk("C2l 正常返回空列表：ok=True「最近 0 条里没有找到」、无 FAIL 日志",
    _r17["ok"] is True
    and _r17["summary"] == "最近 0 条里没有找到含「天气」的消息"
    and not [l for l in ALOG if l.startswith("AGENT_CHAT_SEARCH_FAIL")],
    (_r17["summary"], [l for l in ALOG if "CHAT_SEARCH" in l]))

# 30 条命中的会话：取最近 20 条、时间正序
STORE[("group", "555003")] = [
    {"ts": _NOW - 1000 + i, "message_id": "e%d" % i, "sender_id": "700%02d" % i,
     "sender_name": "用户%02d" % i, "is_bot": False, "text": "关键词句%d" % i,
     "n_images": 0, "image_urls": []}
    for i in range(30)]
HIST_CALLS.clear()
_dm_i = rec_daemon()
_r8 = T.run_tool(T.get_tool("chat_search"), {"query": "关键词"},
                 dict(_CTX_GRP, daemon=_dm_i,
                      msg=base_msg(chat_type="group", chat_id="555003",
                                   chat_name="冒烟三群二号")), {})
_lines8 = str((_r8.get("data") or {}).get("content") or "").split("\n")
chk("C2i 命中多于 20 条：只回最近 20 条、按时间正序（最早 10 条不出现）",
    _r8["ok"] is True and "找到 20 条" in _r8["summary"]
    and len(_lines8) == 20 and "用户10：" in _lines8[0]
    and "用户29：" in _lines8[-1]
    and not any(("用户%02d：" % i) in str((_r8.get("data") or {}).get("content") or "")
                for i in range(10)),
    (_r8["summary"], _lines8[:1]))
STORE.pop(("group", "555003"))

# ============ C3 跨会话（隐私定位） ============

set_cfg(loop_on=False, cross_on=False)
HIST_CALLS.clear()
_r9 = cs({"query": "天气", "chat": "555002"})
chk("C3a 跨会话+开关关：拒（那句说明）、没有取数据",
    _r9["ok"] is False
    and _r9["summary"].startswith("查别的群的聊天记录需要在管理台打开对应的开关")
    and HIST_CALLS == [], (_r9["summary"], HIST_CALLS))

write_reply_cfg({"tool_loop_enabled": False, "tool_cross_chat_search": True})
HIST_CALLS.clear()
_r10 = cs({"query": "天气", "chat": "555002"})
chk("C3b 开关开：执行（history 收到目标群 555002）、拿到那个群的条目",
    _r10["ok"] is True and "找到 1 条" in _r10["summary"]
    and HIST_CALLS == [("group", "555002", 100)]
    and "小赵：别的群的天气记一眼" in str((_r10.get("data") or {}).get("content") or ""),
    (_r10["summary"], HIST_CALLS))

HIST_CALLS.clear()
_r11 = cs({"query": "天气", "chat": "冒烟二群"})
chk("C3c chat 按完整群名解析到同一群", _r11["ok"] is True
    and HIST_CALLS == [("group", "555002", 100)], (HIST_CALLS, _r11["summary"]))

_r12 = cs({"query": "天气", "chat": "冒烟"})
chk("C3d 群名对上多个：歧义拒（不猜）",
    _r12["ok"] is False and "叫这个名字的群有多个" in _r12["summary"]
    and "555001" in _r12["summary"] and "555003" in _r12["summary"],
    _r12["summary"])

set_cfg(loop_on=False, cross_on=False)
_r13 = cs({"query": "天气", "chat": "999999"})
chk("C3e 群号不在白名单：没找到（不猜、不取数据）",
    _r13["ok"] is False and "没找到这个群" in _r13["summary"], _r13["summary"])

_r14 = cs({"query": "天气", "chat": "555001"})
chk("C3f chat＝当前会话自己的群：不算跨会话、开关关着也能查",
    _r14["ok"] is True and "找到 2 条" in _r14["summary"], _r14["summary"])

# ============ C4 经真实 tool_loop：回喂里有条目 ============


class _CM(object):

    def __init__(self, outputs=()):
        self.outputs = list(outputs)
        self.history_snapshots = []

    def __call__(self, history):
        self.history_snapshots.append([dict(m) for m in history])
        return self.outputs.pop(0) if self.outputs else "好。"


_dm_c4 = rec_daemon()
_cm4 = _CM(["主人，查到了"])
T.tool_loop(_cm4, [T.get_tool("chat_search")],
            '[TOOL] chat_search {"query": "天气"}',
            {"is_owner": True, "is_gadmin": False, "daemon": _dm_c4,
             "msg": owner_grp()}, {})
_fb4 = _cm4.history_snapshots[0][-1]["content"] if _cm4.history_snapshots else ""
chk("C4 经真实 tool_loop：回喂里有「内容：」通道与命中条目",
    "- chat_search({\"query\": \"天气\"}) → 成功：" in _fb4
    and "内容：" in _fb4 and "小张：今天天气真不错呀周末" in _fb4
    and "（机器人）：天气话题我插一句" in _fb4, _fb4[-260:])

# ============ C5 process() 全链 + 日志扫描 ============

_MQ_STUBS = dict(mcp_allowed=lambda: False,
                 tool_index=lambda limit_per_server=20, doc_cap=None: ([], []),
                 doc_cap_cfg=lambda: 4000)

# 种子文本：日志扫描逐一核对，任何一条都不许出现在日志里
SEED_A = "天气真不错呀周末"
SEED_B = "金丝雀CANARY不许进日志"
STORE[("private", OWNER_QQ)] = [
    {"ts": _NOW - 50, "message_id": "p1", "sender_id": "70005",
     "sender_name": "小张", "is_bot": False, "text": SEED_A,
     "n_images": 0, "image_urls": []},
    {"ts": _NOW - 40, "message_id": "p2", "sender_id": "70006",
     "sender_name": "小李", "is_bot": False, "text": SEED_B,
     "n_images": 0, "image_urls": []},
]


def run_process(msg, outputs, loop_on=True):
    class _DM(NullDaemon):
        # process 全链用：history 供种子私聊记录（chat_search 的取数口），
        # 其余照 NullDaemon 无副作用。
        def history(self, chat_type, chat_id, limit=30):
            recs = STORE.get((str(chat_type), str(chat_id)), [])
            return recs[-limit:] if limit else list(recs)
    dm = _DM()
    A.PLUGIN_HOST.daemon = dm
    calls = []

    def _chain(messages, text_ctx="", budget_sec=200, **kwargs):
        calls.append([dict(m) for m in messages])
        item = outputs.pop(0)
        return "mx", item

    set_cfg(loop_on, cross_on=False)   # 当前会话检索：跨会话开关保持关
    _force = dict(PROMPT_V2_FORCE=False, REPLY_V2_FORCE=False,
                  REPLY_JSON_FORCE=False, REACT_ONLY_FORCE=False,
                  VCAP_FORCE=False, vision_enabled=lambda: False,
                  web_search_enabled=lambda: False,
                  chat_anti_interrupt_enabled=lambda: True,
                  lean_ctx_on=lambda: True,
                  _cap_chat_allowed=lambda ct, cid: False,
                  call_glm_with_fallback=_chain,
                  owner_qq=lambda: OWNER_QQ,
                  _mcp_tools_doc=lambda: "旧文档",
                  _mcp_agent_loop=lambda d, m, ms, r: None,
                  _devctrl_ask_code=lambda src: None)
    with patched(A, **_force), patched(MQ, **_MQ_STUBS):
        ALOG.clear()
        try:
            A.process(dm, msg)
        except Exception as exc:   # 不许抛
            dm.sent = [["EXC:%r" % exc]]
    return dm, calls


_m5 = base_msg(id=501, message_id="m501", text="帮我找一下天气那句")
_dm5, _c5 = run_process(
    _m5,
    ['我翻一下记录哈\n[TOOL] chat_search {"query": "天气"}',
     "主人，找到啦"],
    loop_on=True)
_j5 = "".join(t for ts in _dm5.sent for t in ts)
_s5_feed = _c5[1][-1]["content"] if len(_c5) > 1 else ""
chk("C5a 清单里有 chat_search(query,chat?,limit?)、循环真实执行",
    "chat_search(query,chat?,limit?)" in _c5[0][0]["content"]
    and any("AGENT_TOOL_LOOP" in l and "chat_search:成功" in l for l in ALOG),
    ([l for l in ALOG if "AGENT_TOOL_LOOP" in l],))
chk("C5b 第二轮回喂里有「内容：」通道与命中条目",
    "内容：" in _s5_feed and SEED_A in _s5_feed
    and "- chat_search(" in _s5_feed, _s5_feed[-200:])
chk("C5c 发出的最终文字正常、不带 [TOOL]",
    "主人，找到啦" in _j5 and "[TOOL]" not in _j5, _j5[:80])
_leak = [l for l in ALOG if SEED_A in l or SEED_B in l]
chk("C5d 日志扫描：整轮跑下来日志里没有任何一条聊天内容（两条种子逐一核对）",
    not _leak, _leak)
chk("C5e 工具自己的日志只有目标与条数（AGENT_CHAT_SEARCH，无内容）",
    any(l.startswith("AGENT_CHAT_SEARCH tgt=private/%s n=2 hits=1 shown=1"
                    % OWNER_QQ) for l in ALOG),
    [l for l in ALOG if l.startswith("AGENT_CHAT_SEARCH")])

# ============ C6 管理台登记 ============

chk("C6a 复选框：绑定与保存写法齐全（tool_cross_chat_search）",
    "允许查别的群的聊天记录" in SRC_UI
    and "rv.tool_cross_chat_search?'checked':''" in SRC_UI
    and "{tool_cross_chat_search:this.checked?1:0}" in SRC_UI, "")
chk("C6b 说明文案：默认只查当前群、打开后可查别的群",
    "默认只能查当前这个群的近期聊天记录" in SRC_UI
    and "打开后，主人让机器人查别的群时也可以查" in SRC_UI, "")
chk("C6c GET 默认值与 POST 白名单登记（不是布尔 true 一律当关）",
    '"tool_cross_chat_search": data.get("tool_cross_chat_search") is True' in SRC_UI
    and 'if "tool_cross_chat_search" in body:' in SRC_UI, "")

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
