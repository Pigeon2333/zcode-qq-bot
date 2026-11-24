# 对话质量评测工具

用冻结的真实对话场景，在不上线、不发消息的前提下比较两个版本的提示词与模型输出。
背景见 `BOT_REPORTS/20261123-全面审计与对话质量复审.md` 第 2.7 节与第 4.7 节。

## 数据在哪

题库、快照、结果都在 `~/.zcode-qq-bot/eval/` 下（含群聊原文与 QQ 号，**不进仓库**）：

```
set_short.jsonl      题库·短历史（每个场景带当时提示词里的 14 行历史）
set_long.jsonl       题库·长历史（从 daemon 会话历史切出，带 40 行以上历史）
snapshots/           daemon 会话历史的快照
runs/                每次评测的逐条结果
```

## 脚本

| 脚本 | 作用 |
|---|---|
| `eval_build.py` | 从上下文注入日志冻结题库（短历史版） |
| `eval_snapshot.py` | `capture` 采集 daemon 会话历史；`carve` 切出长历史题库 |
| `eval_run.py` | 用指定源码目录对题库跑一遍，记录回复、延迟、token |
| `eval_score.py` | 风格指标、残留标记、开销，并与真人对照 |
| `eval_judge.py` | 裁判模型盲评两个版本（位置对调判两次） |
| `golden_compare.py` | 逐字节比对两份导出的提示词（改提示词装配代码后的回归） |

## 用法

```bash
# 1. 冻结题库（只需一次；想换场景时重跑）
python eval/eval_build.py --n-at 15 --n-random 35

# 2. 改了提示词装配之后，先确认旧路径一字未变
python eval/eval_run.py --src <主分支 scripts> --mode v1 --tag g1 --dry --dump a.jsonl
python eval/eval_run.py --src <待测 scripts>   --mode v1 --tag g2 --dry --dump b.jsonl
python eval/golden_compare.py a.jsonl b.jsonl --show

# 3. 两个版本各跑一遍（--mode v2 打开被测开关；--resume 只补跑出错的场景）
python eval/eval_run.py --src <scripts> --mode v1 --tag 基线名 --workers 2
python eval/eval_run.py --src <scripts> --mode v2 --tag 对照名 --workers 2

# 4. 打分与盲评
python eval/eval_score.py 基线名.jsonl 对照名.jsonl
python eval/eval_judge.py --src <scripts> 基线名.jsonl 对照名.jsonl

# 结构化输出稳定性（v2 提示词 + JSON 要求，一半场景带 response_format）
python eval/eval_run.py --src <scripts> --mode json --tag json测试名 --workers 2
```

`--src` 指向 `plugins/qq-bot/scripts`，可以是主分支或任意分支的工作目录。
脚本只读生产的人设、配置与密钥，不写生产文件，不发送消息；模型调用会产生费用
（50 个场景约 40 万输入 token）。

## 指标口径

与审计文档第 0 章「回复风格对照」一致：长度分布（≤10 字、11–30、31–60、>60）、
含问号、省略号、括号动作、叮嘱类词、emoji、清洗后残留的标记。真人基线取自同一批
场景里群友说过的话。

## 注意

- 并发 3 以上容易触发限流（`1305 访问量过大`），建议 `--workers 2`。
- daemon 的会话历史在内存里，组件重启后清零；长历史题库要在机器人运行一段时间后采集。
