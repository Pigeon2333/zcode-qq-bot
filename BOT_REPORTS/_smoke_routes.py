# -*- coding: utf-8 -*-
"""冒烟：WP-05 路由表——机器人按用途选模型（model_routes.json），默认零变化。

对应施工总计划 WP-05「设计定稿」（2026-11-27）与派单验收 1 的每一项：
  A 文件不存在／全跟随主链：候选顺序与 _order_candidates(_chain_candidates()) 一致；
    预算默认 reply/memo=200、gate/judge=30；请求体不受路由影响（glm_chat 桩逐项核对）。
  B 四条路由逐个点名链上另一个条目（含「备用 Key」条目）：该路由先打点名模型，
    冷却／失败后回落主链；其它路由不受影响。
  C 点名条目已不在链上（模型不存在／Key 对不上）→ 当作跟随主链，每路由只提示一次。
  D 「立即使用」锁定仍排最前；高峰降耗不剔点名条目、但照旧剔链首。
  E 预算：配置文件的值覆盖默认、越界钳到 5–600；显式 budget_sec 优先于路由预算。
  F 用量：fallback 记录带 route；识图记录带 caption（源码静态）；/api/usage 的
    by_route 聚合，旧记录缺字段或为空归「未标注」。
  G 保存接口 POST /api/model-routes：空请求体=读取；未知键、不在链上的条目、
    预算越界、类型不对 → 400 带直白说明；合法请求规范化后原子写入。
  H 静态断言：qq_bot_agent.py 源码里每一处 call_glm_with_fallback( 调用都带
    route=；_persona_say 里那一处是字面量 route="reply"（不许把自己的人设路由
    形参传进去）；调用点归类与设计定稿一致。

隔离：import 被测模块前把 QQBOT_DATA_DIR 指到一次性临时目录（照 _smoke_fwdimg.py
的做法）；模型链/Key/路由配置全部写进临时目录；glm_chat/log_usage 打桩，零联网；
管理台处理器用 UiHandler.__new__ 直调，不起服务。测试里出现的模型名全部是假名字，
不预置任何厂商模型。跑完删临时目录。
用法：python BOT_REPORTS/_smoke_routes.py
"""
import contextlib
import copy
import io
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
_SMOKE_OWN_DATA_DIR = None
if not os.environ.get("QQBOT_DATA_DIR"):
    _SMOKE_OWN_DATA_DIR = tempfile.mkdtemp(prefix="qqbot_routes_iso_")
    os.environ["QQBOT_DATA_DIR"] = _SMOKE_OWN_DATA_DIR

import qq_bot_agent as A  # noqa: E402
import qq_admin_ui as U   # noqa: E402

fails = []


def chk(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name
          + (" | " + str(extra)[:160] if extra else ""))
    if not cond:
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


DATA = A.DATA_DIR
CHAIN_PATH = os.path.join(DATA, "model_chain.json")
ROUTES_PATH = A.MODEL_ROUTES_PATH
KEYS_PATH = os.path.join(DATA, "api_keys.json")

# 假模型名与假 Key（不预置任何厂商模型）
MAIN_KEY = "sk-test-main-000000000000000000000000"
MODELS = ["mod-a", {"model": "mod-b", "key": "k1"}, "mod-c"]


