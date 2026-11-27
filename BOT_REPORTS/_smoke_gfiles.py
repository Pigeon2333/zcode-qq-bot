# -*- coding: utf-8 -*-
"""群文件只读接口冒烟：daemon.group_files / group_file_url 两方法与
/group-files、/group-file-url 两路由（2026-11-28 D3 前半）。

不连 ws、不启动机器人：_call_action 全程打桩（记录动作名与参数，按脚本
返回样例 data 或抛异常），零真实 WebSocket 调用；路由用 CtlHandler.__new__
离线驱动 do_POST（照 _smoke_batch1.py §4 手法），真 _check_token，
令牌用本测试现造的假值。
数据目录隔离：import qq_daemon 前把 QQBOT_DATA_DIR 指到一次性临时目录，
不读不写 ~/.zcode-qq-bot。跑完删临时目录。
样例群号一律 5550xx，file_id/folder_id 用 file_test_1 这类假值，
不出现任何真实的文件名、人名、群名。
"""
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join("plugins", "qq-bot", "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 数据目录隔离（必须在 import 前生效：DATA_DIR 等模块常量 import 时定死）
_SMOKE_OWN_DATA_DIR = tempfile.mkdtemp(prefix="qqbot_gfiles_iso_")
os.environ["QQBOT_DATA_DIR"] = _SMOKE_OWN_DATA_DIR

import qq_daemon as D  # noqa: E402

fails = []
n_pass = 0
LOGS = []
D.out = lambda line: LOGS.append(line)   # 失败诊断日志收进内存，输出保持干净


def chk(name, cond, extra=""):
    global n_pass
    print(("PASS " if cond else "FAIL ") + name
          + (" | " + str(extra)[:100] if extra else ""))
    if cond:
        n_pass += 1
    else:
        fails.append(name)


# ---------- 打桩 _call_action ----------
CALLS = []                                # 每项 (action, params)
STUB = {"ret": {}, "exc": None}


def _stub(action, params, timeout=None):
    CALLS.append((action, params))
    if STUB["exc"] is not None:
        raise STUB["exc"]
    return STUB["ret"]


def fresh():
    d = D.Daemon(ws_url="", token="", cfg={}, ctl_port=0)
    d._call_action = _stub
    return d


def no_call_ok(r):
    """校验失败样态：ok=False、detail 一句话、_call_action 没被调。"""
    return (r.get("ok") is False and isinstance(r.get("detail"), str)
            and bool(r["detail"]) and not CALLS)


SAMPLE_FILES = {
    "files": [{"file_id": "file_test_1", "file_name": "sample_report.txt",
               "busid": "10002", "size": 2048, "upload_time": 1700000000,
               "uploader_name": "tester_a"}],
    "folders": [{"folder_id": "folder_test_1", "folder_name": "docs_sample",
                 "total_file_count": 3}],
}
SAMPLE_URL = {"url": "https://example.test/dl/file_test_1"}
SECRET = "D:\\internal_secret\\leak_path.txt"   # 异常里携带的内部细节标记

# ---------- 1. group_files 参数校验 ----------
BAD_GIDS = [("", "空"), ("5550a1", "带字母"), ("５５５０１", "全角数字"),
            ("5550", "4位"), ("5550123456789", "13位"), (None, "None")]
for bad, label in BAD_GIDS:
    CALLS.clear()
    r = fresh().group_files(bad)
    chk("1a 群号%s拒绝且零调用" % label, no_call_ok(r), r.get("detail"))

CALLS.clear()
STUB["ret"] = SAMPLE_FILES
r = fresh().group_files(55501)
chk("1b int群号收下并以字符串传 NapCat(查根目录)",
    r.get("ok") is True
    and CALLS == [("get_group_root_files", {"group_id": "55501"})], CALLS)

CALLS.clear()
r = fresh().group_files("55501")
chk("1c 字符串群号缺省 folder_id 走根目录动作",
    r.get("ok") is True
    and CALLS == [("get_group_root_files", {"group_id": "55501"})], CALLS)

CALLS.clear()
r = fresh().group_files("55501", "f" * 201)
chk("1d folder_id 超长拒绝且零调用", no_call_ok(r), r.get("detail"))

CALLS.clear()
r = fresh().group_files("55501", "fold\ner_1")
chk("1e folder_id 含换行拒绝且零调用", no_call_ok(r), r.get("detail"))

CALLS.clear()
r = fresh().group_files("55501", 123)
chk("1f folder_id 非字符串拒绝且零调用", no_call_ok(r), r.get("detail"))

CALLS.clear()
r = fresh().group_files("55501", "folder_test_1")
chk("1g 非空 folder_id 走子目录动作(动作+参数)",
    r.get("ok") is True and CALLS == [("get_group_files_by_folder",
                                       {"group_id": "55501",
                                        "folder_id": "folder_test_1"})], CALLS)

# ---------- 1h-1n file_count：None 不带键 / 合法透传数字 / 越界·布尔·非整数拒绝 ----------
CALLS.clear()
r = fresh().group_files("55501", "", None)
chk("1h file_count=None 不带 file_count 键",
    r.get("ok") is True and len(CALLS) == 1
    and "file_count" not in CALLS[0][1], CALLS)

FC_BAD = [(0, "越界0"), (201, "越界201"), (True, "布尔True"),
          (False, "布尔False"), ("50", "字符串50"), (1.5, "浮点1.5")]
for fc, label in FC_BAD:
    CALLS.clear()
    r = fresh().group_files("55501", "", fc)
    chk("1i file_count %s → 拒绝且零调用" % label, no_call_ok(r), r.get("detail"))

CALLS.clear()
r = fresh().group_files("55501", "", 100)
chk("1j file_count=100 透传为数字",
    r.get("ok") is True and len(CALLS) == 1
    and CALLS[0][1].get("file_count") == 100, CALLS)

# ---------- 2. group_file_url 参数校验（NapCat 该动作只收群号+文件ID，无 busid） ----------
URL_BAD = [
    ("群号带字母", ("5550a1", "file_test_1")),
    ("file_id缺失", ("55501", None)),
    ("file_id超长", ("55501", "f" * 201)),
    ("file_id含换行", ("55501", "fi\nle_1")),
    ("file_id非字符串", ("55501", 123)),
]
for label, args in URL_BAD:
    CALLS.clear()
    r = fresh().group_file_url(*args)
    chk("2 %s拒绝且零调用" % label, no_call_ok(r), r.get("detail"))

CALLS.clear()
STUB["ret"] = SAMPLE_URL
r = fresh().group_file_url("55502", "file_test_1")
chk("2f 合法输入：参数恰好 group_id/file_id 两键(字符串群号、无 busid)",
    r.get("ok") is True and CALLS == [("get_group_file_url",
                                       {"group_id": "55502",
                                        "file_id": "file_test_1"})], CALLS)

# ---------- 3. 成功/失败路径 ----------
CALLS.clear()
STUB.update({"ret": SAMPLE_FILES, "exc": None})
d = fresh()
r = d.group_files("55501", "folder_test_1")
chk("3a 列表成功返回 data 原样(同一对象)",
    r == {"ok": True, "data": SAMPLE_FILES} and r["data"] is SAMPLE_FILES)

CALLS.clear()
STUB["ret"] = SAMPLE_URL
r = fresh().group_file_url("55502", "file_test_1")
chk("3b 链接成功返回 data 原样(同一对象)",
    r == {"ok": True, "data": SAMPLE_URL} and r["data"] is SAMPLE_URL
    and CALLS == [("get_group_file_url", {"group_id": "55502",
                                          "file_id": "file_test_1"})], CALLS)

EXC_CASES = [
    ("RuntimeError", RuntimeError("OneBot 动作失败 get_group_root_files: " + SECRET)),
    ("TimeoutError", TimeoutError("OneBot 动作超时: " + SECRET)),
    ("任意异常", ValueError("bad value " + SECRET)),
]
for label, exc in EXC_CASES:
    STUB.update({"ret": {}, "exc": exc})
    r = fresh().group_files("55501")
    det = r.get("detail") or ""
    chk("3c 列表 %s → 人话且不带内部细节" % label,
        r.get("ok") is False and isinstance(det, str) and bool(det)
        and SECRET not in det and "RuntimeError" not in det
        and "TimeoutError" not in det and "Traceback" not in det
        and "\n" not in det, det)
STUB["exc"] = EXC_CASES[0][1]
r = fresh().group_file_url("55502", "file_test_1")
det = r.get("detail") or ""
chk("3d 链接 RuntimeError → 人话且不带内部细节",
    r.get("ok") is False and bool(det) and SECRET not in det
    and "RuntimeError" not in det and "\n" not in det, det)
chk("3e 内部细节进了 daemon 日志(诊断不丢)",
    any("group_files_fail" in l and "internal_secret" in l for l in LOGS))
STUB["exc"] = None

# ---------- 4. 路由（离线驱动 do_POST，真 _check_token） ----------
TOK = "smoke-tok-gfiles-fake"            # 本测试现造的假令牌


def drive_route(path, body_bytes, token):
    """离线驱动一条 POST 路由：真 _check_token，两个方法替换为记录器。"""
    d = fresh()
    d.ctl_token = TOK
    d.method_calls = []
    d.group_files = lambda *a: (d.method_calls.append(("group_files", a))
                                or {"ok": True, "data": SAMPLE_FILES})
    d.group_file_url = lambda *a: (d.method_calls.append(("group_file_url", a))
                                   or {"ok": True, "data": SAMPLE_URL})
    h = D.CtlHandler.__new__(D.CtlHandler)
    h.server = type("Srv", (), {"daemon": d})()   # CtlHandler.daemon 只读属性
    h.path = path
    h.headers = {"Content-Length": str(len(body_bytes)),
                 "X-QQBot-Token": token}
    h.rfile = io.BytesIO(body_bytes)
    replies = []
    h._reply = lambda code, obj: replies.append((code, obj))
    h.do_POST()
    return d, replies


GID_BODY = json.dumps({"group_id": "55501"}).encode("utf-8")

d, rep = drive_route("/group-files", GID_BODY, token="")
chk("4a /group-files 无令牌 401 且方法未调",
    bool(rep) and rep[0][0] == 401 and not d.method_calls, rep[:1])

d, rep = drive_route("/group-files", GID_BODY, token="wrong-token-xyz")
chk("4b /group-files 错令牌 401 且方法未调",
    bool(rep) and rep[0][0] == 401 and not d.method_calls, rep[:1])

d, rep = drive_route("/group-files", GID_BODY, token=TOK)
chk("4c /group-files 对令牌 200 回包=方法返回 且 folder_id/file_count 缺省",
    bool(rep) and rep[0] == (200, {"ok": True, "data": SAMPLE_FILES})
    and d.method_calls == [("group_files", ("55501", "", None))], rep[:1])

d, rep = drive_route("/group-files",
                     json.dumps({"group_id": "55501",
                                 "folder_id": "folder_test_1"}).encode("utf-8"),
                     token=TOK)
chk("4d /group-files folder_id 透传方法",
    rep[0][0] == 200 and d.method_calls == [("group_files",
                                             ("55501", "folder_test_1", None))],
    (rep[:1], d.method_calls))

d, rep = drive_route("/group-files",
                     json.dumps({"group_id": "55501",
                                 "file_count": 100}).encode("utf-8"), token=TOK)
chk("4e /group-files file_count 透传方法",
    rep[0][0] == 200 and d.method_calls == [("group_files",
                                             ("55501", "", 100))],
    (rep[:1], d.method_calls))

d, rep = drive_route("/group-files",
                     json.dumps({"folder_id": "folder_test_1"}).encode("utf-8"),
                     token=TOK)
chk("4f /group-files 缺 group_id → 400 且方法未调",
    bool(rep) and rep[0][0] == 400 and not d.method_calls, rep[:1])

d, rep = drive_route("/group-files", json.dumps([1, 2]).encode("utf-8"), token=TOK)
chk("4g /group-files 体不是 dict → 400 且方法未调",
    bool(rep) and rep[0][0] == 400 and not d.method_calls, rep[:1])

UBODY = json.dumps({"group_id": "55502",
                    "file_id": "file_test_1"}).encode("utf-8")

d, rep = drive_route("/group-file-url", UBODY, token="")
chk("4h /group-file-url 无令牌 401 且方法未调",
    bool(rep) and rep[0][0] == 401 and not d.method_calls, rep[:1])

d, rep = drive_route("/group-file-url", UBODY, token="wrong-token-xyz")
chk("4i /group-file-url 错令牌 401 且方法未调",
    bool(rep) and rep[0][0] == 401 and not d.method_calls, rep[:1])

d, rep = drive_route("/group-file-url", UBODY, token=TOK)
chk("4j /group-file-url 对令牌 200 回包=方法返回 且两参原样",
    bool(rep) and rep[0] == (200, {"ok": True, "data": SAMPLE_URL})
    and d.method_calls == [("group_file_url",
                            ("55502", "file_test_1"))], rep[:1])

d, rep = drive_route("/group-file-url",
                     json.dumps({"group_id": "55502", "file_id": "file_test_1",
                                 "busid": "10002"}).encode("utf-8"), token=TOK)
chk("4k /group-file-url 多传 busid 不算错(忽略，仍两参)",
    bool(rep) and rep[0][0] == 200
    and d.method_calls == [("group_file_url",
                            ("55502", "file_test_1"))], (rep[:1], d.method_calls))

d, rep = drive_route("/group-file-url",
                     json.dumps({"group_id": "55502",
                                 "busid": "10002"}).encode("utf-8"), token=TOK)
chk("4l /group-file-url 缺 file_id → 400 且方法未调",
    bool(rep) and rep[0][0] == 400 and not d.method_calls, rep[:1])

d, rep = drive_route("/group-file-url", b'"just-a-string"', token=TOK)
chk("4m /group-file-url 体不是 dict → 400 且方法未调",
    bool(rep) and rep[0][0] == 400 and not d.method_calls, rep[:1])

# 两条路由真的存在（非 404）：4c/4j 已拿到 200，这里补一个对照
d, rep = drive_route("/group-files-nope", GID_BODY, token=TOK)
chk("4n 对照未知路径 404", bool(rep) and rep[0][0] == 404, rep[:1])

print()
print("%d 项通过，%d 项失败" % (n_pass, len(fails)))
shutil.rmtree(_SMOKE_OWN_DATA_DIR, ignore_errors=True)
if os.path.isdir(_SMOKE_OWN_DATA_DIR):
    print("注意：隔离目录没能删净 %s" % _SMOKE_OWN_DATA_DIR)
else:
    print("隔离目录已删净（os.path.isdir=False）：%s" % _SMOKE_OWN_DATA_DIR)
sys.exit(1 if fails else 0)
