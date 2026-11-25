# -*- coding: utf-8 -*-
"""管理台安全六项冒烟（离线：不连任何服务端口、不发真实网络请求、不写用户数据目录）。

覆盖（对应 2026-11-25 施工单 B2 六项）：
  1 密钥不回传页面：GET /api/app-config（update_token→*_set、api_proxy 打码）、
    GET /api/voice/meta（三个 Key→*_set、custom_base 打码）、保存时空串/占位符/
    没带键保持原值、新值照写、打码串提交不覆盖、页面脚本不再回填原值。
  2 表情包池名：路径分隔符/Windows 保留字符/../控制字符/首尾点空格 → 400；
    汉字+emoji+空格等合法名 → 200。
  3 请求体与令牌：超大 413、非对象/坏 JSON 400、正常对象不受影响；
    ui_token 文件不存在→生成、OSError→不覆盖往上抛、损坏→重生成；
    _authed 恒时比较。
  4 插件：停用插件 page.js 前后端都不出、启用不变；两个安装入口先弹 danger
    确认框（更新多一句「这是更新」）；安装 warning 经 toast + plnote 显示。
  5 出站请求钉住地址：连的是校验时的 IP；重定向逐跳重新校验/钉住；
    解析到非公网拒绝；https 的 server_hostname 与 Host 头都用原域名。
  6 设备操控新开关 device_ctrl_open_allow_exe：缺键读出 False、保存后 True。

隔离方式：import 被测模块前把 QQBOT_DATA_DIR 指到 tempfile.mkdtemp()；服务端
处理器用 UiHandler.__new__ 直接调（不起服务、不连端口）；出站请求全程打桩
（socket.create_connection / getaddrinfo / ssl 换成记录桩，响应按队列回放，
绝不出网）；页面脚本在 node 沙箱里跑（照 _smoke_uifix2 的做法）。
跑完删临时目录。

用法：python BOT_REPORTS/_smoke_uisec.py
"""
import contextlib
import io
import json
import os
import pathlib
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import urllib.parse

_TMP = tempfile.mkdtemp(prefix="qqbot_uisec_iso_")
os.environ["QQBOT_DATA_DIR"] = _TMP
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "plugins" / "qq-bot" / "scripts"
SRC = SCRIPTS / "qq_admin_ui.py"
sys.path.insert(0, str(SCRIPTS))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import qq_admin_ui as U  # noqa: E402  （import 前 QQBOT_DATA_DIR 已指向临时目录）

fails = []


def chk(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (" | " + str(extra)[:200] if extra else ""))
    if not cond:
        fails.append(name)


_MISSING = object()


@contextlib.contextmanager
def patched(obj, **names):
    """临时替换模块属性（缺席属性用哨兵记录，退出时补删）。"""
    old = {k: getattr(obj, k, _MISSING) for k in names}
    for k, v in names.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is _MISSING:
                try:
                    delattr(obj, k)
                except AttributeError:
                    pass
            else:
                setattr(obj, k, v)


def ui_call(method, path, bodyq=None, headers=None, raw=None):
    """进程内直调 UiHandler（照 _smoke_arch 18 组的写法），不打桩的回调全穿透。
    raw 给字节串时按原样当请求体（headers 可覆写，供 413 等用例伪造头）。"""
    h = U.UiHandler.__new__(U.UiHandler)
    h.path = path
    if raw is not None:
        data = raw
    else:
        data = json.dumps(bodyq).encode("utf-8") if bodyq is not None else b""
    hh = {"Content-Length": str(len(data))}
    if headers:
        hh.update(headers)
    h.headers = hh
    h.rfile = io.BytesIO(data)
    replies = []
    h._reply = lambda code, obj: replies.append((code, obj))
    h._authed = lambda u, q: True
    getattr(h, method)()
    return replies


def disk_app_cfg():
    return U.C.load_app_config()


# ----------------------------------------------------------- 1 密钥不回传页面
T_GH = "ghp-TEST-NOT-REAL-000"
T_ZK = "sk-TEST-NOT-REAL"
T_SF = "sf-TEST-NOT-REAL"
T_CK = "ck-TEST-NOT-REAL"
T_PROXY = "http://alice:secret99@proxy.corp:8080"
T_BASE = "https://alice:pw123@api.example.com/v1/"
M_PROXY = "http://***@proxy.corp:8080"
M_BASE = "https://***@api.example.com/v1/"


def t_app_config_get(tmp):
    U.C.save_app_config({"update_token": T_GH, "api_proxy": T_PROXY})
    code, b = ui_call("do_GET", "/api/app-config")[0]
    blob = json.dumps(b, ensure_ascii=False)
    chk("1a app-config GET：update_token 原值不回、只回 update_token_set=True",
        code == 200 and "update_token" not in b and b.get("update_token_set") is True
        and T_GH not in blob, {k: b.get(k) for k in ("update_token", "update_token_set")})
    chk("1b api_proxy 带账号密码：GET 返回打码串、原凭据不出现",
        b.get("api_proxy") == M_PROXY and "alice:secret99" not in blob, b.get("api_proxy"))

    U.C.save_app_config({"update_token": "", "api_proxy": ""})
    _, b2 = ui_call("do_GET", "/api/app-config")[0]
    chk("1c 未配置时：update_token_set=False、api_proxy 原样为空",
        b2.get("update_token_set") is False and b2.get("api_proxy") == "",
        (b2.get("update_token_set"), b2.get("api_proxy")))


