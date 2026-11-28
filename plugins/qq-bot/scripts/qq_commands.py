#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qq_commands.py — 主人指令表（纯数据 + 纯逻辑，不 import 项目里的其它模块）

为「指令代理化」（C 批）服务：主人私聊不再只能手打精确命令，而是由判定
模型把自然语言认成「哪条命令 + 参数」，再渲染成精确命令文本交给现有的
owner_command 执行（执行体零重复，回复与手打命令逐字相同）。

模块只做三件事，全部在内存里完成：不碰网络、不碰文件、不碰数据目录，
不读配置开关，不调模型，也不 import qq_bot_agent：

  - 指令表 COMMANDS：18 条主人私聊命令，每条写明 name/risk/intent/desc/
    usage（精确命令的写法，参数校验没过时回给主人看）/examples（三条例句，
    兼判定模型的提示词与测试用例）/params/render（参数 → 精确命令文本）。
    带必填参数的命令另有 ask_missing（缺参时的反问文本）；删定时、切换人设
    两条再带 ask_extra，标记反问时要附上的列表命令——附列表本身由 agent 侧
    实现，这里只放标记。enum 参数可带 aliases（同义归一：「开/打开/启用」
    →「开启」这类），归一后仍不在选项里照样拒。
  - 参数校验 validate：判定模型给的参数做类型转换与校验，任何一项不过
    （必填缺失、类型不对、越界、enum 不在选项、含换行、subset 参数不是
    主人原话的子串、渲染后超过 400 字）一律不通过；通过时返回规范化后的
    参数（时间补零、int 落地、超顶的分钟数/条数按 owner_command 自己的
    规则钳顶）。
  - 提示词片段 intent_doc：只列 intent=True 的命令（名字、说明、三条例句、
    参数说明），给判定模型看；纯函数、输出确定。

「subset」规矩：标识型参数（任务原文、定时内容、人设名、群号、定时编号）
必须能在主人原话里找到——两边去掉全部空白字符后子串成立——判定模型不许
编造主人没说过的东西。数字类（定时编号、群号）还要带数字边界：在原话里
按「前后都不是数字」找，不许从「13」里截出「3」、从「1555001」里截出
「555001」。时间允许被改写（「八点半」→ 08:30）但要过格式校验。

缺省参数不补进 norm_args：渲染时省略（「日志」），缺省值由 owner_command
自己的规则兜底（日志 12 条）；唯一的例外是安静——裸「安静」不命中
owner_command 的正则（「分钟?」的量词只管「钟」字），渲染时把缺省分钟数
补成 10，回复与主人手打的期望逐字一致。渲染文本长度按 owner_command 入口
检查钳在 400 字（正文 strip 后超过 400 字直接不中）。

