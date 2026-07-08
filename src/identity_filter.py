#!/usr/bin/env python3
"""达人雷达 · 身份过滤器 v1(纯规则层, 零模型成本)。

由 2026-07-07「盲投后果审计」催生: 审计判定系统 41% 盲投翻车率, 核心诊断=
系统能算内容相似度但**不认识账号身份**(已合作方/竞品/器材店/搬运号/死号)。
这一层不改排序、不动分数, 只在池内每个频道上贴身份标签 + 三色行动分级:
  🟢 可直接外联  /  🟡 需人工核验(附核验原因)  /  🔴 自动拦截(附拦截原因)

⚠️ 架构铁律(务必遵守):
  身份过滤器**只跑产品路径**(score / run_radar 的展示与排序层)。它可以看品牌 token
  (already_partner 判定需要看 insta360/影石/优惠码)。
  冻结考试池的 backtest.py 官方指标路径**绝不 import 本模块**——打分的语义盲测该剃词照剃,
  身份识别是产品出口层, 与召回质量的官方数字彻底隔离。

判级规则照审计报告「行动分级建议」:
  🔴 命中 already_partner / competitor / brand_or_vendor / reposter / dead_account 任一 → 自动拦截。
  🟢 内容契合(sem 命中)且 last_upload<=stale_days 且无任何红/黄标 → 可直接外联。
  🟡 其余(停更 stale / 无上传日期 / 机构号 / 小语种 / format 存疑)→ 人工核验, 附具体核验点。

数据契约:
  输入 = 池子原始行(dict, 含 channel_name/description/recent_video_titles/subscribers/
         last_upload_date 等)。可选合并 scored 行的 sem/sweet(用于内容契合与 tiny-sweet 死号判定)。
  输出 = annotate() 就地给行加:
    identity_flags: list[str]   命中的身份标签(机器可读键)
    action_grade:   "🟢"|"🟡"|"🔴"
    grade_reasons:  list[str]   人类可读的分级理由(拦截/核验原因标签)
  另暴露 build_identity_index(cfg) 预编译词典+正例名单, 供批量调用复用(不必每行重编正则)。
"""
import json, os, re
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 内容契合门槛: sem 达到此值才算「内容命中」(🟢 的必要条件之一)。
# 取 0.45(审计里语义分最低的真契合创作者约 0.45), 低于此更可能是误召回, 不给绿灯。
SEM_FIT_MIN = 0.45

# ---- W17(2026-07-08 Max 直接反馈): 官方联动 🏢 (official_channel) ----
# 语义 = 「非竞品」的企业/机构/官方账号(央企国企/运营商/车企/品牌官号/车主俱乐部),
# 不属于达人合作范围, 应转官方 BD 联动, 与 🔴「别碰」(竞品/搬运/僵尸)区分开。
# 判定信号中文机构词表 + 英文 corporate 模式; 命中 → 展示「🏢官方联动」。
# 校准锚点(Max 点名): 凯迪拉克车主/中国安能/中国电信 必落 🏢。
# 防误伤: 个人运动员的「国家队/国家一级运动员」不算机构(那是荣誉不是雇主); 漫剧号「官方授权推广」
#          是搬运号话术(留 🔴 reposter/marketing), 不算官号。故信号避开这两类裸词。
# 词表下沉 config(identity.official_channel_pattern / _en_pattern)可调; 缺省用下列默认(离线也能跑)。
_OFFICIAL_ZH_DEFAULT = (
    r"中国(?:电信|移动|联通|石油|石化|石油天然气|银行|平安|人寿|国航|东方航空|南方航空|"
    r"铁路|邮政|安能|中车|中铁|建筑|华能|大唐|华电|国电|能建)"
    r"|运营商|央企|国资委|武警.{0,6}转隶|水电铁军"
    r"|(?:有限|股份|科技|网络|文化|传媒|集团)(?:有限)?公司"
    r"|高新技术企业|专精特新|国家级.{0,4}企业"
    r"|品牌官方(?:账号|旗舰店)?|官方旗舰店|旗舰店官方|官方(?:唯一)?(?:授权)?总代理|中国区(?:唯一)?总代理"
    r"|(?:头盔|轮胎|机油|装备|器材|相机|镜头|背包)官方"
    r"|车主之家|车友会|车主俱乐部|车主之友|\d?S\s*店|经销商门店"
)
# 英文侧收紧: 只认强机构标记(官方旗舰店/授权经销/国企/车主俱乐部)。
# 刻意**不**用裸 inc./corporation/co.,ltd —— 它们在创作者简介里高频误伤(founder of X Inc.、© NobodySurf)。
# 也刻意**不**用裸 'official channel' —— 个人号常写 "Terje's official channel"(=我的真号)而非机构官号,
#   会误伤职业运动员/独立制作。机构语义只认 官方旗舰店/官方商店 这类"卖货/授权"信号。
# 中文机构词已足够精准, 三个锚点(凯迪拉克车主/中国安能/中国电信)也全中文, 英文只兜真·企业店铺。
_OFFICIAL_EN_DEFAULT = (
    r"\bofficial\s+(?:brand\s+)?(?:flagship\s+)?(?:store|shop)\b"
    r"|\bflagship\s+store\b"
    r"|\bauthori[sz]ed\s+(?:dealer|distributor|reseller)\b"
    r"|\bstate[-\s]?owned\s+enterprise\b"
    r"|\bowners?\s+club\b"
)