def t_app_config_post(tmp):
    U.C.save_app_config({"update_token": T_GH, "api_proxy": T_PROXY})

    r = ui_call("do_POST", "/api/app-config",
                {"update_token": "***", "api_proxy": M_PROXY})
    d = disk_app_cfg()
    chk("1d 保存：占位符 *** 与打码串提交 → 磁盘原值不变",
        r[0][0] == 200 and d.get("update_token") == T_GH and d.get("api_proxy") == T_PROXY,
        (d.get("update_token"), d.get("api_proxy")))
    blob = json.dumps(r[0][1], ensure_ascii=False)
    chk("1d' 保存回包同样不回原值（update_token_set / 打码 api_proxy）",
        "update_token" not in r[0][1] and r[0][1].get("update_token_set") is True
        and r[0][1].get("api_proxy") == M_PROXY and T_GH not in blob, r[0][1].get("api_proxy"))

    ui_call("do_POST", "/api/app-config", {"note": "没带键"})
    d = disk_app_cfg()
    chk("1e 没带键：原值不变",
        d.get("update_token") == T_GH and d.get("api_proxy") == T_PROXY, "")

    r = ui_call("do_POST", "/api/app-config",
                {"update_token": "ghp-TEST-NEW", "api_proxy": "1.2.3.4:9090"})
    d = disk_app_cfg()
    chk("1f 新值照写：token 直写、代理无 scheme 补 http://",
        r[0][0] == 200 and d.get("update_token") == "ghp-TEST-NEW"
        and d.get("api_proxy") == "http://1.2.3.4:9090", (d.get("update_token"), d.get("api_proxy")))

    r = ui_call("do_POST", "/api/app-config", {"update_token": "", "api_proxy": ""})
    d = disk_app_cfg()
    chk("1g 空串语义不变：update_token 清空、api_proxy 清空（留空=直连）",
        r[0][0] == 200 and d.get("update_token") == "" and d.get("api_proxy") == "",
        (d.get("update_token"), d.get("api_proxy")))
    U.C.save_app_config({"update_token": T_GH, "api_proxy": T_PROXY})  # 还原


def _set_voice(**over):
    import qq_tts as T
    base = {"provider": "edge", "zhipu_key": T_ZK, "sf_key": T_SF,
            "custom_key": T_CK, "custom_base": T_BASE}
    base.update(over)
    T.save_voice_cfg(base)
    return T


def t_voice_meta_get(tmp):
    T = _set_voice()
    with patched(sys.modules["qq_tts"], gsv_status=lambda cfg: {"running": False}):
        code, b = ui_call("do_GET", "/api/voice/meta")[0]
    cfg = b.get("config") or {}
    blob = json.dumps(b, ensure_ascii=False)
    chk("1h voice/meta GET：三个 Key 原值不回、只回 *_set=True",
        code == 200 and "zhipu_key" not in cfg and "sf_key" not in cfg and "custom_key" not in cfg
        and cfg.get("zhipu_key_set") is True and cfg.get("sf_key_set") is True
        and cfg.get("custom_key_set") is True
        and T_ZK not in blob and T_SF not in blob and T_CK not in blob,
        {k: cfg.get(k) for k in ("zhipu_key", "zhipu_key_set", "sf_key_set", "custom_key_set")})
    chk("1i custom_base 带账号密码：GET 返回打码串",
        cfg.get("custom_base") == M_BASE and "alice:pw123" not in blob, cfg.get("custom_base"))

    T.save_voice_cfg({"provider": "edge"})
    with patched(sys.modules["qq_tts"], gsv_status=lambda cfg: {"running": False}):
        _, b2 = ui_call("do_GET", "/api/voice/meta")[0]
    cfg2 = b2.get("config") or {}
    chk("1j 全部未配置时：*_set=False、custom_base 为空",
        cfg2.get("zhipu_key_set") is False and cfg2.get("sf_key_set") is False
        and cfg2.get("custom_key_set") is False and cfg2.get("custom_base") == "",
        {k: cfg2.get(k) for k in ("zhipu_key_set", "sf_key_set", "custom_key_set", "custom_base")})
    _set_voice()  # 还原供后续用例使用


