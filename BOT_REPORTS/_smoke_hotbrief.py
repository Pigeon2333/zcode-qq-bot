# -*- coding: utf-8 -*-
"""冒烟：热点速览插件「往日热点只留标题」开关（old_titles_only，v1.5.0）。

全部离线：QQBOT_DATA_DIR 在 import 被测插件之前指向临时目录（不碰
~/.zcode-qq-bot 生产数据，尤其不动其中已安装的插件副本）；存档用固定样本
直写临时目录，注入只读调用 _line——不联网、不调模型、不起任何进程。
「关」的期望值＝改前（HEAD）实现的输出口径（今日整单＋往日各截 400 字、
「条目截断」表头、开头以这里为准那句原样），已先对 HEAD 版实跑钉住后再
写成字面模板逐字比对。不变量：存档文件内容与格式全程零改动（只改注入侧）。
"""
import importlib.util
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time

sys.dont_write_bytecode = True          # 不在仓库/临时目录留 .pyc
REPO = pathlib.Path(__file__).resolve().parent.parent
TMP_ROOT = pathlib.Path(tempfile.mkdtemp(prefix="hotbrief-iso-"))
# 必须在 import 被测模块之前设置：plugin.py 在 import 时读该变量定 DATA_DIR
os.environ["QQBOT_DATA_DIR"] = str(TMP_ROOT / "data")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_spec = importlib.util.spec_from_file_location(
    "hot_brief_plugin_under_test", str(REPO / "market" / "hot_brief" / "plugin.py"))
HB = importlib.util.module_from_spec(_spec)
sys.modules["hot_brief_plugin_under_test"] = HB
_spec.loader.exec_module(HB)

fails = []
passed = 0


def chk(name, cond, detail=""):
    global passed
    print(("PASS " if cond else "FAIL ") + name
          + ((" | " + str(detail)) if (detail and not cond) else ""))
    if cond:
        passed += 1
    else:
        fails.append(name)


# --- 固定样本（虚构通例，不含真实案例细节；要点长度按实测的真实清单量级） ---
TODAY = time.strftime("%Y-%m-%d")


def _day(k):
    return time.strftime("%Y-%m-%d", time.localtime(time.time() - k * 86400))


YDAY, D2 = _day(1), _day(2)

TODAY_TEXT = (
    "- 全国统一大市场建设指导意见发布：明确年内落地一批标志性改革举措\n"
    "- 某芯片公司发布新一代移动端芯片：AI 算力翻倍，旗舰机型将首批搭载\n"
    "- 联赛总决赛落幕：主队在决胜局完成逆转，时隔十年再度夺冠")
# 往日样本按实测的真实量级（补充测量：往日要点合计约千字、条目约 40-50 字）
Y_TEXT = (
    "- 北方多地出现极端大风天气：多趟高铁列车临时停运，多个城市发布中小学停课通知并启动应急响应\n"
    "- 某国载人飞船完成无人对接：两名航天员明日进入空间站，计划开展为期约半年的驻留任务后返回\n"
    "- 知名车企宣布大规模召回：因制动系统软件存在潜在缺陷，召回涉及车辆二十余万辆，车主可预约升级\n"
    "- 头部虚拟偶像团体宣布无限期停播：运营方在公告中未说明具体原因，粉丝社区讨论持续发酵并登上热搜\n"
    "- 经典游戏宣布推出重制版：官方确认将于明年春季发售，游戏将同步登陆主机与 PC 平台并支持中文\n"
    "- 某新型电池技术亮相行业展会：能量密度提升近一倍，多家车企宣布将在两年内量产装车验证\n"
    "- 国际学术团队发布新研究：发现某常见饮食习惯与代谢指标的明确关联，样本覆盖十万人群\n"
    "- 某地推出假期新型消费场景：文旅融合街区客流量创同期新高，周边酒店预订量同比增长明显")
D_TEXT = (
    "- 某开源项目发布重大版本更新：核心模块性能提升数倍，大量旧插件需要适配后才可继续使用\n"
    "- 国际气候会议达成新协议：多个国家承诺提前实现减排目标，并出资设立联合援助基金支持转型\n"
    "- 某电商平台调整运费规则：偏远地区免邮门槛上调，不少商家表示运营成本有所增加\n"
    "- 某航空公司宣布新增多条国际航线：计划下月起陆续开航，票价与行李额度同步公布\n"
    "- 全国多地开启冬季文体活动季：博物馆延时开放，户外冰雪场地预约量明显上升\n"
    "- 某短视频平台上线新功能：创作者可对长视频添加章节标记，用户跳转观看更方便")
