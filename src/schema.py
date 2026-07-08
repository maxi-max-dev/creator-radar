#!/usr/bin/env python3
"""达人雷达 · 列结构单一来源(single source of truth)。

Max 2026-07-07 简化拍板: 系统对使用者只有「三个词、两个分、一盏灯」——
  对路分(fit) / 在涨分(rising) / 红绿灯(action_grade), 潜力 = 对路 × 在涨。
工程细节(语义/甜点/POV/起势/浪/破圈/百分位…)一个不删, 全部下沉为「证据」。

于是列分两组, 四个输出面(cards_table.csv / 日报分数表 / 飞书 cards 表 /
全池榜单 sync_full_ranking / B站榜单)全部从这里取列定义, 杜绝各处硬编码漂移:

  OPS_COLUMNS  运营主视图(人话, 约 10 列): 日期/排名/频道名/频道链接/订阅数/
               对路分/在涨分/潜力分/红绿灯/身份标签/档案/状态。
  ENG_COLUMNS  工程证据列: 语义分/甜点分/POV标记分/起势分/浪层分/破圈比/
               排名百分位/命中主题/是否新发现 等现有全部列。

命名迁移(数据含义不变, 只改展示名):
  总分   -> 对路分     (fit: 内容像不像影石会签的人)
  行动分级 -> 红绿灯    (能不能直接联系)
  新增   在涨分         (rising: 是不是正在被越来越多陌生人看到; 合成起势/浪/破圈三信号)

铁律: 本模块只定义「展示层的列」。冻结考试池官方指标(backtest.py)不经此,
一个字节不受影响。
"""

# ---- 列名常量(改名只改这里, 全仓库跟随) ----
COL_FIT = "对路分"        # 原「总分」= fit score, 展示改名
COL_RISING = "在涨分"     # 新增: 三信号合成的「正在被越来越多陌生人看到」
COL_POTENTIAL = "潜力分"  # fit × 在涨
COL_LIGHT = "红绿灯"      # 原「行动分级」= action_grade, 展示改名

# W17(2026-07-08 Max 直接反馈): 表格产品化。
COL_THEME_TAGS = "主题标签"   # 命中主题的中文多选标签(取代英文 key 文本列; cards 表新增此列, 旧「命中主题」文本列保留写历史)
COL_SUBS_TEXT = "订阅"        # 订阅数双格式文本: "73.1万 (731,345)"(纯数字列「订阅数」保留供排序/AI 问数)
COL_EVIDENCE = "证据摘要"     # 一行拼好的证据串(对路分档/中文主题/在涨证据/身份), 给飞书 AI 字段当原料

# W22(2026-07-08 Max「只有国外的, 国内的呢」): cards 表加「平台」文本列, 让 YouTube / B站 卡一眼分清。
#   existing 17 行回填 "YouTube"; B站卡写 "B站"。取值恒为这两个字面量之一(单选式文本, 不建选项也可, 保持文本列最省)。
COL_PLATFORM = "平台"
PLATFORM_YOUTUBE = "YouTube"
PLATFORM_BILIBILI = "B站"

# ---- 命中主题: 工程 key -> 中文短标签(单一来源, <=6 字, 一眼懂) ----
# key 全集来自 config/insta360.json 与 config/insta360_bilibili.json 的 themes 节(6 个)。
# 全池/B站/cards 三表的「主题标签」多选列 + 日报/CSV 命中主题列都从这里取中文名(工程 key 下沉档案)。
THEME_LABELS = {
    "pov_native":        "POV原生",     # 第一视角/头盔机位/沉浸式 360
    "authentic_vlog":    "真实vlog",     # 自拍原声/一个人拍/个人叙事
    "journey_narrative": "长途纪录",     # 环游/川藏/几万公里/旅途纪录片
    "gear_native":       "器材玩法",     # 装备机位/固定拍摄/技法教程
    "vertical_craft":    "硬核技术",     # 专业技术流/赛道技巧/垂类上限
    "adventure_bold":    "极限挑战",     # 硬核大胆/突破极限/野外冒险(B站主题)
}


def theme_tags_zh(keys):
    """命中主题 key 列表 -> 中文短标签列表(飞书多选列的选项名数组; 未知 key 原样保留兜底)。"""
    return [THEME_LABELS.get(k, k) for k in (keys or [])]