def t_voice_config_post(tmp):
    T = _set_voice()

    r = ui_call("do_POST", "/api/voice/config", {"voice": {"enabled": True, "provider": "edge"}})
    d = T.voice_cfg()
    chk("1k voice/config：没带键 → 三个 Key 原值不变（custom_base 是表单整体字段，随老语义清空）",
        r[0][0] == 200 and d.get("zhipu_key") == T_ZK and d.get("sf_key") == T_SF
        and d.get("custom_key") == T_CK,
        (d.get("zhipu_key"), d.get("sf_key"), d.get("custom_key")))

    _set_voice()  # 重置（1k 按既有语义清掉了 custom_base）
    r = ui_call("do_POST", "/api/voice/config",
                {"voice": {"zhipu_key": "***", "sf_key": "***", "custom_key": "***",
                           "custom_base": M_BASE}})
    d = T.voice_cfg()
    chk("1l 占位符 *** 与打码串提交 → 原值不变",
        d.get("zhipu_key") == T_ZK and d.get("sf_key") == T_SF and d.get("custom_key") == T_CK
        and d.get("custom_base") == T_BASE, d.get("custom_base"))

    r = ui_call("do_POST", "/api/voice/config",
                {"voice": {"zhipu_key": "", "sf_key": "", "custom_key": ""}})
    d = T.voice_cfg()
    chk("1m 空串提交 Key → 原值不变（页面「已配置，留空保持不变」的落点）",
        d.get("zhipu_key") == T_ZK and d.get("sf_key") == T_SF and d.get("custom_key") == T_CK, "")

    r = ui_call("do_POST", "/api/voice/config", {"voice": {"custom_base": ""}})
    d = T.voice_cfg()
    chk("1n custom_base 空串=清空（该字段既有语义不变）",
        d.get("custom_base") == "", d.get("custom_base"))

    r = ui_call("do_POST", "/api/voice/config",
                {"voice": {"zhipu_key": "sk-TEST-NEW", "sf_key": "sf-NEW", "custom_key": "ck-NEW",
                           "custom_base": "https://api2.example.com/v1/"}})
    d = T.voice_cfg()
    rc = r[0][1].get("config") or {}
    chk("1o 新值照写",
        d.get("zhipu_key") == "sk-TEST-NEW" and d.get("sf_key") == "sf-NEW"
        and d.get("custom_key") == "ck-NEW"
        and d.get("custom_base") == "https://api2.example.com/v1/", "")
    chk("1o' 保存回包 config 不回 Key 原值、*_set=True",
        "zhipu_key" not in rc and rc.get("zhipu_key_set") is True
        and "sk-TEST-NEW" not in json.dumps(r[0][1], ensure_ascii=False), "")

    ui_call("do_POST", "/api/voice/config", {"voice": {}})
    d = T.voice_cfg()
    chk("1p custom_base 没带键 → 清成空串（该字段既有语义：随表单整体保存）",
        d.get("custom_base") == "", d.get("custom_base"))
    _set_voice()  # 还原供后续用例使用

    # 1q 显式清除标志：<键名>_clear=1 → 落盘空串、回包 *_set=False
    r = ui_call("do_POST", "/api/voice/config",
                {"voice": {"zhipu_key_clear": 1, "sf_key_clear": 1, "custom_key_clear": 1}})
    d = T.voice_cfg()
    rc = r[0][1].get("config") or {}
    chk("1q 清除标志为真 → 三个 Key 落盘空串、回包 *_set=False",
        r[0][0] == 200 and d.get("zhipu_key") == "" and d.get("sf_key") == ""
        and d.get("custom_key") == ""
        and rc.get("zhipu_key_set") is False and rc.get("sf_key_set") is False
        and rc.get("custom_key_set") is False,
        {k: d.get(k) for k in ("zhipu_key", "sf_key", "custom_key")})
    chk("1q' 清除标志不落盘（voice 配置里没有 *_clear 键）",
        "zhipu_key_clear" not in d and "sf_key_clear" not in d
        and "custom_key_clear" not in d,
        [k for k in d if k.endswith("_clear")])
    _set_voice()
    r = ui_call("do_POST", "/api/voice/config",
                {"voice": {"zhipu_key": "sk-TEST-NEW2", "custom_base": T_BASE}})
    d = T.voice_cfg()
    chk("1q'' 不带清除标志的保存 → 原值仍不变、新值照写",
        d.get("sf_key") == T_SF and d.get("custom_key") == T_CK
        and d.get("zhipu_key") == "sk-TEST-NEW2", "")
    _set_voice()  # 还原供后续用例使用


# ----------------------------------------------------------- 2 表情包池名
def t_pool_names(tmp):
    good = "汉字 Emoji👍 空格 ok-1_"

    def up(pool):
        return ui_call("do_POST", "/api/stickers/upload",
                       {"pool": pool, "filename": "x.jpg", "data_b64": "aGVsbG8="})

    bad_all = ("a/b", "a\\b", "a:b", "a*b", 'a"b', "a<b", "a|b", "a?b", "a>b",
               "..", "a..b", "..x", "x..", "x.", ".x",
               "a\nb", "a\x01b", "a\x7fb", "_x", "___", "x" * 41)
    ok = True
    for pool in bad_all:
        r = up(pool)
        if not (r and r[0][0] == 400 and "库名非法" in str(r[0][1].get("error"))):
            chk("2a 非法池名应 400：%r" % pool, False, r[:1])
            ok = False
            break
    if ok:
        chk("2a 非法池名（/ \\ : * ? \" < > | ..  控制字符 首尾点空格 超长 _开头）→ 全部 400", True,
            str(up("a/b")[0][1].get("error")))
    chk("2a' 单独的「.」也是非法名", up(".")[0][0] == 400)

    r = up(good)
    pdir = pathlib.Path(U.STICKERS_ROOT) / good
    chk("2b 合法池名（汉字+emoji+空格+短横+下划线）→ 200 且子目录落在临时数据目录",
        r[0][0] == 200 and r[0][1].get("ok") is True and pdir.is_dir(), (r[:1], str(pdir)))
    chk("2c 没碰生产数据目录", str(pdir).startswith(str(pathlib.Path(_TMP).resolve())), str(pdir))

    chk("2d _global 保留池不受影响", up("_global")[0][0] == 200)
    chk("2e 40 字整长合法名放行", up("长" * 40)[0][0] == 200)

    # 2f sync-qq：池名来自请求体（远端只回图片 URL），与 upload 同一把尺
    r = ui_call("do_POST", "/api/stickers/sync-qq", {"pool": "a/b"})
    chk("2f sync-qq 非法池名 → 400 同文案",
        r[0][0] == 400 and "库名非法" in str(r[0][1].get("error")), r[:1])
    r = ui_call("do_POST", "/api/stickers/sync-qq", {"pool": ".."})
    chk("2f' sync-qq 的 .. 与单独点同样拒绝",
        r[0][0] == 400 and "库名非法" in str(r[0][1].get("error")), r[:1])
    # 合法池名：打桩 _napcat_action（绝不连 daemon/NapCat），空列表走完真实同步路径
    with patched(U, _napcat_action=lambda action, params, timeout_sec=25:
                 {"retcode": 1, "data": []}):
        r = ui_call("do_POST", "/api/stickers/sync-qq", {"pool": "同步入库池"})
    pdir2 = pathlib.Path(U.STICKERS_ROOT) / "同步入库池"
    chk("2f'' sync-qq 合法池名 → 200，子目录落在临时数据目录",
        r[0][0] == 200 and r[0][1].get("ok") is True and r[0][1].get("fetched") == 0
        and pdir2.is_dir(), (r[:1], str(pdir2)))


