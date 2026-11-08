# -*- coding: utf-8 -*-
"""sing_svc：AI 翻唱（歌声转换）——引用一条语音消息说「翻唱」，机器人用
指定音色把它重新唱一遍发回来。

引擎：so-vits-svc 4.1 整合包（用户自装，插件只用它的 runtime 跑推理）；
模型：社区歌声模型包（G*.pth + config.json）。全程本机 GPU，零 API 成本。

QQ 用法：
  ① 引用一条语音消息 → @机器人（群里）说「翻唱」或「用<音色名>唱」
  ② 私聊里引用自己的语音说「翻唱」
需要宿主 0.5.14+（get_voice_file 语音取件能力）。"""
import importlib.util as _ilu
import json
import os as _os
import re as _re
import threading as _th

_dir = _os.path.dirname(_os.path.abspath(__file__))
_spec = _ilu.spec_from_file_location("sing_svc_engine", _os.path.join(_dir, "engine.py"))
E = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(E)

# 触发词：翻唱 / 用XX唱 / 唱一个（必须引用了一条消息）
_TRIG_RE = _re.compile(r"翻唱|唱一个|用[^\s，。,!？?]{1,20}唱")
_NAME_RE = _re.compile(r"用([^\s，。,!？?]{1,20})唱")


def _cfg(api):
    c = api.load("cfg", None)
    return c if isinstance(c, dict) else E.default_cfg()


def _save_cfg(api, c):
    api.store("cfg", c)


def register(api):
    # ---- 页面路由 ----
    api.route("status", lambda args: _status(api))
    api.route("cfg_save", lambda args: _cfg_save(api, args or {}))
    api.route("engine", lambda args: _engine(api, args or {}))
    api.route("models", lambda args: {"ok": True, "models": E.models_list()})
    api.route("model_import", lambda args: E.model_import(
        str((args or {}).get("src") or ""), str((args or {}).get("name") or "")))
    api.route("model_del", lambda args: E.model_del(str((args or {}).get("id") or "")))
    api.route("model_use", lambda args: _model_use(api, args or {}))
    api.route("convert_test", lambda args: _convert_test(api, args or {}))

    # ---- QQ 触发（on_message 收到的是已触发消息；回复丢工作线程绝不阻塞主循环） ----
    api.on_message(lambda m: _on_msg(api, m))
    api.log("ready（引擎本机 GPU 推理；需要 0.5.14+ 宿主的语音取件能力）")


# ---------------- 页面路由实现 ----------------

def _status(api):
    cfg = _cfg(api)
    return {"ok": True, "cfg": cfg,
            "engine": E.engine_status(cfg),
            "models": E.models_list(),
            "detected": E.detect_svc_dirs(),
            "ffmpeg": bool(E.find_ffmpeg(cfg))}


def _cfg_save(api, a):
    cfg = _cfg(api)
    if "dir" in a:
        cfg["dir"] = str(a.get("dir") or "").strip()[:200]
    if "port" in a:
        try:
            cfg["port"] = max(1024, min(65535, int(a.get("port"))))
        except (TypeError, ValueError):
            pass
    if "autostart" in a:
        cfg["autostart"] = bool(a.get("autostart"))
    if "tran" in a:
        try:
            cfg["tran"] = max(-24, min(24, int(a.get("tran"))))
        except (TypeError, ValueError):
            pass
    if "max_sec" in a:
        try:
            cfg["max_sec"] = max(15, min(300, int(a.get("max_sec"))))
        except (TypeError, ValueError):
            pass
    _save_cfg(api, cfg)
    return {"ok": True, "cfg": cfg}


def _engine(api, a):
    cfg = _cfg(api)
    act = str(a.get("action") or "")
    if act == "start":
        _th.Thread(target=lambda: E.engine_start(cfg), daemon=True).start()
        return {"ok": True, "starting": True,
                "hint": "引擎启动中（torch 首次加载可到 1-2 分钟），稍后刷新"}
    if act == "stop":
        return E.engine_stop()
    return {"ok": False, "error": "未知 action"}


