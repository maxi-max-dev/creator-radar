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
        "competitor_re": _c(idn.get("competitor_pattern", "")),
        "competitor_official_re": _c(idn.get("competitor_official_pattern", "")),
        "competitor_staff_re": _c(idn.get("competitor_staff_pattern", "")),
        "brand_vendor_re": _c(idn.get("brand_vendor_pattern", "")),
        "reposter_re": _c(idn.get("reposter_pattern", "")),
        "marketing_re": _c(idn.get("marketing_finance_pattern", "")),
        "org_re": _c(idn.get("org_account_pattern", "")),
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


def _looks_official_name(name):
    """频道名/简介是否像'官方号'(用于竞品官方 vs 竞品用户号的半自动初判)。"""
    n = (name or "").lower()
    return bool(re.search(r"\bofficial\b|官方|\bbike\b$", n)) or n.strip() in ("gopro", "dji", "akaso", "sjcam")


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
    # 1) already_partner: 正例名单命中 / 打标层新挖的 partner_ids(按 channel_id) / 简介带影石痕迹 / 优惠码 / ambassador
    url = (row.get("channel_url") or "").rstrip("/")
    cid = row.get("channel_id")
    in_positive = cid in index["pos_ids"] or url in index["pos_urls"]
    in_partner_list = bool(cid) and cid in index["partner_ids"]
    leak_hit = bool(index["leak_re"].search(text))
    partner_hit = bool(index["partner_extra_re"].search(text))
    if in_positive or in_partner_list or leak_hit or partner_hit:
        flags.append("already_partner")
        why = []
        if in_positive:
            why.append("在26正例名单")
        if in_partner_list:
            why.append("在已合作名单(打标层新增)")
        if leak_hit:
            why.append("简介带影石痕迹")
        if partner_hit:
            why.append("带优惠码/ambassador")
        reasons.append(("red", "already_partner(已合作/带码: " + "、".join(why) + " → 转存量维护)"))

    # 2) competitor: 竞品词命中 且 (官方名信号 或 在职自述)
    comp_hit = bool(index["competitor_re"].search(text))
    if comp_hit:
        staff_hit = bool(index["competitor_staff_re"].search(text))
        official_hit = bool(index["competitor_official_re"].search(text)) and _looks_official_name(name)
        if staff_hit or official_hit:
            flags.append("competitor")
            tag = "在职员工" if staff_hit else "官方号"
            reasons.append(("red", f"competitor(竞品{tag}, 命中 gopro/dji/akaso 等 → 拦截)"))
        else:
            # 竞品词命中但更像'用户提到用过竞品器材', 不拦, 交人工看一眼
            flags.append("competitor_mention")
            reasons.append(("yellow", "competitor_mention(简介提到竞品器材, 疑似用户非官方 → 人工确认)"))

    # 3) brand_or_vendor: 品牌方/器材店/招商/厂商
    if index["brand_vendor_re"].search(text):
        flags.append("brand_or_vendor")
        reasons.append(("red", "brand_or_vendor(品牌/厂商/器材店/招商信号 → 拦截, 非创作者)"))

    # 4) reposter: 搬运/侵删/合集/代录/录播
    if index["reposter_re"].search(text):
        flags.append("reposter")
        reasons.append(("red", "reposter(搬运/侵删/合集/代录/录播信号 → 拦截, 非原创)"))

    # 4b) marketing_or_finance: 营销号/理财致富/玄学疗愈/带货种草(Max 点名必拦第三类)
    if index["marketing_re"].search(text):
        flags.append("marketing_or_finance")
        reasons.append(("red", "marketing_or_finance(理财致富/玄学疗愈/带货种草/营销号信号 → 拦截, 非运动影像创作者)"))

    # 5) dead_account: 停更 > dead_days, 或 订阅<tiny 且 sweet≈0
    days = _days_since(row.get("last_upload_date"), today)
    is_dead = False
    if days is not None and days > index["dead_days"]:
        is_dead = True
        reasons.append(("red", f"dead_account(停更 {days} 天 > {index['dead_days']} → 拦截, 僵尸号)"))
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
    elif index["stale_days"] < days <= index["dead_days"]:
        flags.append("stale")
        reasons.append(("yellow", f"stale(停更 {days} 天, 60~365 → 核验是否弃坑)"))

    if index["org_re"].search(text):
        flags.append("org_account")
        reasons.append(("yellow", "org_account(媒体/机构/俱乐部信号 → 走机构合作而非达人盲投)"))

    # off_topic 兜底(W7): 打标层判定 vertical=other(垂类不属运动影像十类, 如汽车/航空) → 疑似跑题误召回。
    # 只标注 + 转 🟡 人工核验, 绝不物理删除。无标签(频道未打标或 tags_path 缺失)则不触发。
    vertical = index["tags_by_id"].get(cid)
    if vertical is not None and vertical in index["off_topic_verticals"]:
        flags.append("off_topic_suspect")
        reasons.append(("yellow", f"off_topic_suspect(打标层判 vertical={vertical}, 疑似非运动影像 → 人工核验内容相关性)"))

    # ---------- 判级 ----------
    reds = [lbl for g, lbl in reasons if g == "red"]
    yellows = [lbl for g, lbl in reasons if g == "yellow"]
    if reds:
        grade = "🔴"
        grade_reasons = reds
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
    "competitor": "竞品官方/在职",
    "competitor_mention": "提及竞品器材",
    "brand_or_vendor": "品牌/厂商/器材店",
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