# ----------------------------------------------------------- 3 请求体与令牌
def t_body(tmp):
    r = ui_call("do_POST", "/api/app-config", {},
                headers={"Content-Length": str(U._MAX_BODY_BYTES + 1)})
    chk("3a Content-Length 超上限 → 413",
        r[0][0] == 413 and "上限" in str(r[0][1].get("error")), r[:1])

    bad = True
    for label, raw in (("JSON 数组", b"[]"), ("JSON 字符串", b'"abc"'), ("坏 JSON", b'{"a":')):
        r = ui_call("do_POST", "/api/app-config", raw=raw,
                    headers={"Content-Length": str(len(raw))})
        if not (r[0][0] == 400 and r[0][1].get("ok") is False):
            chk("3b %s → 400" % label, False, r[:1])
            bad = False
    if bad:
        chk("3b 非对象与坏 JSON → 400（原来静默当空对象）", True)

    r = ui_call("do_POST", "/api/stickers/fav-toggle", {"enabled": False})
    chk("3c 正常对象不受影响（fav-toggle 200）",
        r[0][0] == 200 and r[0][1].get("ok") is True and r[0][1].get("fav_enabled") is False, r[:1])

    r = ui_call("do_POST", "/api/stickers/fav-toggle", raw=b"")
    chk("3d 空请求体照旧当空对象（无 body 的 POST 不坏）",
        bool(r) and r[0][0] == 200 and r[0][1].get("fav_enabled") is True, r[:1])


def t_ui_token(tmp):
    tp = pathlib.Path(U.DATA_DIR) / "ui_token"
    with patched(U, UI_TOKEN_PATH=str(tp)):
        tp.unlink(missing_ok=True)
        t1 = U.ui_token()
        chk("3e 文件不存在 → 生成长度≥24 的令牌并落盘",
            len(t1) >= 24 and tp.is_file() and tp.read_text(encoding="utf-8") == t1, len(t1))
        t2 = U.ui_token()
        chk("3f 文件完好 → 原值复用（不重写）", t2 == t1, "")
        tp.write_text("short", encoding="utf-8")
        t3 = U.ui_token()
        chk("3g 内容损坏（不足 24）→ 重新生成并覆盖",
            len(t3) >= 24 and t3 != "short" and tp.read_text(encoding="utf-8") == t3, "")
        as_dir = pathlib.Path(U.DATA_DIR) / "ui_token_dir"
        as_dir.mkdir(exist_ok=True)
        with patched(U, UI_TOKEN_PATH=str(as_dir)):
            try:
                U.ui_token()
                raised = None
            except OSError as e:
                raised = e
        chk("3h 读取抛 OSError（非「不存在」）→ 不覆盖、往上抛",
            isinstance(raised, OSError) and not isinstance(raised, FileNotFoundError)
            and as_dir.is_dir(), repr(raised))


def t_authed(tmp):
    tok = U.ui_token()
    h = U.UiHandler.__new__(U.UiHandler)
    h.headers = {"X-UI-Token": tok}
    ok1 = h._authed(urllib.parse.urlsplit("/x"), {})
    h.headers = {"X-UI-Token": "0" * len(tok)}
    ok2 = h._authed(urllib.parse.urlsplit("/x"), {})
    h.headers = {}
    ok3 = h._authed(urllib.parse.urlsplit("/x"), {"tk": tok})
    chk("3i _authed：头/参数对令牌通过、错令牌拒绝",
        ok1 is True and ok2 is False and ok3 is True, (ok1, ok2, ok3))
    import inspect
    src = inspect.getsource(U.UiHandler._authed)
    chk("3j _authed 用 hmac.compare_digest 恒时比较", "hmac.compare_digest" in src, src[:120])


# ----------------------------------------------------------- 4 插件 pagejs / 安装确认
def _mk_plugin(name, default_enabled):
    pdir = pathlib.Path(U.DATA_DIR) / "plugins" / name
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "manifest.json").write_text(json.dumps({
        "name": name, "title": name, "version": "1.0",
        "default_enabled": default_enabled,
        "page": {"js": name + "Page"},
    }, ensure_ascii=False), encoding="utf-8")
    (pdir / "page.js").write_text("function %sPage(){return '<b>x</b>'}" % name,
                                  encoding="utf-8")
    return pdir


def t_pagejs_server(tmp):
    _mk_plugin("uisec_off", False)
    _mk_plugin("uisec_on", True)
    code, b = ui_call("do_GET", "/api/plugins/pagejs?name=uisec_off")[0]
    chk("4a 服务端：停用插件 page.js 返回空内容",
        code == 200 and b.get("ok") is True and b.get("js") == "" and b.get("disabled") is True, b)
    code, b = ui_call("do_GET", "/api/plugins/pagejs?name=uisec_on")[0]
    chk("4b 服务端：启用插件照常返回脚本内容",
        code == 200 and b.get("ok") is True and "uisec_onPage" in str(b.get("js")), str(b)[:80])
    code, b = ui_call("do_GET", "/api/plugins/pagejs?name=uisec_no")[0]
    chk("4b' 不存在的插件照旧 404", code == 404, code)


