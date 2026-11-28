# -*- coding: utf-8 -*-
"""冒烟：E0 评测工具的模型调用桩（eval_common.make_fixed_chain）。

全程离线：不调真实模型、不读生产数据目录里的任何文件。行为验证只用假的
call_fn（记录收到的参数，返回构造好的响应，形状照 eval_run._model_call 的
真实返回 (status, data, 耗时, usage)）；对两个被改脚本做纯文本静态断言；
--dry 路径用子进程真跑一次（先按 eval_run.py 的代码核实过：--dry 在写任何
结果文件之前就 return，只读题库、只构造提示词，不调模型、不写配置），跑完
把它在 runs/ 下写出的结果文件按精确路径删掉（若真的写了）；发现写了别的
文件就 FAIL 且不代删、留给人工处理。

数据目录隔离：照 _smoke_fwd.py 开头的做法，import 任何 eval 模块之前把
QQBOT_DATA_DIR 指到一次性临时目录（本测试只 import eval_common，它不做
任何文件读写，设上只是防 import 链上有人按它落盘）；--dry 子进程不受它
影响（要在真实环境里读题库），跑完删临时目录。"""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_SMOKE_OWN_DATA_DIR = tempfile.mkdtemp(prefix="qqbot_evaltools_iso_")
os.environ["QQBOT_DATA_DIR"] = _SMOKE_OWN_DATA_DIR

import eval_common as EC  # noqa: E402

fails = []
_total = [0]


def check(name, cond, detail=""):
    _total[0] += 1
    if cond:
        print("PASS %s" % name)
    else:
        print("FAIL %s  %s" % (name, detail))
        fails.append(name)


MSG = [{"role": "system", "content": "sys"}, {"role": "user", "content": "u"}]


def make_fake_call_fn(result=None):
    """假的 _model_call：记录收到的参数，返回构造好的 (status, data, 耗时, usage)。"""
    seen = []

    def call_fn(model, kv, mo, messages, text_ctx):
        seen.append({"model": model, "kv": kv, "mo": mo,
                     "messages": messages, "text_ctx": text_ctx})
        if result is not None:
            return result
        return 200, {"choices": [{"message": {"content": "你好"}}]}, 0.01, {}

    call_fn.seen = seen
    return call_fn


