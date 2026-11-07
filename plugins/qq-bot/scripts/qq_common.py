#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qq_common.py — 公共基础：路径推导、环境配置、端口约定

发布适配原则：
  - 一切路径由本模块动态推导，任何业务文件禁止硬编码绝对路径
  - 环境差异（QQNT 安装目录、机器人账号、端口）集中在 app_config.json，
    不存在时自动生成默认值（QQNT 目录用注册表探测）
  - 数据目录 ~/.zcode-qq-bot/（跟随用户家目录，跨用户可用）
"""

import json
import os
import pathlib
import time
import winreg

# 脚本目录（本文件所在目录），所有业务脚本与其同级
SCRIPTS_DIR = os.path.dirname(os.path.realpath(__file__))
# 数据目录（用户家目录下）
# 数据目录：默认 ~/.zcode-qq-bot；可用环境变量 QQBOT_DATA_DIR 覆盖
# （发布版测试 / 便携多实例用；生产保持默认）
DATA_DIR = os.environ.get("QQBOT_DATA_DIR") or os.path.join(
    os.path.expanduser("~"), ".zcode-qq-bot")
DATA_ROOT = pathlib.Path(DATA_DIR)

APP_VERSION = "0.5.11"         # 发布版本号（更新检查与本页展示用；发版时同步改）
# 更新默认指向主仓库（已开源，release 资产匿名可读；旧发布仓库已归档）
DEFAULT_UPDATE_REPO = "Pigeon2333/zcode-qq-bot"

APP_CONFIG_PATH = DATA_ROOT / "app_config.json"

DEFAULT_CONFIG = {
    "qqnt_dir": "",            # 空 = 自动探测注册表；探测失败由用户在设置页填写
    "bot_qq": "",              # 机器人 QQ 号
    "owner_qq": "",            # 主人 QQ（任务模式与私聊遥控的唯一白名单，空=禁用）
    "task_workspace": "",      # 任务沙箱根目录（空=~/.zcode-qq-bot/tasks；可指向 ZCode 工作区子目录）
    "napcat_ws_port": 3001,
    "daemon_port": 39763,
    "agent_admin_port": 39764,
    "ui_port": 39765,
    "autostart_with_windows": False,
    "auto_power_on": False,    # 打开应用窗口后自动启动机器人组件
    "close_action": "tray",    # tray=最小化到托盘 | exit_app=退出应用(机器人保持) | exit_all=退出并停止机器人
    "review_time": "03:30",    # 夜间复盘触发时间（HH:MM）
    "backup_enabled": True,    # 每日数据快照开关
    "backup_time": "04:00",    # 备份触发时间（HH:MM）
    "backup_dir": "",          # 备份目录（空=~/.zcode-qq-bot/backups）
    "logs_dir": "",            # 日志目录（空=~/.zcode-qq-bot/logs；改动重启各组件后生效）
    "logs_autoclean_enabled": False,  # 日志自动清理（删 N 天前的 *.log）
    "qq_version_lock": True,  # 锁定 QQ 版本（hosts 屏蔽自动更新，保护 NapCat 兼容）
    "napcat_mode": "qqnt",    # qqnt=跟随用户 QQ 安装 | runtime=独立运行时（分离布局，QQ 升级不影响机器人）
    "logs_keep_days": 30,      # 日志保留天数（1-365）
    "owner_bilibili_uid": "",  # 主人的 B 站 UID（B站插件站内私信分享目标；曾漏在白名单外导致存了读不到）
    "backup_keep_days": 30,    # 备份保留天数（超过自动清理）
    "backup_excluded": [],     # 备份范围排除的组名（见 BACKUP_GROUPS；默认空=全备）
    "backup_keep_copies": 30,  # 每个备份保留份数（超过自动清理最旧的）
    "evolved_max_notes": 30,   # 自进化观察每人设保留上限（30-300；只影响存档）
    "evolved_inject_notes": 12,  # 自进化注入提示词条数（4-40；直接影响每条回复 token）
    "timezone": "Asia/Shanghai",  # 机器人时区（IANA 名，影响 AI 感知的时间）
    "device_ctrl_enabled": False,  # 设备操控（高危，默认关）
    "device_ctrl_2fa": True,       # 设备操控动态授权码（每次需验证）
    "device_ctrl_protected_dirs": [],  # 额外保护目录（不可搜索/发送）
    "device_ctrl_actions": False,  # 动作执行（打开/关闭程序等真操作；独立高危开关，默认关）
    "sched_pause_enabled": False,  # 定时暂停（时段内停止一切回复+零消耗）
    "sched_pause_start": "23:00",  # 暂停开始时间
    "sched_pause_end": "07:00",    # 暂停结束时间（支持跨午夜）
    "watchdog_enabled": True,      # NapCat 活性看门狗（半死连接检测）
    "watchdog_mode": "auto",       # auto=自动恢复（重连+重启 NapCat）| alert=仅告警
    "watchdog_interval": 120,      # 探测间隔秒（60-600）
    "qq_status_auto": False,       # 自动设置 QQ 在线状态（上线→在线；手动停止前→离开）
    "bot_name_enabled": False,     # 固定自称（开=覆盖所有人设卡的名字）
    "bot_name_value": "智谱娘",
    "owner_name_enabled": False,   # 固定主人称呼（开=覆盖人设里对主人的叫法）
    "owner_name_value": "主人",
    "user_blacklist": [],         # 用户黑名单：命中的 QQ 号无论群聊/私聊/@/关键词/戳一戳一律不响应
    "weather_city": "",           # 机器人所在地（城市名；主动消息感知当地天气，空=不感知）
    "api_proxy": "",               # API 出站代理（http://host:port；供 OpenAI/Grok/Gemini 等境外供应商用）
    "zcode_bridge_enabled": False, # ZCode 联动总开关（需本机安装 ZCode；以下子项都受它门控）
    "zcode_handoff_enabled": False,    # 任务转交（转交ZCode 指令 + 收件箱）
    "zcode_return_enabled": True,      # 收件箱任务完成后自动回传 QQ
    "zcode_notify_enabled": True,      # 允许 ZCode 侧（MCP qq_notify）向主人 QQ 发通知
    "zcode_reports_enabled": False,    # 每日复盘报告写入 ZCode 工作区 BOT_REPORTS/
    "zcode_sticker_enabled": False,    # 表情包生产线（MCP qq_sticker_add 一键入库）
    "zcode_watch_enabled": False,      # ZCode 进程监控（退出/重启时 QQ 提醒）
    "zcode_cron_enabled": False,       # 定时 ZCode 任务（定时 HH:MM zcode 任务）
    "zcode_context_enabled": False,    # 主动消息感知主人在 ZCode 的近期项目（默认关，隐私）
    "update_check": True,          # 启动时检查 GitHub Release 新版本（只查一次，不自动装）
    "update_repo": DEFAULT_UPDATE_REPO,  # 更新源仓库 owner/name（可改成自己的 fork/镜像）
    "update_token": "",            # 私有仓库读取 Release 用的 GitHub Token（留空=公开仓库匿名读）
    "update_mirror": "auto",       # 下载加速：auto=直连失败自动换镜像 | off=只直连 | 自定义前缀
    "update_skip_version": "",     # 用户选择跳过的版本号（如 0.1.2）
}

# 备份/恢复统一文件清单（basename 冲突无；子目录文件扁平存放）
# 备份范围分组（UI 勾选用）。critical=True 的组取消勾选时 UI 弹强提醒
# 二次确认；group 键存 app_config.backup_excluded（被取消的组名列表）。
BACKUP_GROUPS = [
    {"group": "persona", "label": "人设与风格", "critical": True,
     "desc": "人设合集（含每人设风格卡）、通用风格卡、分对象人设路由、自进化"
             "观察——机器人的「性格」本体，重建成本最高（长期积累），强烈建议备份",
     "files": ["personas/collection.json", "styles/current_style.md",
               "styles/evolved.json", "persona_routes.json"]},
    {"group": "memory", "label": "长期记忆", "critical": True,
     "desc": "机器人对群友/好友的记忆流水（谁说过什么、喜好等）——纯积累型"
             "数据，丢了无法再生，强烈建议备份",
     "files": ["memory_stream.jsonl"]},
    {"group": "config", "label": "行为与开关配置", "critical": False,
     "desc": "回复模式/白名单/私聊/模型链/应用设置等——丢了可手动重配",
     "files": ["group_reply_modes.json", "reply_config.json",
               "private_config.json", "group_whitelist.json",
               "model_chain.json", "app_config.json"]},
    {"group": "voice", "label": "语音音色", "critical": False,
     "desc": "克隆音色（索引+参考音频本体）、微调模型索引——音频本体很小"
             "（约 1MB）会一起备；微调模型文件太大只备索引，丢了需重新导入",
     "files": ["gsv_voices.json", "gsv_ft_models.json", "gsv_voices"]},
    {"group": "plugins", "label": "插件与导入数据", "critical": False,
     "desc": "插件启停/设置状态、各插件的私有数据（热点配置与历史、风格学习"
             "的样本与记录）、聊天导入库——丢了插件数据清零、导入需重来",
     "files": ["plugins_state.json", "plugins_data", "chat_import"]},
    {"group": "misc", "label": "表情/成员画像/日报", "critical": False,
     "desc": "表情包索引（图片本体体积原因不备）、成员画像、每日复盘日报、"
             "设备操控流程、定时任务、用量流水——丢了影响小或可再生",
     "files": ["stickers.json", "member_profiles.json", "daily_summaries",
               "device_ctrl_flow.json", "cron_tasks.json",
               "proactive_state.json", "usage_log.jsonl", ".last_review_pos"]},
]

# 展平的完整清单（执行侧用：按 excluded 组过滤）
BACKUP_FILES = [f for g in BACKUP_GROUPS for f in g["files"]]


def backup_files_for(app_cfg):
    """按用户勾选返回实际备份文件清单。
    app_cfg.backup_excluded = 被取消勾选的组名列表（默认空=全备）。"""
    try:
        excluded = set(str(x) for x in (app_cfg or {}).get("backup_excluded") or [])
    except Exception:
        excluded = set()
    return [f for g in BACKUP_GROUPS if g["group"] not in excluded
            for f in g["files"]]

# 密钥类（api_keys.json / zhipu_api_key / token / ws_token）刻意不备份：备份目录
# 可能被用户指向网盘同步盘，避免明文密钥外泄。

# 多供应商注册表（chat/completions 均为 OpenAI 兼容）
# custom 供应商的 base 由 Key 条目自带 base_url。
# 注：openai/grok/gemini 国内直连不可达，需在设置里配 API 代理。
API_PROVIDERS = {
    "zhipu": {"label": "智谱", "base": "https://open.bigmodel.cn",
              "path": "/api/paas/v4/chat/completions"},
    "deepseek": {"label": "DeepSeek", "base": "https://api.deepseek.com",
                 "path": "/v1/chat/completions"},
    "kimi": {"label": "Kimi 月之暗面", "base": "https://api.moonshot.cn",
             "path": "/v1/chat/completions"},
    "openai": {"label": "OpenAI", "base": "https://api.openai.com",
               "path": "/v1/chat/completions"},
    "grok": {"label": "xAI Grok", "base": "https://api.x.ai",
             "path": "/v1/chat/completions"},
    "gemini": {"label": "Gemini", "base": "https://generativelanguage.googleapis.com",
               "path": "/v1beta/openai/chat/completions"},
}


def safe_api_host(host):
    """API 主机安全校验：拒绝 localhost/环回/私网/保留地址（防 SSRF 打内网服务）。"""
    import ipaddress
    import socket
    h = str(host or "").strip().rstrip(".")
    if not h or h.lower() in ("localhost",) or h.endswith(".local"):
        return False
    try:
        addr = ipaddress.ip_address(socket.gethostbyname(h))
        return not (addr.is_private or addr.is_loopback or addr.is_reserved
                    or addr.is_link_local or addr.is_multicast or addr.is_unspecified)
    except (OSError, ValueError):
        return False


def provider_endpoint(provider, base_url=""):
    """供应商 → (base, path)。custom 必须给 base_url；未知供应商回落智谱。"""
    p = API_PROVIDERS.get(str(provider or "zhipu").lower())
    if p:
        return p["base"], p["path"]
    b = str(base_url or "").strip()
    if b:
        return b.rstrip("/"), "/chat/completions"
    return API_PROVIDERS["zhipu"]["base"], API_PROVIDERS["zhipu"]["path"]


def zcode_installed():
    """检测本机是否安装 ZCode（多标记任一命中即认为已安装）。"""
    try:
        markers = [
            os.path.join(os.path.expanduser("~"), ".zcode"),
            r"D:\Program Files\ZCode\resources",
            r"C:\Program Files\ZCode\resources",
        ]
        return any(os.path.isdir(m) for m in markers)
    except OSError:
        return False


def detect_qqnt_dir():
    """从注册表探测 QQNT 安装目录（NapCat launcher 同款逻辑）。"""
    for root in ("SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\QQ",
                 "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\QQ"):
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root)
            try:
                uninstall, _ = winreg.QueryValueEx(key, "UninstallString")
            finally:
                winreg.CloseKey(key)
        except OSError:
            continue
        qq_dir = os.path.dirname(os.path.abspath(uninstall.strip('"')))
        if os.path.isfile(os.path.join(qq_dir, "QQ.exe")):
            return qq_dir
    return ""


_LEGACY_UPDATE_REPO = "Pigeon2333/zcode-qq-bot-release"  # 已归档的旧发布仓库


def logs_dir():
    """日志目录（app_config.logs_dir 覆盖；默认 DATA_DIR/logs）。
    返回 str；调用方按需 mkdir。改动后需重启各组件生效（重定向句柄在启动时打开）。"""
    try:
        p = str(load_app_config().get("logs_dir") or "").strip()
    except Exception:
        p = ""
    return p or os.path.join(DATA_DIR, "logs")


def load_app_config():
    """读 app_config.json；不存在或格式异常时返回默认值（不回写，写由显式保存触发）。"""
    cfg = dict(DEFAULT_CONFIG)
    try:
        data = json.loads(APP_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    if isinstance(data, dict):
        for k in DEFAULT_CONFIG:
            if k in data:
                cfg[k] = data[k]
    # 一次性迁移：旧发布仓库已归档，指向它的更新源改回主仓库
    # （用户手动改成其它 fork/镜像的不受影响）
    if cfg.get("update_repo") == _LEGACY_UPDATE_REPO:
        cfg["update_repo"] = DEFAULT_UPDATE_REPO
    return cfg


def save_app_config(cfg):
    """写 app_config.json（仅数据目录内；pathlib 常量路径）。

    Windows 下 os.replace 需要目标文件没有其他进程持有句柄——agent/daemon
    高频读取本文件时原子替换会偶发 WinError 5。策略：
    ① 最多重试 6 次 os.replace（每次间隔递增）；② 仍失败则退化为就地写入。
    """
    merged = load_app_config()
    for k in DEFAULT_CONFIG:
        if k in cfg:
            merged[k] = cfg[k]
    text = json.dumps(merged, ensure_ascii=False, indent=2)
    tmp = DATA_ROOT / "app_config.json.tmp"
    tmp.write_text(text, encoding="utf-8")
    last = None
    for attempt in range(6):
        try:
            os.replace(str(tmp), str(APP_CONFIG_PATH))
            return merged
        except PermissionError as e:
            last = e
            time.sleep(0.08 * (attempt + 1))
    # 兜底：就地覆盖（放弃原子性，保证保存成功）；失败才向上抛
    try:
        APP_CONFIG_PATH.write_text(text, encoding="utf-8")
        os.remove(str(tmp)) if tmp.exists() else None
        return merged
    except OSError:
        raise last if last else OSError("保存 app_config 失败")


def qqnt_dir_resolved(cfg=None):
    """返回机器人实际使用的 QQNT 目录：
    napcat_mode=runtime（独立运行时）且运行时就绪 → 数据目录 napcat-runtime/；
    否则配置 qqnt_dir 优先，注册表探测兜底。"""
    cfg = cfg or load_app_config()
    if str(cfg.get("napcat_mode") or "") == "runtime":
        rt = os.path.join(DATA_DIR, "napcat-runtime")
        if os.path.isfile(os.path.join(rt, "QQ.exe")):
            return rt
        # 运行时未就绪时回落到常规目录（启动预检会引导安装）
    d = str(cfg.get("qqnt_dir") or "").strip()
    if d and os.path.isfile(os.path.join(d, "QQ.exe")):
        return d
    return detect_qqnt_dir()


def qq_current_version(qqnt=None):
    """QQ 当前实际运行版本：versions 布局读 config.json 的 curVersion；
    老式根目录布局返回 "legacy"。探测失败返回 ""。"""
    qqnt = qqnt or qqnt_dir_resolved()
    if not qqnt:
        return ""
    vcfg = os.path.join(qqnt, "versions", "config.json")
    try:
        with open(vcfg, "r", encoding="utf-8") as f:
            cur = str(json.loads(f.read()).get("curVersion") or "")
        return cur or "legacy"
    except (OSError, ValueError):
        # 无 versions 目录 = 老式根目录布局（QQ 程序直接在根目录）
        return "legacy" if os.path.isfile(os.path.join(qqnt, "QQNT.dll")) or \
            os.path.isdir(os.path.join(qqnt, "resources")) else ""


# ---------------- 高峰降耗（econ）：daemon 与 agent 共用的生效判定 ----------------

def _hhmm_to_min(s, default):
    try:
        h, m = str(s).strip().split(":")
        v = int(h) * 60 + int(m)
        return v if 0 <= v < 24 * 60 else default
    except (ValueError, AttributeError):
        return default


def econ_active(cfg):
    """高峰降耗此刻是否生效（reply_config 的 econ_* 字段）。
    econ_enabled 总开关关闭 → 不生效；econ_manual=true 立即开启（不限时）；
    否则按时间表：econ_days 命中当天（0=周一…6=周日，空=每天）+ start<=now<end（支持跨午夜）。
    兼容旧字段：econ_mode_override="on" / econ_workdays_only。异常一律 False。"""
    try:
        if not cfg.get("econ_enabled"):
            return False
        if cfg.get("econ_manual"):
            return True
        if str(cfg.get("econ_mode_override") or "") == "on":  # 旧三态兼容
            return True
        lt = time.localtime()
        days = cfg.get("econ_days")
        if isinstance(days, list) and days:
            try:
                allowed = [int(d) for d in days]
            except (TypeError, ValueError):
                allowed = []
            if allowed and lt.tm_wday not in allowed:
                return False
        elif days is None and cfg.get("econ_workdays_only", True) and lt.tm_wday >= 5:
            return False  # 旧仅工作日开关兼容
        cur = lt.tm_hour * 60 + lt.tm_min
        s = _hhmm_to_min(cfg.get("econ_start", "14:00"), 14 * 60)
        e = _hhmm_to_min(cfg.get("econ_end", "18:00"), 18 * 60)
        if s <= e:
            return s <= cur < e
        return cur >= s or cur < e  # 跨午夜时段（如 22:00-06:00）
    except Exception:
        return False


def tz_now_info():
    """按配置时区返回结构化当前时间（agent 注入 system prompt 用）。
    时区名无效时回退系统本地时间。"""
    import zoneinfo
    tz_name = str(load_app_config().get("timezone") or "Asia/Shanghai").strip()
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except (ValueError, KeyError, OSError):
        tz = None
    import datetime
    now = datetime.datetime.now(tz) if tz else datetime.datetime.now()
    wd = ["一", "二", "三", "四", "五", "六", "日"][now.weekday()]
    ampm = "上午" if now.hour < 12 else ("下午" if now.hour < 18 else "晚上")
    h12 = now.hour if 0 < now.hour < 13 else (now.hour - 12 if now.hour >= 13 else 12)
    return {
        "iso": now.strftime("%Y-%m-%d"),
        "weekday": "周%s" % wd,
        "clock24": now.strftime("%H:%M"),
        "clock12": "%s%d:%02d" % (ampm, h12, now.minute),
        "hour": now.hour,
        "tz": tz_name,
    }


def gen_auth_code():
    """生成 6 位数字动态授权码（设备操控用）。"""
    import secrets
    return str(secrets.randbelow(900000) + 100000)