def t_page_js(work):
    if not shutil.which("node"):
        chk("页面脚本 node 沙箱（需要 node）", False, "找不到 node")
        return
    script = re.search(r"<script>(.*?)</script>", SRC.read_text(encoding="utf-8"), re.S).group(1)

    def grab_line(pattern):
        m = re.search(pattern, script, re.M)
        if not m:
            raise SystemExit("页面脚本里找不到：%s" % pattern)
        return m.group(0)

    def grab_block(start_pat, end_pat):
        a = re.search(start_pat, script, re.M)
        b = re.search(end_pat, script, re.M)
        if not a or not b or b.start() < a.start():
            raise SystemExit("区块定位失败：%s" % start_pat)
        return script[a.start():b.start()].rstrip() + "\n"

    helpers = "\n".join(grab_line(p) for p in (
        r"^function esc\(s\)\{.*$", r"^function toast\(m,ok=true\)\{.*$"))
    code = (grab_block(r"^const _PL_INSTALL_WARN=", r"^async function plPage")
            + grab_block(r"^async function plPage\(name\)\{", r"^async function plInstall\(\)\{")
            + grab_block(r"^async function plInstall\(\)\{", r"^async function plUninstall")
            + grab_block(r"^function voiceCollect\(\)\{", r"^async function voiceSave")
            + grab_block(r"^async function voiceKeyClear\(key\)\{", r"^function voiceProvChange"))

    fix_js = """
globalThis.__FIXFN = function(p, opts){
  if (p.indexOf('/api/plugins/pagejs') === 0) return {ok: true, js: "function x(){return '<i>y</i>'}"};
  if (p === '/api/plugins') {
    function plug(name, enabled){ return {name: name, title: name, version: '1.0',
      enabled: enabled, page: {js: name + 'Page'}, page_js: true,
      settings: [{key: 'k', type: 'toggle', label: 'K', default: false}], values: {}}; }
    return {ok: true, plugins: [plug('uisec_off', false), plug('uisec_on', true)]};
  }
  if (p === '/api/plugins/install') return {ok: true, title: 'Demo', warning: '装好了，但有个提醒 WARNTXT'};
  return {};
};
"""
    HARNESS = r"""
const input = JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8'));
const els = {};
const calls = {api: [], apiFull: [], runTask: [], dlg: [], scripts: [], toasts: []};
let confirmAnswer = true;
const view = {html: '', set innerHTML(v){this.html = String(v);}, get innerHTML(){return this.html;}};
function mkEl(id){ return {id: id, value: '', checked: false, textContent: '', style: {}}; }
['plnote', 'plpage'].forEach(id => els[id] = mkEl(id));
// 为 voiceCollect 提供最小元素集
const ce = function(id, props){ els[id] = Object.assign(mkEl(id), props); };
ce('v-prov', {value: 'zhipu'}); ce('v-on', {checked: true}); ce('v-priv', {checked: false});
ce('v-grp', {checked: false}); ce('v-wt', {checked: false});
ce('v-kw', {value: '你好'}); ce('v-prob', {value: '50'});
const sandbox = {
  TAB: 'plugins',
  window: {},
  _VM: {config: {zhipu_key_set: true, sf_key_set: true, custom_key_set: true,
                 custom_base: 'https://***@api.example.com/v1/'}},
  $: function(sel){ if (sel === '#view') return view;
    const id = String(sel).slice(1);
    return els[id] || (els[id] = mkEl(id)); },
  head: function(sub){ return '<h2>插件</h2>'; },
  load: function(){},
  voiceProvChange: function(){ calls.provRender = (calls.provRender || 0) + 1; },
  setTimeout: function(){ return 0; },
  dlgConfirm: function(o){ calls.dlg.push(o); return Promise.resolve(confirmAnswer); },
  runTask: function(title, path, body){ calls.runTask.push([title, path, body]);
    return Promise.resolve({ok: true, title: '插件'}); },
  api: function(p, opts){ calls.api.push(p);
    calls.apiFull.push({path: p, opts: JSON.parse(JSON.stringify(opts || {}))});
    return Promise.resolve(JSON.parse(JSON.stringify(globalThis.__FIXFN(p, opts) || {}))); },
  document: {
    getElementById: function(id){ return els[id] || null; },
    querySelector: function(sel){ if (String(sel).indexOf('vmode') >= 0) return {value: 'keyword'};
      return null; },
    querySelectorAll: function(){ return []; },
    createElement: function(tag){ return {tag: tag, textContent: ''}; },
    body: {appendChild: function(o){ calls.scripts.push(String(o.textContent || '')); }},
  },
};
const names = Object.keys(sandbox);
const mod = new Function(...names, input.helpers + '\n' + input.code +
  '\nreturn {plPage: plPage, mktInstall: mktInstall, plInstall: plInstall, voiceCollect: voiceCollect, voiceKeyClear: voiceKeyClear};')(
  ...Object.values(sandbox));
const out = {};
(async () => {
  out.collect = mod.voiceCollect();
  // 停用插件：不请求 pagejs、不注入脚本、顶部有停用说明
  view.html = ''; els.plpage.innerHTML = ''; calls.api.length = 0; calls.scripts.length = 0;
  await mod.plPage('uisec_off');
  out.off = {apiCalls: calls.api.slice(), scripts: calls.scripts.length,
    html: view.html, page: els.plpage.innerHTML};
  // 启用插件：请求 pagejs、注入脚本、无停用说明
  view.html = ''; els.plpage.innerHTML = ''; calls.api.length = 0; calls.scripts.length = 0;
  await mod.plPage('uisec_on');
  out.on = {apiCalls: calls.api.slice(), scripts: calls.scripts.length,
    html: view.html, page: els.plpage.innerHTML};
  // mktInstall：确认框拦截
  confirmAnswer = false; calls.dlg.length = 0; calls.runTask.length = 0;
  await mod.mktInstall('uisec_off', false);
  out.mktNo = {dlg: calls.dlg.slice(), run: calls.runTask.length};
  confirmAnswer = true; calls.dlg.length = 0; calls.runTask.length = 0;
  await mod.mktInstall('uisec_off', true);
  out.mktUpd = {dlg: calls.dlg.slice(), run: calls.runTask.length};
  // plInstall：确认框 + warning 双通道
  confirmAnswer = false; calls.dlg.length = 0; calls.api.length = 0;
  await mod.plInstall();
  out.plNo = {dlg: calls.dlg.slice(), api: calls.api.length};
  confirmAnswer = true; calls.dlg.length = 0; calls.api.length = 0;
  els.plnote.textContent = ''; els.plnote.style.display = '';
  els.toast.textContent = ''; els.toast.style.borderColor = '';
  await mod.plInstall();
  out.plYes = {dlg: calls.dlg.length, api: calls.api.slice(),
    warn: els.plnote.textContent, shown: els.plnote.style.display,
    toastText: els.toast.textContent, toastErr: els.toast.style.borderColor};
  // 语音专属 Key 清除按钮：取消不发请求；确认后带 <键名>_clear
  confirmAnswer = false; calls.dlg.length = 0; calls.apiFull.length = 0;
  await mod.voiceKeyClear('zhipu_key');
  out.clrNo = {dlg: calls.dlg.length, api: calls.apiFull.length, dlgOpts: calls.dlg.slice()};
  confirmAnswer = true; calls.dlg.length = 0; calls.apiFull.length = 0;
  calls.provRender = 0;
  await mod.voiceKeyClear('custom_key');
  const lastCall = calls.apiFull[calls.apiFull.length - 1] || {};
  let sentVoice = null;
  try { sentVoice = JSON.parse(lastCall.opts.body).voice; } catch (e) {}
  out.clrYes = {dlg: calls.dlg.length, path: lastCall.path,
    provRender: calls.provRender, voice: sentVoice};
  process.stdout.write(JSON.stringify(out));
})().catch(e => { process.stdout.write(JSON.stringify({fatal: String(e && e.stack || e)})); });
"""
    harness = HARNESS.replace("const names = Object.keys(sandbox);",
                              fix_js + "\nconst names = Object.keys(sandbox);")
    jf = work / "uisec_harness.js"
    jf.write_text(harness, encoding="utf-8")
    inp = work / "uisec_input.json"
    inp.write_text(json.dumps({"helpers": helpers, "code": code},
                              ensure_ascii=False), encoding="utf-8")
    r = subprocess.run(["node", str(jf), str(inp)], capture_output=True, text=True,
                       encoding="utf-8", timeout=120)
    if r.returncode != 0 or not r.stdout.strip():
        chk("页面脚本能跑起来", False, (r.stderr or r.stdout)[:400])
        return
    try:
        out = json.loads(r.stdout)
    except ValueError:
        chk("页面脚本能跑起来", False, r.stdout[:400])
        return
    if "fatal" in out:
        chk("页面脚本能跑起来", False, out["fatal"][:800])
        return
    chk("页面脚本能跑起来", True)

    col = json.dumps(out.get("collect") or {}, ensure_ascii=False)
    c = out.get("collect") or {}
    chk("4c voiceCollect：Key 字段交空串（不把 *_set 布尔/占位符当密钥发回）",
        c.get("zhipu_key") == "" and c.get("sf_key") == "" and c.get("custom_key") == "", c)
    chk("4c' voiceCollect：custom_base 交打码串（服务端认出=没改）",
        c.get("custom_base") == M_BASE, c.get("custom_base"))
    chk("4c'' voiceCollect 基本字段照常（enabled/provider/keywords/prob）",
        c.get("enabled") is True and c.get("provider") == "zhipu"
        and c.get("keywords") == ["你好"] and c.get("prob") == 0.5, "")
    chk("4c''' 收集结果里没有测试密钥残影", T_ZK not in col and T_SF not in col, col[:140])

    off = out.get("off") or {}
    on = out.get("on") or {}
    chk("4d 停用插件：不请求 pagejs、不注入脚本、页面有停用说明",
        off.get("scripts") == 0
        and not any("/api/plugins/pagejs" in x for x in (off.get("apiCalls") or []))
        and "插件已停用：设置可以先改，启用后生效" in (off.get("html") or ""), off.get("apiCalls"))
    chk("4d' 停用插件：声明式设置区块照常渲染（设置卡 + plSet 保存通道还在）",
        "插件设置" in (off.get("page") or "") and 'onchange="plSet' in (off.get("page") or ""),
        (off.get("page") or "")[:160])
    chk("4e 启用插件：请求 pagejs 且注入脚本、无停用说明",
        on.get("scripts") == 1 and any("/api/plugins/pagejs" in x for x in (on.get("apiCalls") or []))
        and "插件已停用" not in (on.get("html") or ""), on.get("apiCalls"))

    mno = out.get("mktNo") or {}
    mup = out.get("mktUpd") or {}
    d0 = (mno.get("dlg") or [{}])[0]
    d1 = (mup.get("dlg") or [{}])[0]
    chk("4f mktInstall：先弹 danger 确认框（继续安装键），取消则不发起安装",
        mno.get("run") == 0 and d0.get("danger") is True and d0.get("okText") == "继续安装"
        and "插件代码会在机器人进程里运行" in (d0.get("body") or "")
        and "只安装你信任的来源" in (d0.get("body") or ""), d0)
    chk("4g mktInstall 更新：同样确认且文案多一句「这是更新」，确认后发起 market-install",
        mup.get("run") == 1 and d1.get("danger") is True and "这是更新" in (d1.get("body") or ""),
        (d1, mup.get("run")))

    pno = out.get("plNo") or {}
    pyes = out.get("plYes") or {}
    pd0 = (pno.get("dlg") or [{}])[0]
    chk("4h plInstall（zip 安装）：先弹 danger 确认框，取消则不请求",
        pno.get("api") == 0 and pd0.get("danger") is True and pd0.get("okText") == "继续安装"
        and "插件代码会在机器人进程里运行" in (pd0.get("body") or ""), pd0)
    chk("4i plInstall warning：toast 与 plnote 元素都显示（元素此前根本不存在）",
        pyes.get("warn") == "装好了，但有个提醒 WARNTXT" and pyes.get("shown") == "block"
        and "WARNTXT" in str(pyes.get("toastText")) and pyes.get("toastErr") == "var(--err)",
        (pyes.get("warn"), pyes.get("toastText"), pyes.get("toastErr")))

    cno = out.get("clrNo") or {}
    cy = out.get("clrYes") or {}
    d2 = (cno.get("dlgOpts") or [{}])[0]
    chk("4n 语音清除：取消 → 不发请求；确认框 danger + 指定文案",
        cno.get("api") == 0 and d2.get("danger") is True and d2.get("okText") == "清除"
        and "清除后这项改用主密钥（或该语音的默认方式），确定吗？" in (d2.get("body") or ""), d2)
    v = cy.get("voice") or {}
    chk("4o 语音清除：确认后提交 custom_key_clear=1（打 voice/config），并重渲染占位提示",
        cy.get("path") == "/api/voice/config" and v.get("custom_key_clear") == 1
        and "zhipu_key_clear" not in v and (cy.get("provRender") or 0) >= 1, cy)


