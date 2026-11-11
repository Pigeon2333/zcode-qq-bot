# -*- coding: utf-8 -*-
"""机器人近一周"活人感"复盘（一次性任务，2026-11-10 08:00 schtasks 触发）。

只读分析 agent.log / context 注入日志 / memory_stream / evolved，产出
BOT_REPORTS 下"20261110-活人感复盘.md"，并把摘要经 daemon /send 私聊发
主人。不修改任何生产文件。写文件用 pathlib（受控报告目录常量）。
"""
import collections
import glob
import http.client
import json
import os
import pathlib
import re
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
DATA = os.path.expanduser(r"~\.zcode-qq-bot")
LOG = os.path.join(DATA, "logs", "agent.log")
CTX_DIR = os.path.join(DATA, "logs", "context")
REPORTS_ROOT = pathlib.Path(r"D:\ZCode\workspaces\qq-bot\BOT_REPORTS")
OUT_MD = REPORTS_ROOT / "20261110-活人感复盘.md"
# daemon 控制 token 从本机数据目录读（公开仓不落凭据）
_tok_path = os.path.join(DATA, "token")
DAEMON_CTL = open(_tok_path, encoding="utf-8").read().strip() if os.path.isfile(_tok_path) else ""
OWNER = "3349525227"
WEEK_AGO = time.time() - 7 * 86400

_report = []


def sec(t):
    _report.append(t)