def _load_positives(cfg):
    """26 个正例名单 -> (channel_id 集, channel_url 集)。找不到文件返回空集(优雅降级)。"""
    node = cfg.get("identity", {})
    p = node.get("positives_path", "data/positives/positives.jsonl")
    path = p if os.path.isabs(p) else os.path.join(ROOT, p)
    ids, urls = set(), set()
    if not os.path.exists(path):
        return ids, urls
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("channel_id"):
            ids.add(r["channel_id"])
        if r.get("channel_url"):
            urls.add(r["channel_url"].rstrip("/"))
    return ids, urls


def _load_tags(cfg):
    """打标层产物 tags_for_scoring.jsonl -> {channel_id: vertical}。
    供 off_topic 兜底(W7): vertical=other 的频道自动转 🟡。找不到文件返回空 dict(优雅降级, 无兜底)。"""
    node = cfg.get("identity", {})
    p = node.get("tags_path")
    if not p:
        return {}
    path = p if os.path.isabs(p) else os.path.join(ROOT, p)
    out = {}
    if not os.path.exists(path):
        return out
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        cid = r.get("channel_id")
        if cid:
            out[cid] = r.get("vertical")
    return out


def _c(pat):
    """大小写不敏感编译; 空模式返回一个永不匹配的正则。"""
    return re.compile(pat, re.I) if pat else re.compile(r"(?!x)x")


def build_identity_index(cfg):
    """预编译身份词典 + 正例名单, 返回可复用的 index dict。批量调用只编一次。"""
    idn = cfg.get("identity", {})
    pos_ids, pos_urls = _load_positives(cfg)
    return {
        "cfg": cfg,
        "idn": idn,
        "pos_ids": pos_ids,
        "pos_urls": pos_urls,
        "partner_ids": set(idn.get("already_partner_ids", [])),
        "tags_by_id": _load_tags(cfg),
        "off_topic_verticals": set(idn.get("off_topic_verticals", ["other"])),
        "leak_re": _c(cfg.get("leak_tokens_pattern", "")),
        "partner_extra_re": _c(idn.get("partner_extra_pattern", "")),
        "partner_sponsored_re": _c(idn.get("partner_sponsored_pattern", "")),
        "competitor_re": _c(idn.get("competitor_pattern", "")),
        "competitor_official_re": _c(idn.get("competitor_official_pattern", "")),
        "competitor_official_desc_re": _c(idn.get("competitor_official_desc_pattern", "")),
        "competitor_official_prefix_re": _c(idn.get("competitor_official_name_prefix", "")),
        "competitor_staff_re": _c(idn.get("competitor_staff_pattern", "")),
        "brand_vendor_re": _c(idn.get("brand_vendor_pattern", "")),
        "reposter_re": _c(idn.get("reposter_pattern", "")),
        "reposter_channel_re": _c(idn.get("reposter_channel_pattern", "")),
        "reposter_title_comp_re": _c(idn.get("reposter_title_compilation", "")),
        "reposter_guard_re": _c(idn.get("reposter_original_guard_pattern", "")),
        "marketing_re": _c(idn.get("marketing_finance_pattern", "")),
        "org_re": _c(idn.get("org_account_pattern", "")),
        # W17 官方联动 🏢: config 有词表用 config, 缺省回落内置默认(离线也能判)。
        "official_zh_re": _c(idn.get("official_channel_pattern") or _OFFICIAL_ZH_DEFAULT),
        "official_en_re": _c(idn.get("official_channel_en_pattern") or _OFFICIAL_EN_DEFAULT),
        "dead_days": idn.get("dead_days", 365),
        "stale_days": idn.get("stale_days", 60),
        "dead_tiny_subs": idn.get("dead_tiny_subs", 500),
        "dead_tiny_sweet": idn.get("dead_tiny_sweet", 0.05),
    }