# 期望标题独立钉死（不经被测函数推导）
Y_TITLES = ["北方多地出现极端大风天气", "某国载人飞船完成无人对接",
            "知名车企宣布大规模召回", "头部虚拟偶像团体宣布无限期停播",
            "经典游戏宣布推出重制版", "某新型电池技术亮相行业展会",
            "国际学术团队发布新研究", "某地推出假期新型消费场景"]
D_TITLES = ["某开源项目发布重大版本更新", "国际气候会议达成新协议",
            "某电商平台调整运费规则", "某航空公司宣布新增多条国际航线",
            "全国多地开启冬季文体活动季", "某短视频平台上线新功能"]


def write_rec(p, text, date):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"date": date, "text": text, "ts": 1700000000.0},
                            ensure_ascii=False), encoding="utf-8")


BRIEF = pathlib.Path(HB.BRIEF_PATH)
HIST = pathlib.Path(HB.HIST_DIR)
F_YDAY = HIST / (YDAY + ".json")
F_D2 = HIST / (D2 + ".json")
write_rec(BRIEF, TODAY_TEXT, TODAY)
write_rec(F_YDAY, Y_TEXT, YDAY)
write_rec(F_D2, D_TEXT, D2)
SNAP0 = {p: p.read_bytes() for p in (BRIEF, F_YDAY, F_D2)}


class StubAPI:
    """只读替身：store/load 用进程内 dict 顶替宿主的 plugins_data 读写。"""

    def __init__(self):
        self.routes = {}
        self.data = {}
        self.logs = []

    def route(self, method, fn):
        self.routes[str(method)] = fn

    def on_tick(self, fn):
        pass

    def every(self, n, fn):
        return fn

    def inject_context(self, fn):
        pass

    def log(self, msg):
        self.logs.append(str(msg))

    def store(self, key, value):
        self.data[key] = value

    def load(self, key, default=None):
        return self.data.get(key, default)


API = StubAPI()
HB.register(API)

# 改前（HEAD）输出口径，钉成字面模板
PRE_OFF = ("\n\n## 近几天热点速览（按日分组的新闻/热搜摘要——**包括昨天前天的旧闻**，"
           "群友聊到其中任何一条都算真实新闻，别否认说没这回事；细节不确定就说不"
           "确定，但\"有没有这件事\"以这里为准）")
OLD_HEAD = "（前天以前的旧闻，条目截断）"


def off_expected():
    parts = ["### %s\n- %s" % (TODAY, TODAY_TEXT[:800]),
             "### %s%s\n- %s" % (YDAY, OLD_HEAD, Y_TEXT[:400]),
             "### %s%s\n- %s" % (D2, OLD_HEAD, D_TEXT[:400])]
    return PRE_OFF + "\n" + "\n".join(parts)


def on_day_body(day, titles):
    return ("### %s（前天以前的旧闻，只留标题）\n- - %s"
            % (day, titles[0])
            + "".join("\n- " + t for t in titles[1:]))


def segments(out):
    """按日切块：[0]=开头说明，[1]=今天，[2]=昨天，[3]=前天。"""
    return out.split("\n### ")


def past_len(out):
    """往日部分＝各日清单正文（不含两边相同的「### 日期（表头）」脚手架行，
    表头两种模式等长，只会稀释掉压缩效果本身）。"""
    total = 0
    for x in segments(out)[2:]:
        nl = x.find("\n")
        if nl >= 0:
            total += len(x) - nl - 1
    return total


# --- 场景A：缺键 → 默认开，往日只剩标题、今天不变 ---
chk("A1 缺键默认开：load_auto.old_titles_only=True",
    API.routes["load_auto"]({}).get("old_titles_only") is True)
out_a = HB._line("group", "12345")
seg_a = segments(out_a)
chk("A2 开：昨日只留标题（整块逐字）",
    ("\n" + on_day_body(YDAY, Y_TITLES)) in out_a)
chk("A3 开：前天只留标题（整块逐字）",
    ("\n" + on_day_body(D2, D_TITLES)) in out_a)
chk("A4 开：往日要点不再出现",
    "临时停运" not in out_a and "制动系统软件" not in out_a
    and "联合援助基金" not in out_a and "能量密度" not in out_a)
chk("A5 开：今天清单原样保留",
    len(seg_a) > 1 and seg_a[1] == TODAY + "\n- " + TODAY_TEXT)
chk("A6 开：开头口径保住（以这里为准别否认）",
    "有没有这件事" in out_a and "别否认说没这回事" in out_a)
chk("A7 开：往日只有标题写进开头说明",
    "标题就是全部已知信息" in out_a and "别自行补充细节" in out_a)

# --- 场景B：保存「关」→ 输出与改前逐字一致 ---
r_off = API.routes["set_old_titles_only"]({"enabled": False})
chk("B1 保存关：返回 ok+False",
    r_off == {"ok": True, "old_titles_only": False})
