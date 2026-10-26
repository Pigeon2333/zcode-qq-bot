#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qq_task_worker.py — 沙箱任务执行器（主人任务模式的引擎）

独立进程，由 qq_bot_agent.py 的能力桥 spawn。工作方式类似一个微型编码代理：
从 stdin 读任务描述 → 循环（GLM 决策 → 执行动作 → 观察结果）→ 最终答复打到 stdout。

动作协议（每轮模型输出必须是以下四种之一，首行标记）：
  RUN <命令行>      在沙箱目录执行白名单程序（python/pip/dir/type/where/tree/findstr）；
                    复杂逻辑先 WRITE 一个 .py 再 RUN python xx.py
  WRITE <相对路径>   随后紧跟一个 ``` 围栏块写入文件（路径禁 .. 越界）
  READ <相对路径>    读沙箱内文件（<=8000 字节）
  DONE              随后内容为给主人看的最终答复

安全边界：任务仅由主人 QQ 触发（桥侧校验）；--workdir 强制校验位于任务根内；
RUN 走参数列表 + 程序白名单（shell=False，无拼接注入面）；文件读写限制在沙箱内；
总轮次与单命令时长双限制。

用法：echo 任务描述 | python qq_task_worker.py --workdir <path> [--max-turns 15]
退出码：0=正常完成（stdout 为 DONE 答复）；2=异常
"""

import argparse
import os
import pathlib
import shlex
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import qq_bot_agent as A  # 复用 glm_chat / DATA_DIR / 模型链常量（模块级无副作用）

RUN_TIMEOUT = 60
READ_LIMIT = 8000
OUT_LIMIT = 3000

_RUN_WHITELIST = {"python", "python3", "py", "pip", "pip3",
                  "dir", "type", "where", "tree", "findstr"}

SYSTEM_TMPL = (
    "你是任务执行器，在沙箱目录里完成主人交给你的任务（写代码/脚本/处理数据等）。"
    "每轮只输出一个动作，首行必须是动作标记：\n"
    "RUN <命令行> —— 在工作目录执行；可用程序白名单：python/pip/dir/type/where/"
    "tree/findstr（写法示例：RUN python solve.py）；复杂逻辑请先 WRITE 一个 "
    ".py 文件再 RUN python 运行它\n"
    "WRITE <相对路径> —— 下一行用 ``` 围栏给出文件完整内容，我会写入沙箱\n"
    "READ <相对路径> —— 读取沙箱内文件\n"
    "DONE —— 后面直接给出给主人看的最终中文答复：简洁说明做了什么、"
    "关键结果（代码放 ``` 围栏，长文件只贴要点）、产物文件相对路径。"
    "不要提\"动作\"\"协议\"这些内部词。\n\n"
    "工作目录：%(workdir)s\n"
    "约束：所有文件只写在沙箱目录内；命令用白名单程序完成；"
    "先想清楚再动手，步骤尽量少；与任务无关的事一律不做。")


def task_root_dir():
    """任务沙箱根目录（app_config.task_workspace 或默认 ~/.zcode-qq-bot/tasks）。"""
    root = os.path.join(A.DATA_DIR, "tasks")
    try:
        ws = str(A.C.load_app_config().get("task_workspace") or "").strip()
        if ws:
            root = ws
    except Exception:
        pass
    return os.path.realpath(root)


def _safe_path(cwd, rel):
    """沙箱内相对路径 → 绝对路径；空段/含 .. /越出沙箱返回 None。"""
    rel = str(rel).replace("\\", "/").strip().lstrip("/")
    segs = [s for s in rel.split("/") if s]
    if not segs or any(s == ".." for s in segs):
        return None
    root = os.path.realpath(cwd)
    path = os.path.realpath(os.path.join(root, *segs))
    if path != root and not path.startswith(root + os.sep):
        return None
    return path