def _row_text(row):
    """频道名 + 简介 + 近期标题拼成一段, 供词典匹配。"""
    return " ".join(filter(None, [
        row.get("channel_name") or "",
        row.get("description") or "",
        " ".join(row.get("recent_video_titles") or []),
    ]))


def _days_since(iso_day, today=None):
    """距今天数; 解析不了返回 None(=无上传日期, 走 🟡)。"""
    if not iso_day:
        return None
    today = today or date.today()
    s = str(iso_day)[:10]
    try:
        d = datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None
    return (today - d).days


# W14(2026-07-08) 日期精度: yt-dlp flat 元数据对老视频只给相对时间("1 year ago"),
# 换算成绝对日期后落在抓取日, 导致停更天数在老频道上是伪精度(见 REPORT.md 数据可信度发现)。
# 采集层给每条记录新增 last_upload_precision ∈ {day, month, year}(见 collect.py)。
# dead/stale 判定按精度下界: 年级精度不做日级裁决(临界一律 🟡, 不 🔴)。
# 无此字段的历史记录(pool 里精度字段还没自然长出来)按"未知精度=保守 year"处理。
_PRECISION_TO_BOUND_DAYS = {"day": 1, "month": 31, "year": 365}


def _upload_precision(row):
    """取记录的 last_upload_precision; 缺失(老记录)按最保守的 'year' 处理(=按下界最松)。"""
    p = row.get("last_upload_precision")
    if p in _PRECISION_TO_BOUND_DAYS:
        return p
    return "year"


def _days_lower_bound(days, precision):
    """按精度取停更天数的下界: 年级精度的 '停更 X 天' 真实含义是'至少 X - 365 天'(最乐观)。
    dead 判定用下界=最乐观值, 保证'伪精度不硬拦'(365~730 天且精度=year 的临界一律不判死)。
    返回 (下界天数, 精度是否为日级)。"""
    if days is None:
        return None, False
    slack = _PRECISION_TO_BOUND_DAYS.get(precision, 365)
    # 日级精度: 下界=本值(slack=1, 减 1 无实质影响); 月/年级: 下界=本值减去该精度粒度。
    lower = days - (slack - 1) if slack > 1 else days
    return max(lower, 0), (precision == "day")


def _looks_official_name(name, prefix_re=None):
    """频道名是否像竞品'官方系'(用于竞品官方 vs 竞品用户号的半自动初判)。

    W14(2026-07-08)改法: 弃 `bike$` 类后缀碰运气, 改用竞品词前缀正则(名字以 gopro/dji 等开头
    =官方系, 如 'GoPro Bike'/'GoPro Motorsports'); 保留 official/官方 显式词与裸竞品名。
    prefix_re 由 index['competitor_official_prefix_re'] 传入(config 可调)。
    """
    n = (name or "").lower()
    if re.search(r"\bofficial\b|官方", n):
        return True
    if prefix_re is not None and prefix_re.search(name or ""):
        return True
    return n.strip() in ("gopro", "dji", "akaso", "sjcam")


