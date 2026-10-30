#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qq_plugins.py — 插件宿主（插件化第一阶段）

插件形态：
  <目录>/<插件名>/
    manifest.json  必需：{"name","title","description","default_enabled",
                          "settings":[{"key","type":"toggle|number|text",
                                       "label","default","min","max"}]}
    plugin.py      可选：def register(api): 在里面用 api 注册钩子

目录两处（同名时用户级覆盖内置）：
  内置：<scripts>/plugins/   （随应用发布，用户级目录可覆盖其启停）
  用户：<DATA_DIR>/plugins/  （用户自装；内置/用户插件都可停用）

钩子（v1 最小集，后续按需扩展）：
  api.on_tick(fn)            fn(daemon) —— 主循环每轮调用（窗口制节拍请自行限频）
  api.inject_context(fn)     fn(chat_type, chat_id) -> str —— 返回注入聊天的感知块（空串=零成本）

启停与设置持久化：<DATA_DIR>/plugins_state.json：
  {"enabled": {"<名>": bool}, "settings": {"<名>": {key: value}}}
安全约定：插件与宿主同进程同权限——只加载本机数据目录与应用内置的插件，
不提供任何远程加载；新钩子务必保持"禁用即零成本"。
"""

import importlib.util
import json
import os
import pathlib
import sys
import time

DATA_DIR = os.environ.get("QQBOT_DATA_DIR") or os.path.join(
    os.path.expanduser("~"), ".zcode-qq-bot")
STATE_PATH = os.path.join(DATA_DIR, "plugins_state.json")
BUILTIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugins")
USER_DIR = os.path.join(DATA_DIR, "plugins")


class PluginAPI:
    """发给每个插件 register(api) 的注册句柄。"""

    def __init__(self, host, name):
        self._host = host
        self.name = name
        self._windows = {}

    def on_tick(self, fn):
        self._host.hooks.setdefault(self.name, {})["tick"] = fn

    def inject_context(self, fn):
        self._host.hooks.setdefault(self.name, {})["inject"] = fn

    def setting(self, key, default=None):
        return self._host.setting(self.name, key, default)

    def log(self, msg):
        self._host.log("PLUGIN[%s] %s" % (self.name, msg))

    def every(self, minutes, fn):
        """窗口节流的 tick：minutes 分钟最多跑一次（挂到宿主的统一节拍上）。
        用法：api.every(180, my_round) —— my_round(daemon) 每 3 小时至多执行一次。
        agent 重启后首个 tick 立即执行一次（冷启动即跑），之后按窗口。"""
        state = {"last": 0.0}

        def _wrapped(daemon):
            now = time.time()
            if now - state["last"] < max(1, float(minutes)) * 60:
                return
            state["last"] = now
            try:
                fn(daemon)
            except Exception as e:
                self.log("EVERY_FAIL %r" % e)
        return _wrapped


class PluginHost:
    def __init__(self, log_fn=None):
        self.hooks = {}          # name -> {"tick": fn, "inject": fn}
        self.manifests = {}      # name -> manifest dict
        self._log_fn = log_fn or (lambda m: print(m, flush=True))
        self._state_mtime = 0.0
        self._state_cache = {}
        self._loaded_names = set()

    def log(self, msg):
        try:
            self._log_fn(msg)
        except Exception:
            pass

    # ---- 状态（启停 + 设置） ----

    def _state(self):
        try:
            mt = os.path.getmtime(STATE_PATH)
        except OSError:
            mt = 0.0
        if mt != self._state_mtime or not self._state_cache:
            try:
                d = json.loads(pathlib.Path(STATE_PATH).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                d = {}
            self._state_cache = d if isinstance(d, dict) else {}
            self._state_mtime = mt
        return self._state_cache

    def enabled(self, name, default_enabled):
        st = self._state().get("enabled")
        if isinstance(st, dict) and name in st:
            return bool(st.get(name))
        return bool(default_enabled)

    def setting(self, name, key, default=None):
        st = self._state().get("settings") or {}
        vals = st.get(name) if isinstance(st, dict) else None
        if isinstance(vals, dict) and key in vals:
            return vals[key]
        for m in (self.manifests.get(name) or {}).get("settings") or []:
            if isinstance(m, dict) and m.get("key") == key:
                return m.get("default", default)
        return default

    # ---- 扫描与加载 ----

    def _dirs(self):
        return [(BUILTIN_DIR, True), (USER_DIR, False)]

    def scan(self):
        """扫描两处目录，返回 {name: (manifest, plugin_dir, builtin)}。"""
        found = {}
        for base, builtin in self._dirs():
            try:
                names = sorted(os.listdir(base))
            except OSError:
                continue
            for n in names:
                mpath = os.path.join(base, n, "manifest.json")
                mf = _read_json(mpath)
                if not isinstance(mf, dict) or not mf.get("name"):
                    continue
                name = str(mf["name"])
                if not _safe_name(name):
                    continue
                found[name] = (mf, os.path.join(base, n), builtin)
        return found

    def load_all(self):
        """扫描并加载所有启用插件的钩子。重复调用安全（先卸载再装）。"""
        self.hooks = {}
        self.manifests = {}
        loaded, skipped = [], []
        for name, (mf, pdir, builtin) in self.scan().items():
            mf = dict(mf)
            mf.setdefault("title", name)
            mf.setdefault("description", "")
            mf.setdefault("default_enabled", False)
            mf.setdefault("settings", [])
            mf["builtin"] = builtin
            self.manifests[name] = mf
            if not self.enabled(name, mf["default_enabled"]):
                skipped.append(name)
                continue
            pyp = os.path.join(pdir, "plugin.py")
            if not os.path.isfile(pyp):
                continue  # 纯声明型（仅设置卡）也允许
            try:
                mod_name = "qqplugin_%s" % name
                spec = importlib.util.spec_from_file_location(mod_name, pyp)
                mod = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = mod
                spec.loader.exec_module(mod)
                reg = getattr(mod, "register", None)
                if callable(reg):
                    reg(PluginAPI(self, name))
                loaded.append(name)
            except Exception as e:
                self.log("PLUGIN_LOAD_FAIL %s %r" % (name, e))
        self._loaded_names = set(loaded)
        self.log("PLUGINS loaded=%s disabled=%s" % (",".join(loaded) or "-", ",".join(skipped) or "-"))

    # ---- 运行时钩子 ----

    def run_ticks(self, daemon):
        """主循环每轮调用：启停状态变化自动热生效（mtime 检查，代价极低）。"""
        try:
            self.load_all_if_changed()
        except Exception as e:
            self.log("PLUGIN_HOTLOAD_FAIL %r" % e)
        for name in list(self.hooks.keys()):
            fn = self.hooks[name].get("tick")
            if not fn:
                continue
            try:
                fn(daemon)
            except Exception as e:
                self.log("PLUGIN_TICK_FAIL %s %r" % (name, e))

    def inject_all(self, chat_type, chat_id, sender_id=None, sender_name=None):
        """汇总所有启用插件的上下文注入块。
        sender_id/name 可选：有发言者上下文时传入（member_profiles 等插件用）；
        不支持 kwarg 的旧式注入函数按 (ct, cid) 调用（兼容）。"""
        out = []
        for name in list(self.hooks.keys()):
            fn = self.hooks[name].get("inject")
            if not fn:
                continue
            try:
                try:
                    s = str(fn(chat_type, chat_id, sender_id=sender_id,
                               sender_name=sender_name) or "")
                except TypeError:
                    s = str(fn(chat_type, chat_id) or "")
                if s.strip():
                    out.append(s.strip())
            except Exception as e:
                self.log("PLUGIN_INJECT_FAIL %s %r" % (name, e))
        return "\n".join(out)

    def load_all_if_changed(self):
        mt = _state_mtime()
        if mt != self._state_mtime:
            self.load_all()


def _state_mtime():
    try:
        return os.path.getmtime(STATE_PATH)
    except OSError:
        return 0.0


def _read_json(path):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _safe_name(name):
    import re as _re
    return bool(_re.fullmatch(r"[a-z0-9_]{2,32}", str(name)))


# ---------------- 安装 / 卸载 ----------------

def install_from_zip(zip_path):
    """从 zip 安装/更新一个用户插件。返回 {"ok":bool,...}。
    安全约束：zip 内路径逐条校验（禁绝对路径/../、只允许安全文件名字符集），
    必须恰好含一个 manifest.json（在根或单层子目录），name 合法且与目标目录一致。"""
    zp = pathlib.Path(str(zip_path))
    if not zp.is_file() or zp.suffix.lower() != ".zip":
        return {"ok": False, "error": "不是 zip 文件"}
    import re as _re
    import shutil as _sh
    import tempfile
    import zipfile
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="plugin-ins-"))
    try:
        with zipfile.ZipFile(zp) as zf:
            for info in zf.infolist():
                n = info.filename.replace("\\", "/")
                if not n or n.startswith("/") or ".." in n.split("/") \
                        or ":" in n or not _re.fullmatch(
                            r"[A-Za-z0-9_][A-Za-z0-9._\- /]*", n):
                    return {"ok": False, "error": "包内路径非法: %s" % n[:60]}
                target = (tmp / n).resolve()
                if not str(target).startswith(str(tmp.resolve())):
                    return {"ok": False, "error": "路径越界"}
                zf.extract(info, tmp)
        # 定位 manifest：根或单层子目录
        base = tmp
        mf_path = base / "manifest.json"
        if not mf_path.is_file():
            subs = [d for d in base.iterdir() if d.is_dir()]
            if len(subs) == 1 and (subs[0] / "manifest.json").is_file():
                base = subs[0]
                mf_path = subs[0] / "manifest.json"
            else:
                return {"ok": False, "error": "包里找不到 manifest.json（应为一个插件文件夹打包）"}
        mf = _read_json(mf_path)
        if not isinstance(mf, dict) or not mf.get("name") or not _safe_name(mf["name"]):
            return {"ok": False, "error": "manifest.name 非法（小写字母/数字/下划线，2-32 位）"}
        # 内置同名保护：不允许用安装覆盖内置插件
        if os.path.isdir(os.path.join(BUILTIN_DIR, str(mf["name"]))):
            return {"ok": False,
                    "error": "「%s」是内置插件，不能覆盖安装（内置插件随应用更新）" % mf["name"]}
        dest = pathlib.Path(USER_DIR) / str(mf["name"])
        updated = dest.is_dir()
        if dest.exists():
            _sh.rmtree(dest, ignore_errors=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        _sh.copytree(base, dest)
        return {"ok": True, "name": mf["name"],
                "title": str(mf.get("title") or mf["name"]),
                "updated": updated,
                "note": "已更新覆盖" if updated else "已安装（默认停用，请在列表中启用）"}
    except zipfile.BadZipFile:
        return {"ok": False, "error": "zip 包损坏"}
    except OSError as e:
        return {"ok": False, "error": repr(e)}
    finally:
        _sh.rmtree(tmp, ignore_errors=True)


def uninstall(name):
    """卸载用户级插件（内置不可卸载）；同时清掉它的启停与设置。"""
    name = str(name or "")
    if not _safe_name(name):
        return {"ok": False, "error": "插件名非法"}
    dest = pathlib.Path(USER_DIR) / name
    if not dest.is_dir():
        return {"ok": False, "error": "插件不存在或不是用户安装的（内置插件不可卸载）"}
    import shutil as _sh
    _sh.rmtree(dest, ignore_errors=True)
    # 清状态
    try:
        st = _read_json(STATE_PATH) or {}
        changed = False
        if isinstance(st.get("enabled"), dict) and name in st["enabled"]:
            st["enabled"].pop(name)
            changed = True
        if isinstance(st.get("settings"), dict) and name in st["settings"]:
            st["settings"].pop(name)
            changed = True
        if changed:
            pathlib.Path(STATE_PATH).write_text(
                json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError):
        pass
    return {"ok": True, "name": name}