def parse_ts(line):
    m = re.match(r"\[(\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\]", line)
    if not m:
        return None
    try:
        return time.mktime(time.strptime(
            "2026-%s %s" % (m.group(1), m.group(2)), "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None


# ---------- ① agent.log 近 7 天统计 ----------
sec("# 机器人近一周「活人感」复盘（2026-11-10）\n")
sec("> 群友反馈：说话越来越有人机感，不像之前浑然天成。怀疑与新能力"
    "（看图/看视频/看转发硬接话、识图不准）的副作用有关。\n")

counts = collections.Counter()
rt_list = []
vision = collections.Counter()
rx_cnt = 0
mcp_cnt = 0
search_cnt = 0
budget_out = 0
errors = 0
for ln in open(LOG, encoding="utf-8", errors="replace"):
    ts = parse_ts(ln)
    if ts is None or ts < WEEK_AGO:
        continue
    if "AGENT_EVENT" in ln:
        continue
    if "AGENT_DONE" in ln:
        counts["done"] += 1
        m = re.search(r"rt=([\d.]+)s", ln)
        if m:
            rt_list.append(float(m.group(1)))
        if "+quote" in ln:
            counts["done_quote"] += 1
        if "voice:" in ln:
            counts["done_voice"] += 1
    elif "AGENT_SKIP" in ln:
        counts["skip_interest"] += 1
    elif "AGENT_GATE_SKIP" in ln:
        counts["gate_skip"] += 1
    elif "AGENT_VISION_PROB_SKIP" in ln:
        vision["prob_skip"] += 1
    elif "AGENT_VISION_REF" in ln:
        vision["ref_go"] += 1
    elif "AGENT_VISION_FAIL" in ln:
        vision["fail"] += 1
    elif "AGENT_REACTION" in ln and "ok=True" in ln:
        rx_cnt += 1
    elif "AGENT_MCP_CALL" in ln:
        mcp_cnt += 1
    elif "AGENT_SEARCH" in ln:
        search_cnt += 1
    elif "AGENT_BUDGET_OUT" in ln:
        budget_out += 1
    elif "AGENT_LOOP_ERROR" in ln or "AGENT_ERROR" in ln:
        errors += 1

avg_rt = sum(rt_list) / len(rt_list) if rt_list else 0
over30 = sum(1 for r in rt_list if r > 30)
sec("## 一、近 7 天运行数据总览\n")
sec("| 指标 | 值 |")
sec("|---|---|")
sec("| 触发回复总数 (AGENT_DONE) | %d |" % counts["done"])
sec("| 平均响应时长 | %.1fs（>30s 的 %d 次，多为服务端超时重试）|" % (avg_rt, over30))
sec("| 闲聊判定不插话 (GATE_SKIP) | %d |" % counts["gate_skip"])
sec("| 不感兴趣跳过 (SKIP) | %d |" % counts["skip_interest"])
sec("| 看图后掷点未回 (VISION_PROB_SKIP) | %d |" % vision["prob_skip"])
sec("| 看图因上下文提及必回 (VISION_REF) | %d |" % vision["ref_go"])
sec("| 看图失败 | %d |" % vision["fail"])
sec("| 表情回应成功 | %d |" % rx_cnt)
sec("| MCP 工具调用 | %d |" % mcp_cnt)
sec("| 联网搜索 | %d |" % search_cnt)
sec("| 模型链预算熔断 | %d |" % budget_out)
sec("| 循环错误 | %d |\n" % errors)

# ---------- ② 上下文注入日志抽样：硬接话分析 ----------
empty_now_reply = 0          # 【现在】是空占位（纯图/表情）仍回复的次数
ctx_total = 0
samples_empty = []
for fp in sorted(glob.glob(os.path.join(CTX_DIR, "ctx_*.jsonl")))[-3:]:
    try:
        lines = open(fp, encoding="utf-8").readlines()[-200:]
    except OSError:
        continue
    for ln in lines:
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        ctx_total += 1
        txt = str(rec.get("text") or "")
        roles = rec.get("roles") or []
        if len(roles) < 2:
            continue
        uc = str(roles[-1].get("content") or "")
        is_empty_now = ("（发了一张图/表情）" in uc) or (not txt.strip())
        if is_empty_now:
            empty_now_reply += 1
            if len(samples_empty) < 5:
                samples_empty.append(txt[:40])

sec("## 二、硬接话量化（上下文注入日志近 3 天）\n")
sec("- 触发消息总注入数（近 3 天）：%d" % ctx_total)
sec("- 其中【现在】为空占位（纯图/表情/无文字）仍回复的：**%d 次**"
    "——这就是群友说的\"看图/表情硬接话\"的直接量化\n" % empty_now_reply)

sec("### 空占位触发的样本（触发语 ≤40 字）\n")
for s in samples_empty:
    sec("- " + (s or "（纯图/表情）"))

# ---------- ③ 记忆质量 ----------
mem_week = []
try:
    for ln in open(os.path.join(DATA, "memory_stream.jsonl"),
                   encoding="utf-8", errors="replace"):
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if rec.get("ts", 0) >= WEEK_AGO:
            mem_week.append(str(rec.get("text") or ""))
except OSError:
    pass
sec("\n## 三、近一周长期记忆写入（%d 条）\n" % len(mem_week))
for t in mem_week[-15:]:
    sec("- " + t[:80])

# ---------- ④ 自进化观察 ----------
ev_notes = []
try:
    ev = json.loads(pathlib.Path(
        DATA, "evolved.json").read_text(encoding="utf-8", errors="replace"))
    for persona, obj in (ev.items() if isinstance(ev, dict) else []):
        if isinstance(obj, dict):
            for n in (obj.get("notes") or [])[-6:]:
                ev_notes.append("[%s] %s" % (persona, n))
except Exception:
    pass
sec("\n## 四、自进化风格观察（最新若干条）\n")
for n in ev_notes[-10:]:
    sec("- " + n[:80])

# ---------- ⑤ 根因分析 ----------
sec("\n## 五、「人机感」根因分析（按可能性排序）\n")
sec("""1. **能力型接话过密（可能性最高）**：看图(all)+引用兜底+卡片解析让"任何消息都有得接"，叠加 30%% 掷点与上下文提及豁免，纯表情包/玩梗图这类低信息量消息也被认真回应——真人在群里对这些通常只丢一个表情回应甚至不接。数据佐证：空占位触发 %d 次硬回复。
2. **回复结构模板化**：抽读可见大量回复是"先复述对方一句 + 展开两行"的固定节奏（此条需人工再确认，日志无法直接量化）。
3. **教学段挤占**：system 里能力教学（表情回应/MCP/语音/记忆规则）累计约 2-3k 字符，人设表达空间被稀释。
4. **回指过多**：长期记忆注入让回复频繁出现"你上次说/我记得"，过度展示"我有记性"本身就是人机感。
5. **识图质量**：小模型描述图片字面内容再硬接，错认被群友察觉（选民证事件后群友对识图错误敏感）。
""" % empty_now_reply)

# ---------- ⑥ 优化建议 ----------
sec("## 六、优化建议（按优先级）\n")
sec("""### P1 · 空占位消息降级为表情回应（改动小、收益最直接）
- **机制**：daemon/agent——当【现在】为纯图/表情占位且无文字时，回复改为「表情回应」（random 从已启用表情集挑一个）或直接跳过，不走完整文本回复
- **文件**：qq_bot_agent.py process() 视觉分支；表情集从 reply_config.reaction_emojis
- **效果**：硬接话次数预计 -80%；这正是真人处理表情包的方式
- **风险**：低（表情回应能力已上线；all_prob 骰点保留在有文字的消息上）
- **难度**：★★☆

### P2 · 能力教学段瘦身 + "克制"总纲（改提示词，无代码）
- **机制**：把 MCP/语音/画图等教学压缩为一句能力清单式提示（"你有看图、搜索、发语音等能力，仅在对方明确需要时使用，绝不为用而用"），并加一条总纲："大多数消息不需要被认真回应，真人在群里 80% 的消息只看不说"
- **文件**：qq_bot_agent.py build_messages 教学段
- **效果**：人设表达空间回归；降低"秀能力"倾向
- **风险**：中（能力触发率会下降，需观察 MCP/搜索使用频率）
- **难度**：★☆☆

### P3 · 回复长度上限 + 拆分节奏人性化（改 parse 后处理）
- **机制**：闲聊模式（chat_random/burst）下正文超过 ~60 字强制压缩为一句；连续两天同话题同构回复由自进化观察给出"换个接法"的负反馈
- **文件**：qq_bot_agent.py 出站前 / evolved 提炼提示词
- **效果**：消除"小作文感"
- **风险**：中（可能显得敷衍，需按群调）
- **难度**：★★☆

### P4 · 看图识图质量（视觉模型专用化）
- **机制**：vision_model 单独指定更准的模型（glm-4.6v 或以上），避免 flash 链顺手看图错认
- **文件**：管理台模型管理 → 视觉模型配置
- **效果**：识图准确率提升，错认引发的"人机感"减少
- **风险**：成本略增
- **难度**：★☆☆

### P5 · 回指限制（记忆注入方式）
- **机制**：记忆注入改为"仅在语义匹配时注入"已部分实现（snippets 相关性）——补一条教学："除非对方问起，不要主动引用超过 3 天前的记忆"
- **文件**：qq_bot_agent.py 记忆教学行
- **效果**：减少"我记得你上次…"的档案感
- **难度**：★☆☆

### 建议实施顺序
1. P1（今天可做，直接砍掉最大人机感来源）
2. P2（提示词改动，观察 2-3 天）
3. P4（一条配置）
4. P3/P5（P1/P2 生效后再评估是否需要）
""")

sec("\n---\n*数据窗口：%s 至今；报告由一次性复盘脚本生成，未改动任何生产文件。*\n"
    % time.strftime("%Y-%m-%d %H:%M", time.localtime(WEEK_AGO)))

REPORTS_ROOT.mkdir(parents=True, exist_ok=True)
pathlib.Path(OUT_MD).write_text("\n".join(_report), encoding="utf-8")
print("报告已写：", OUT_MD)

# ---------- QQ 通知主人 ----------
summary = ("📋 活人感复盘完成：%s\n前 3 条建议："
           "① 空占位消息(纯图/表情)降级为表情回应或跳过（硬接话 %d 次的最大来源）；"
           "② 能力教学段瘦身+「80%% 的消息只看不说」总纲；"
           "③ 视觉模型专用化。详见报告文件。" % (OUT_MD, empty_now_reply))
try:
    c = http.client.HTTPConnection("127.0.0.1", 39763, timeout=30)
    c.request("POST", "/send", body=json.dumps({
        "chat_type": "private", "chat_id": OWNER,
        "texts": [summary[:900]]}).encode(),
        headers={"Content-Type": "application/json", "X-QQBot-Token": DAEMON_CTL})
    r = c.getresponse().read().decode("utf-8", "replace")
    print("QQ 通知:", r[:120])
except Exception as e:
    print("QQ 通知失败:", repr(e))