try:
    check("eval_common 有 make_fixed_chain", hasattr(EC, "make_fixed_chain"))

    # ---- 基本行为：返回 (固定模型名, 内容)，call_fn 按约定形状收到参数 ----
    fake = make_fake_call_fn()
    chain = EC.make_fixed_chain(fake, "mod-a", "k-1", 600)
    got = chain(MSG, "ctx")
    check("chain 返回 (固定模型名, 内容)", got == ("mod-a", "你好"), repr(got))
    s0 = fake.seen[0]
    check("call_fn 收到 (model, kv, mo, messages, text_ctx)",
          (s0["model"], s0["kv"], s0["mo"], s0["messages"], s0["text_ctx"])
          == ("mod-a", "k-1", 600, MSG, "ctx"), repr(s0))

    # ---- 与生产 call_glm_with_fallback 同签名：route / budget_sec / 多余 kw 收下 ----
    try:
        got = chain(MSG, "", budget_sec=5, route="judge", future_arg=1)
        check("带 route / budget_sec / 多余关键字参数调用不抛", True)
    except Exception as e:
        check("带 route / budget_sec / 多余关键字参数调用不抛", False, repr(e))
    check("route 不影响 call_fn 收到的固定参数",
          (fake.seen[-1]["model"], fake.seen[-1]["kv"], fake.seen[-1]["mo"])
          == ("mod-a", "k-1", 600), repr(fake.seen[-1]))

    # ---- 不给 pick_model：模型名一律固定 ----
    check("没给 pick_model 时模型名固定（gate）",
          chain(MSG, route="gate") == ("mod-a", "你好"))
    check("没给 pick_model 时模型名固定（judge）",
          chain(MSG, route="judge") == ("mod-a", "你好"))

    # ---- 给 pick_model：返回值里的模型名随 route 变，call_fn 仍用固定模型 ----
    fake2 = make_fake_call_fn()
    chain2 = EC.make_fixed_chain(
        fake2, "mod-a", "k-1", 600,
        pick_model=lambda r: {"judge": "mod-j", "gate": "mod-g"}.get(r) or "")
    check("pick_model：judge 用 mod-j", chain2(MSG, route="judge") == ("mod-j", "你好"))
    check("pick_model：gate 用 mod-g", chain2(MSG, route="gate") == ("mod-g", "你好"))
    check("pick_model：没配的 route 用固定模型",
          chain2(MSG, route="reply") == ("mod-a", "你好"))
    check("pick_model 只改模型名：call_fn 仍收固定 model/kv/mo",
          all(s["model"] == "mod-a" and s["kv"] == "k-1" and s["mo"] == 600
              for s in fake2.seen), repr(fake2.seen))

    pm_routes = []

    def pm(route):
        pm_routes.append(route)
        return {"judge": "mod-j"}.get(route) or ""
    chain3 = EC.make_fixed_chain(make_fake_call_fn(), "mod-a", "k-1", 600, pick_model=pm)
    chain3(MSG, route="judge")
    chain3(MSG, route="reply")
    check("pick_model 按本条 route 逐次调用",
          pm_routes == ["judge", "reply"], repr(pm_routes))

    # ---- pick_model 三元组：call_fn 连 kv / mo 一起换（docstring 写明的用法） ----
    fake4 = make_fake_call_fn()
    chain4 = EC.make_fixed_chain(fake4, "mod-a", "k-1", 600,
                                 pick_model=lambda r: ("mod-j", "k-2", 1500))
    got = chain4(MSG, route="judge")
    check("pick_model 三元组：返回三元组里的模型名", got == ("mod-j", "你好"), repr(got))
    s = fake4.seen[0]
    check("pick_model 三元组：call_fn 按三元组的 kv/mo 调",
          (s["model"], s["kv"], s["mo"]) == ("mod-j", "k-2", 1500), repr(s))
    fake4b = make_fake_call_fn()
    chain4b = EC.make_fixed_chain(fake4b, "mod-a", "k-1", 600,
                                  pick_model=lambda r: ("mod-j",))
    chain4b(MSG)
    s = fake4b.seen[0]
    check("pick_model 三元组缺项沿用固定值",
          (s["model"], s["kv"], s["mo"]) == ("mod-j", "k-1", 600), repr(s))

    # ---- 记账按用途分开，读取与清零 ----
    fake5 = make_fake_call_fn()
    chain5 = EC.make_fixed_chain(fake5, "mod-a", "k-1", 600)
    chain5(MSG, route="judge")
    chain5(MSG, route="judge")
    chain5(MSG, route="gate")
    chain5(MSG)
    check("记账按 route 分开",
          chain5.get_counts() == {"judge": 2, "gate": 1, "reply": 1},
          repr(chain5.get_counts()))
    check("call_counts 就是计数 dict 本体", chain5.call_counts == chain5.get_counts())
    chain5.reset_counts()
    check("reset_counts 清零", chain5.get_counts() == {}, repr(chain5.get_counts()))
    chain5(MSG, route="judge")
    check("清零后重新累计", chain5.get_counts() == {"judge": 1}, repr(chain5.get_counts()))

    # ---- 失败路径：非 200 / data 非 dict / data 为空 ----
    bad = EC.make_fixed_chain(
        make_fake_call_fn((429, {"error": {"message": "rate"}}, 0.01, {})),
        "mod-a", "k-1", 600)
    try:
        bad(MSG)
        check("非 200 抛 RuntimeError", False, "没抛")
    except RuntimeError as e:
        check("非 200 抛 RuntimeError", str(e).startswith("eval http 429"), repr(str(e)))
    bad2 = EC.make_fixed_chain(
        make_fake_call_fn((200, ["not-a-dict"], 0.01, {})), "mod-a", "k-1", 600)
    try:
        bad2(MSG)
        check("data 非 dict 抛 RuntimeError", False, "没抛")
    except RuntimeError as e:
        check("data 非 dict 抛 RuntimeError", str(e).startswith("eval http 200"),
              repr(str(e)))
    bad3 = EC.make_fixed_chain(
        make_fake_call_fn((200, None, 0.01, {})), "mod-a", "k-1", 600)
    try:
        bad3(MSG)
        check("data 为空抛 RuntimeError（empty 分支）", False, "没抛")
    except RuntimeError as e:
        check("data 为空抛 RuntimeError（empty 分支）",
              str(e) == "eval http 200: empty", repr(str(e)))

    # ---- 静态断言：两个脚本不再各自定义 _chain，都改用 make_fixed_chain ----
    src_run = (ROOT / "eval" / "eval_run.py").read_text(encoding="utf-8")
    src_gate = (ROOT / "eval" / "eval_gate_baseline.py").read_text(encoding="utf-8")
    check("eval_run.py 不再定义 _chain", "def _chain(" not in src_run)
    check("eval_gate_baseline.py 不再定义 _chain", "def _chain(" not in src_gate)
    check("eval_run.py 调用 make_fixed_chain", "make_fixed_chain(" in src_run)
    check("eval_gate_baseline.py 调用 make_fixed_chain",
          "make_fixed_chain(" in src_gate)
    check("eval_run.py work() 仍按场景清零 _tls.calls",
          "        _tls.calls = []" in src_run)

    # ---- --dry 路径仍能跑：只读题库、只构造提示词，不调模型、不写配置 ----
    runs_dir = pathlib.Path.home() / ".zcode-qq-bot" / "eval" / "runs"
    expected_out = runs_dir / "smoke_evaltools_dry.jsonl"
    before_runs = sorted(p.name for p in runs_dir.glob("*")) if runs_dir.is_dir() else []
    before_eval = sorted(p.name for p in runs_dir.parent.glob("*")) \
        if runs_dir.parent.is_dir() else []
    sub_env = dict(os.environ)
    sub_env.pop("QQBOT_DATA_DIR", None)   # --dry 要在真实环境里读题库
    sub_env["PYTHONIOENCODING"] = "utf-8"
    sub_env["PYTHONDONTWRITEBYTECODE"] = "1"
    dry = subprocess.run(
        [sys.executable, str(ROOT / "eval" / "eval_run.py"),
         "--src", str(ROOT / "plugins" / "qq-bot" / "scripts"),
         "--mode", "json", "--dry", "--limit", "1",
         "--tag", "smoke_evaltools_dry", "--set", "set_short.jsonl"],
        cwd=str(ROOT), capture_output=True, timeout=300, env=sub_env)
    tail = (dry.stdout.decode("utf-8", "replace")
            + dry.stderr.decode("utf-8", "replace")).strip().splitlines()
    print("  --dry 退出码 %s，输出尾部：%s" % (
        dry.returncode, " | ".join(tail[-3:]) if tail else "(空)"))
    check("eval_run --dry 退出码 0", dry.returncode == 0)
    after_runs = sorted(p.name for p in runs_dir.glob("*")) if runs_dir.is_dir() else []
    after_eval = sorted(p.name for p in runs_dir.parent.glob("*")) \
        if runs_dir.parent.is_dir() else []
    new_runs = [n for n in after_runs if n not in before_runs]
    new_eval = [n for n in after_eval if n not in before_eval]
    unexpected = [n for n in new_runs if n != "smoke_evaltools_dry.jsonl"]
    check("--dry 没写别的东西（runs/）", not unexpected, repr(unexpected))
    check("--dry 没写别的东西（eval/ 顶层）", not new_eval, repr(new_eval))
    if expected_out.exists():
        expected_out.unlink()
        left = expected_out.exists()
        check("结果文件已按精确路径删除", not left,
              "删除后仍存在" if left else "")
    else:
        check("结果文件已按精确路径删除", True,
              "（与代码路径一致：--dry 提前 return，未写出结果文件）")
finally:
    shutil.rmtree(_SMOKE_OWN_DATA_DIR, ignore_errors=True)
    check("临时数据目录已清理", not pathlib.Path(_SMOKE_OWN_DATA_DIR).exists())

print("合计：%d 项检查，失败 %d 项" % (_total[0], len(fails)))
sys.exit(1 if fails else 0)