def annotate(row, index, sem=None, sweet=None, today=None):
    """给单个池子行贴身份标签 + 行动分级。就地修改并返回同一个 row。

    sem/sweet 可从 scored 行传入(内容契合 & tiny-sweet 死号判定)。缺失时:
      - sem 缺 -> 内容契合判定放宽(不因缺分误判死号, 但也拿不到绿灯的 sem 命中)。
      - sweet 缺 -> tiny-sweet 死号分支跳过(只靠停更天数判死号)。
    """
    text = _row_text(row)
    name = row.get("channel_name") or ""
    flags = []
    reasons = []  # (grade, label) 供分级; grade ∈ {red, yellow}

    # ---------- 🔴 硬拦截类 ----------
    # 1) already_partner 分级(W14 2026-07-08): 🔴 只留三类实锤 → ①显式 id 集(26正例+官方名册+打标层
    #    already_partner_ids)②优惠码 ③自述 ambassador/大使/sponsored by insta360。
    #    仅命中品牌词(leak_tokens 如 Antigravity/insta360 出现在评测标题)而无上述实锤 → 🟡 brand_mention。
    #    根因: Chris Rogers/Danny Mcgee 标题提 Antigravity(评测非合作)、World of Ozz 标题提 Insta360 X5
    #    (评测非合作)原被 leak_hit 误判 🔴; Vital MTB 'Unaffiliated' 里的 affiliate 假命中已在词典层修掉。
    url = (row.get("channel_url") or "").rstrip("/")
    cid = row.get("channel_id")
    in_positive = cid in index["pos_ids"] or url in index["pos_urls"]
    in_partner_list = bool(cid) and cid in index["partner_ids"]
    leak_hit = bool(index["leak_re"].search(text))                       # 品牌词出现(不足以定 🔴)
    partner_hit = bool(index["partner_extra_re"].search(text))           # 优惠码/自述大使等实锤
    sponsored_hit = bool(index["partner_sponsored_re"].search(text))     # sponsored by insta360 / 带编号联盟码
    hard_partner = in_positive or in_partner_list or partner_hit or sponsored_hit
    if hard_partner:
        flags.append("already_partner")
        why = []
        if in_positive:
            why.append("在26正例名单")
        if in_partner_list:
            why.append("在已合作名单(打标层新增)")
        if partner_hit or sponsored_hit:
            why.append("带优惠码/自述大使/sponsored by insta360")
        reasons.append(("red", "already_partner(实锤: " + "、".join(why) + " → 转存量维护)"))
    elif leak_hit:
        # 只命中品牌词、无实锤 → 提及品牌不等于合作, 转 🟡 人工确认(不再 🔴 误伤器材评测大号)
        flags.append("brand_mention")
        reasons.append(("yellow", "brand_mention(提及影石/Antigravity 等品牌词但无优惠码/大使/赞助实锤, 疑似评测提及非合作 → 人工确认)"))

    # 2) competitor(W14 2026-07-08): 竞品词命中 且 (在职自述 / 官方名前缀 / 官方简介模板指纹)。
    #    弃 bike$ 后缀碰运气, 改 ①频道名以竞品词开头(GoPro Motorsports/GoPro Bike) ②简介模板指纹
    #    (同竞品多子频道共用的公司模板句)。补拦 GoPro Motorsports(简介=GoPro 公司模板逐字原文, 原漏网)。
    comp_hit = bool(index["competitor_re"].search(text))
    if comp_hit:
        staff_hit = bool(index["competitor_staff_re"].search(text))
        desc_template_hit = bool(index["competitor_official_desc_re"].search(text))
        name_official = _looks_official_name(name, index["competitor_official_prefix_re"])
        official_hit = desc_template_hit or (bool(index["competitor_official_re"].search(text)) and name_official)
        if staff_hit or official_hit:
            flags.append("competitor")
            if staff_hit:
                tag = "在职员工"
            elif desc_template_hit:
                tag = "官方号(简介模板指纹)"
            else:
                tag = "官方号(频道名前缀)"
            reasons.append(("red", f"competitor(竞品{tag}, 命中 gopro/dji/akaso 等 → 拦截)"))
        else:
            # 竞品词命中但更像'用户提到用过竞品器材', 不拦, 交人工看一眼
            flags.append("competitor_mention")
            reasons.append(("yellow", "competitor_mention(简介提到竞品器材, 疑似用户非官方 → 人工确认)"))

    # 2b) official_channel(W17 2026-07-08 Max 直接反馈): 「非竞品」企业/机构/官方账号 → 🏢官方联动。
    #     语义 = 不属于达人合作范围, 转官方 BD 联动(央企国企/运营商/车企/品牌官号/车主俱乐部)。
    #     与 🔴「别碰」(竞品/搬运/僵尸)区分。命中竞品的已在上面判 🔴, 这里只认非竞品(comp_hit=False)。
    #     锚点: 凯迪车主之家/中国安能/中国电信 必落此类。它会**吸收** brand_or_vendor / org_account
    #     两个重叠信号(否则凯迪车主之家会同时挂 brand_or_vendor 🔴), 由 🏢 统一表达。
    #     防误伤: 词表避开裸「国家队/国家一级运动员」(个人荣誉非雇主)与「官方授权推广」(漫剧搬运话术)。
    is_official = (not comp_hit) and bool(
        index["official_zh_re"].search(text) or index["official_en_re"].search(text))
    if is_official:
        flags.append("official_channel")
        reasons.append(("official", "official_channel(企业/机构/官方账号, 非竞品 → 不属达人合作, 转官方 BD 联动)"))

    # 3) brand_or_vendor: 品牌方/器材店/招商/厂商。官方联动号已由 🏢 吸收, 此处不再重复挂 🔴。
    if not is_official and index["brand_vendor_re"].search(text):
        flags.append("brand_or_vendor")
        reasons.append(("red", "brand_or_vendor(品牌/厂商/器材店/招商信号 → 拦截, 非创作者)"))

    # 4) reposter(W14 2026-07-08 裸词治理): 先看原创者反搬运声明护栏('Do not repost')= 原创信号, 命中即豁免。
    #    否则命中实锤搬运词(reposter_re: 搬运/侵删/代录/录播/reupload) 或 频道定位词组(reposter_channel_re:
    #    rider submissions / compilation channel / 全网录播) 或 近多条标题都是合集型(>=2 条) → 🔴。
    #    单标题碰 reaction/compilation 不再拦(CycleCruza 'YouTubers Reaction'/Langona 土耳其语 REACTION 是原创)。
    reposter_guarded = bool(index["reposter_guard_re"].search(text))
    comp_titles = sum(1 for t in (row.get("recent_video_titles") or [])
                      if index["reposter_title_comp_re"].search(t or ""))
    reposter_signal = (bool(index["reposter_re"].search(text))
                       or bool(index["reposter_channel_re"].search(text))
                       or comp_titles >= 2)
    if reposter_signal and not reposter_guarded:
        flags.append("reposter")
        why = "频道定位为投稿合集/reaction/录播代录" if comp_titles >= 2 or index["reposter_channel_re"].search(text) \
            else "简介明写搬运/侵删/代录/录播"
        reasons.append(("red", f"reposter({why} → 拦截, 非原创)"))

    # 4b) marketing_or_finance: 营销号/理财致富/玄学疗愈/带货种草(Max 点名必拦第三类)
    if index["marketing_re"].search(text):
        flags.append("marketing_or_finance")
        reasons.append(("red", "marketing_or_finance(理财致富/玄学疗愈/带货种草/营销号信号 → 拦截, 非运动影像创作者)"))

    # 5) dead_account(W14 2026-07-08 精度化): 停更判定按精度下界。yt-dlp 对老视频只给相对时间,
    #    换算绝对日期后停更天数在老频道上是伪精度(年级)。dead 用下界(=最乐观停更天数): 只有下界仍
    #    > dead_days 才判死; 年级精度的临界(365~730 天)下界 <= 0~365 不达线, 一律不硬拦 → 落 🟡 stale。
    #    无 last_upload_precision 字段的老记录按'未知=year'保守处理(精度字段从下次采集自然长出, 不回填)。
    days = _days_since(row.get("last_upload_date"), today)
    precision = _upload_precision(row)
    days_lb, is_day_precise = _days_lower_bound(days, precision)
    is_dead = False
    if days_lb is not None and days_lb > index["dead_days"]:
        is_dead = True
        prec_note = "" if is_day_precise else f", 精度={precision}按下界"
        reasons.append(("red", f"dead_account(停更下界 {days_lb} 天 > {index['dead_days']}{prec_note} → 拦截, 僵尸号)"))
    elif (row.get("subscribers") is not None and row["subscribers"] < index["dead_tiny_subs"]
          and sweet is not None and sweet <= index["dead_tiny_sweet"]):
        is_dead = True
        reasons.append(("red", f"dead_account(订阅 {row['subscribers']}<{index['dead_tiny_subs']} 且甜点≈0 → 拦截, 空号)"))
    if is_dead:
        flags.append("dead_account")

    # ---------- 🟡 人工核验类(不拦截, 只标核验点) ----------
    if days is None:
        # 无上传日期(YT last_upload 缺失, 或 B站本就没这字段) = data_coverage 不足
        flags.append("no_upload_date")
        reasons.append(("yellow", "no_upload_date(无上传日期, data_coverage 不足 → 补活性数据)"))
    elif not is_dead and days > index["stale_days"]:
        # 停更超 stale_days 但未判死(含年级精度躲过 dead 下界的 366+ 天临界僵尸) → 🟡 核验弃坑。
        # W14: 上界不再硬卡 dead_days; 年级精度临界(下界未过线)落这里而非 🔴 硬拦。
        flags.append("stale")
        if precision != "day" and days > index["dead_days"]:
            reasons.append(("yellow", f"stale(停更 {days} 天但精度={precision}, 下界 {days_lb} 天未过 {index['dead_days']} 死线 → 年级精度不做日级裁决, 核验是否弃坑)"))
        else:
            reasons.append(("yellow", f"stale(停更 {days} 天, >{index['stale_days']} → 核验是否弃坑)"))

    # org_account: 媒体/机构/俱乐部。官方联动号(🏢)已吸收此信号, 不再叠 🟡(中国安能=水电铁军
    # 会命中 org 的「国家队」联想词, 但它已判 official → 归 🏢 不归 🟡)。
    if not is_official and index["org_re"].search(text):
        flags.append("org_account")
        reasons.append(("yellow", "org_account(媒体/机构/俱乐部信号 → 走机构合作而非达人盲投)"))

    # off_topic 兜底(W7): 打标层判定 vertical=other(垂类不属运动影像十类, 如汽车/航空) → 疑似跑题误召回。
    # 只标注 + 转 🟡 人工核验, 绝不物理删除。无标签(频道未打标或 tags_path 缺失)则不触发。
    vertical = index["tags_by_id"].get(cid)
    if vertical is not None and vertical in index["off_topic_verticals"]:
        flags.append("off_topic_suspect")
        reasons.append(("yellow", f"off_topic_suspect(打标层判 vertical={vertical}, 疑似非运动影像 → 人工核验内容相关性)"))

    # ---------- 判级 ----------
    # 优先级: 🔴 真拦(竞品/搬运/营销/僵尸/已合作) > 🏢 官方联动(非竞品企业/机构, 转 BD) > 🟡 核验 > 🟢。
    # 🏢 排在 🔴 之后: 若同一账号还真踩了别的硬红(如既是官号又已判死), 硬红优先(仍是"别碰")。
    reds = [lbl for g, lbl in reasons if g == "red"]
    officials = [lbl for g, lbl in reasons if g == "official"]
    yellows = [lbl for g, lbl in reasons if g == "yellow"]
    if reds:
        grade = "🔴"
        grade_reasons = reds
    elif officials:
        grade = "🏢"
        grade_reasons = officials
    elif yellows:
        grade = "🟡"
        grade_reasons = yellows
    else:
        # 无任何红/黄标: 要拿绿灯还需内容契合(sem 命中) + 活性 ok(days<=stale_days)。
        # sem 缺失时不强卡(信息不足按 🟡 更安全), days<=stale 已由上面保证(否则会进 stale/no_date/dead)。
        if sem is not None and sem < SEM_FIT_MIN:
            grade = "🟡"
            grade_reasons = [f"low_fit(语义分 {sem:.2f}<{SEM_FIT_MIN}, 疑似弱召回 → 核验内容契合)"]
        else:
            grade = "🟢"
            grade_reasons = []

    row["identity_flags"] = flags
    row["action_grade"] = grade
    row["grade_reasons"] = grade_reasons
    return row