def t_page_static():
    script = re.search(r"<script>(.*?)</script>", SRC.read_text(encoding="utf-8"), re.S).group(1)
    chk("4j 页面静态：三个 Key 输入框不再回填原值（value= 原值的写法已移除）",
        "esc(String(c.zhipu_key" not in script and "esc(String(c.sf_key" not in script
        and "esc(String(c.custom_key" not in script, "")
    chk("4k 页面静态：占位提示「已配置，留空保持不变 / 未配置」三处齐",
        script.count("已配置，留空保持不变") == 3 and script.count(":'未配置'") == 3, "")
    chk("4l 页面静态：apiproxy 回填 GET 给的串（服务端已打码），提交通道不变",
        'id="apiproxy" value="${esc(String(c.api_proxy||\'\'))}"' in script
        and "{api_proxy:this.value.trim()}" in script, "")
    chk("4m 页面静态：plnote 元素已在插件列表页出现", 'id="plnote"' in script, "")
    chk("4m' 页面静态：三个密钥框旁各有一个 voiceKeyClear 清除按钮",
        script.count("voiceKeyClear(\\'") == 3
        and "voiceKeyClear(\\'zhipu_key\\')" in script
        and "voiceKeyClear(\\'sf_key\\')" in script
        and "voiceKeyClear(\\'custom_key\\')" in script, "")


