# -*- coding: utf-8 -*-
"""冒烟：审计 S15 协议端鉴权——ws_token 单一令牌源。

全离线：QQBOT_DATA_DIR 指到系统临时目录（qq_common 在 import 时读它），
QQNT 用带假 QQ.exe 的临时目录打桩；不连 ws、不启动任何服务、不碰生产目录。
覆盖：令牌生成/读取/空文件重生、NapCat 配置生成的 token、NapCat 采纳自愈
（非空采纳不变、空令牌不采纳）、LLOneBot 新建与旧配置两条路径的 token、
令牌不进日志/返回消息。"""
import json
import os
import pathlib
import re
import secrets
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugins" / "qq-bot" / "scripts"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="qqbot-smoke-wstoken-"))
os.environ["QQBOT_DATA_DIR"] = str(_TMP / "data")   # 必须在 import qq_common 前
import qq_common as QC  # noqa: E402
import qq_services as S  # noqa: E402

fails = []


def chk(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def fresh(name):
    """每个场景一套全新隔离目录，并把 qq_common 的数据根指过去。"""
    d = _TMP / name
    d.mkdir(parents=True, exist_ok=True)
    QC.DATA_DIR = str(d)
    QC.DATA_ROOT = d
    return d


def fake_qqnt(name):
    d = _TMP / name
    (d / "config").mkdir(parents=True, exist_ok=True)
    (d / "QQ.exe").write_bytes(b"")   # qqnt_dir_resolved 只认有 QQ.exe 的目录
    return d


def napcat_cfg(ws_port, qqnt):
    return {"bot_qq": "10001", "napcat_ws_port": ws_port, "qqnt_dir": str(qqnt)}


def read_onebot_token(path):
    data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    return str(data["network"]["websocketServers"][0].get("token", ""))


# --- 场景 1：令牌文件不存在 → 生成并写盘；两次调用同一值（第二次读文件）---
d1 = fresh("s1")
t1 = S.ensure_ws_token()
t2 = S.ensure_ws_token()
f1 = d1 / "ws_token"
chk("S1 文件缺失生成非空令牌", bool(t1) and f1.is_file())
chk("S1 两次调用返回同一值", t1 == t2)
chk("S1 文件内容与返回一致", f1.read_text(encoding="utf-8").strip() == t1)
chk("S1 格式 32 hex（同 ui_token）", bool(re.fullmatch(r"[0-9a-f]{32}", t1)))

# --- 场景 2：文件存在且有内容 → 原样返回，不改写 ---
d2 = fresh("s2")
seed = "seed" + secrets.token_hex(12)          # 运行时生成，不写凭据字面量
(d2 / "ws_token").write_text(seed, encoding="utf-8")
got = S.ensure_ws_token()
on_disk = (d2 / "ws_token").read_text(encoding="utf-8")
chk("S2 返回文件里的值", got == seed)
chk("S2 文件未被改写", on_disk == seed)

# --- 场景 3：文件存在但是空文件 → 当不存在：覆盖写入新令牌 ---
d3 = fresh("s3")
(d3 / "ws_token").write_text("", encoding="utf-8")
t3 = S.ensure_ws_token()
chk("S3 空文件视为缺失返回非空", bool(t3))
chk("S3 空文件被覆盖写入", (d3 / "ws_token").read_text(encoding="utf-8") == t3)

# --- 场景 4：缺省路径=数据目录 ws_token（唯一令牌源落点） ---
d4 = fresh("s4")
t4 = S.ensure_ws_token()
chk("S4 缺省路径落在 DATA_ROOT/ws_token",
    (d4 / "ws_token").is_file()
    and (d4 / "ws_token").read_text(encoding="utf-8") == t4)

# --- 场景 5：生成 NapCat 的 WS 配置 → token 字段=令牌文件值，非空 ---
d5 = fresh("s5")
q5 = fake_qqnt("qqnt5")
tok5 = S.ensure_ws_token()
ok5, msg5 = S.ensure_napcat_config(napcat_cfg(3001, q5))
cfgp = q5 / "config" / "onebot11_10001.json"
chk("S5 配置生成成功", ok5 and cfgp.is_file())
chk("S5 NapCat token=令牌文件值且非空",
    read_onebot_token(cfgp) == tok5 and read_onebot_token(cfgp) != "")
chk("S5 返回消息不含令牌", tok5 not in msg5)

# --- 场景 6：采纳自愈（行为不变）：NapCat 换了非空令牌 → 采纳写回文件 ---
d6 = fresh("s6")
q6 = fake_qqnt("qqnt6")
old6 = S.ensure_ws_token()
theirs6 = "nc" + secrets.token_hex(8)
S.ensure_napcat_config(napcat_cfg(3001, q6))   # 先落一份旧配置（token=old6）
p6 = q6 / "config" / "onebot11_10001.json"
conf6 = json.loads(p6.read_text(encoding="utf-8"))
conf6["network"]["websocketServers"][0]["token"] = theirs6
(q6 / "config" / "onebot11_10001.json").write_text(
    json.dumps(conf6, ensure_ascii=False), encoding="utf-8")
ok6, msg6 = S.ensure_napcat_config(napcat_cfg(3001, q6))
chk("S6 非空新令牌被采纳", ok6 and (d6 / "ws_token").read_text(
    encoding="utf-8") == theirs6 and old6 != theirs6)
chk("S6 自愈消息与旧版一致",
    msg6 == "NapCat 已更换 token，已自动同步（机器人将自动重连）")

# --- 场景 7：配置已存在且令牌一致 → 「已存在」，文件不动 ---
d7 = fresh("s7")
q7 = fake_qqnt("qqnt7")
t7 = S.ensure_ws_token()
S.ensure_napcat_config(napcat_cfg(3001, q7))
ok7, msg7 = S.ensure_napcat_config(napcat_cfg(3001, q7))
chk("S7 已存在不重复写", ok7 and msg7 == "NapCat 配置已存在"
    and (d7 / "ws_token").read_text(encoding="utf-8") == t7)

# --- 场景 8：NapCat 侧令牌为空 → 不采纳（唯一令牌源不回退为空） ---
d8 = fresh("s8")
q8 = fake_qqnt("qqnt8")
t8 = S.ensure_ws_token()
S.ensure_napcat_config(napcat_cfg(3001, q8))
conf8 = json.loads((q8 / "config" / "onebot11_10001.json").read_text(
    encoding="utf-8"))
conf8["network"]["websocketServers"][0]["token"] = ""
(q8 / "config" / "onebot11_10001.json").write_text(
    json.dumps(conf8, ensure_ascii=False), encoding="utf-8")
ok8, msg8 = S.ensure_napcat_config(napcat_cfg(3001, q8))
chk("S8 空令牌不被采纳，文件保持非空",
    ok8 and (d8 / "ws_token").read_text(encoding="utf-8") == t8 and t8 != "")

# --- 场景 9：生成 LLOneBot 配置（新建与旧配置两条路径）token=令牌文件值 ---
d9 = fresh("s9")
l9 = _TMP / "llob9"
tok9 = S.ensure_ws_token()
cfg9 = {"llonebot_dir": str(l9), "llonebot_ws_port": 3002}
S.ensure_llonebot_config(cfg9)
c9 = json.loads((l9 / "config.json").read_text(encoding="utf-8"))
new_tok = str(c9["ob11"]["connect"][0].get("token", ""))
chk("S9 新建配置 token=令牌文件值且非空", new_tok == tok9 and new_tok != "")
# 旧格式（token=""）配置走 update 分支同样补上真令牌
c9["ob11"]["connect"][0]["token"] = ""
(l9 / "config.json").write_text(json.dumps(c9, ensure_ascii=False),
                                encoding="utf-8")
S.ensure_llonebot_config(cfg9)
c9b = json.loads((l9 / "config.json").read_text(encoding="utf-8"))
chk("S9 旧配置 update 分支 token 补真值",
    str(c9b["ob11"]["connect"][0].get("token", "")) == tok9)

# --- 场景 10：NapCat/LLOneBot 两边同源（同一数据根下同一个令牌文件） ---
d10 = fresh("s10")
q10 = fake_qqnt("qqnt10")
tok10 = S.ensure_ws_token()
S.ensure_napcat_config(napcat_cfg(3001, q10))
S.ensure_llonebot_config({"llonebot_dir": str(_TMP / "llob10"),
                          "llonebot_ws_port": 3002})
llob_tok10 = str(json.loads((_TMP / "llob10" / "config.json").read_text(
    encoding="utf-8"))["ob11"]["connect"][0]["token"])
chk("S10 两边配置同源同令牌",
    read_onebot_token(q10 / "config" / "onebot11_10001.json") == tok10
    and llob_tok10 == tok10)

# --- 场景 11：改动过的函数不把令牌写进日志/打印/返回消息 ---
import inspect  # noqa: E402
leaky = []
for fn in (S.ensure_ws_token, S.ensure_daemon, S.ensure_napcat_config,
           S.ensure_llonebot_config):
    src = inspect.getsource(fn)
    if re.search(r"\bprint\(|\bout\(|logging\.", src):
        leaky.append(fn.__name__)
chk("S11 改动函数无 print/out/logging 语句", not leaky)
chk("S11 全部返回消息不含令牌明文",
    all(t not in m for t, m in ((tok5, msg5), (theirs6, msg6),
                                (t7, msg7), (t8, msg8))))

# --- 清理：只删本脚本建的前缀临时目录 ---
shutil.rmtree(_TMP, ignore_errors=True)
print()
print("%d 项通过，%d 项失败" % (11 - len(fails), len(fails)))
sys.exit(1 if fails else 0)