def annotate_scored(scored, pool_by_url, cfg, today=None):
    """批量: 对 scored 列表(run_radar 打分产物)逐行贴身份标签。
    从 pool_by_url 取原始行(简介/last_upload 等词典要看的字段), 从 scored 取 sem/sweet。
    就地给每个 scored 行加 identity_flags / action_grade / grade_reasons。返回 scored。"""
    index = build_identity_index(cfg)
    for s in scored:
        row = dict(pool_by_url.get(s.get("channel_url"), {}))
        # 词典要看频道名/简介/标题(pool 行有), scored 行只带 channel_name; 合并出一份判定输入。
        row.setdefault("channel_name", s.get("channel_name"))
        row.setdefault("channel_url", s.get("channel_url"))
        row.setdefault("subscribers", s.get("subscribers"))
        # channel_id 供 already_partner_ids 精确匹配与 off_topic 兜底的 tag 查询(pool 行有则用其值)。
        if s.get("channel_id"):
            row.setdefault("channel_id", s.get("channel_id"))
        res = annotate(row, index, sem=s.get("sem"), sweet=s.get("sweet"), today=today)
        s["identity_flags"] = res["identity_flags"]
        s["action_grade"] = res["action_grade"]
        s["grade_reasons"] = res["grade_reasons"]
    return scored