chk("B2 保存关：load_auto 读回 False",
    API.routes["load_auto"]({})["old_titles_only"] is False)
out_b = HB._line("group", "12345")
chk("B3 关：输出与改前逐字一致", out_b == off_expected(),
    "len %d vs %d" % (len(out_b), len(off_expected())))

# --- 场景C：再开 → 长度比（固定样本：往日部分 ≤ 关时的 40%） ---
r_on = API.routes["set_old_titles_only"]({"enabled": True})
chk("C1 再开：返回 ok+True",
    r_on == {"ok": True, "old_titles_only": True}
    and API.routes["load_auto"]({})["old_titles_only"] is True)
out_c = HB._line("group", "12345")
plo, poff = past_len(out_c), past_len(out_b)
tot_on, tot_off = len(out_c), len(out_b)
chk("C2 开：往日部分长度 ≤ 关时的 40%（固定样本）",
    plo * 10 <= poff * 4,
    "正文 on=%d off=%d (%.1f%%)；整段含表头 on=%d off=%d (%.1f%%)"
    % (plo, poff, plo * 100.0 / poff, tot_on, tot_off, tot_on * 100.0 / tot_off))
chk("C3 开：今天部分与关时逐字相同",
    segments(out_c)[1] == segments(out_b)[1])
chk("C4 存档零改动（A-C 三个文件字节不变）",
    all(p.read_bytes() == SNAP0[p] for p in (BRIEF, F_YDAY, F_D2)))

# --- 场景D：今天没有清单、只有往日 → 照常注入（两种模式） ---
write_rec(BRIEF, "过期内容不应出现在注入里", YDAY)   # 日期≠今天 → 不注入
out_d = HB._line("group", "12345")
chk("D1 仅往日（开）：照常注入且只留标题",
    out_d.startswith("\n\n## 近几天热点速览（") and "只留标题" in out_d
    and "标题就是全部已知信息" in out_d)
chk("D2 仅往日（开）：过期今日清单与今天日期都不出现",
    "过期内容不应出现在注入里" not in out_d and TODAY not in out_d)
chk("D3 仅往日（开）：开头口径保住", "有没有这件事" in out_d)
API.routes["set_old_titles_only"]({"enabled": False})
out_d2 = HB._line("group", "12345")
chk("D4 仅往日（关）：照常注入且是改前口径",
    out_d2.startswith(PRE_OFF) and OLD_HEAD in out_d2 and "只留标题" not in out_d2)
API.routes["set_old_titles_only"]({"enabled": True})

# --- 场景E：标题切分（实际会出现的条目形态） ---
chk("E1 切分：标准「- 事件：要点」",
    HB._title_of("- 事件名称：一句话要点") == "- 事件名称")
chk("E2 切分：圆点「• 词条：描述」",
    HB._title_of("• 词条名称：补充描述") == "• 词条名称")
chk("E3 切分：无项目符号（原始词条形态）",
    HB._title_of("词条名称：补充描述") == "词条名称")
chk("E4 切分：只有第一个冒号前算标题",
    HB._title_of("- 事件名称：要点一：要点二") == "- 事件名称")
chk("E5 切分：无冒号→保留整句",
    HB._title_of("- 没有要点的孤立词条") == "- 没有要点的孤立词条")
chk("E6 切分：冒号在开头→保留整句",
    HB._title_of("- ：开头就是冒号的条目") == "- ：开头就是冒号的条目")

# --- 场景F：manifest 版本与 page.js 接线 ---
mf = json.loads((REPO / "market" / "hot_brief" / "manifest.json").read_text(encoding="utf-8"))
chk("F1 manifest 版本 1.5.0", mf.get("version") == "1.5.0")
page = (REPO / "market" / "hot_brief" / "page.js").read_text(encoding="utf-8")
chk("F2 page.js 已接新开关（路由名+控件 id）",
    "set_old_titles_only" in page and "hb-oldtitles" in page)

# --- 场景G：收尾核对——存档文件全程零改动（只改注入侧） ---
chk("G1 历史存档字节不变", F_YDAY.read_bytes() == SNAP0[F_YDAY]
    and F_D2.read_bytes() == SNAP0[F_D2])
chk("G2 今日存档即场景D写入的原样（插件没动它）",
    json.loads(BRIEF.read_text(encoding="utf-8"))["text"] == "过期内容不应出现在注入里")

print("%d 项通过，%d 项失败" % (passed, len(fails)))
if not fails:
    # 全绿才清理自建的临时目录（精确路径；FAIL 时保留现场供排查）
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    print("cleaned: %s" % TMP_ROOT)
sys.exit(1 if fails else 0)