def _model_use(api, a):
    cfg = _cfg(api)
    mid = str(a.get("id") or "")
    if mid:
        ok = any(m["id"] == mid for m in E.models_list())
        if not ok:
            return {"ok": False, "error": "模型不存在"}
    cfg["default_model"] = mid
    _save_cfg(api, cfg)
    return {"ok": True, "default_model": mid,
            "hint": ("已设为默认音色（下次翻唱即用）" if mid else "已清除默认音色")}


def _convert_test(api, a):
    """页面端到端测试：用一条本地音频文件走完整转换链路（模型须已导入）。"""
    cfg = _cfg(api)
    mid = str(a.get("id") or "") or cfg.get("default_model") or ""
    src = str(a.get("path") or "")
    if not mid:
        return {"ok": False, "error": "先导入并启用一个模型"}
    if not _os.path.isfile(src):
        return {"ok": False, "error": "测试音频文件不存在"}
    return E.convert(src, mid, cfg)


# ---------------- QQ 触发链路 ----------------

def _on_msg(api, msg):
    try:
        text = str(msg.get("text") or "").strip()
        if not text or not _TRIG_RE.search(text):
            return
        reply_id = str(msg.get("reply_id") or "").strip()
        if not reply_id:
            return          # 没引用消息：不抢话（说明文案在页面引导里）
        ct = str(msg.get("chat_type") or "group")
        cid = str(msg.get("chat_id") or "")
        _th.Thread(target=_do_convert, daemon=True,
                   args=(api, ct, cid, reply_id, text)).start()
    except Exception as e:
        api.log("TRIG_FAIL %r" % e)


def _pick_model(api, text):
    """「用XX唱」→ 名称模糊匹配模型；没指名用默认。返回 (mid, err_msg)。"""
    ms = E.models_list()
    if not ms:
        return None, None            # 没导入过模型（走引导话术）
    m = _NAME_RE.search(text)
    if m:
        want = m.group(1).strip().lower()
        for it in ms:
            if want in str(it.get("name") or "").lower():
                return it["id"], None
        return None, "没找到叫「%s」的音色，先去管理台导入吧" % m.group(1)
    cfg = _cfg(api)
    dft = str(cfg.get("default_model") or "")
    if dft and any(it["id"] == dft for ms in [E.models_list()] for it in ms):
        return dft, None
    return ms[0]["id"], None         # 没默认就用最新的


def _do_convert(api, chat_type, chat_id, reply_id, text):
    try:
        mid, err = _pick_model(api, text)
        if err:
            api.send(chat_type, chat_id, [err])
            return
        if mid is None:
            api.send(chat_type, chat_id,
                     ["还没有可用的翻唱音色～让主人去管理台「AI翻唱」页装一个吧"])
            return
        name = ""
        for it in E.models_list():
            if it["id"] == mid:
                name = str(it.get("name") or mid)
                break
        api.send(chat_type, chat_id,
                 ["收到～用「%s」的嗓子翻唱中，唱完马上发出来（大约半分钟）" % name])
        vf = api.get_voice_file(reply_id)
        if not vf.get("ok"):
            api.send(chat_type, chat_id,
                     ["没拿到那条语音：%s" % str(vf.get("error") or "")[:80]])
            return
        cfg = _cfg(api)
        r = E.convert(str(vf.get("path")), mid, cfg)
        if not r.get("ok"):
            api.send(chat_type, chat_id, ["翻唱翻车了：%s" % str(r.get("error") or "")[:100]])
            return
        api.send_voice(chat_type, chat_id, str(r.get("path")),
                       text="（用「%s」的音色翻唱了一段歌声，%s秒）"
                       % (name, r.get("dur") or "?"))
        E.log("QQ convert done chat=%s/%s model=%s dur=%s"
              % (chat_type, chat_id, mid, r.get("dur")))
    except Exception as e:
        api.log("CONVERT_FAIL %r" % e)
        try:
            api.send(chat_type, chat_id, ["翻唱出了点故障，稍后再试试～"])
        except Exception:
            pass