# 供 run_radar / 日报组装用的短标签映射(机器键 -> 中文短标签, 表格「身份标签」列用)
FLAG_LABELS = {
    "already_partner": "已合作/带码",
    "brand_mention": "提及品牌(疑非合作)",
    "competitor": "竞品官方/在职",
    "competitor_mention": "提及竞品器材",
    "brand_or_vendor": "品牌/厂商/器材店",
    "official_channel": "🏢官方联动",
    "reposter": "搬运/合集/代录",
    "marketing_or_finance": "营销/理财/玄学/带货",
    "dead_account": "僵尸/空号",
    "stale": "久未更新",
    "no_upload_date": "无上传日期",
    "org_account": "媒体/机构",
    "off_topic_suspect": "off_topic 疑似",
    # 浪层的蹭热点标记(radar_lib.fuse_rising/fuse_trends 追加, 非本层判定): 只做展示层 ⚠️ 徽章, 不进分级
    "trend_chaser": "⚠️蹭热点",
    # 在涨证据 >80% 来自同一条视频(radar_lib.fuse_rising 追加): 只标注不改分(Max 拍板采纳)
    "single_video_driven": "⚠️单视频驱动",
}


def flags_zh(flags):
    """身份标签列表 -> 中文短标签串(表格「身份标签」列)。"""
    return "、".join(FLAG_LABELS.get(f, f) for f in (flags or []))