def subs_dual(n):
    """W17 订阅数双格式文本(单一来源, 三表 + CSV 共用):
    >=1万 → 'X.X万 (731,345)'; <1万 → 纯数字 '5,320'。None → 空串。
    Max: 光纯数字分不清量级, 光写多少万看不清精确值, 两个都给。"""
    if n is None:
        return ""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if n >= 10000:
        return "%s万 (%s)" % (("%.1f" % (n / 10000)).rstrip("0").rstrip("."), format(n, ","))
    return format(n, ",")


# =========================================================================
# 飞书字段「显示格式 + 一句话描述」单一来源(W20 表格观感完善批)。
# 三表(cards / 全池 / B站)建字段与线上打磨都从这里取, 杜绝各处漂移。
# =========================================================================

# 数字列显示格式(飞书 number 字段 property.formatter):
#   分数列一律两位小数 "0.00"(原来 0.0/0.0000 长短不一, 观感杂); 订阅数千分位 "1,000"; 排名整数 "0"。
FIELD_FORMATTERS = {
    COL_FIT: "0.00", COL_RISING: "0.00", COL_POTENTIAL: "0.00",
    "起势分": "0.00", "浪层分": "0.00", "破圈比": "0.00",
    "语义分": "0.00", "甜点分": "0.00", "POV标记分": "0.00",
    "订阅数": "1,000", "排名": "0",
}

# 一句话字段描述(飞书 field.description, 中文, <=20 字, 让表格自解释)。
# 只给核心列; 缺席的列(值得签/风险/垂类/语言/档案/日期/频道名 等)含义自明, 不加噪。
FIELD_DESCRIPTIONS = {
    COL_FIT: "内容像不像影石会签的人，0-1",
    COL_RISING: "是不是越来越多陌生人在看他",
    COL_POTENTIAL: "对路 × 在涨，综合潜力",
    COL_LIGHT: "🟢可联系 🟡再看看 🔴别碰 🏢转官方BD",
    "起势分": "近期涨得快不快(在涨三信号之一)",
    "浪层分": "热度是一波还是持续(在涨三信号)",
    "破圈比": "陌生人比老粉多多少(在涨三信号)",
    "身份标签": "机判身份：搬运/机构/竞品等",
    "拦截原因": "降级/别碰的具体理由",
    COL_EVIDENCE: "拼给飞书AI当原料的一行证据",
    COL_THEME_TAGS: "命中的内容主题(中文多选)",
    COL_SUBS_TEXT: "订阅数双格式：X万 (精确值)",
    "订阅数": "订阅数原值(供排序/AI问数)",
    "排名百分位": "在全池里排前百分之几",
    "语义分": "语义相似度(对路的证据之一)",
    "甜点分": "订阅量级是否在影石中腰部甜点",
    "POV标记分": "第一视角/沉浸式内容强度",
}

# =========================================================================
# W21(2026-07-08 Max「表格太单调, 学飞书官方最佳实践」): 选项颜色 + 进度条 UI 单一来源。
# 原本红绿灯四态 / 主题六色的定义散在 sync_full_ranking.py 与 sync_cards_columns.py 两处,
# 极易漂移。收进 schema 后, 三表建字段(reconcile)与线上富化(enrich)都从这里取, 一处改全跟随。
# =========================================================================

# 飞书选项颜色 index(0-54)。语义化配色, 一眼分辨。
# 红绿灯单选四态: 绿(可联系)/黄(再看看)/红(别碰)/蓝(官方联动)。选项名保持存量的裸 emoji
#   (🟢🟡🔴 + 🏢官方联动), 与写入端 action_grade 对齐——改名会让写入创建重复选项, 得不偿失,
#   emoji 本身已表达红绿灯语义, 颜色只是强化。语义标签放在字段描述里(FIELD_DESCRIPTIONS[红绿灯])。
LIGHT_OPTIONS = [
    {"name": "🟢", "color": 3},          # 绿 = 可联系
    {"name": "🟡", "color": 1},          # 黄(橙) = 再看看
    {"name": "🔴", "color": 0},          # 红 = 别碰
    {"name": "🏢官方联动", "color": 8},   # 蓝 = 转官方 BD(别家公司)
]