def write_json(path, obj):
    import pathlib
    pathlib.Path(path).write_text(
        json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def setup_chain():
    write_json(CHAIN_PATH, {"models": list(MODELS), "paused": False})
    write_json(KEYS_PATH, {"keys": [
        {"id": "k1", "label": "备一", "key": "sk-test-backup-0000000000000000",
         "provider": "zhipu", "base_url": ""},
        {"id": "k2", "label": "主Key", "key": MAIN_KEY,
         "provider": "zhipu", "base_url": ""}],
        "primary": "k2"})


def write_routes(routes=None, budget=None):
    obj = {}
    if routes is not None:
        obj["routes"] = routes
    if budget is not None:
        obj["budget"] = budget
    write_json(ROUTES_PATH, obj)


def reset_routes_file():
    try:
        os.unlink(ROUTES_PATH)
    except OSError:
        pass


def reset_state():
    A._ROUTE_WARNED.clear()
    with A.RT.lock:
        A.RT.cooldowns.clear()
        A.RT.pinned = None


_OK_DATA = {"choices": [{"message": {"content": " 好"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}}


def glm_stub(fail_models=(), calls=None):
    """glm_chat 桩：记录 (model, messages, kwargs)；fail_models 里的模型回 500。"""
    def _stub(model, messages, timeout=40, extra_body=None, key=None):
        if calls is not None:
            calls.append({"model": model, "messages": messages,
                          "extra_body": extra_body, "key": key})
        if model in fail_models:
            return 500, {"error": {"message": "server exploded"}}
        return 200, _OK_DATA
    return _stub


def run_fallback(route="reply", messages=None, **kw):
    msgs = messages if messages is not None else [{"role": "user", "content": "嗨"}]
    return A.call_glm_with_fallback(msgs, "", route=route, **kw)


class _FakeTime(object):
    """time 桩：每次 time() 前进 1000 秒 → 第一次预算检查就超时。"""

    def __init__(self, base=1000000.0):
        self.base = base
        self.n = 0

    def time(self):
        self.n += 1
        return self.base + self.n * 1000.0

    def strftime(self, fmt, ts=None):
        return "00:00:00"


def run_chain(route="reply", fail=("mod-a", "mod-b", "mod-c")):
    """让整条链全失败跑一遍，桩记录的模型序列＝完整候选序（全部失败会抛
    RuntimeError，这里吞掉只看顺序）。"""
    calls = []
    with patched(A, glm_chat=glm_stub(fail_models=set(fail), calls=calls)):
        try:
            run_fallback(route=route, messages=[{"role": "user", "content": "顺序"}])
        except RuntimeError:
            pass
    return call_models(calls)


def ui_call(method, path, bodyq=None):
    """进程内直调 UiHandler（照 _smoke_visionmove.py 的写法）。"""
    h = U.UiHandler.__new__(U.UiHandler)
    h.path = path
    raw = json.dumps(bodyq).encode("utf-8") if bodyq is not None else b""
    h.headers = {"Content-Length": str(len(raw))}
    h.rfile = io.BytesIO(raw)
    replies = []
    h._reply = lambda code, obj: replies.append((code, obj))
    h._authed = lambda u, q: True
    getattr(h, method)()
    return replies


def call_models(calls):
    return [c["model"] for c in calls]


# --------------------------------------------------------------- A 全跟随主链
def t_follow():
    reset_routes_file()
    reset_state()
    setup_chain()
    base = [(c[0], c[2]) for c in A._order_candidates(A._chain_candidates())]
    chk("A0 链上三条候选齐（主 Key 两条 + 备用 Key 一条）",
        base == [("main", "mod-a"), ("k1", "mod-b"), ("main", "mod-c")], base)
    for route in ("reply", "gate", "judge", "memo"):
        reset_state()
        got = run_chain(route=route)
        chk("A1 %s 跟随主链：候选顺序与 _order_candidates(_chain_candidates()) 一致"
            % route,
            got == [m for _k, m in base], got)
    chk("A2 默认预算：reply=200 memo=200 gate=30 judge=30",
        (A._route_budget_sec("reply"), A._route_budget_sec("memo"),
         A._route_budget_sec("gate"), A._route_budget_sec("judge")) == (200, 200, 30, 30),
        tuple(A._route_budget_sec(r) for r in ("reply", "memo", "gate", "judge")))
    # 请求体不受路由影响：同一 messages 对象、不同 route，桩收到的一致
    msgs = [{"role": "user", "content": "内容一致吗"}]
    snap = copy.deepcopy(msgs)
    recs = {}
    for route in ("reply", "gate", "judge", "memo"):
        reset_state()
        calls = []
        with patched(A, glm_chat=glm_stub(calls=calls)):
            run_fallback(route=route, messages=msgs)
        recs[route] = calls
    same = all(
        (c["messages"] is msgs or json.dumps(c["messages"], ensure_ascii=False)
         == json.dumps(recs["reply"][i]["messages"], ensure_ascii=False))
        and c["extra_body"] == recs["reply"][i]["extra_body"]
        and c["key"] == recs["reply"][i]["key"]
        for route in recs for i, c in enumerate(recs[route]))
    chk("A3 请求体不受路由影响：四条路由的 messages/extra_body/key 逐项一致",
        same and json.dumps(msgs, ensure_ascii=False) == json.dumps(snap, ensure_ascii=False),
        {r: call_models(v) for r, v in recs.items()})


# --------------------------------------------------------------- B 点名与回落
def t_named():
    setup_chain()
    for route in ("reply", "gate", "judge", "memo"):
        other = "gate" if route != "gate" else "reply"
        reset_state()
        write_routes(routes={other: {"model": "mod-b", "key": "k1"}})
        got = run_chain(route=route)
        chk("B1 %s 未点名不受影响：仍按主链序" % route,
            got == ["mod-a", "mod-b", "mod-c"], got)
        reset_state()
        got = run_chain(route=other)
        chk("B1 %s 点名备用 Key 条目 mod-b：先打点名模型" % other,
            got == ["mod-b", "mod-a", "mod-c"], got)
    # 点名字符串条目（主 Key 条目 mod-c）
    reset_state()
    write_routes(routes={"judge": {"model": "mod-c", "key": ""}})
    got = run_chain(route="judge")
    chk("B2 judge 点名字符串条目 mod-c（key 空=主 Key）：先打 mod-c",
        got == ["mod-c", "mod-a", "mod-b"], got)
    # 冷却回落：点名条目在冷却中照旧跳过
    reset_state()
    write_routes(routes={"gate": {"model": "mod-b", "key": "k1"}})
    A.RT.cooldown("k1:mod-b", 300)
    got = run_chain(route="gate", fail=("mod-a", "mod-c"))
    chk("B3 gate 点名 mod-b 但它冷却中：跳过点名条目，从主链链首开始",
        got == ["mod-a", "mod-c"], got)
    # 失败回落：点名条目 500 → 冷却 → 同一次调用内接着走主链
    reset_state()
    calls = []
    with patched(A, glm_chat=glm_stub(fail_models=("mod-b",), calls=calls)):
        run_fallback(route="gate")
    chk("B4 gate 点名 mod-b 它 500：同一次调用内先点名后回落主链",
        call_models(calls) == ["mod-b", "mod-a"], call_models(calls))
    calls = []
    with patched(A, glm_chat=glm_stub(calls=calls)):
        run_fallback(route="gate")
    chk("B4' 失败后的下一次 gate 调用：mod-b 已冷却，直接从主链开始",
        call_models(calls) == ["mod-a"], call_models(calls))
    A.RT.cooldowns.clear()
    # 其它路由不受点名的冷却影响：reply 首选仍是 mod-a
    calls = []
    with patched(A, glm_chat=glm_stub(calls=calls)):
        run_fallback(route="reply")
    chk("B5 gate 的点名与冷却不影响 reply：仍从 mod-a 开始",
        call_models(calls) == ["mod-a"], call_models(calls))


# ------------------------------------------------- C 点名条目不在链上
def t_missing():
    setup_chain()
    for label, ent in (("模型不在链上", {"model": "mod-x", "key": ""}),
                       ("Key 对不上", {"model": "mod-b", "key": "k9"}),
                       ("空模型名", {"model": "", "key": ""})):
        reset_state()
        write_routes(routes={"judge": ent})
        got = run_chain(route="judge")
        chk("C %s → 当作跟随主链" % label,
            got == ["mod-a", "mod-b", "mod-c"], got)
    # 每个路由每个进程只提示一次
    reset_state()
    write_routes(routes={"memo": {"model": "mod-x", "key": ""}})
    warned = []
    with patched(A, out=lambda s: warned.append(s)):
        A._route_named("memo", A._chain_candidates())
        A._route_named("memo", A._chain_candidates())
    chk("C' 对不上链的路由只提示一次",
        len([w for w in warned if "ROUTE_MODEL_MISS" in w]) == 1, warned)
    reset_state()
    reset_routes_file()


# ------------------------------------------------- D 锁定与高峰降耗
def t_pin_econ():
    setup_chain()
    reset_state()
    write_routes(routes={"gate": {"model": "mod-b", "key": "k1"}})
    A.RT.pin("main:mod-c")
    got = run_chain(route="gate")
    chk("D1 锁定 mod-c + gate 点名 mod-b：锁定仍排最前、点名第二",
        got == ["mod-c", "mod-b", "mod-a"], got)
    A.RT.unpin()
    # 高峰降耗：跟随主链剔链首；点名条目不参与剔链首
    with patched(A, econ_on=lambda: True):
        reset_routes_file()
        reset_state()
        got = run_chain(route="reply", fail=("mod-b", "mod-c"))
        chk("D2 高峰降耗、跟随主链：照旧剔链首（从 mod-b 开始）",
            got == ["mod-b", "mod-c"], got)
        write_routes(routes={"reply": {"model": "mod-b", "key": "k1"}})
        reset_state()
        got = run_chain(route="reply", fail=("mod-b", "mod-c"))
        chk("D3 高峰降耗、点名 mod-b：点名条目保留在前，主链链首被剔",
            got == ["mod-b", "mod-c"], got)
        reset_state()
        write_routes(routes={"reply": {"model": "mod-a", "key": ""}})
        got = run_chain(route="reply")
        chk("D4 高峰降耗、点名恰是链首 mod-a：点名条目不参与剔链首（链序原样）",
            got == ["mod-a", "mod-b", "mod-c"], got)
        # 锁定不在链首时，点名路由下「其余主链」的排序必须与跟随主链时
        # 一模一样（现有排序：有锁定条目就不剔链首），不能因为点名而多剔一个
        reset_state()
        A.RT.pin("main:mod-c")
        got_follow = run_chain(route="reply")
        # 上面遍历里锁定的条目失败后会自动解除锁定（现有行为），这里重新锁定
        reset_state()
        A.RT.pin("main:mod-c")
        write_routes(routes={"reply": {"model": "mod-b", "key": "k1"}})
        got = run_chain(route="reply")
        chk("D5 高峰降耗 + 锁定不在链首：点名路由下其余主链的排序与跟随主链时一致",
            got_follow == ["mod-c", "mod-a", "mod-b"]
            and got == ["mod-c", "mod-b", "mod-a"], (got_follow, got))
        A.RT.unpin()
    reset_state()
    reset_routes_file()


# ------------------------------------------------- E 预算
def t_budget():
    setup_chain()
    reset_state()
    reset_routes_file()
    write_routes(budget={"reply": 42, "gate": 77, "memo": 9999, "judge": 1})
    chk("E1 配置值覆盖默认（reply=42 gate=77）",
        (A._route_budget_sec("reply"), A._route_budget_sec("gate")) == (42, 77),
        (A._route_budget_sec("reply"), A._route_budget_sec("gate")))
    chk("E2 越界钳到 5–600（memo 9999→600 judge 1→5）",
        (A._route_budget_sec("memo"), A._route_budget_sec("judge")) == (600, 5),
        (A._route_budget_sec("memo"), A._route_budget_sec("judge")))
    ft = _FakeTime()
    with patched(A, time=ft):
        try:
            run_fallback(route="gate")
            raised = ""
        except RuntimeError as e:
            raised = str(e)
    chk("E3 预算耗尽报错带路由预算（gate=77）", "预算耗尽（77s）" in raised, raised)
    ft = _FakeTime()
    misses = []
    orig_bud = A._route_budget_sec

    def _spy(route):
        misses.append(route)
        return orig_bud(route)

    with patched(A, time=ft, _route_budget_sec=_spy):
        try:
            run_fallback(route="gate", budget_sec=9)
            raised = ""
        except RuntimeError as e:
            raised = str(e)
    chk("E4 显式 budget_sec 优先（9 不取路由的 77），且不读路由预算表",
        "预算耗尽（9s）" in raised and misses == [], (raised, misses))
    reset_routes_file()


# ------------------------------------------------- F 用量
def t_usage():
    setup_chain()
    reset_state()
    reset_routes_file()
    write_routes(routes={"gate": {"model": "mod-b", "key": "k1"}})
    recs = []
    with patched(A, glm_chat=glm_stub(), log_usage=lambda *a, **k: recs.append((a, k))):
        run_fallback(route="gate")
    a0 = recs[0][0] if recs else ()
    chk("F1 fallback 的用量记录带 route=gate 与点名模型",
        len(recs) == 1 and a0[0] == "mod-b" and a0[2] == "k1" and a0[3] == "gate",
        recs)
    reset_routes_file()
    # 真实 log_usage 写文件带 route 字段
    A.log_usage("mod-z", {"total_tokens": 7, "prompt_tokens": 2, "completion_tokens": 5},
                "main", "judge")
    lines = [json.loads(x) for x in pathlib.Path(A.USAGE_LOG_PATH).read_text(
        encoding="utf-8").splitlines() if x.strip()]
    chk("F2 log_usage 写出的记录带 route 字段",
        lines and lines[-1].get("route") == "judge" and lines[-1]["total_tokens"] == 7,
        lines[-1] if lines else None)
    # /api/usage 的 by_route：缺字段或为空的旧记录归「未标注」。
    # 前面用例的成功调用也写了真实记录——先取基线，再按追加行的增量核对。
    r0 = ui_call("do_GET", "/api/usage")
    base_route = {x["route"]: x for x in (r0[0][1].get("by_route", []) if r0 else [])}
    now = int(time.time())
    # 走模块自己的用量日志记录器写（与生产写入同一条路）。前两条是旧记录：
    # 一条没有 route 字段、一条 route 为空，都该归「未标注」。
    for rec in ({"ts": now, "model": "mod-a", "key": "main",
                 "prompt_tokens": 10, "completion_tokens": 20,
                 "total_tokens": 30},
                {"ts": now, "model": "mod-a", "key": "main", "route": "",
                 "prompt_tokens": 1, "completion_tokens": 1,
                 "total_tokens": 2},
                {"ts": now, "model": "mod-b", "key": "main", "route": "reply",
                 "prompt_tokens": 100, "completion_tokens": 200,
                 "total_tokens": 300},
                {"ts": now, "model": "mod-c", "key": "main", "route": "caption",
                 "prompt_tokens": 5, "completion_tokens": 5,
                 "total_tokens": 10},
                {"ts": now, "model": "mod-a", "key": "main", "route": "judge",
                 "prompt_tokens": 3, "completion_tokens": 4,
                 "total_tokens": 7}):
        A._usage_log.info(json.dumps(rec, ensure_ascii=False))
    r = ui_call("do_GET", "/api/usage")
    body = r[0][1] if r else {}
    br = {x["route"]: x for x in body.get("by_route", [])}

    def delta(name):
        return (br.get(name, {}).get("total", 0)
                - base_route.get(name, {}).get("total", 0))

    chk("F3 /api/usage 返回 by_route",
        r and r[0][0] == 200 and "by_route" in body,
        (r[0][0] if r else None, list(br)))
    chk("F4 by_route 聚合：reply+300 未标注+32 caption+10 judge+7",
        delta("reply") == 300 and delta("未标注") == 32
        and delta("caption") == 10 and delta("judge") == 7,
        {k: (base_route.get(k, {}).get("total"), br.get(k, {}).get("total"))
         for k in ("reply", "未标注", "caption", "judge")})
    chk("F5 by_route 口径与按模型一致（calls/prompt/completion/total 齐全）",
        all(set(("calls", "prompt", "completion", "total")) <= set(x) for x in br.values()),
        list(br.values())[:1])


# ------------------------------------------------- G 保存接口
def t_api():
    setup_chain()
    reset_state()
    reset_routes_file()
    r = ui_call("do_POST", "/api/model-routes")
    ok = r and r[0][0] == 200
    body = r[0][1] if r else {}
    chk("G1 空请求体＝读取：routes 全 null、budget 全默认、entries 来自模型链",
        ok and body.get("routes") == {"reply": None, "gate": None, "judge": None, "memo": None}
        and body.get("budget") == {"reply": 200, "gate": 30, "judge": 30, "memo": 200}
        and body.get("entries") == [{"model": "mod-a", "key": ""},
                                    {"model": "mod-b", "key": "k1"},
                                    {"model": "mod-c", "key": ""}], body)
    bad = [("G2 未知路由键", {"routes": {"hacker": None}, "budget": {}}),
           ("G2' 未知预算键", {"routes": {}, "budget": {"foo": 30}}),
           ("G3 条目不在链上", {"routes": {"reply": {"model": "mod-x", "key": ""}}, "budget": {}}),
           ("G3' Key 与链对不上", {"routes": {"reply": {"model": "mod-b", "key": "k9"}}, "budget": {}}),
           ("G3\" 空模型名", {"routes": {"gate": {"model": " ", "key": ""}}, "budget": {}}),
           ("G4 预算超上界", {"routes": {}, "budget": {"reply": 601}}),
           ("G4' 预算低于下界", {"routes": {}, "budget": {"gate": 4}}),
           ("G5 routes 不是对象", {"routes": [], "budget": {}}),
           ("G5' budget 不是对象", {"routes": {}, "budget": 30})]
    for name, req in bad:
        r = ui_call("do_POST", "/api/model-routes", req)
        ok = bool(r) and r[0][0] == 400 and str(r[0][1].get("error") or "").strip()
        chk("%s → 400 带直白说明" % name, ok, r[0][1] if r else None)
    # 合法保存：原子写入 + 规范化（key 空 → 不落 key 键）
    r = ui_call("do_POST", "/api/model-routes", {
        "routes": {"reply": None, "gate": {"model": "mod-b", "key": "k1"},
                   "judge": {"model": "mod-c", "key": ""}, "memo": None},
        "budget": {"reply": 180, "gate": 45, "judge": 30, "memo": 200}})
    body = r[0][1] if r else {}
    on_disk = {}
    try:
        on_disk = json.loads(pathlib.Path(ROUTES_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    chk("G6 合法保存 → 200 且规范化（key 空=省略 key 键）",
        bool(r) and r[0][0] == 200
        and body.get("routes", {}).get("judge") == {"model": "mod-c"}
        and body.get("routes", {}).get("gate") == {"model": "mod-b", "key": "k1"},
        body)
    chk("G6' 原子写入落盘内容与规范化一致，机器人侧立即读到",
        on_disk.get("routes", {}).get("judge") == {"model": "mod-c"}
        and on_disk.get("budget", {}).get("reply") == 180
        and A._model_routes_read()["budget"]["gate"] == 45,
        on_disk)
    chk("G6\" 保存接口不预置任何厂商模型名（下拉只来自用户链）",
        "entries" in body and all(e["model"].startswith("mod-") for e in body["entries"]),
        body.get("entries"))
    reset_routes_file()


# ------------------------------------------------- H 静态断言
_CALL_OWNER_ROUTES = {
    "_interrupt_gate": "gate", "_nudge_compose": "reply", "_nudge_compose_v2": "gate",
    "_semantic_dup_check": "judge", "_proactive_tick": ("judge", "reply"),
    "_botloop_judge": "judge", "_botloop_precheck": "judge",
    "_intent_botloop_verdict": "judge", "_persona_say": "reply",
    "devctrl_agent_loop": "reply", "evolve_tick": "memo", "_mcp_agent_loop": "reply",
    "_reply_gate_v2": "gate", "_generate_reply_v2": "reply", "process": (
        "reply", "reply", "reply", "reply", "reply", "reply", "memo"),
    "_daily_review_once": "memo",
}


def _call_text(src, start):
    depth = 0
    i = start
    while i < len(src):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return src[start:i + 1]


def t_static():
    src = open("plugins/qq-bot/scripts/qq_bot_agent.py", encoding="utf-8").read()
    lines = src.split("\n")
    owners = []
    for i, l in enumerate(lines):
        m = re.match(r"^def (\w+)", l)
        if m:
            owners.append((i + 1, m.group(1)))
    bad, per_owner = [], {}
    n_calls = 0
    for m in re.finditer(r"call_glm_with_fallback\(", src):
        ln = src.count("\n", 0, m.start()) + 1
        line_start = src.rfind("\n", 0, m.start()) + 1
        if src[line_start:m.start()].strip().startswith("def"):
            continue
        n_calls += 1
        text = _call_text(src, m.start())
        owner = "?"
        for fo, fn in owners:
            if fo <= ln:
                owner = fn
            else:
                break
        if "route=" not in text:
            bad.append((ln, owner, "缺 route="))
            continue
        got = re.findall(r'route="([a-z]+)"', text)
        per_owner.setdefault(owner, []).append(got[0] if got else "?变量")
    chk("H1 源码里每一处 call_glm_with_fallback( 调用都带 route=（共 %d 处）"
        % n_calls, not bad and n_calls == 23, bad or n_calls)
    chk("H2 调用点归类与设计定稿一致",
        all(per_owner.get(k, []) == (list(v) if isinstance(v, tuple) else [v])
            for k, v in _CALL_OWNER_ROUTES.items())
        and set(per_owner) == set(_CALL_OWNER_ROUTES),
        {k: v for k, v in per_owner.items()})
    # _persona_say：字面量 reply，绝不把自己的人设路由形参传进去
    i = src.index("def _persona_say")
    j = src.index("\ndef ", i + 10)
    body = src[i:j]
    ps = [m.start() for m in re.finditer(r"call_glm_with_fallback\(", body)]
    ok = len(ps) == 1 and 'route="reply"' in _call_text(body, ps[0]) \
        and not re.search(r"route=route", _call_text(body, ps[0]))
    chk("H3 _persona_say 里那一处是字面量 route=\"reply\"（不传自己的 route 形参）",
        ok, _call_text(body, ps[0])[-60:] if ps else "没有调用")
    # 识图两条路的成功记录都带 caption
    vi = src.index("def call_vision")
    vj = src.index("\ndef ", vi + 10)
    ci = src.index("def _caption_call")
    cj = src.index("\ndef ", ci + 10)
    chk("H4 识图两条路（call_vision / _caption_call）的成功记录带 route=\"caption\"",
        'route="caption"' in src[vi:vj] and 'route="caption"' in src[ci:cj], "")
    # 行数兜底：确认没有第 24 处调用漏标（23 处调用 + 1 处定义，定义行已排除）
    chk("H5 调用总数与现状一致（23 处调用）", n_calls == 23, n_calls)


def main():
    t_follow()
    t_named()
    t_missing()
    t_pin_econ()
    t_budget()
    t_usage()
    t_api()
    t_static()
    print()
    print("失败 %d 项" % len(fails) if fails else "全部通过 ✓")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    try:
        main()
    finally:
        if _SMOKE_OWN_DATA_DIR:
            shutil.rmtree(str(_SMOKE_OWN_DATA_DIR), ignore_errors=True)