C1 只做表与纯逻辑；C2 接线只开放查询类与安静、继续九条；C3 起会改设置的
九条 write 也开放（intent 全 True），write 靠更高的置信度门槛、标识型参数
必须出自主人原话、以及 agent 侧 tool_confirm_write 开时的复述确认来兜。
risk 归类：read＝只读信息；soft＝安静/继续这类可随时反悔的；write＝有对
外副作用的。
"""
import re

RISKS = ("read", "soft", "write")
PARAM_TYPES = ("int", "gid", "hm", "enum", "text")

# 判定模型置信度门槛（C2 用）：写类误判代价高，门槛更高。
INTENT_MIN_CONF = {"read": 0.7, "soft": 0.7, "write": 0.85}

# owner_command 入口：正文 strip 后超过 400 字直接不中——渲染文本同一条杠。
RENDER_MAX = 400

# 「暂不支持」标注的原文：intent_doc 的 mark 模式给未开放条目加的行尾标注。
# agent 侧的提示词引用这个常量判断清单里有没有标注（有才输出标注说明与
# 「不许代替」那条规矩）——两边用同一份，不许各写各的字面量。
CLOSED_NOTE = ("（暂不支持：主人要的正是这件事时，is_cmd 填 false，"
               "不要换成清单里的其它条目）")

# 用 [0-9] 不用 \d：\d 连全角数字也认，全角群号会被 owner_command 的 \d
# 放行后存成对不上的字符串。
_GID_RE = re.compile(r"[0-9]{5,12}\Z")
_HM_RE = re.compile(r"([0-9]{1,2}):([0-9]{2})\Z")


# ---- 参数 → 精确命令文本（render 字段用的函数；无参命令直接放整条文本） ----

def _render_quiet(a):
    # 手打裸「安静」如今也命中 owner_command（Z-20 后省略分钟数按默认 10 算），
    # 但这里渲染的是带分钟数的精确写法「安静 10 分钟」，回复与手打逐字一致
    # （owner_command 对省略同样按 10 算）。
    return "安静 %d 分钟" % a.get("minutes", 10)


def _render_log(a):
    return ("日志 %d" % a["n"]) if "n" in a else "日志"


def _render_cron_add(a):
    return "定时 %s %s" % (a["time"], a["text"])


def _render_cron_del(a):
    return "删定时 %d" % a["id"]


def _render_persona_switch(a):
    return "切换人设 %s" % a["name"]


def _render_group_add(a):
    return "加群 %s" % a["gid"]


def _render_group_remove(a):
    return "移除群 %s" % a["gid"]


def _render_feature_toggle(a):
    return "%s %s" % (a["action"], a["feature"])


def _render_zcode_handoff(a):
    return "转交ZCode %s" % a["task"]


# ---- 表：18 条主人指令（顺序即 intent_doc 的顺序） ----

COMMANDS = [
    {
        "name": "help",
        "risk": "read",
        "intent": True,
        "desc": "发一遍主人遥控指令的帮助清单",
        "usage": "帮助",
        "examples": ("你都会些啥指令", "看看帮助", "把菜单发我看看"),
        "params": (),
        "render": "帮助",
    },
    {
        "name": "status",
        "risk": "read",
        "intent": True,
        "desc": "查看机器人当前的运行状态（管道、账号、模型）",
        "usage": "状态",
        "examples": ("给我报个状态", "现在什么状态", "看看你的运行状态"),
        "params": (),
        "render": "状态",
    },
    {
        "name": "usage",
        "risk": "read",
        "intent": True,
        "desc": "查看模型 token 用量统计（今日、近 7 天、总量）",
        "usage": "用量",
        "examples": ("今天用了多少 token", "看看最近的用量", "模型用量统计一下"),
        "params": (),
        "render": "用量",
    },
    {
        "name": "pause",
        "risk": "write",
        "intent": True,
        "desc": "完全暂停自动回复（运维用；恢复前一律不回任何人）",
        "usage": "暂停",
        "examples": ("先暂停，别回复任何人", "暂停一下所有自动回复",
                     "现在开始停一停，全部暂停"),
        "params": (),
        "render": "暂停",
    },
    {
        "name": "resume",
        "risk": "write",
        "intent": True,
        "desc": "解除暂停，恢复自动回复",
        "usage": "恢复",
        "examples": ("好了，恢复回复吧", "可以恢复了", "解除暂停，继续干活"),
        "params": (),
        "render": "恢复",
    },
    {
        "name": "quiet",
        "risk": "soft",
        "intent": True,
        "desc": "让机器人安静若干分钟：群里不再主动说话，有人@或回复仍会答",
        "usage": "安静 分钟数",
        "examples": ("安静半小时", "别在群里说话了，安静俩小时", "先安静个五分钟吧"),
        "params": ({
            "name": "minutes",
            "type": "int",
            "required": False,
            "subset": False,
            "min": 0,
            "max": 10080,
            "clamp_max": 720,
            "default": 10,
            "desc": "安静多少分钟；省略＝10，最多算一周，超过 720 按 720 算",
        },),
        "render": _render_quiet,
    },
    {
        "name": "unquiet",
        "risk": "soft",
        "intent": True,
        "desc": "结束安静状态、恢复正常聊天；主人让她安静之后说会开完了、忙完了、不用安静了这类话也算；只是让她接着讲、接着做某件事的不算",
        "usage": "继续",
        "examples": ("好了，可以说话了", "继续吧", "出来说话",
                     "会开完啦，可以继续了", "忙完了，恢复吧", "不用安静了，继续"),
        "params": (),
        "render": "继续",
    },
    {
        "name": "log",
        "risk": "read",
        "intent": True,
        "desc": "看最近的服务日志（默认 12 条，最多 40 条）",
        "usage": "日志 条数",
        "examples": ("看看最近的日志", "把最近 30 条日志发我", "拉 5 条日志给我瞅瞅"),
        "params": ({
            "name": "n",
            "type": "int",
            "required": False,
            "subset": False,
            "min": 1,
            "max": 999,
            "clamp_max": 40,
            "default": 12,
            "desc": "要看多少条；省略＝12，超过 40 按 40 算",
        },),
        "render": _render_log,
    },
    {
        "name": "cron_add",
        "risk": "write",
        "intent": True,
        "desc": "设一个每天到点的定时任务：到点后机器人把这句话当任务交给"
                "沙箱执行器跑，结果私聊发你（提醒类的事也可以这么说）",
        "usage": "定时 HH:MM 要做的事",
        "examples": ("每天早上八点半提醒我给花浇水", "每晚十点整提醒我写日报",
                     "晚上十一点半的时候，记得提醒我关窗"),
        "params": ({
            "name": "time",
            "type": "hm",
            "required": True,
            "subset": False,
            "desc": "每天几点执行，HH:MM（24 小时制，分钟要两位）；口语时间换算成它",
        }, {
            "name": "text",
            "type": "text",
            "required": True,
            "subset": True,
            "min": 4,
            "max": 300,
            "desc": "定时要做的事：取主人原话里完整的那个短语（从「提醒我…」"
                    "起，如「提醒我写日报」），至少 4 个字，必须用原话",
        }),
        "render": _render_cron_add,
        "ask_missing": "定几点、做什么？一句话说全，比如「每天八点半提醒我喝水」",
    },
    {
        "name": "cron_list",
        "risk": "read",
        "intent": True,
        "desc": "列出当前所有定时代码任务",
        "usage": "定时列表",
        "examples": ("现在都有哪些定时任务", "看看定时列表", "我把哪些定时都列一下"),
        "params": (),
        "render": "定时列表",
    },
    {
        "name": "cron_del",
        "risk": "write",
        "intent": True,
        "desc": "按编号删除一个定时代码任务",
        "usage": "删定时 编号",
        "examples": ("把 3 号定时删了", "把定时 5 那条删了吧", "2 号那个定时不要了"),
        "params": ({
            "name": "id",
            "type": "int",
            "required": True,
            "subset": True,
            "desc": "要删的定时编号（定时列表里的 #号），必须用主人原话里的数字",
        },),
        "render": _render_cron_del,
        "ask_missing": "删哪个？报个编号（定时列表里的 #号）",
        "ask_extra": "cron_list",
    },
    {
        "name": "persona_list",
        "risk": "read",
        "intent": True,
        "desc": "列出所有人设和当前激活的人设",
        "usage": "人设列表",
        "examples": ("现在有哪些人设", "看看人设列表", "把人设都列出来"),
        "params": (),
        "render": "人设列表",
    },
    {
        "name": "persona_switch",
        "risk": "write",
        "intent": True,
        "desc": "切换到指定名字的人设，即时生效",
        "usage": "切换人设 人设名",
        "examples": ("切换到小雪这个人设", "换成人设「毒舌」", "用「助理」那个人设回我消息"),
        "params": ({
            "name": "name",
            "type": "text",
            "required": True,
            "subset": True,
            "min": 1,
            "max": 80,
            "desc": "人设名，必须用主人原话里的原文",
        },),
        "render": _render_persona_switch,
        "ask_missing": "切换到哪个人设？报个名字",
        "ask_extra": "persona_list",
    },
    {
        "name": "group_add",
        "risk": "write",
        "intent": True,
        "desc": "把一个群号加进群回应白名单",
        "usage": "加群 群号",
        "examples": ("把 555001 这个群加进白名单", "加个群 555002", "555003 也加进去"),
        "params": ({
            "name": "gid",
            "type": "gid",
            "required": True,
            "subset": True,
            "desc": "群号（5–12 位数字），必须用主人原话里的数字",
        },),
        "render": _render_group_add,
        "ask_missing": "拉哪个群？把群号发我",
    },
    {
        "name": "group_list",
        "risk": "read",
        "intent": True,
        "desc": "列出机器人在哪些群里（群回应白名单）",
        "usage": "群列表",
        "examples": ("你都在哪些群里", "看看群列表", "列一下白名单里的群"),
        "params": (),
        "render": "群列表",
    },
    {
        "name": "group_remove",
        "risk": "write",
        "intent": True,
        "desc": "把一个群号从群回应白名单移除",
        "usage": "移除群 群号",
        "examples": ("把 555002 移出白名单", "移除群 555003", "555001 不用了，从白名单里撤了吧"),
        "params": ({
            "name": "gid",
            "type": "gid",
            "required": True,
            "subset": True,
            "desc": "群号（5–12 位数字），必须用主人原话里的数字",
        },),
        "render": _render_group_remove,
        "ask_missing": "移除哪个群？报群号",
    },
    {
        "name": "feature_toggle",
        "risk": "write",
        "intent": True,
        "desc": "开启或关闭某个功能（看图、联网搜索、防滥用）",
        "usage": "开启/关闭 看图|联网搜索|防滥用",
        "examples": ("把看图关掉吧", "联网搜索给我打开", "防滥用先关一会儿"),
        "params": ({
            "name": "action",
            "type": "enum",
            "required": True,
            "subset": False,
            "choices": ("开启", "关闭"),
            "aliases": {"开": "开启", "打开": "开启", "启用": "开启",
                        "开启": "开启",
                        "关": "关闭", "关掉": "关闭", "关闭": "关闭",
                        "停用": "关闭", "禁用": "关闭"},
            "desc": "开还是关（说「开/打开/启用/关/关掉/停用/禁用」也认）",
        }, {
            "name": "feature",
            "type": "enum",
            "required": True,
            "subset": False,
            "choices": ("看图", "联网搜索", "防滥用"),
            "aliases": {"识图": "看图", "图片识别": "看图",
                        "搜索": "联网搜索", "联网": "联网搜索"},
            "desc": "要动哪个功能（说「识图/图片识别」＝看图，"
                    "「搜索/联网」＝联网搜索）",
        }),
        "render": _render_feature_toggle,
        "ask_missing": "开还是关、哪个功能？看图、联网搜索、防滥用，说一个",
    },
    {
        "name": "zcode_handoff",
        "risk": "write",
        "intent": True,
        "desc": "把一个任务原文转交到电脑上 ZCode 的收件箱，打开 ZCode 即可接手",
        "usage": "转交ZCode 任务原文",
        "examples": ("把这个任务转交给 ZCode：写一个爬虫脚本", "转给 ZCode 处理一下：整理周报数据",
                     "让 ZCode 帮我修一下登录的bug"),
        "params": ({
            "name": "task",
            "type": "text",
            "required": True,
            "subset": True,
            "min": 4,
            "max": 2000,
            "desc": "要转交的任务原文，4–2000 字，必须用主人原话里的原文",
        },),
        "render": _render_zcode_handoff,
        "ask_missing": "要转交啥任务？把活儿说给我，我放进 ZCode 收件箱",
    },
]


# ---- 加载时全量自检（照 qq_tools.register 的手法：表坏了当场炸） ----

def _check_table():
    """字段齐、名字唯一、risk/intent/subset 合法、usage 非空、例句三条互不
    相同、有必填参数的命令要有反问文本、ask_extra 指向表里真有的命令、
    enum 的 aliases 是「非空字典且归一的值都在 choices 里」。"""
    seen = set()
    for c in COMMANDS:
        for key in ("name", "risk", "intent", "desc", "usage", "examples",
                    "params", "render"):
            if key not in c:
                raise ValueError("指令 %r 缺字段 %s" % (c.get("name"), key))
        name = c["name"]
        if not isinstance(name, str) or not name.strip():
            raise ValueError("指令名要是非空字符串，现在是 %r" % (name,))
        if name in seen:
            raise ValueError("指令名重复：%s" % name)
        seen.add(name)
        if c["risk"] not in RISKS:
            raise ValueError("指令 %s 的 risk 不合法：%r" % (name, c["risk"]))
        if not isinstance(c["intent"], bool):
            raise ValueError("指令 %s 的 intent 要是布尔" % name)
        if not isinstance(c["usage"], str) or not c["usage"].strip():
            raise ValueError("指令 %s 的 usage 要是非空字符串"
                             "（给主人看的精确命令写法）" % name)
        if not isinstance(c["desc"], str) or not c["desc"].strip():
            raise ValueError("指令 %s 的 desc 要是非空字符串" % name)
        ex = c["examples"]
        if (not isinstance(ex, (list, tuple)) or len(ex) < 3
                or len(set(ex)) != len(ex)
                or not all(isinstance(x, str) and x.strip() for x in ex)):
            raise ValueError("指令 %s 的 examples 要是三条互不相同的非空口语" % name)
        req = 0
        pnames = set()
        for p in c["params"]:
            for key in ("name", "type", "required", "subset", "desc"):
                if key not in p:
                    raise ValueError("指令 %s 的参数 %r 缺字段 %s"
                                     % (name, p.get("name"), key))
            if p["type"] not in PARAM_TYPES:
                raise ValueError("指令 %s 参数 %s 的 type 不合法：%r"
                                 % (name, p["name"], p["type"]))
            if p["name"] in pnames:
                raise ValueError("指令 %s 参数名重复：%s" % (name, p["name"]))
            pnames.add(p["name"])
            if p["required"]:
                req += 1
            if p["subset"] and not p["required"]:
                raise ValueError("指令 %s 参数 %s 是 subset 却非必填"
                                 % (name, p["name"]))
            if p["type"] == "enum" and not (isinstance(p.get("choices"),
                                                       (list, tuple))
                                            and p["choices"]
                                            and all(isinstance(x, str) and x
                                                    for x in p["choices"])):
                raise ValueError("指令 %s 参数 %s 是 enum 但 choices 不合法"
                                 % (name, p["name"]))
            if "aliases" in p:
                _al = p["aliases"]
                if not isinstance(_al, dict) or not _al \
                        or not all(isinstance(k, str) and k
                                   and v in p.get("choices", ())
                                   for k, v in _al.items()):
                    raise ValueError("指令 %s 参数 %s 的 aliases 要是非空字典"
                                     "且归一的值都在 choices 里"
                                     % (name, p["name"]))
            if p["type"] == "text" and not (isinstance(p.get("min"), int)
                                            and isinstance(p.get("max"), int)):
                raise ValueError("指令 %s 参数 %s 是 text 但没写 min/max 长度"
                                 % (name, p["name"]))
        if req and not str(c.get("ask_missing", "")).strip():
            raise ValueError("指令 %s 有必填参数但没有 ask_missing 反问文本" % name)
        if "ask_extra" in c and c["ask_extra"] not in seen:
            raise ValueError("指令 %s 的 ask_extra 指向表里没有的命令：%r"
                             % (name, c.get("ask_extra")))


_check_table()

_BY_NAME = dict((c["name"], c) for c in COMMANDS)


def by_name(name):
    """按名字取指令条目（表本体，只读，别改）；没有这条返回 None。"""
    return _BY_NAME.get(name)


# ---- 参数校验（就地归一化） ----

def _squash(text):
    """去掉全部空白字符（空格、换行、制表、全角空格都算）：subset 子串核对
    两边都用它——「在原话但空格不同」也算数。"""
    return "".join(str(text or "").split())


def _norm_one(p, v):
    """单个参数的类型转换与校验，返回 (ok, 规范值, 原因)。int 收纯数字文本
    不收布尔；hm 要 H:MM（分钟两位）且 00:00–23:59，补零成 HH:MM；gid 收
    5–12 位数字（数字或文本都行），落成文本；enum 要在 choices 里；text 去
    首尾空白、不许带换行、长度按 min/max。clamp_max：范围过了先按上限拒、
    没过再把超顶的值钳回去（安静 720、日志 40，与 owner_command 一致）。"""
    n = p["name"]
    t = p["type"]
    if t == "int":
        if isinstance(v, bool):
            return False, None, "参数 %s 要整数（true/false 不算）" % n
        if isinstance(v, int):
            pass
        elif isinstance(v, str) and v.isascii() and v.isdigit():
            v = int(v)
        else:
            return False, None, "参数 %s 要整数（纯数字的文本也行）" % n
        lo = p.get("min")
        hi = p.get("max")
        if lo is not None and v < lo:
            return False, None, "参数 %s 最小 %s" % (n, lo)
        if hi is not None and v > hi:
            return False, None, "参数 %s 最大 %s" % (n, hi)
        clamp = p.get("clamp_max")
        if clamp is not None and v > clamp:
            v = clamp
        return True, v, ""
    if t == "gid":
        if isinstance(v, bool):
            return False, None, "参数 %s 要群号（5–12 位数字）" % n
        if isinstance(v, int):
            v = str(v)
        if not isinstance(v, str) or not _GID_RE.fullmatch(v.strip()):
            return False, None, "参数 %s 要 5–12 位数字的群号" % n
        return True, v.strip(), ""
    if t == "hm":
        m = _HM_RE.fullmatch(v.strip()) if isinstance(v, str) else None
        if not m:
            return False, None, ("参数 %s 要 HH:MM 的时间（分钟要两位，"
                                 "像 8:30 或 08:30）" % n)
        hh, mm = int(m.group(1)), int(m.group(2))
        if hh > 23 or mm > 59:
            return False, None, "参数 %s 要 00:00–23:59 的时间" % n
        return True, "%02d:%02d" % (hh, mm), ""
    if t == "enum":
        if not isinstance(v, str):
            return False, None, "参数 %s 要是 %s 之一" % (n, "/".join(p["choices"]))
        s = v.strip()
        ali = p.get("aliases") or {}
        if s in ali:
            s = ali[s]                     # 同义归一（「开」→「开启」这类）
        if s not in p["choices"]:
            return False, None, "参数 %s 要是 %s 之一" % (n, "/".join(p["choices"]))
        return True, s, ""
    # text
    if not isinstance(v, str):
        return False, None, "参数 %s 要文本（字符串）" % n
    if "\n" in v or "\r" in v:
        return False, None, "参数 %s 不能带换行" % n
    v = v.strip()
    if not v:
        return False, None, "参数 %s 不能是空的" % n
    if len(v) < p["min"] or len(v) > p["max"]:
        return False, None, "参数 %s 长度要 %d–%d 字" % (n, p["min"], p["max"])
    return True, v, ""


def validate(name, args, text):
    """按表校验判定模型给的参数。返回 (ok, norm_args, reason)：不过时
    norm_args 是空 dict、reason 是一句直白说明；通过时 norm_args 是规范化
    后的参数（时间补零、int 落地、超顶钳回），可以直接喂 render。
    text 是主人原话：subset=True 的参数值必须在原话里（两边去掉全部空白后
    子串成立；int/gid 类再带数字边界——前后都不是数字才算）。最后把渲染
    文本过一遍 400 字入口检查——owner_command 不收的，这里就不放行。未知
    命令名、args 不是 dict、多了不认识的参数，都算不过。"""
    c = by_name(name)
    if c is None:
        return False, {}, "没有这条指令：%s" % name
    if not isinstance(args, dict):
        return False, {}, "参数要是 JSON 对象（{…}）"
    known = dict((p["name"], p) for p in c["params"])
    unknown = [k for k in args if k not in known]
    if unknown:
        return False, {}, "多了不认识的参数：%s" % "、".join(str(k) for k in unknown)
    norm = {}
    for p in c["params"]:
        n = p["name"]
        if n not in args:
            if p["required"]:
                return False, {}, "缺少必填参数：%s" % n
            continue
        ok, v, why = _norm_one(p, args[n])
        if not ok:
            return False, {}, why
        if p.get("subset"):
            hay = _squash(text)
            needle = _squash(v)
            if p["type"] in ("int", "gid"):
                # 数字带边界找（前后都不是数字）：「把 13 号定时删了」里
                # 不许截出「3」、「1555001」里不许截出「555001」。
                hit_in_text = re.search(r"(?<![0-9])" + re.escape(needle)
                                        + r"(?![0-9])", hay)
            else:
                hit_in_text = needle in hay
            if not hit_in_text:
                return False, {}, ("参数 %s 要用主人原话里的原文：%r 不在原话里"
                                   % (n, v))
        norm[n] = v
    try:
        rt = render(name, norm)
    except ValueError as exc:
        return False, {}, str(exc)
    if len(rt) > RENDER_MAX:
        return False, {}, "拼出来的命令超过 %d 字，owner_command 不收" % RENDER_MAX
    return True, norm, ""


def missing(name, args):
    """缺失的必填参数名列表（只看有没有给，不做其它校验）；命令名不认识
    抛 ValueError（与 render 一致）；args 不是 dict 时按空 dict 算。"""
    c = by_name(name)
    if c is None:
        raise ValueError("没有这条指令：%s" % name)
    if not isinstance(args, dict):
        args = {}
    return [p["name"] for p in c["params"]
            if p["required"] and p["name"] not in args]


def explain_failure(name, why):
    """参数校验没过时回给主人的确定性文本：一句口语，含原因（validate 给的
    why）和这条指令精确命令的写法（usage），不说教。只拼文本、不做任何
    动作；名字不认识（正常路径到不了这里）就只回原因。"""
    c = by_name(name)
    usage = str(c.get("usage") or "").strip() if c else ""
    reason = str(why or "").strip() or "参数没过校验"
    if not usage:
        return "这句话我没能当成指令来执行：%s" % reason
    return ("这句话我没能当成指令来执行：%s。要办这件事可以直接发：%s"
            % (reason, usage))


def render(name, norm_args):
    """把 validate 通过的参数拼成精确命令文本（喂给 owner_command 一定命中
    的那种）。参数没先过 validate（缺必填键、类型不对、越界、enum 不在
    选项）直接抛 ValueError——宁可炸也不拼出半截命令。render 字段是无参
    命令的整条文本（str）或带参命令的函数。"""
    c = by_name(name)
    if c is None:
        raise ValueError("没有这条指令：%s" % name)
    if not isinstance(norm_args, dict):
        raise ValueError("参数要是 dict，现在是 %r" % (norm_args,))
    clean = {}
    for p in c["params"]:
        n = p["name"]
        if n not in norm_args:
            if p["required"]:
                raise ValueError("缺必填参数 %s：先过 validate 再 render，"
                                 "不许拼出半截命令" % n)
            continue
        ok, v, why = _norm_one(p, norm_args[n])
        if not ok:
            raise ValueError("参数 %s 没过 validate（%s）" % (n, why))
        clean[n] = v
    r = c["render"]
    return r(clean) if callable(r) else r


# ---- 给判定模型看的提示词片段 ----

def _param_note(p):
    """一个参数的说明：必填的不带 ?；约束写在 desc 里。"""
    return "%s%s（%s）" % (p["name"], "" if p["required"] else "?", p["desc"])


def intent_doc(max_chars=2500, closed="mark"):
    """给判定模型看的提示词片段：closed="mark"（默认）＝全列，未开放的条目
    在行尾加「暂不支持」标注（清单里没有主人要的那条时，模型会被迫在已开放
    条目里挑最像的，标注让模型对这类话直接回 is_cmd=false）；closed="hide"
    ＝只列 intent=True 的；closed="plain"＝全列但不标注（评测对照用）。
    全部条目都开放时（C3 起的现状）没有可标注的条目，mark 的输出与 hide
    完全相同——参数保留，给将来再分批留。一条一行「名字：说明。例：
    「…」「…」「…」。参数：…」。总长不超过 max_chars（默认 2500），放不下
    的从第一条放不下的起整条截掉，末尾注明还剩几条（那行说明不计入预算）；
    标注计入长度。纯函数、输出确定。"""
    if closed not in ("mark", "hide", "plain"):
        raise ValueError("closed 要是 mark/hide/plain 之一：%r" % (closed,))
    note = CLOSED_NOTE
    lines = []
    total = sum(1 for c in COMMANDS
                if c["intent"] or closed in ("mark", "plain"))
    used = 0
    for c in COMMANDS:
        if not c["intent"] and closed == "hide":
            continue
        ps = "、".join(_param_note(p) for p in c["params"]) or "无"
        line = "- %s：%s。例：「%s」。参数：%s" % (
            c["name"], c["desc"], "」「".join(c["examples"]), ps)
        if closed == "mark" and not c["intent"]:
            line += note
        if used + len(line) + 1 > max_chars:
            rest = total - len(lines)
            if rest > 0:
                lines.append("（还有 %d 条指令没列出）" % rest)
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)