def _main():
    """CLI 自检 / 批量跑一个 ranked.json + pool, 打印分级分布与红榜样例。"""
    import argparse, sys
    sys.path.insert(0, HERE)
    from radar_lib import load_config, load_pool
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "config", "insta360.json"))
    ap.add_argument("--pool", help="池子 jsonl(缺省取 config.pool_path)")
    ap.add_argument("--ranked", help="可选: ranked.json(带 sem/sweet), 更准的死号/契合判定")
    ap.add_argument("--top", type=int, default=50, help="只看前 N 名(按 ranked 顺序)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    pool_path = args.pool or os.path.join(ROOT, cfg.get("pool_path", "data/pool/creator_pool.jsonl"))
    pool = load_pool(pool_path)
    pool_by_url = {r["channel_url"]: r for r in pool}
    index = build_identity_index(cfg)

    if args.ranked:
        scored = json.load(open(args.ranked))[:args.top]
        annotate_scored(scored, pool_by_url, cfg)
        rows = scored
    else:
        rows = [annotate(dict(r), index) for r in pool[:args.top]]

    from collections import Counter
    grades = Counter(r["action_grade"] for r in rows)
    print(json.dumps({"n": len(rows), "grades": dict(grades)}, ensure_ascii=False))
    for r in rows:
        if r["action_grade"] == "🔴":
            print(f"  🔴 {r.get('channel_name'):<28} {flags_zh(r['identity_flags'])}  | {r['grade_reasons'][0] if r['grade_reasons'] else ''}")


if __name__ == "__main__":
    _main()