def run_cmd(cwd, cmd_line):
    """白名单程序 + 参数列表执行（shell=False，无拼接注入面）。"""
    try:
        parts = shlex.split(str(cmd_line), posix=False)
    except ValueError as e:
        return "命令解析失败：%r" % e
    parts = [str(p).strip('"') for p in parts if str(p).strip('"')]
    if not parts:
        return "空命令"
    prog = os.path.splitext(os.path.basename(parts[0]))[0].lower()
    if prog not in _RUN_WHITELIST:
        return ("程序 %r 不在白名单；复杂逻辑请先 WRITE 一个 .py 再 RUN python 运行"
                % parts[0])
    if prog in ("python", "python3", "py") and os.path.isfile(sys.executable):
        parts[0] = sys.executable
    try:
        p = subprocess.run(parts, cwd=cwd, capture_output=True,
                           timeout=RUN_TIMEOUT, shell=False)
        out = (p.stdout or b"") + (p.stderr or b"")
        text = out.decode("utf-8", "replace").strip()
        return "exit=%d\n%s" % (p.returncode, text[:OUT_LIMIT] or "(无输出)")
    except subprocess.TimeoutExpired:
        return "命令超时（>%ds），换更快的方式或拆步" % RUN_TIMEOUT
    except OSError as e:
        return "执行失败：%r" % e


def read_file(cwd, rel):
    path = _safe_path(cwd, rel)
    if path is None:
        return "拒绝：路径非法或越出沙箱"
    try:
        with pathlib.Path(path).open("r", encoding="utf-8", errors="replace") as f:
            return f.read(READ_LIMIT)
    except OSError as e:
        return "读取失败：%r" % e


def write_file(cwd, rel, content):
    path = _safe_path(cwd, rel)
    if path is None:
        return "拒绝：路径非法或越出沙箱"
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8", newline="")
    return "已写入 %s（%d 字节）" % (rel, len(content.encode("utf-8")))


def extract_fence(text):
    """从 WRITE 动作后的文本中提取首个 ``` 围栏内容。"""
    lines = text.split("\n")
    start = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if start is None and s.startswith("```"):
            start = i + 1
            continue
        if start is not None and s.startswith("```"):
            return "\n".join(lines[start:i])
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--max-turns", type=int, default=15)
    args = ap.parse_args()

    # 沙箱校验：workdir 必须位于任务根目录内
    root = task_root_dir()
    real = os.path.realpath(args.workdir)
    if real != root and not real.startswith(root + os.sep):
        sys.stderr.write("workdir escapes task root\n")
        sys.exit(2)
    workdir = pathlib.Path(real)
    workdir.mkdir(parents=True, exist_ok=True)

    task = sys.stdin.read(4000).strip()

    model = "glm-5.3-flash"
    try:
        chain = A.load_json_const(A.MODEL_CHAIN_PATH, {})
        if chain.get("models"):
            model = chain["models"][0]
    except Exception:
        pass

    messages = [
        {"role": "system", "content": SYSTEM_TMPL % {"workdir": str(workdir)}},
        {"role": "user", "content": "任务：%s" % task},
    ]
    final = ""
    budget_end = time.time() + 480  # 总预算 8 分钟，防止逐轮超时叠加拖满
    for _turn in range(args.max_turns):
        if time.time() > budget_end:
            final = "任务耗时较长已强制收尾，当前进展见沙箱目录文件。"
            break
        try:
            status, data = A.glm_chat(model, messages, timeout=90,
                                      extra_body={"max_tokens": 4096})
        except (OSError, ValueError) as e:
            print("GLM 调用失败：%r" % e)
            sys.exit(2)
        if status != 200:
            print("GLM HTTP %d：%s" % (
                status, str(data.get("error", {}).get("message", ""))[:120]))
            sys.exit(2)
        try:
            A.log_usage(model, data.get("usage") or {}, "task")
        except Exception:
            pass  # 统计失败不影响任务
        reply = (data["choices"][0]["message"]["content"] or "").strip()
        messages.append({"role": "assistant", "content": reply})
        first = reply.split("\n", 1)[0].strip()
        rest = reply.split("\n", 1)[1] if "\n" in reply else ""
        if first.startswith("DONE"):
            final = rest.strip() or "（任务结束，无说明）"
            break
        if first.startswith("RUN "):
            obs = run_cmd(str(workdir), first[4:].strip())
        elif first.startswith("WRITE "):
            rel = first[6:].strip()
            content = extract_fence(rest)
            obs = write_file(str(workdir), rel, content or "") if content is not None \
                else "格式错误：WRITE 后需要 ``` 围栏内容"
        elif first.startswith("READ "):
            obs = read_file(str(workdir), first[5:].strip())
        else:
            obs = ("格式错误：首行必须是 RUN/WRITE/READ/DONE 之一。你刚才输出的是：%s"
                   % first[:60])
        messages.append({"role": "user", "content": "执行结果：\n%s" % obs})
    else:
        final = "已达最大轮次，强制收尾。最后进展见沙箱目录文件。"

    print(final)
    sys.exit(0)


if __name__ == "__main__":
    main()
