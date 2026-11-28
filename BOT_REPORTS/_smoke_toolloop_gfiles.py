# -*- coding: utf-8 -*-
"""冒烟：D 批 D3 后半——group_files / group_file_url（群文件列表与下载
链接）并入工具表。对应施工单验收与不变量：

  G1  登记：两个工具 name/risk(read)/scope(owner)/参数；group_files_fn/
      group_file_url_fn/group_files_enabled_fn 三个都传了才登记（缺一
      不登记）；连调两次不抛；main() 接线。
  G2  DaemonClient.group_files / group_file_url：请求路径、请求头（内部
      令牌、Content-Type）、请求体（群号是字符串；file_count None 时请求
      体里没有这个键；folder_id 空时没有这个键；file_id 原样透传）；返回
      原样；连接异常 / 超时 / 非 JSON → ok=False 一句不带内部细节的原因、
      不外抛。（HTTP 层全程打桩，不连真实服务。）
  G3  开关 tool_group_files_enabled：缺键/脏值一律关，只有布尔 true 开；
      关时 _tool_available 假、清单没有两个工具；开时有。
  G4  group_files：根目录（文件夹+文件）、空目录「（空的）」、folder 给
      ID / 给名字（先取根目录换 folder_id，根目录那次不带 count）、folder
      找不到（附现有文件夹清单、无第二次调用）、群歧义、群找不到、消息
      服务 ok=False 透传 detail、抛异常收成一句人话、count 钳制 1-200、
      开关关直跑拒绝；回喂 content 有 file_id= 原文、大小 B/KB/MB 三档、
      上传者为空省略。
  G5  group_file_url：正常（摘要含链接、不带 content、file_id 一字不差
      透传）、file_id 缺失（参数校验拒）、服务失败、data 无 url、抛异常。
  G6  日志扫描：整轮跑下来 ALOG 里没有文件名、链接、上传者、file_id /
      folder_id；工具日志只有群号、条数、成败。
  G7  管理台：「读取群文件列表（仅主人）」复选框（绑定/保存写法）、说明
      文案、GET 默认值与 POST 白名单登记、「启用工具循环」工具清单补句、
      _smoke_settings_tabs.py 的 SAVE_KEYS 加键。

隔离：import 被测模块前把 QQBOT_DATA_DIR 指到一次性临时目录；daemon
（group_files/group_file_url 记账桩 + group_names）与 HTTP 连接全部打桩
——零真实数据、零真实网络。群号 5550xx，文件名「课件.pdf」「合影.jpg」
这类假名，上传者「成员甲/乙」；令牌用现造假值。一次性临时数据目录，跑完删。
用法：python BOT_REPORTS/_smoke_toolloop_gfiles.py
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
_SMOKE_OWN_DATA_DIR = tempfile.mkdtemp(prefix="qqbot_toolloop_gfiles_iso_")
os.environ["QQBOT_DATA_DIR"] = _SMOKE_OWN_DATA_DIR
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

import qq_daemon as D  # noqa: E402,F402  (与其它冒烟保持同一导入面)
import qq_bot_agent as A  # noqa: E402
import qq_tools as T  # noqa: E402
import qq_tools_builtin as TB  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC_A = (ROOT / "plugins" / "qq-bot" / "scripts" / "qq_bot_agent.py").read_text(
    encoding="utf-8")
SRC_UI = (ROOT / "plugins" / "qq-bot" / "scripts" / "qq_admin_ui.py").read_text(
    encoding="utf-8")
SRC_TABS = (ROOT / "BOT_REPORTS" / "_smoke_settings_tabs.py").read_text(
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

# ---- 群白名单（临时数据目录里造：全部是测试群号 5550xx）----

WL_PATH = os.path.join(A.DATA_DIR, "group_whitelist.json")
pathlib.Path(WL_PATH).write_text(json.dumps(
    {"enabled": True, "groups": ["555001", "555002", "555003"],
     "allow_all": False}), encoding="utf-8")

GNAMES = {"555001": "冒烟一群", "555002": "冒烟重名群",
          "555003": "冒烟重名群"}   # 555002/555003 同名＝歧义用

# ---- 群文件样例（假数据：群号 5550xx、假文件名、假 ID、假链接）----

ROOT_DATA = {
    "files": [
        {"file_id": "fx_big_1", "file_name": "课件.pdf", "size": 3145728,
         "upload_time": 1700000000, "uploader_name": "成员甲"},   # 3.0 MB
        {"file_id": "fx_mid_2", "file_name": "合影.jpg", "size": 2048,
         "upload_time": 1700000001, "uploader_name": ""},          # 2.0 KB，无上传者
        {"file_id": "fx_sml_3", "file_name": "note.txt", "size": 512,
         "upload_time": 1700000002, "uploader_name": "成员乙"},    # 512 B
    ],
    "folders": [{"folder_id": "fld_a_1", "folder_name": "资料",
                 "total_file_count": 3}],
}
SUB_DATA = {"files": [{"file_id": "fx_sub_9", "file_name": "课件.pdf",
                       "size": 1024, "upload_time": 1700000003,
                       "uploader_name": "成员甲"}],
            "folders": []}
EMPTY_DATA = {"files": [], "folders": []}
URL_DATA = {"url": "https://example.test/dl/fx_big_1"}


class StubDaemon(object):
    """daemon 桩：group_files / group_file_url 记账并按队列回样例或抛异常；
    队列空了按「未设置桩」失败返回（宁可 ok=False 也不静默给假数据）。"""

    def __init__(self, names=None):
        self._names = dict(GNAMES if names is None else names)
        self.files_calls = []
        self.url_calls = []
        self.files_queue = []
        self.url_queue = []

    def group_names(self):
        return dict(self._names)

    def group_files(self, group_id, folder_id="", file_count=None):
        self.files_calls.append({"gid": str(group_id), "folder_id": folder_id,
                                 "file_count": file_count})
        step = self.files_queue.pop(0) if self.files_queue else \
            {"exc": RuntimeError("smoke: group_files 桩未设置")}
        if step.get("exc") is not None:
            raise step["exc"]
        return step.get("out") or {"ok": False, "detail": "smoke: 桩空"}

    def group_file_url(self, group_id, file_id):
        self.url_calls.append({"gid": str(group_id), "fid": file_id})
        step = self.url_queue.pop(0) if self.url_queue else \
            {"exc": RuntimeError("smoke: group_file_url 桩未设置")}
        if step.get("exc") is not None:
            raise step["exc"]
        return step.get("out") or {"ok": False, "detail": "smoke: 桩空"}

    def send(self, chat_type, chat_id, texts, reply_message_id=None):
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


def set_cfg(gfiles_on=False):
    write_reply_cfg({"tool_loop_enabled": True,
                     "tool_group_files_enabled": bool(gfiles_on)})


set_cfg(gfiles_on=False)


def owner_priv():
    return {"id": 1, "ts": 0, "chat_type": "private", "chat_id": OWNER_QQ,
            "chat_name": "主人", "message_id": "m1", "sender_id": OWNER_QQ,
            "sender_name": "主人", "triggered": True, "trigger_reason": "chat",
            "text": "看看群文件", "at_me": False, "at_users": [],
            "image_urls": [], "has_reply": False, "reply_to": None,
            "reply_text": "", "face_count": 0, "has_record": False,
            "reply_id": "", "record_file": "", "record_url": "",
            "forward_lines": []}


def run_named(name, args, dm):
    ctx = {"daemon": dm, "msg": owner_priv(), "is_owner": True,
           "is_gadmin": False}
    return T.run_tool(T.get_tool(name), dict(args), ctx, {})


def avail_names():
    return [s["name"] for s in T.tools_for({"is_owner": True, "is_gadmin": False})
            if A._tool_available(s.get("name"))]


def noop(*a, **k):
    return {"ok": False, "detail": "smoke: noop"}


# ============ G1 登记 ============

TB.register_builtin(noop)
chk("G1a 三个 fn 不传：group_files / group_file_url 都不登记（web_search 照常）",
    T.get_tool("group_files") is None and T.get_tool("group_file_url") is None
    and T.get_tool("web_search") is not None, "")

TB.register_builtin(noop, group_files_fn=noop, group_file_url_fn=noop)
chk("G1b 缺 group_files_enabled_fn：两个都不登记",
    T.get_tool("group_files") is None and T.get_tool("group_file_url") is None, "")

TB.register_builtin(noop, group_files_fn=A._group_files_tool,
                    group_file_url_fn=A._group_file_url_tool,
                    group_files_enabled_fn=A._group_files_tool_enabled)
_spec = T.get_tool("group_files")
chk("G1c group_files 登记：risk=read、scope=owner",
    _spec is not None and _spec["risk"] == "read" and _spec["scope"] == "owner",
    (_spec and (_spec["risk"], _spec["scope"])))
_pnames = [(p["name"], p["type"], p["required"])
           for p in (_spec or {}).get("params", [])]
chk("G1d group_files 参数表：group 必填、folder/count 可选",
    _pnames == [("group", "str", True), ("folder", "str", False),
                ("count", "int", False)], _pnames)
_spec2 = T.get_tool("group_file_url")
chk("G1e group_file_url 登记：risk=read、scope=owner",
    _spec2 is not None and _spec2["risk"] == "read" and _spec2["scope"] == "owner",
    (_spec2 and (_spec2["risk"], _spec2["scope"])))
_pnames2 = [(p["name"], p["type"], p["required"])
            for p in (_spec2 or {}).get("params", [])]
chk("G1f group_file_url 参数表：group/file_id 都必填 str",
    _pnames2 == [("group", "str", True), ("file_id", "str", True)], _pnames2)
_dup_exc = ""
try:
    TB.register_builtin(noop, group_files_fn=A._group_files_tool,
                        group_file_url_fn=A._group_file_url_tool,
                        group_files_enabled_fn=A._group_files_tool_enabled)
except Exception as exc:
    _dup_exc = repr(exc)
chk("G1g 连调两次不抛、登记原样保留",
    _dup_exc == "" and T.get_tool("group_files") is not None, _dup_exc)
chk("G1h main() 把两个工具与开关接进 register_builtin",
    "group_files_fn=_group_files_tool" in SRC_A
    and "group_file_url_fn=_group_file_url_tool" in SRC_A
    and "group_files_enabled_fn=_group_files_tool_enabled" in SRC_A, "")

# ============ G2 DaemonClient 两个方法（HTTP 打桩） ============

REQ = []          # 每次 FakeConn.request 记一笔 {method, path, body, headers}
BEHAVIOR = {"exc": None, "raw": b"{}"}


class _FakeResp(object):
    def __init__(self, raw):
        self._raw = raw

    def read(self):
        return self._raw


class _FakeConn(object):
    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout

    def request(self, method, path, body=None, headers=None):
        REQ.append({"method": method, "path": path, "body": body,
                    "headers": dict(headers or {})})
        if BEHAVIOR["exc"] is not None:
            raise BEHAVIOR["exc"]

    def getresponse(self):
        return _FakeResp(BEHAVIOR["raw"])

    def close(self):
        pass


def client_call(fn, *a, **kw):
    """patch 掉 http.client.HTTPConnection 跑一次 DaemonClient 调用。"""
    REQ.clear()
    BEHAVIOR["exc"] = kw.pop("exc", None)
    BEHAVIOR["raw"] = kw.pop("raw", b"{}")
    client = A.DaemonClient(19999, "smoke_token_x")
    with patched(A.http.client, HTTPConnection=_FakeConn):
        return fn(client, *a, **kw)


RET_LIST = {"ok": True, "data": {"files": [{"file_id": "fx_big_1"}],
                                 "folders": [{"folder_id": "fld_a_1"}]}}
r = client_call(lambda c: c.group_files("555001"), raw=json.dumps(RET_LIST).encode())
req = REQ[0] if REQ else {}
_body = json.loads(req["body"].decode("utf-8")) if req.get("body") else {}
chk("G2a group_files：POST /group-files，带令牌与 JSON 头",
    req.get("method") == "POST" and req.get("path") == "/group-files"
    and req["headers"].get("X-QQBot-Token") == "smoke_token_x"
    and req["headers"].get("Content-Type") == "application/json"
    and req["headers"].get("Accept") == "application/json",
    req.get("headers"))
chk("G2b 请求体：群号是字符串；folder_id 空、file_count None 时不带这两个键",
    _body == {"group_id": "555001"}, _body)
chk("G2c 返回原样（{ok, data} dict）", r == RET_LIST, r)

RET_URL = {"ok": True, "data": URL_DATA}
r2 = client_call(lambda c: c.group_file_url("555001", "fx_big_1"),
                 raw=json.dumps(RET_URL).encode())
req2 = REQ[0] if REQ else {}
_body2 = json.loads(req2["body"].decode("utf-8")) if req2.get("body") else {}
chk("G2d group_file_url：POST /group-file-url，体 {group_id, file_id}",
    req2.get("path") == "/group-file-url" and req2.get("method") == "POST"
    and _body2 == {"group_id": "555001", "file_id": "fx_big_1"}, _body2)
chk("G2e group_file_url 返回原样", r2 == RET_URL, r2)

client_call(lambda c: c.group_files(555001, folder_id="fld_a_1", file_count=10))
_body3 = json.loads(REQ[0]["body"].decode("utf-8"))
chk("G2f int 群号转字符串；folder_id/file_count 给了带键",
    _body3 == {"group_id": "555001", "folder_id": "fld_a_1",
               "file_count": 10}, _body3)

r4 = client_call(lambda c: c.group_files("555001"),
                 exc=ConnectionRefusedError("refused"))
r5 = client_call(lambda c: c.group_files("555001"), exc=TimeoutError("timed out"))
r6 = client_call(lambda c: c.group_files("555001"), raw=b"<html>not json</html>")
r7 = client_call(lambda c: c.group_file_url("555001", "fx_big_1"),
                 exc=ConnectionRefusedError("refused"))
chk("G2g 连接异常/超时/非 JSON → ok=False 一句原因，不外抛",
    r4.get("ok") is False and isinstance(r4.get("detail"), str) and r4["detail"]
    and r5.get("ok") is False and r5.get("detail")
    and r6.get("ok") is False and r6.get("detail")
    and r7.get("ok") is False and r7.get("detail"),
    (r4, r5, r6, r7))
chk("G2h 失败 detail 不带内部细节（异常类名/原文不在里面）",
    all("ConnectionRefused" not in str(x.get("detail"))
        and "TimeoutError" not in str(x.get("detail"))
        and "not json" not in str(x.get("detail"))
        for x in (r4, r5, r6, r7)),
    [x.get("detail") for x in (r4, r5, r6, r7)])

# ============ G3 开关 ============

write_reply_cfg({"tool_loop_enabled": True})
chk("G3a 文件缺键：_group_files_tool_enabled False",
    A._group_files_tool_enabled() is False, "")
for dirty in ("1", 1, "true", None):
    write_reply_cfg({"tool_loop_enabled": True, "tool_group_files_enabled": dirty})
    if A._group_files_tool_enabled() is not False:
        chk("G3b 脏值只当关（%r）" % (dirty,), False, A._group_files_tool_enabled())
        break
else:
    chk("G3b 脏值（\"1\"/1/\"true\"/null）一律当关", True, "")
write_reply_cfg({"tool_loop_enabled": True, "tool_group_files_enabled": True})
chk("G3c 布尔 true 才算开", A._group_files_tool_enabled() is True, "")

set_cfg(gfiles_on=False)
chk("G3d 开关关：_tool_available 假、主人可用清单没有两个工具",
    A._tool_available("group_files") is False
    and A._tool_available("group_file_url") is False
    and "group_files" not in avail_names()
    and "group_file_url" not in avail_names(), "")
set_cfg(gfiles_on=True)
chk("G3e 开关开：_tool_available 真、清单里有（清单来自登记表过滤）",
    A._tool_available("group_files") is True
    and A._tool_available("group_file_url") is True
    and "group_files" in avail_names() and "group_file_url" in avail_names(), "")

# ============ G4 group_files 行为 ============

set_cfg(gfiles_on=True)

dm = StubDaemon()
dm.files_queue = [{"out": {"ok": True, "data": ROOT_DATA}}]
r = run_named("group_files", {"group": "555001"}, dm)
chk("G4a 根目录：一次调用（无 folder 无 count）、摘要带群名与条数",
    dm.files_calls == [{"gid": "555001", "folder_id": "", "file_count": None}]
    and r["ok"] is True
    and r["summary"] == "冒烟一群(555001) 的文件（在根目录）：1 个文件夹、3 个文件",
    (dm.files_calls, r["summary"]))
_lines = str((r.get("data") or {}).get("content") or "").splitlines()
chk("G4b content：文件夹行（ID、文件数）",
    len(_lines) == 6 and _lines[0] == "文件夹："
    and _lines[1] == "- 资料（ID：fld_a_1，共 3 个文件）"
    and _lines[2] == "文件：", _lines)
chk("G4c 文件行：file_id= 原文、大小 B/KB/MB 三档、上传者在括号里",
    _lines[3] == "- 课件.pdf（3.0 MB，成员甲）file_id=fx_big_1"
    and _lines[5] == "- note.txt（512 B，成员乙）file_id=fx_sml_3", _lines)
chk("G4d 上传者为空：括号里省略那一项",
    _lines[4] == "- 合影.jpg（2.0 KB）file_id=fx_mid_2", _lines)

dm = StubDaemon()
dm.files_queue = [{"out": {"ok": True, "data": EMPTY_DATA}}]
r = run_named("group_files", {"group": "555001"}, dm)
chk("G4e 空目录：摘要 0/0、content「（空的）」",
    r["ok"] is True
    and r["summary"] == "冒烟一群(555001) 的文件（在根目录）：0 个文件夹、0 个文件"
    and (r.get("data") or {}).get("content") == "（空的）",
    (r["summary"], (r.get("data") or {}).get("content")))

dm = StubDaemon()
dm.files_queue = [{"out": {"ok": True, "data": ROOT_DATA}},
                  {"out": {"ok": True, "data": SUB_DATA}}]
r = run_named("group_files", {"group": "555001", "folder": "fld_a_1",
                              "count": 10}, dm)
chk("G4f folder 给 ID：先取根目录（不带 count）换 ID，再带 count 列子目录；摘要带文件夹名",
    dm.files_calls == [{"gid": "555001", "folder_id": "", "file_count": None},
                       {"gid": "555001", "folder_id": "fld_a_1",
                        "file_count": 10}]
    and r["ok"] is True
    and r["summary"] == "冒烟一群(555001) 的文件（文件夹「资料」）：0 个文件夹、1 个文件",
    (dm.files_calls, r["summary"]))

dm = StubDaemon()
dm.files_queue = [{"out": {"ok": True, "data": ROOT_DATA}},
                  {"out": {"ok": True, "data": SUB_DATA}}]
r = run_named("group_files", {"group": "555001", "folder": "资料"}, dm)
chk("G4g folder 给名字：归一成 folder_id 再列",
    r["ok"] is True
    and dm.files_calls[1] == {"gid": "555001", "folder_id": "fld_a_1",
                              "file_count": None}
    and "文件夹「资料」" in r["summary"], (dm.files_calls, r["summary"]))

dm = StubDaemon()
dm.files_queue = [{"out": {"ok": True, "data": ROOT_DATA}}]
r = run_named("group_files", {"group": "555001", "folder": "不存在的夹"}, dm)
chk("G4h folder 找不到：ok=False、附现有文件夹清单、无第二次调用",
    r["ok"] is False and "没找到这个文件夹" in r["summary"]
    and "现有文件夹：资料" in r["summary"]
    and len(dm.files_calls) == 1, (r["summary"], dm.files_calls))

dm = StubDaemon()
dm.files_queue = [{"out": {"ok": True, "data": EMPTY_DATA}}]
r = run_named("group_files", {"group": "555001", "folder": "哪里"}, dm)
chk("G4i 根目录没有文件夹时找不到的说明（根目录下没有文件夹、不猜）",
    r["ok"] is False and "根目录下没有文件夹" in r["summary"]
    and len(dm.files_calls) == 1, (r["summary"], dm.files_calls))

dm = StubDaemon()
dm.files_queue = [{"exc": RuntimeError("boom")}]
r = run_named("group_files", {"group": "555001", "count": 999}, dm)
chk("G4k count 钳制：999→200（异常路径不影响钳制前的调用形状）",
    dm.files_calls == [{"gid": "555001", "folder_id": "", "file_count": 200}]
    and r["ok"] is False, (dm.files_calls, r["summary"]))

dm = StubDaemon()
dm.files_queue = [{"out": {"ok": True, "data": EMPTY_DATA}}]
r = run_named("group_files", {"group": "555001", "count": 0}, dm)
chk("G4l count 下限：0→1",
    dm.files_calls == [{"gid": "555001", "folder_id": "", "file_count": 1}]
    and r["ok"] is True, dm.files_calls)

dm = StubDaemon()
r = run_named("group_files", {"group": "冒烟重名群"}, dm)
chk("G4m 群歧义：列出 群名(群号) 请主人报群号，不取数据",
    r["ok"] is False
    and r["summary"] == "叫这个名字的群有多个：冒烟重名群(555002)、"
                        "冒烟重名群(555003)——请告诉我群号"
    and not dm.files_calls, r["summary"])

dm = StubDaemon()
r = run_named("group_files", {"group": "555099"}, dm)
chk("G4n 群找不到：确定性说明，不取数据",
    r["ok"] is False and "没找到这个群" in r["summary"] and not dm.files_calls,
    r["summary"])

dm = StubDaemon()
dm.files_queue = [{"out": {"ok": False,
                           "detail": "群文件列表没取到（QQ那边没让这次查询成功）"}}]
r = run_named("group_files", {"group": "555001"}, dm)
chk("G4o 消息服务 ok=False：detail 原样回喂",
    r["ok"] is False
    and r["summary"] == "群文件列表没取到（QQ那边没让这次查询成功）", r["summary"])

dm = StubDaemon()
dm.files_queue = [{"exc": RuntimeError("内部细节 xyz")}]
r = run_named("group_files", {"group": "555001"}, dm)
chk("G4p 抛异常：收成一句人话，不外抛、不带异常原文",
    r["ok"] is False and r["summary"] == "群文件没取到（消息服务没答上来）"
    and "xyz" not in r["summary"], r["summary"])

set_cfg(gfiles_on=False)
dm = StubDaemon()
r = run_named("group_files", {"group": "555001"}, dm)
chk("G4q 开关关：工具直跑也拒绝（第一道闸），不取数据",
    r["ok"] is False and "没有开启" in r["summary"] and not dm.files_calls,
    r["summary"])
set_cfg(gfiles_on=True)

# ============ G5 group_file_url 行为 ============

dm = StubDaemon()
dm.url_queue = [{"out": {"ok": True, "data": URL_DATA}}]
r = run_named("group_file_url", {"group": "555001", "file_id": "fx_big_1"}, dm)
chk("G5a 正常：摘要「下载链接：<url>」、不带 content、调用 (gid, file_id)",
    r["ok"] is True and r["summary"] == "下载链接：https://example.test/dl/fx_big_1"
    and not (r.get("data") or {}).get("content")
    and dm.url_calls == [{"gid": "555001", "fid": "fx_big_1"}],
    (r["summary"], dm.url_calls))

WEIRD_ID = "fxA_b-9/1=="
dm = StubDaemon()
dm.url_queue = [{"out": {"ok": True,
                         "data": {"url": "https://example.test/dl/x"}}}]
r = run_named("group_file_url", {"group": "555001", "file_id": WEIRD_ID}, dm)
chk("G5b file_id 原样透传（含 / = 等字符，一字不差）",
    dm.url_calls == [{"gid": "555001", "fid": WEIRD_ID}] and r["ok"] is True,
    dm.url_calls)

dm = StubDaemon()
r = run_named("group_file_url", {"group": "555001"}, dm)
chk("G5c file_id 缺失：参数校验拒（缺少必填参数），不取数据",
    r["ok"] is False and "缺少必填参数" in r["summary"] and not dm.url_calls,
    r["summary"])

dm = StubDaemon()
dm.url_queue = [{"out": {"ok": False,
                         "detail": "下载链接没取到（QQ那边没让这次查询成功）"}}]
r = run_named("group_file_url", {"group": "555001", "file_id": "fx_big_1"}, dm)
chk("G5d 消息服务失败：detail 原样回喂",
    r["ok"] is False
    and r["summary"] == "下载链接没取到（QQ那边没让这次查询成功）", r["summary"])

dm = StubDaemon()
dm.url_queue = [{"out": {"ok": True, "data": {}}}]
r = run_named("group_file_url", {"group": "555001", "file_id": "fx_big_1"}, dm)
chk("G5e data 里没有 url：如实说没取到",
    r["ok"] is False and "没取到" in r["summary"], r["summary"])

dm = StubDaemon()
dm.url_queue = [{"exc": RuntimeError("内部细节 xyz")}]
r = run_named("group_file_url", {"group": "555001", "file_id": "fx_big_1"}, dm)
chk("G5f 抛异常：收成一句人话，不外抛、不带异常原文",
    r["ok"] is False and r["summary"] == "下载链接没取到（消息服务没答上来）"
    and "xyz" not in r["summary"], r["summary"])

# ============ G6 日志 ============

chk("G6a 列表日志：只有群号与条数",
    "AGENT_GROUP_FILES gid=555001 folders=1 files=3" in ALOG
    and "AGENT_GROUP_FILES gid=555001 folders=0 files=0" in ALOG,
    [l for l in ALOG if l.startswith("AGENT_GROUP_FILES")])
chk("G6b 链接日志：只有群号与成败（成功与失败各一条）",
    "AGENT_GROUP_FILE_URL gid=555001 ok=True" in ALOG
    and "AGENT_GROUP_FILE_URL gid=555001 ok=False" in ALOG,
    [l for l in ALOG if l.startswith("AGENT_GROUP_FILE_URL")])
_LEAK_TOKENS = ["课件.pdf", "合影.jpg", "note.txt", "example.test",
                "成员甲", "成员乙", "fx_big_1", "fx_mid_2", "fx_sml_3",
                "fx_sub_9", WEIRD_ID, "fld_a_1"]
_leak = [t for t in _LEAK_TOKENS
         for l in ALOG if t in l]
chk("G6c 日志扫描：整轮跑下来没有文件名、链接、上传者、file_id/folder_id",
    not _leak, _leak)

# ============ G7 管理台登记 ============

chk("G7a 复选框：绑定与保存写法齐全（tool_group_files_enabled）",
    "读取群文件列表（仅主人）" in SRC_UI
    and "rv.tool_group_files_enabled?'checked':''" in SRC_UI
    and "{tool_group_files_enabled:this.checked?1:0}" in SRC_UI, "")
chk("G7b 说明文案原文（群成员上传的内容；不下载、不解析）",
    "群文件是群成员上传的内容。打开后，主人可以让机器人列出某个群的文件、"
    "取下载链接；机器人不下载、也不解析文件内容" in SRC_UI, "")
chk("G7c GET 默认值与 POST 白名单登记（不是布尔 true 一律当关）",
    '"tool_group_files_enabled": data.get("tool_group_files_enabled") is True'
    in SRC_UI
    and 'if "tool_group_files_enabled" in body:' in SRC_UI
    and 'data["tool_group_files_enabled"] = bool(body["tool_group_files_enabled"])'
    in SRC_UI, "")
chk("G7d 「启用工具循环」工具清单补句",
    "群文件列表与下载链接（对应开关开着才出现）" in SRC_UI, "")
chk("G7e _smoke_settings_tabs.py 的 SAVE_KEYS 登记了键",
    '"tool_group_files_enabled": 1' in SRC_TABS, "")
chk("G7f _tool_available 有两个工具的分支",
    'if n in ("group_files", "group_file_url"):' in SRC_A
    and "return _group_files_tool_enabled()" in SRC_A, "")

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