# ----------------------------------------------------------- 5 出站请求钉住
class _FakeSock:
    def __init__(self, resp_q):
        self._resp_q = resp_q
        self.sent = []

    def sendall(self, b):
        self.sent.append(b)

    def makefile(self, mode, **kw):
        data = self._resp_q.pop(0) if self._resp_q else \
            b"HTTP/1.1 500 E\r\nContent-Length: 0\r\n\r\n"
        return io.BytesIO(data)

    def close(self):
        pass


class _FakeSslCtx:
    def __init__(self, log):
        self._log = log

    def wrap_socket(self, sock, server_hostname=None):
        self._log["sni"].append(server_hostname)
        return sock


@contextlib.contextmanager
def fake_net(log, dns):
    """打桩 socket.create_connection / getaddrinfo / ssl.create_default_context：
    记录连接目标、解析记录、SNI；响应按队列回放——全程不出网。"""

    def fake_create_connection(address, timeout=None, **kw):
        log["targets"].append("%s:%s" % (address[0], address[1]))
        s = _FakeSock(log["resp_q"])
        log["socks"].append(s)
        return s

    def fake_getaddrinfo(host, port, *a, **k):
        log["resolved"].append((str(host), port))
        ips = dns[str(host)]
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, port)) for ip in ips]

    ctx = _FakeSslCtx(log)
    with patched(socket, create_connection=fake_create_connection,
                 getaddrinfo=fake_getaddrinfo), \
            patched(ssl, create_default_context=lambda **k: ctx):
        yield


def _newlog(resps):
    return {"targets": [], "resolved": [], "sni": [], "socks": [], "resp_q": list(resps)}


