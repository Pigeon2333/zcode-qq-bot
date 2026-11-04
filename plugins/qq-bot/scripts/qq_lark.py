# -*- coding: utf-8 -*-
"""qq_lark.py — 飞书互通引擎（宿主模块；设置页由「飞书互通」插件承载）。

能力（用户在飞书开放平台免费创建自建应用，填 app_id/secret 即可）：
  ① QQ→飞书 群消息推送（纯 REST，零第三方依赖）：指定 QQ 群的消息实时转发到飞书群
  ② 飞书→QQ 反向（可选）：用飞书官方 lark-oapi SDK 的 WebSocket 长连接收事件
     （免公网 IP，与 NapCat 同款接入模型）。未装 SDK 时只开 ①
安全：app_secret 只存本机 DATA_DIR/lark_config.json；消息转发在本地完成
"""

import http.client
import json
import pathlib
import threading
import time
import urllib.parse

import qq_common as C

CFG_PATH = pathlib.Path(C.DATA_DIR) / "lark_config.json"
_TOKEN = {"value": "", "exp": 0.0}
_DAEMON = None            # on_tick 注入，供飞书→QQ 方向发消息
_WS_THREAD = {"started": False}


def cfg():
    try:
        d = json.loads(CFG_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cfg(d):
    CFG_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2),
                        encoding="utf-8")


# ---------------------------------------------------------------------------
# 飞书 REST 基础
# ---------------------------------------------------------------------------

def _api_post(path, body, token=None, timeout=30):
    conn = http.client.HTTPSConnection("open.feishu.cn", timeout=timeout)
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = "Bearer " + token
    conn.request("POST", path, body=json.dumps(body).encode("utf-8"),
                 headers=headers)
    r = conn.getresponse()
    data = r.read()
    conn.close()
    try:
        j = json.loads(data.decode("utf-8", "replace"))
    except ValueError:
        j = {"code": -1, "msg": data[:120].decode("utf-8", "replace")}
    return r.status, j


def _api_get(path, token, timeout=30):
    conn = http.client.HTTPSConnection("open.feishu.cn", timeout=timeout)
    conn.request("GET", path, headers={"Authorization": "Bearer " + token})
    r = conn.getresponse()
    data = r.read()
    conn.close()
    try:
        return r.status, json.loads(data.decode("utf-8", "replace"))
    except ValueError:
        return r.status, {}


def tenant_token(c=None):
    c = c or cfg()
    app_id = str(c.get("app_id") or "").strip()
    app_secret = str(c.get("app_secret") or "").strip()
    if not app_id or not app_secret:
        raise RuntimeError("未配置飞书自建应用的 app_id / app_secret")
    if _TOKEN["value"] and time.time() < _TOKEN["exp"]:
        return _TOKEN["value"]
    st, j = _api_post("/open-apis/auth/v3/tenant_access_token/internal",
                      {"app_id": app_id, "app_secret": app_secret})
    tok = str((j.get("tenant_access_token") or ""))
    if st != 200 or not tok:
        raise RuntimeError("获取飞书 token 失败: %s" % j.get("msg", j))
    _TOKEN["value"] = tok
    _TOKEN["exp"] = time.time() + int(j.get("expire", 3600)) - 300
    return tok


def send_text(chat_id, text):
    """向飞书群发文本（receive_id_type=chat_id）。"""
    st, j = _api_post(
        "/open-apis/im/v1/messages?receive_id_type=chat_id",
        {"receive_id": str(chat_id), "msg_type": "text",
         "content": json.dumps({"text": str(text)[:4000]}, ensure_ascii=False)},
        token=tenant_token())
    if st != 200 or j.get("code") not in (0, None):
        raise RuntimeError("飞书发送失败: %s %s" % (j.get("code"), j.get("msg")))
    return True


def list_chats():
    """机器人所在的飞书群列表（设置页下拉用）。"""
    tok = tenant_token()
    out = []
    page = ""
    for _ in range(3):
        st, j = _api_get("/open-apis/im/v1/chats?page_size=50" + page, tok)
        items = ((j.get("data") or {}).get("items")) or []
        for it in items:
            out.append({"chat_id": it.get("chat_id"), "name": it.get("name")})
        if not ((j.get("data") or {}).get("has_more")):
            break
        page = "&page_token=" + str((j.get("data") or {}).get("page_token") or "")
    return out


# ---------------------------------------------------------------------------
# 桥：QQ 群 ↔ 飞书群
# ---------------------------------------------------------------------------

def _bridge_for(qq_group):
    qq = str(qq_group)
    for b in (cfg().get("bridges") or []):
        if str(b.get("qq_group")) == qq and b.get("to_lark", True):
            return b
    return None


def on_qq_message(msg):
    """插件 on_message 观察器 → 匹配桥 → 推飞书（开线程，不阻塞主链路）。"""
    try:
        if msg.get("chat_type") != "group" or msg.get("is_bot"):
            return
        b = _bridge_for(msg.get("chat_id"))
        if not b:
            return
        text = str(msg.get("text") or "").strip()
        if not text:
            return
        name = str(msg.get("sender_name") or msg.get("sender_id") or "?")
        line = "「%s」%s" % (name, text[:500])
        threading.Thread(target=_try_send, args=(b.get("lark_chat_id"), line),
                         daemon=True).start()
    except Exception:
        pass


def _try_send(chat_id, line):
    try:
        send_text(chat_id, line)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 飞书→QQ：lark-oapi WebSocket 长连接（可选，检测到 SDK 才启用）
# ---------------------------------------------------------------------------

def sdk_available():
    try:
        import lark_oapi  # noqa
        return True
    except ImportError:
        return False


def start_ws_bridge(daemon):
    """启动飞书事件长连接（幂等）。需要 pip install lark-oapi。"""
    global _DAEMON
    _DAEMON = daemon
    c = cfg()
    if not c.get("enabled") or not c.get("app_id") or not c.get("app_secret"):
        return
    if not any(b.get("to_qq") for b in (c.get("bridges") or [])):
        return
    if not sdk_available() or _WS_THREAD["started"]:
        return

    def _run():
        try:
            import lark_oapi as lark
            from lark_oapi.api.im.v1 import (
                P2ImMessageReceiveV1, ReplyMessageRequestBody)

            def handler(ctx, data = None):
                try:
                    ev = data if data is not None else ctx
                    msg = getattr(ev, "event", None) or {}
                    item = msg.get("message") or {}
                    chat_id = item.get("chat_id")
                    content = json.loads(item.get("content") or "{}")
                    text = str(content.get("text") or "").strip()
                    sender = ((msg.get("sender") or {}).get("sender_id") or {}) \
                        .get("user_id") or "飞书用户"
                    if not text:
                        return
                    for b in (cfg().get("bridges") or []):
                        if b.get("to_qq") and b.get("lark_chat_id") == chat_id:
                            _DAEMON.send_texts(str(b.get("qq_group")),
                                               ["[飞书] %s: %s" % (sender, text[:800])])
                            break
                except Exception:
                    pass

            client = lark.ws.Client(c.get("app_id"), c.get("app_secret"),
                                    event_handler=P2ImMessageReceiveV1(handler))
            client.start()
        except Exception as e:
            _WS_THREAD["started"] = False

    _WS_THREAD["started"] = True
    threading.Thread(target=_run, daemon=True).start()