# 命中主题多选六色(中文标签来自 THEME_LABELS, 六主题互异色)。
THEME_COLORS = {
    "POV原生": 5, "真实vlog": 6, "长途纪录": 7,
    "器材玩法": 9, "硬核技术": 10, "极限挑战": 11,
}
THEME_OPTIONS = [{"name": lbl, "color": THEME_COLORS.get(lbl, 4)}
                 for lbl in THEME_LABELS.values()]

# 列名 -> 预置选项(仅 单选/多选 列)。建字段与线上富化都用它下发颜色。
FIELD_OPTIONS = {
    COL_LIGHT: LIGHT_OPTIONS,
    COL_THEME_TAGS: THEME_OPTIONS,
}

# 进度条列(飞书 number 字段 ui_type="Progress"): 值域 0-1 的分数改进度条, 一眼看条长。
#   对路分/在涨分 = 0-1, 适合进度条; 潜力分 = 对路 × 在涨, 值域<1 但语义上"综合分",
#   保持普通数字两位小数(进度条会把它和 0-1 列混淆, 且它不是"占比"语义), 故不入此表。
# 结构: {列名: {"formatter": "0%", "range_customize": True, "min": 0, "max": 1}}。
# 说明: formatter 用 "0%" 把 0-1 显示成 0%-100%(数据仍存 0-1, 只改显示); range 固定 0-1 让条长可比。
FIELD_UI_PROGRESS = {
    COL_FIT: {"formatter": "0%", "range_customize": True, "min": 0, "max": 1},
    COL_RISING: {"formatter": "0%", "range_customize": True, "min": 0, "max": 1},
}

# =========================================================================
# OPS: 运营主视图(人话列)。cards 表在此之外另含推荐理由三列 + 风险 + 首次合作。
# 顺序即展示顺序。
# =========================================================================

# 全池/B站榜单等「一行一频道」出口的运营列(无推荐卡语)。
OPS_COLUMNS = [
    "日期", "排名", "频道名", "频道链接", "订阅数",
    COL_FIT, COL_RISING, COL_POTENTIAL, COL_LIGHT,
    "身份标签", "档案", "状态",
]

# cards 表(当日推荐)在 OPS 之外追加的推荐卡专属列。
CARDS_EXTRA_COLUMNS = ["值得签1", "值得签2", "值得签3", "风险", "首次合作建议"]

# =========================================================================
# ENG: 工程证据列。本地 CSV 保留全列 = 工程视图; 飞书表用字段保留, 运营视图默认隐藏。
# =========================================================================
ENG_COLUMNS = [
    "语义分", "甜点分", "POV标记分",
    "起势分", "浪层分", "破圈比",
    "排名百分位", "命中主题", "是否新发现",
]


# =========================================================================
# 四个输出面的完整列序(OPS + ENG 交织, 但集合恒等于 OPS∪ENG(∪CARDS_EXTRA))。
# 各出口从这里取, 保证同构。CSV/飞书列顺序历史沿用, 故这里给出显式列序常量,
# 而契约自检只校验「列集合」一致(顺序是各出口自己的事)。
# =========================================================================

# cards_table.csv / 飞书 cards 表 的完整列(运营 + 证据 + 推荐卡语 + 状态)。
# 顺序: 运营主列在前 → 证据列 → 推荐卡语 → 状态收尾(状态列永远给运营留空)。
# W17: cards 表有历史, 旧「命中主题」文本列**保留不删**(停写), 新增「主题标签」多选列; 「订阅」双格式文本列并入。
CARDS_TABLE_COLUMNS = [
    "日期", "排名", "频道名", "频道链接", COL_PLATFORM, "订阅数", COL_SUBS_TEXT,
    COL_FIT, COL_RISING, COL_POTENTIAL, COL_LIGHT, "身份标签",
    # 证据列(工程视图)
    "语义分", "甜点分", "POV标记分", "起势分", "浪层分", "破圈比",
    "排名百分位", "命中主题", COL_THEME_TAGS, "是否新发现", "排名变动",
    "档案",
    # 推荐卡语
    "值得签1", "值得签2", "值得签3", "风险", "首次合作建议",
    "状态",
]