def t_public_http(tmp):
    # (a) http 两跳重定向：逐跳重新解析、逐跳钉住 IP
    log = _newlog([
        b"HTTP/1.1 302 Found\r\nLocation: http://hop2.example.com/b\r\nContent-Length: 0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi"])
    dns = {"example.com": ["1.2.3.4"], "hop2.example.com": ["5.6.7.8"]}
    try:
        with fake_net(log, dns):
            raw = U._http_get_public("http://example.com/a", timeout=5)
    except Exception as e:  # pragma: no cover
        chk("5a http 两跳重定向：下载成功", False, repr(e))
        return
    chk("5a http 两跳重定向：下载成功", raw == b"hi", raw)
    chk("5b 连接目标是被校验过的 IP，不是域名（两跳各自钉住）",
        log["targets"] == ["1.2.3.4:80", "5.6.7.8:80"], log["targets"])
    chk("5c 重定向每一跳都重新解析校验",
        [r[0] for r in log["resolved"]] == ["example.com", "hop2.example.com"], log["resolved"])

    # (b) https：SNI/证书名用原域名，连接仍钉 IP，Host 头用原域名
    log2 = _newlog([b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi"])
    with fake_net(log2, {"tls.example.com": ["9.9.9.9"]}):
        U._http_get_public("https://tls.example.com/a", timeout=5)
    sent = b"".join(b for s in log2["socks"] for b in s.sent)
    chk("5e https：连接目标为校验过的 IP", log2["targets"] == ["9.9.9.9:443"], log2["targets"])
    chk("5f https：TLS server_hostname（SNI/证书校验名）用原域名",
        log2["sni"] == ["tls.example.com"], log2["sni"])
    chk("5g Host 头用原域名（不是 IP）",
        b"Host: tls.example.com" in sent and b"Host: 9.9.9.9" not in sent, sent[:120])

    # (c) 非默认端口：Host 头带端口；默认端口不带
    log3 = _newlog([b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi"])
    with fake_net(log3, {"p8080.example.com": ["1.2.3.4"]}):
        U._http_get_public("http://p8080.example.com:8080/a", timeout=5)
    sent3 = b"".join(b for s in log3["socks"] for b in s.sent)
    chk("5j http 8080：Host 头为 host:port",
        log3["targets"] == ["1.2.3.4:8080"] and b"Host: p8080.example.com:8080" in sent3
        and b"Host: p8080.example.com\r\n" not in sent3, sent3[:160])

    log4 = _newlog([b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi"])
    with fake_net(log4, {"tls8443.example.com": ["5.6.7.8"]}):
        U._http_get_public("https://tls8443.example.com:8443/a", timeout=5)
    sent4 = b"".join(b for s in log4["socks"] for b in s.sent)
    chk("5k https 8443：Host 头为 host:port、连接钉 8443",
        log4["targets"] == ["5.6.7.8:8443"] and b"Host: tls8443.example.com:8443" in sent4,
        (log4["targets"], sent4[:160]))

    log5 = _newlog([b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi"])
    with fake_net(log5, {"plain.example.com": ["1.2.3.4"]}):
        U._http_get_public("http://plain.example.com/a", timeout=5)
    sent5 = b"".join(b for s in log5["socks"] for b in s.sent)
    chk("5l 默认端口 80：Host 头不带端口",
        b"Host: plain.example.com\r\n" in sent5 and b"Host: plain.example.com:" not in sent5,
        sent5[:160])


def t_public_blocked(tmp):
    log = _newlog([])
    dns = {"evil.example.com": ["10.0.0.5"]}
    try:
        with fake_net(log, dns):
            U._http_get_public("http://evil.example.com/a", timeout=5)
        err = None
    except ValueError as e:
        err = str(e)
    chk("5h 解析到非公网地址 → 拒绝且从未建连",
        err is not None and "非公网" in err and log["targets"] == [], (err, log["targets"]))

    try:
        U._assert_public_host("localhost", 80)
        e1 = None
    except ValueError as e:
        e1 = e
    try:
        U._assert_public_host("192.168.1.1", 80)
        e2 = None
    except ValueError as e:
        e2 = e
    h, ip = U._assert_public_host("1.2.3.4", 80)
    chk("5i 字面量私网地址拒绝；字面量公网地址直接钉自身",
        e1 is not None and e2 is not None and (h, ip) == ("1.2.3.4", "1.2.3.4"),
        (str(e1), str(e2), h, ip))


# ----------------------------------------------------------- 6 设备操控新开关
def t_devctrl_switch(tmp):
    # device_ctrl_open_allow_exe 已在 qq_common.py DEFAULT_CONFIG 登记（默认 False），
    # 走真实登记验证，不再模拟
    _, b = ui_call("do_GET", "/api/app-config")[0]
    chk("6a 缺键读出 False（DEFAULT_CONFIG 默认关）",
        b.get("device_ctrl_open_allow_exe") is False, b.get("device_ctrl_open_allow_exe"))
    r = ui_call("do_POST", "/api/app-config", {"device_ctrl_open_allow_exe": 1})
    d = U.C.load_app_config()
    chk("6b 保存 True 后读出 True（与其它设备操控开关同一条 app-config 通道）",
        r[0][0] == 200 and d.get("device_ctrl_open_allow_exe") is True,
        d.get("device_ctrl_open_allow_exe"))
    r = ui_call("do_POST", "/api/app-config",
                {"device_ctrl_open_allow_exe": 0, "device_ctrl_2fa": 1, "device_ctrl_actions": 1})
    d = U.C.load_app_config()
    chk("6c 同批保存：新开关关回、其余设备操控开关读写不受影响",
        d.get("device_ctrl_open_allow_exe") is False and d.get("device_ctrl_2fa") is True
        and d.get("device_ctrl_actions") is True,
        {k: d.get(k) for k in ("device_ctrl_open_allow_exe", "device_ctrl_2fa", "device_ctrl_actions")})
    script = re.search(r"<script>(.*?)</script>", SRC.read_text(encoding="utf-8"), re.S).group(1)
    chk("6d 界面：设置卡里有该开关（标题+说明+默认关，走 device_ctrl_2fa 同款 saveKVLoad 通道）",
        "允许『打开』动作启动程序和脚本" in script
        and "默认只打开文档、图片、音视频、文件夹和网页" in script
        and "${c.device_ctrl_open_allow_exe?'checked':''}" in script
        and "{device_ctrl_open_allow_exe:this.checked?1:0}" in script, "")


def main():
    work = pathlib.Path(tempfile.mkdtemp(prefix="qqbot_uisec_work_"))
    try:
        chk("0 隔离生效：数据目录在临时目录",
            str(pathlib.Path(U.DATA_DIR).resolve()).startswith(str(pathlib.Path(_TMP).resolve())),
            U.DATA_DIR)
        t_app_config_get(_TMP)
        t_app_config_post(_TMP)
        t_voice_meta_get(_TMP)
        t_voice_config_post(_TMP)
        t_pool_names(_TMP)
        t_body(_TMP)
        t_ui_token(_TMP)
        t_authed(_TMP)
        t_pagejs_server(_TMP)
        t_page_js(work)
        t_page_static()
        t_public_http(_TMP)
        t_public_blocked(_TMP)
        t_devctrl_switch(_TMP)
    finally:
        shutil.rmtree(str(work), ignore_errors=True)
        shutil.rmtree(str(_TMP), ignore_errors=True)
    print()
    print("失败 %d 项" % len(fails) if fails else "全部通过 ✓")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
