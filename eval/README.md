# 对话质量评测工具

用冻结的真实对话场景，在不上线、不发消息的前提下比较两个版本的提示词与模型输出。
背景见 `BOT_REPORTS/20261123-全面审计与对话质量复审.md` 第 2.7 节与第 4.7 节。

## 数据在哪

题库、快照、结果都在 `~/.zcode-qq-bot/eval/` 下（含群聊原文与 QQ 号，**不进仓库**）：

```
set_short.jsonl      题库·短历史（每个场景带当时提示词里的 14 行历史）
set_long.jsonl       题库·长历史（从 daemon 会话历史切出，每个场景带 60 行之前的历史）
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
| `eval_gate_baseline.py` | 实测旧闸门（线上精简上下文开着时实际在用的那道轻量判定）的通过率和 token |

## 用法

```bash
# 1. 冻结题库（只需一次；想换场景时重跑）
python eval/eval_build.py --n-at 15 --n-random 35

# 2. 改了提示词装配之后，先确认旧路径一字未变
#    （两份导出要在同一时刻做：自进化观察、相对时间都跟着线上数据变）
python eval/eval_run.py --src <主分支 scripts> --mode v1 --tag g1 --dry --dump a.jsonl
python eval/eval_run.py --src <待测 scripts>   --mode v1 --tag g2 --dry --dump b.jsonl
python eval/golden_compare.py a.jsonl b.jsonl --show

# 3. 各跑一遍。三种模式：
#      v1   旧行为（提示词 + 主调用 + 文本协议，强制关掉 v2 开关）
#      v2   只用 v2 提示词，回复逻辑还是旧的 —— 隔离「历史扩窗」的效果
#      json 完整的新流程（判定 → 结构化输出 → 策略层）—— 将来上线的那条路径
python eval/eval_run.py --src <scripts> --mode v1   --tag 基线名 --workers 2
python eval/eval_run.py --src <scripts> --mode v2   --tag 只换提示词 --workers 2
python eval/eval_run.py --src <scripts> --mode json --tag 完整新流程 --workers 2
#    --resume 只补跑出错/缺失的场景；--set 换题库（默认 set_short.jsonl）
#    --cfg 键=值 临时覆盖回复配置里的一个键（可重复），只影响本次评测，不写配置文件。
#              例：--cfg len_follow_group=true 对比某个风格开关开和关

# 4. 打分与盲评（裁判甲乙位置对调各判一次，稳胜才计）
python eval/eval_score.py --set set_short.jsonl 基线名.jsonl 完整新流程.jsonl
python eval/eval_judge.py --src <scripts> --set set_short.jsonl 基线名.jsonl 完整新流程.jsonl --workers 2

# 长历史题库：daemon 跑了几小时之后
python eval/eval_snapshot.py capture
python eval/eval_snapshot.py carve --n-at 10 --n-random 30 --hist 60   # 每个场景带 60 行之前的历史
#    然后把上面的 --set 换成 set_long.jsonl 再跑一遍

# 5. 比 token 之前先量旧闸门（见下面「比 token 的注意事项」）
python eval/eval_gate_baseline.py --src <线上版 scripts> --set set_short.jsonl --tag oldgate_short \\
    --main-run 基线名.jsonl --new-run 完整新流程.jsonl
```

`--src` 指向 `plugins/qq-bot/scripts`，可以是主分支或任意分支的工作目录。
脚本只读生产的人设、配置与密钥，不写生产文件，不发送消息；模型调用会产生费用
（短历史 50 个场景：基线约 40 万输入 token，完整新流程约 20 万）。
并发保持 `--workers 2`：并发 3 会触发限流（1305）。
跑的过程中不要改被测目录里的源码；需要同时改代码时，先用 `git archive <提交> plugins/qq-bot/scripts`
导出一份冻结副本，`--src` 指向它。

## 指标口径

与审计文档第 0 章「回复风格对照」一致：长度分布（≤10 字、11–30、31–60、>60）、
含问号、省略号、括号动作、叮嘱类词、emoji、清洗后残留的标记。真人基线取自同一批
场景里群友说过的话。

## 注意

- 并发 3 以上容易触发限流（`1305 访问量过大`），建议 `--workers 2`。
- daemon 的会话历史在内存里，组件重启后清零；长历史题库要在机器人运行一段时间后采集。

## 比 token 的注意事项

- **`--mode v1` 不含旧闸门**：它让每条旁观消息都跑一次完整调用。而线上「精简上下文」
  默认开着，旧版每条旁观消息其实先过一次轻量判定，通过了才发完整调用。所以
  「新版比 v1 模式省多少」会大大高估新版相对线上的节省。要和线上比，用
  `eval_gate_baseline.py` 把旧闸门在同一批场景上真跑一遍再算。
- **直接@的回复和旁观消息分开算**：直接@两边都是一次完整调用，提示词大小相近；
  省或费都出在旁观消息上（判定调用的大小 × 条数 + 通过率 × 完整调用）。
- 风格开关会影响 token：比如开了「向群友的消息长度看齐」，回复变短、输出 token 变少。
  比较时两边的开关要一致，或者用 `--cfg` 显式指定。

## 盲评的读法

- 连发的多条回复会标成 ①② 给裁判看。早期版本是拿「 / 」拼成一串，裁判会把
  「A / B」读成拼接痕迹或几个备选——对连发多的回复不公平（结构化输出连发更多）。
  2026-11-24 之前的盲评数字都带着这个偏差。
- 样本通常只有十几对，且裁判自己也有波动：3 对以内的差距不要当结论。