# 全池榜单(飞书第二张表)完整列。档案列只 top50 有值。
# W17: 「命中主题」英文文本列 → 换成「主题标签」中文多选列; 新增「订阅」双格式文本列 + 「证据摘要」给 AI 当原料。
FULL_RANKING_COLUMNS = [
    "排名", "频道名", "频道链接", "订阅数", COL_SUBS_TEXT,
    COL_FIT, COL_RISING, COL_POTENTIAL, COL_LIGHT, "身份标签", "拦截原因",
    "起势分", "浪层分", "破圈比",
    COL_THEME_TAGS, "垂类", "语言", COL_EVIDENCE, "档案", "入池日期",
]

# B站榜单 = 全池榜单去掉档案 + 去掉视频级证据(B站无 RSS 视频级数据)。
_BILI_DROP = {"档案", "在涨分", "潜力分", "起势分", "浪层分", "破圈比"}
BILI_RANKING_COLUMNS = [c for c in FULL_RANKING_COLUMNS if c not in _BILI_DROP]


# 飞书「运营视图」默认只显示的列(OPS 主列)。其余字段保留但隐藏 = 工程证据。
FEISHU_OPS_VIEW_FIELDS = [
    "排名", "频道名", "频道链接", "订阅数",
    COL_FIT, COL_RISING, COL_POTENTIAL, COL_LIGHT, "身份标签", "档案",
]


# W17 表格产品化衍生列(不属 OPS/ENG 的原始定义, 是展示层加工产物):
#   订阅 = 订阅数的双格式文本; 主题标签 = 命中主题的中文多选; 证据摘要 = 拼给 AI 的原料串。
_W17_DERIVED = {COL_SUBS_TEXT, COL_THEME_TAGS, COL_EVIDENCE}


def _assert_contract():
    """契约自检(<=6 行核心断言): 四出口列集必须 = OPS∪ENG(各自允许的专属列除外)。
    任一出口漏列/多列/改名不同步都会在这里炸出来。"""
    ops, eng = set(OPS_COLUMNS), set(ENG_COLUMNS)
    # cards 表: = 运营列 ∪ 证据列 ∪ 推荐卡专属列 ∪ {排名变动} ∪ W17 衍生列(订阅/主题标签; 证据摘要不入 cards) ∪ {W22 平台列}
    assert set(CARDS_TABLE_COLUMNS) == ops | eng | set(CARDS_EXTRA_COLUMNS) | {"排名变动", COL_SUBS_TEXT, COL_THEME_TAGS, COL_PLATFORM}, "cards 表列集漂移"
    # 全池榜单: 无「日期/状态」(整表重写非按日 append), 有「垂类/语言/拦截原因」+ W17 三衍生列; 命中主题 key 列不进(换成中文主题标签)
    fr = set(FULL_RANKING_COLUMNS)
    assert {COL_FIT, COL_RISING, COL_POTENTIAL, COL_LIGHT} <= fr, "全池榜单缺三词核心列"
    assert _W17_DERIVED <= fr, "全池榜单缺 W17 衍生列(订阅/主题标签/证据摘要)"
    assert "命中主题" not in fr, "全池榜单应用中文主题标签列, 不再挂英文命中主题 key 列"
    assert set(BILI_RANKING_COLUMNS) == fr - _BILI_DROP, "B站榜单列集 != 全池去掉视频级证据"
    return True


# import 即自检(四出口任何一处改列忘了同步, 一 import 就炸)。
_assert_contract()


if __name__ == "__main__":
    import json
    print(json.dumps({
        "contract_ok": _assert_contract(),
        "OPS_COLUMNS": OPS_COLUMNS,
        "ENG_COLUMNS": ENG_COLUMNS,
        "CARDS_TABLE_COLUMNS": CARDS_TABLE_COLUMNS,
        "FULL_RANKING_COLUMNS": FULL_RANKING_COLUMNS,
        "BILI_RANKING_COLUMNS": BILI_RANKING_COLUMNS,
        "FEISHU_OPS_VIEW_FIELDS": FEISHU_OPS_VIEW_FIELDS,
    }, ensure_ascii=False, indent=1))
