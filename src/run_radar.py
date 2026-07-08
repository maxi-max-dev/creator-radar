#!/usr/bin/env python3
"""达人雷达总调度: 一条命令跑全链。
collect(refresh+discover) -> 全池重排 -> 与上次运行 diff -> 生成推荐卡 -> 日报 markdown -> 推送 -> 写 logs/radar.log。
一切产出留仓库目录(reports/logs/data)，绝不写 iCloud 路径(launchd 下 TCC 会静默失败)。
"""
import argparse, csv, json, os, subprocess, sys, glob
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from radar_lib import load_config, load_pool, score_pool, fuse_rising
import identity_filter
import ai_review
import schema

# 引擎版本戳(打进 scoreboard picks, 让每期预测可追溯是哪版引擎下的注)。
# fit 打分核仍是冻结考试池验证过的 v1.2; rising = 2026-07-07 三信号合成的在涨层(单段式融合)。
ENGINE_VERSION = "v1.2-rising"

POOL = os.path.join(ROOT, "data", "pool", "creator_pool.jsonl")
RSS_DIR = os.path.join(ROOT, "data", "rss")
TRENDS_DIR = os.path.join(ROOT, "data", "trends")
DAILY = os.path.join(ROOT, "data", "runs", "daily")
SCOREBOARD = os.path.join(ROOT, "data", "scoreboard")
REPORTS = os.path.join(ROOT, "reports")
LOGS = os.path.join(ROOT, "logs")
NOTIFY = "/Users/max/.openclaw/workspace/scripts/notify-imessage.sh"


def prev_ranked(today):
    """找上一次 daily run 的 ranked.json(日期 < today)，用于排名 diff。没有则 None。"""
    dirs = sorted(d for d in glob.glob(os.path.join(DAILY, "*")) if os.path.basename(d) < today)
    for d in reversed(dirs):
        p = os.path.join(d, "ranked.json")
        if os.path.exists(p):
            return {r["channel_url"]: r["rank"] for r in json.load(open(p))}
    return None


def theme_keywords(cfg):
    """从 config 主题变体里抽关键词(去停用词)，用于 themes_hit 的诚实文本命中。"""
    stop = set("the a an of to in on for and with across is are riding ride video footage perspective "
               "camera degree real daily life sharing solo self personal storytelling review technique "
               "tutorial expert level professional skill track epic wild terrain long distance journey "
               "road trip documentary overcoming hardship mounting filming immersive first person "
               "action bold adventurous challenge extreme stunts pushing limits chest mount".split())
    out = {}
    for name, t in cfg["themes"].items():
        words = set()
        for v in t["variants"]:
            for w in v.lower().replace(",", " ").replace("-", " ").split():
                if len(w) > 3 and w not in stop:
                    words.add(w)
        out[name] = words
    return out


def themes_hit(row, tkw):
    """频道文本命中了哪些主题(关键词出现即算)。返回主题名列表。"""
    txt = " ".join(filter(None, [
        row.get("channel_name") or "", row.get("description") or "",
        " ".join(row.get("recent_video_titles") or []),
    ])).lower()
    return [name for name, kws in tkw.items() if any(k in txt for k in kws)]


def run_collect(cfg_path, budget, discover_terms):
    """跑采集器(refresh + discover)，返回其 JSON 结果。失败返回占位。"""
    cmd = ["python3", os.path.join(HERE, "collect.py"), "--config", cfg_path, "--refresh", "--discover"]
    if budget is not None:
        cmd += ["--budget", str(budget)]
    if discover_terms is not None:
        cmd += ["--discover-terms", str(discover_terms)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    sys.stderr.write(p.stderr)
    for line in reversed(p.stdout.strip().splitlines()):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return {"pool_before": None, "pool_after": None, "discovered": 0, "refreshed": 0, "discovered_names": []}


def diff_ranks(scored, prev):
    """标注每行 rank_delta(上升为正)，返回 (新进前100, 窜升榜)。"""
    newcomers, jumpers = [], []
    for s in scored:
        old = prev.get(s["channel_url"]) if prev else None
        s["rank_delta"] = (old - s["rank"]) if old else None
        if prev is not None and s["rank"] <= 100:
            if old is None or old > 100:
                newcomers.append(s)
            elif s["rank_delta"] and s["rank_delta"] >= 200:
                jumpers.append(s)
        elif prev is not None and s.get("rank_delta") and s["rank_delta"] >= 200:
            jumpers.append(s)
    return newcomers, jumpers


def gen_cards(cfg_path, ranked_path, out_dir, top_n=None, max_cards=None):
    cmd = ["python3", os.path.join(HERE, "explain.py"), "--config", cfg_path,
           "--pool", POOL, "--ranked", ranked_path, "--out", out_dir]
    if top_n is not None:
        cmd += ["--top-n", str(top_n)]
    if max_cards is not None:
        cmd += ["--max-cards", str(max_cards)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    sys.stderr.write(p.stderr)
    cards_path = os.path.join(out_dir, "cards.json")
    return json.load(open(cards_path)) if os.path.exists(cards_path) else []


def _last_json_line(text):
    """从子进程 stdout 末尾找最后一行合法 JSON(各工具都把结果打成 stdout 最后一行)。"""
    for line in reversed((text or "").strip().splitlines()):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def run_cross_platform(cfg_path):
    """跨平台矩阵重扫(零网络)。温和降级: 失败只返回错误串。"""
    try:
        p = subprocess.run(["python3", os.path.join(HERE, "cross_platform.py")],
                           capture_output=True, text=True, timeout=120)
        sys.stderr.write(p.stderr)
        res = _last_json_line(p.stdout)
        return res if res is not None else {"error": "no json output"}
    except Exception as e:
        return {"error": str(e)}


def run_dossiers(cfg_path):
    """当日推荐卡频道各产一页档案 MD。温和降级: 失败只返回错误串。"""
    try:
        p = subprocess.run(["python3", os.path.join(HERE, "dossier.py"), "--config", cfg_path],
                           capture_output=True, text=True, timeout=180)
        sys.stderr.write(p.stderr)
        res = _last_json_line(p.stdout)
        return res if res is not None else {"error": "no json output"}
    except Exception as e:
        return {"error": str(e)}


def run_comments(cfg_path, top_n=None):
    """top N 频道评论采样(受预算+去重账本约束)。温和降级: 失败只返回错误串。
    评论采样含真实网络请求，超时给足(单频道多视频)。"""
    cmd = ["python3", os.path.join(HERE, "comments.py"), "--config", cfg_path]
    if top_n is not None:
        cmd += ["--top-n", str(top_n)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        sys.stderr.write(p.stderr)
        res = _last_json_line(p.stdout)
        return res if res is not None else {"error": "no json output"}
    except Exception as e:
        return {"error": str(e)}


def run_transcripts(cfg_path, today, top_n=None):
    """top N 频道字幕采集(受预算+video 级去重账本约束)。温和降级: 失败只返回错误串。
    字幕采集含真实网络请求(yt-dlp 探测+拉字幕，禁下载本体)，超时给足(单频道多视频)。"""
    cmd = ["python3", os.path.join(HERE, "transcripts.py"), "--config", cfg_path, "--date", today]
    if top_n is not None:
        cmd += ["--top-n", str(top_n)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        sys.stderr.write(p.stderr)
        res = _last_json_line(p.stdout)
        return res if res is not None else {"error": "no json output"}
    except Exception as e:
        return {"error": str(e)}


def run_rss_momentum(cfg_path, pool_path, ranked_path, today):
    """起势层每日集成(MOMENTUM.md 第六节 步骤1+2): RSS 采集 -> momentum 打分。
    返回 momentum_scores.json 路径(供 fuse_momentum), 失败返回 None(fuse 会优雅降级为全中性)。

    RSS 采集是**只读网络**(拉 YouTube 官方 feed, 零下载), 按 --dry-run 铁律它可保留:
    dry-run 只掐真实推送(iMessage/飞书/commit), 不掐只读采集。momentum 打分是纯本地计算。
    采集器 append 友好(同日多跑不重复建文件, 攒历史), momentum 就地整算。"""
    stamp = today  # collect_rss 用 UTC 日期命名; 与主链 today(本地)可能差一天, 故显式对齐取产物
    mom_dir = os.path.join(DAILY, today)
    os.makedirs(mom_dir, exist_ok=True)
    mom_path = os.path.join(mom_dir, "momentum_scores.json")
    # 1) RSS 采集(只读). 失败不致命: 没有当日 RSS 就用已存在的最近一份, 再没有就跳过 momentum。
    try:
        p = subprocess.run(["python3", os.path.join(HERE, "collect_rss.py"), "--config", cfg_path,
                            "--pool", pool_path, "--ranked", ranked_path, "--out", RSS_DIR],
                           capture_output=True, text=True, timeout=1800)
        sys.stderr.write(p.stderr[-1500:] if p.stderr else "")
    except Exception as e:
        sys.stderr.write(f"[momentum] RSS 采集异常(不致命): {e}\n")
    # 找当日 RSS(UTC 命名可能是 today 或前后一天), 取最新一份
    rss_files = sorted(glob.glob(os.path.join(RSS_DIR, "*.jsonl")))
    if not rss_files:
        sys.stderr.write("[momentum] 无 RSS 快照, 跳过 momentum(fuse 将全中性降级)\n")
        return None
    rss_path = rss_files[-1]
    # 2) momentum 打分(纯本地)
    try:
        p = subprocess.run(["python3", os.path.join(HERE, "momentum.py"), "--config", cfg_path,
                            "--pool", pool_path, "--rss", rss_path, "--out", mom_path],
                           capture_output=True, text=True, timeout=600)
        sys.stderr.write(p.stderr[-1500:] if p.stderr else "")
        return mom_path if os.path.exists(mom_path) else None
    except Exception as e:
        sys.stderr.write(f"[momentum] 打分异常(不致命): {e}\n")
        return None


def run_trends_layer(cfg_path, pool_path, today, dry_run=False):
    """浪层每日集成(TRENDS.md 第7节 步骤1+2): 四路趋势采集 -> trend_score + breakout 打分。
    返回 (trend_scores_path, breakout_scores_path), 任一失败对应返回 None(fuse 优雅降级)。

    dry-run 铁律(总控裁量): 掐掉浪层**外部采集**(google/bilibili/reddit 三路真实外网请求),
    只留 pool_wave(池内浪, 复用已有 RSS+池子字段, 零新增网络)。这比 momentum 的 RSS 采集更严:
    RSS 是拉 YouTube 官方只读 feed(保留), 浪层外部三路涉及第三方端点(dry-run 一律不碰)。
    打分器(trends.py/breakout.py)是纯本地计算, 照常跑。任何失败只记 stderr 不中断主链。"""
    out_dir = os.path.join(DAILY, today)
    os.makedirs(out_dir, exist_ok=True)
    trend_out = os.path.join(out_dir, "trend_scores.json")
    terms_out = os.path.join(out_dir, "rising_terms.json")
    brk_out = os.path.join(out_dir, "breakout_scores.json")
    # 1) 采集(append 到 data/trends/<today>.jsonl; 四路独立 try, 失败只记原因)
    cmd = ["python3", os.path.join(HERE, "collect_trends.py"), "--date", today]
    if dry_run:
        cmd += ["--only", "pool_wave"]  # 外部三路掐掉, 池内浪零网络保留
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        sys.stderr.write(p.stderr[-1500:] if p.stderr else "")
    except Exception as e:
        sys.stderr.write(f"[trends] 采集异常(不致命): {e}\n")
    trends_files = sorted(glob.glob(os.path.join(TRENDS_DIR, "*.jsonl")))
    rss_files = sorted(glob.glob(os.path.join(RSS_DIR, "*.jsonl")))
    if not trends_files or not rss_files:
        sys.stderr.write("[trends] 无趋势/RSS 快照, 跳过浪层(fuse 将优雅降级)\n")
        return None, None
    trends_path, rss_path = trends_files[-1], rss_files[-1]
    # 2) 打分(纯本地)
    tp = bp = None
    try:
        p = subprocess.run(["python3", os.path.join(HERE, "trends.py"), "--trends", trends_path,
                            "--pool", pool_path, "--rss", rss_path,
                            "--out-scores", trend_out, "--out-terms", terms_out],
                           capture_output=True, text=True, timeout=600)
        sys.stderr.write(p.stderr[-1500:] if p.stderr else "")
        tp = trend_out if os.path.exists(trend_out) else None
    except Exception as e:
        sys.stderr.write(f"[trends] trend 打分异常(不致命): {e}\n")
    try:
        p = subprocess.run(["python3", os.path.join(HERE, "breakout.py"), "--pool", pool_path,
                            "--rss", rss_path, "--out", brk_out],
                           capture_output=True, text=True, timeout=600)
        sys.stderr.write(p.stderr[-1500:] if p.stderr else "")
        bp = brk_out if os.path.exists(brk_out) else None
    except Exception as e:
        sys.stderr.write(f"[trends] breakout 打分异常(不致命): {e}\n")
    return tp, bp


# 列结构单一来源在 schema.py。本地 CSV = 工程视图(全列); 飞书运营视图默认只显示 OPS 列。
# 三词化(2026-07-07): 总分->对路分, 行动分级->红绿灯, 新增在涨分; 起势/浪/破圈下沉为证据列。
CSV_COLUMNS = schema.CARDS_TABLE_COLUMNS


def build_table_rows(today, cards, scored, pool_by_url, dossier_links=None):
    """推荐行的单一数据源(原始类型值)。CSV 与 bitable 两个出口都从这里取数，保证同构不漂移。
    键名用 schema 的展示名(对路分/在涨分/潜力分/红绿灯), 数据含义不变。"""
    s_by_url = {s["channel_url"]: s for s in scored}
    dl_map = dossier_links or {}
    rows = []
    for c in cards:
        url = c.get("_channel_url", "")
        s = s_by_url.get(url, {})
        p = pool_by_url.get(url, {})
        why = (c.get("why_worth_signing") or []) + ["", "", ""]
        cid = url.rstrip("/").rsplit("/", 1)[-1]
        rows.append({
            "日期": today,
            "排名": s.get("rank", c.get("_rank")),
            "频道名": s.get("channel_name", c.get("channel_name", "")),
            "频道链接": url,
            "订阅数": s.get("subscribers"),
            schema.COL_SUBS_TEXT: schema.subs_dual(s.get("subscribers")),  # 订阅(双格式文本 W17)
            "排名百分位": s.get("pct"),
            schema.COL_FIT: s.get("score"),            # 对路分(原总分)
            schema.COL_RISING: s.get("rising"),        # 在涨分(合成)
            schema.COL_POTENTIAL: s.get("potential"),  # 潜力分 = 对路 × 在涨
            "语义分": s.get("sem"), "甜点分": s.get("sweet"), "POV标记分": s.get("pov"),
            "起势分": s.get("momentum"), "浪层分": s.get("trend"), "破圈比": s.get("breakout"),
            schema.COL_LIGHT: s.get("action_grade") or "",  # 红绿灯(原行动分级)
            "身份标签": identity_filter.flags_zh(s.get("identity_flags")),
            "命中主题": s.get("themes_hit") or [],           # 工程 key(旧列, CSV 下沉档案用)
            schema.COL_THEME_TAGS: schema.theme_tags_zh(s.get("themes_hit") or []),  # 主题标签(中文多选 W17)
            "是否新发现": p.get("source") == "auto-discover",
            "排名变动": s.get("rank_delta"),
            "档案": dl_map.get(cid, ""),
            "值得签1": why[0], "值得签2": why[1], "值得签3": why[2],
            "风险": c.get("risk", ""), "首次合作建议": c.get("first_collab", ""),
        })
    return rows


def _fmt_delta(d):
    return (f"+{d}" if d > 0 else str(d)) if d is not None else ""


# CSV 各列的格式化器(数值列保留原精度; None/缺失出空串)。缺省=原样 str。
def _fmt_num(v, nd):
    return f"{v:.{nd}f}" if v is not None else ""


def write_cards_table(today, rows, out_dir):
    """推荐卡表格化: 一行一个推荐人。utf-8-sig 保 Numbers/Excel 中文不乱码，状态列留空给运营填。
    列序来自 schema.CARDS_TABLE_COLUMNS(工程视图=全列)。"""
    path = os.path.join(out_dir, "cards_table.csv")
    fmt = {  # 列名 -> 单元格文本
        "日期": lambda r: r["日期"],
        "排名": lambda r: r["排名"] if r["排名"] is not None else "",
        "频道名": lambda r: r["频道名"],
        "频道链接": lambda r: r["频道链接"],
        "订阅数": lambda r: r["订阅数"] if r["订阅数"] is not None else "",
        schema.COL_SUBS_TEXT: lambda r: r.get(schema.COL_SUBS_TEXT, ""),
        schema.COL_FIT: lambda r: _fmt_num(r[schema.COL_FIT], 4),
        schema.COL_RISING: lambda r: _fmt_num(r[schema.COL_RISING], 4),
        schema.COL_POTENTIAL: lambda r: _fmt_num(r[schema.COL_POTENTIAL], 5),
        schema.COL_LIGHT: lambda r: r.get(schema.COL_LIGHT, ""),
        "身份标签": lambda r: r.get("身份标签", ""),
        "语义分": lambda r: _fmt_num(r["语义分"], 4),
        "甜点分": lambda r: _fmt_num(r["甜点分"], 4),
        "POV标记分": lambda r: _fmt_num(r["POV标记分"], 4),
        "起势分": lambda r: _fmt_num(r["起势分"], 4),
        "浪层分": lambda r: _fmt_num(r["浪层分"], 4),
        "破圈比": lambda r: _fmt_num(r["破圈比"], 3),
        "排名百分位": lambda r: f"{r['排名百分位']}%" if r["排名百分位"] is not None else "",
        # 命中主题 CSV 单元格换中文标签(工程 key 下沉档案; Max: 日报/CSV 也换中文名)
        "命中主题": lambda r: "、".join(schema.theme_tags_zh(r["命中主题"])),
        schema.COL_THEME_TAGS: lambda r: "、".join(r.get(schema.COL_THEME_TAGS, [])),
        "是否新发现": lambda r: "是" if r["是否新发现"] else "否",
        "排名变动": lambda r: _fmt_delta(r["排名变动"]),
        "档案": lambda r: r.get("档案", ""),
        "值得签1": lambda r: r["值得签1"], "值得签2": lambda r: r["值得签2"], "值得签3": lambda r: r["值得签3"],
        "风险": lambda r: r["风险"], "首次合作建议": lambda r: r["首次合作建议"],
        "状态": lambda r: "",
    }
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        for r in rows:
            w.writerow([fmt.get(col, lambda _r: "")(r) for col in CSV_COLUMNS])
    return path


def write_scoreboard_picks(today, scored, cards, pool_by_url, note=None):
    """记分板 picks 存档(每周一): 当日推荐 + top50 快照 + 全池双口径基线(订阅 + 播放总量)。
    engine_version 版本戳让每期预测可追溯是哪版引擎下的注(对账时能分辨算法迭代的影响)。
    全池基线让文件自足: 到期结算时不依赖任何外部历史。幂等，同日已存在则跳过。"""
    os.makedirs(SCOREBOARD, exist_ok=True)
    path = os.path.join(SCOREBOARD, f"picks-{today}.json")
    if os.path.exists(path):
        return path
    s_by = {s["channel_url"]: s for s in scored}

    def _views(url):  # 频道累计播放总量(池子里有才有; 缺则 None, 播放口径该行结算时降级)
        p = pool_by_url.get(url, {})
        return p.get("channel_view_count") or p.get("view_count")

    picks = []
    for c in cards:
        url = c.get("_channel_url")
        s = s_by.get(url, {})
        picks.append({
            "channel_url": url,
            "channel_name": s.get("channel_name") or c.get("channel_name"),
            "rank_at_pick": s.get("rank", c.get("_rank")),
            "score_at_pick": s.get("score"),
            "subscribers_baseline": s.get("subscribers"),
            "channel_view_count_baseline": _views(url),
        })
    doc = {
        "picked_date": today,
        "engine_version": ENGINE_VERSION,
        "note": note,
        "picks": picks,
        "top50": [{"rank": s["rank"], "channel_url": s["channel_url"], "channel_name": s["channel_name"],
                   "score": s["score"], "subscribers": s.get("subscribers")} for s in scored[:50]],
        "pool_size": len(scored),
        "pool_subscribers_baseline": {s["channel_url"]: s.get("subscribers") for s in scored},
        # 第二口径基线: 全池累计播放总量(池里有此字段才填, 用于播放速度对账)。
        "pool_views_baseline": {s["channel_url"]: _views(s["channel_url"])
                                for s in scored if _views(s["channel_url"]) is not None},
    }
    with open(path, "w") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    return path


def _pool_median_growth(baseline_map, now_map):
    """全池增速中位: 对 {url: 基线值} 与 {url: 现值}, 逐 url 算 (现-基)/基*100, 取中位。
    基线或现值缺/为 0 的行不计入。样本不足返回 (None, 0)。"""
    growths = []
    for url, b in (baseline_map or {}).items():
        n = now_map.get(url)
        if b and n:
            growths.append((n - b) / b * 100)
    if not growths:
        return None, 0
    return sorted(growths)[len(growths) // 2], len(growths)


def _verdict(g, median, margin):
    """增速 g 相对全池中位给判词(跑赢/跑平/跑输), median=None(样本不足)时返回'基线缺失'。"""
    if median is None:
        return "基线缺失"
    return "跑赢" if g > median + margin else ("跑输" if g < median - margin else "跑平")


def check_scoreboard(today, scored, cfg):
    """结算到期 picks: 双口径对账(订阅增速 + 播放速度)。每个 pick 的增长各自对照全池同口径中位
    给 verdict，写 verdicts 文件(结算一次不重复)。
    第一口径=订阅增速(向后兼容, 字段名不变: growth_pct/verdict/subscribers_*)。
    第二口径=播放速度(累计播放总量增速, 池里有 channel_view_count 基线的行才算): views_growth_pct/views_verdict。
    播放口径基线全缺(当前池未采集播放总量)时该口径整体优雅降级(picks 不带 views 字段, 报告不显示该列)。"""
    sb = cfg.get("scoreboard", {})
    due_days = sb.get("due_days", 28)
    margin = sb.get("verdict_margin_pct", 2.0)
    now_subs = {s["channel_url"]: s.get("subscribers") for s in scored}
    # 现值播放总量(池里有才有; 打分产物 scored 未必带, 故也从池子取, 缺则该口径降级)
    now_views = {s["channel_url"]: (s.get("channel_view_count") or s.get("view_count")) for s in scored}
    results = []
    for pf in sorted(glob.glob(os.path.join(SCOREBOARD, "picks-*.json"))):
        pdate = os.path.basename(pf)[len("picks-"):-len(".json")]
        try:
            age = (date.fromisoformat(today) - date.fromisoformat(pdate)).days
        except ValueError:
            continue
        vf = os.path.join(SCOREBOARD, f"verdicts-for-{pdate}.json")
        if age < due_days or os.path.exists(vf):
            continue
        doc = json.load(open(pf))

        sub_median, _ = _pool_median_growth(doc.get("pool_subscribers_baseline"), now_subs)
        views_base = doc.get("pool_views_baseline") or {}
        views_median, views_n = _pool_median_growth(views_base, now_views)
        has_views = views_n > 0            # 播放口径是否有足够基线可结算

        verdicts = []
        for p in doc.get("picks", []):
            url = p.get("channel_url")
            b, n = p.get("subscribers_baseline"), now_subs.get(url)
            row = {"channel_name": p.get("channel_name"), "channel_url": url}
            if not b or not n:
                row["verdict"] = "数据缺失"
            else:
                g = (n - b) / b * 100
                row.update({"subscribers_baseline": b, "subscribers_now": n,
                            "growth_pct": round(g, 2), "verdict": _verdict(g, sub_median, margin)})
            # 第二口径: 播放速度(有基线 + 有现值才算; 否则该行不带 views 字段)
            vb, vn = p.get("channel_view_count_baseline"), now_views.get(url)
            if has_views and vb and vn:
                vg = (vn - vb) / vb * 100
                row.update({"views_baseline": vb, "views_now": vn,
                            "views_growth_pct": round(vg, 2),
                            "views_verdict": _verdict(vg, views_median, margin)})
            verdicts.append(row)
        out = {"picked_date": pdate, "settled_date": today, "window_days": age,
               "engine_version": doc.get("engine_version"),
               "pool_median_growth_pct": round(sub_median, 2) if sub_median is not None else None,
               "pool_median_views_growth_pct": round(views_median, 2) if (has_views and views_median is not None) else None,
               "verdicts": verdicts}
        with open(vf, "w") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        results.append(out)
    return results


def commit_snapshots(today):
    """数据快照自动入库+push(私有仓库当异地备份)。只提交数据路径，push 失败只记结果不中断。
    data/dossiers 进库(会进公开仓库+飞书文档); data/comments 绝不加(第三方用户内容，gitignore 守着)。"""
    paths = ["data/history", "data/pool", "data/scoreboard", "data/dossiers"]
    msg = f"数据快照 {today}\n\nCo-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
    try:
        subprocess.run(["git", "add"] + paths, cwd=ROOT, capture_output=True, text=True)
        c = subprocess.run(["git", "commit", "-m", msg, "--"] + paths, cwd=ROOT, capture_output=True, text=True)
        if c.returncode != 0:
            return f"commit skipped: {(c.stdout + c.stderr).strip().splitlines()[-1][:100]}"
        p = subprocess.run(["git", "push"], cwd=ROOT, capture_output=True, text=True, timeout=120)
        return "committed+pushed" if p.returncode == 0 else f"committed, push failed: {p.stderr.strip()[:100]}"
    except Exception as e:
        return f"git error: {e}"


def _scoreboard_oneliner(scoreboard_results):
    """对账一句话摘要(给日报开头三句话第③行用)。"""
    if not scoreboard_results:
        return "对账页：本次无到期预测"
    parts = []
    for r in scoreboard_results:
        won = sum(1 for v in r["verdicts"] if v.get("verdict") == "跑赢")
        parts.append(f"{r['picked_date']} 那期结算：{won}/{len(r['verdicts'])} 跑赢全池中位")
    return "对账页：" + "；".join(parts)


def _blocked_reason_dist(blocked):
    """拦截原因分布(给三行结论第②行): 按拦截原因标签的类别名聚合成「僵尸号 3 · 已合作 2」这种一句话。"""
    from collections import Counter
    cats = Counter()
    for b in blocked or []:
        r = b.get("reason") or ""
        cat = r.split("(")[0] if r else "其他"
        cats[cat] += 1
    label = {"already_partner": "已合作", "competitor": "竞品", "brand_or_vendor": "品牌/厂商",
             "reposter": "搬运号", "marketing_or_finance": "营销/理财/玄学", "dead_account": "僵尸/空号"}
    return " · ".join(f"{label.get(k, k)} {v}" for k, v in cats.most_common())


def write_report(today, collect_res, scored, newcomers, jumpers, cards, pool_by_url, first_run, csv_path=None,
                 scoreboard_results=None, dossier_links=None, blocked=None, ai_review_stats=None):
    """日报 markdown(三词化 · 结论先行)。开头三行结论(建议联系谁/拦下谁/对账状态)，
    推荐卡每张一个小节(三词分数表 + 证据 + 值得签)，「窜升榜/排名 diff」已退役(起势层接班)。
    dossier_links: {channel_id: 档案飞书链接}(互链，拿不到就温和省略; 上传前 push 流程还会按最新映射刷新一遍)。"""
    import radar_lib
    n = len(scored)
    delta = ""
    if collect_res.get("pool_before") is not None:
        d = collect_res["pool_after"] - collect_res["pool_before"]
        delta = f"+{d}" if d else "+0"

    L = [f"# 达人雷达日报 · {today}", ""]

    # === 三行结论先行(①建议联系 N 个 ②拦下 M 个 ③对账状态) ===
    names = [c.get("channel_name", "?") for c in cards][:8]
    names_str = "、".join(names) + ("…" if len(cards) > 8 else "") if names else "（本次无）"
    line2 = (f"拦下 **{len(blocked)}** 个：{_blocked_reason_dist(blocked)}"
             if blocked else "拦下 **0** 个（扫描区内无 🔴）")
    if scoreboard_results:
        line3 = _scoreboard_oneliner(scoreboard_results)
    else:
        line3 = "对账页：本次无到期预测（结算中）"
    L += ["> 📡 **今日三句话**  ",
          f"> ① 今天建议联系 **{len(cards)}** 个：{names_str}  ",
          f"> ② {line2}  ",
          f"> ③ {line3}",
          "", "---", ""]

    L += ["## 今日池子", "",
          f"- 池子规模：**{n}** 频道（{delta}）",
          f"- 本次刷新：{collect_res.get('refreshed', 0)} 个频道元数据",
          f"- 新发现入池：{collect_res.get('discovered', 0)} 个"]
    if collect_res.get("discovered_names"):
        L.append(f"  - {', '.join(collect_res['discovered_names'][:15])}")
    # 起势层覆盖披露(W5): 有真实起势证据(RSS 拉到播放数、momentum_cov=ok)的频道数 / 全池。
    # RSS 预算已提至全池, 此行如实报覆盖率, 拿不到证据的频道走中性分(不冤杀), 覆盖率随每日快照爬升。
    m_ok = sum(1 for s in scored if s.get("momentum_cov") == "ok")
    cov_pct = f"{m_ok / n * 100:.0f}%" if n else "n/a"
    L.append(f"- 起势数据覆盖：**{m_ok}/{n}**（{cov_pct}）；未覆盖频道走中性分不惩罚，覆盖率随每日快照爬升")
    L += ["", "---", ""]

    # 「窜升榜 / 排名 diff」已正式退役: 起势层(在涨分)已接班「谁正在被越来越多陌生人看到」。

    # 今日拦截(红绿灯 🔴): 列名字+原因, 不只个数, 让运营一眼复核。绝不物理删数据, 仅不进推荐卡。
    L += ["## 今日拦截", ""]
    if blocked:
        L.append(f"推荐卡扫描区内亮 🔴 红灯自动拦截 **{len(blocked)}** 个，不进推荐卡"
                 f"（仍保留在全池榜单表 + 拦截原因列，人工终审自行决定去留）：")
        L.append("")
        L += ["| 频道 | 排名 | 身份标签 | 拦截原因 |", "|---|---:|---|---|"]
        for b in blocked:
            L.append(f"| **{b['name']}** | #{b['rank']} | {b['flags']} | {b['reason']} |")
        L.append("")
    else:
        L += ["_扫描区内本次无 🔴 拦截。_", ""]
    # AI 复核层退回(规则灯之上的语义过滤; 只降级不升级, 🟢→🟡 + 🤖AI复核 标)。
    ar = ai_review_stats or {}
    if ar.get("enabled"):
        dg = ar.get("downgraded_green", 0)
        fy = ar.get("flagged_yellow", 0)
        rv = ar.get("reviewed", 0)
        L.append("**AI 复核退回 %d 个**（复核 %d 个候选：🟢→🟡 降级 %d，🟡 加疑点标 %d）"
                 "——规则灯之上的语义过滤，把疑似搬运/合集、机构带货、内容离题的候选退出可直接联系队列，"
                 "只降级不升级、不物理删除，仍留在全池榜单表供人工终审。" % (dg, rv, dg, fy))
        L.append("")
    L += ["---", ""]

    L += ["## 推荐卡", ""]
    if cards:
        s_by_url = {s["channel_url"]: s for s in scored}
        for c in cards:
            s = s_by_url.get(c.get("_channel_url"), {})
            grade = s.get("action_grade", "")
            fl = identity_filter.flags_zh(s.get("identity_flags"))
            L.append(f"### {grade} {c.get('channel_name', '?')}  ·  #{c.get('_rank', '?')}")
            L.append("")
            if fl:
                L.append(f"身份标签：{fl}")
                L.append("")
            # 互链: 该达人的飞书档案(channel_id 取自频道链接末段; 映射里没有就省略)
            cid = (c.get("_channel_url") or "").rstrip("/").rsplit("/", 1)[-1]
            dl = (dossier_links or {}).get(cid)
            if dl:
                L += [f"[🗂️ 达人档案]({dl})", ""]
            # 三词分数表(运营视图): 对路分 × 在涨分 = 潜力分 + 红绿灯。工程证据(语义/甜点/起势/浪/破圈)见档案。
            L += ["| 排名 | 订阅 | 对路分 | 在涨分 | 潜力分 | 红绿灯 |",
                  "|---:|---:|---:|---:|---:|:--:|",
                  f"| #{s.get('rank', c.get('_rank', '?'))} | {s.get('subscribers', '?')} | "
                  f"{s.get('score', '?')} | {s.get('rising', '?')} | {s.get('potential', '?')} | {grade or '?'} |", ""]
            # 在涨的三条人话证据(有才列)
            ev_lines = radar_lib.rising_evidence_lines(s)
            if ev_lines:
                L.append("在涨证据：")
                for e in ev_lines:
                    L.append(f"- 📈 {e}")
                L.append("")
            for r in (c.get("why_worth_signing") or []):
                L.append(f"- ✅ 值得签：{r}")
            if c.get("risk"):
                L.append(f"- ⚠️ 风险：{c['risk']}")
            if c.get("first_collab"):
                L.append(f"- 🤝 首次合作：{c['first_collab']}")
            L.append("")
    else:
        L += ["_本次未生成推荐卡（无符合条件的候选，或模型不可用）。_", ""]
    L += ["---", ""]

    L += ["## 对账页", ""]
    if scoreboard_results:
        for r in scoreboard_results:
            ev = f" · 引擎 {r['engine_version']}" if r.get("engine_version") else ""
            has_views = any("views_growth_pct" in v for v in r["verdicts"])
            vhdr = (f"，播放中位 {r['pool_median_views_growth_pct']}%" if has_views and r.get("pool_median_views_growth_pct") is not None else "")
            L.append(f"**{r['picked_date']} 那期预测到期**（{r['window_days']} 天窗口，"
                     f"全池订阅中位增速 {r['pool_median_growth_pct']}%{vhdr}{ev}）")
            L.append("")
            if has_views:
                # 双口径: 订阅增速 + 播放速度并列
                L += ["| 频道 | 订阅(下注时→现在) | 订阅增长 | 播放增长 | 结论(订阅/播放) |",
                      "|---|---|---:|---:|---|"]
                for v in r["verdicts"]:
                    if v.get("verdict") == "数据缺失":
                        L.append(f"| {v.get('channel_name', '?')} | 数据缺失 | - | - | 数据缺失 |")
                    else:
                        vg = f"{v['views_growth_pct']:+.2f}%" if "views_growth_pct" in v else "—"
                        vv = v.get("views_verdict", "—")
                        L.append(f"| **{v['channel_name']}** | {v['subscribers_baseline']} → {v['subscribers_now']} | "
                                 f"{v['growth_pct']:+.2f}% | {vg} | {v['verdict']} / {vv} |")
            else:
                # 播放口径无基线(当前池未采集播放总量), 只出订阅口径
                L += ["| 频道 | 订阅(下注时→现在) | 增长 | 结论 |", "|---|---|---:|---|"]
                for v in r["verdicts"]:
                    if v.get("verdict") == "数据缺失":
                        L.append(f"| {v.get('channel_name', '?')} | 数据缺失 | - | 数据缺失 |")
                    else:
                        L.append(f"| **{v['channel_name']}** | {v['subscribers_baseline']} → {v['subscribers_now']} | "
                                 f"{v['growth_pct']:+.2f}% | {v['verdict']} |")
            L.append("")
    else:
        L += ["对账页：无到期预测", ""]
    L += ["---", ""]

    L += ["## 运行统计", "",
          f"- 时间：{datetime.now().isoformat(timespec='seconds')}",
          f"- 池子规模：{len(scored)} 频道",
          f"- 推荐卡：{len(cards)} 张"]
    if csv_path:
        L.append(f"- 当日推荐表格：data/runs/daily/{today}/cards_table.csv")
    L += ["", "_本日报每天 08:30 自动生成并同步到飞书总部（达人雷达 / 日报）。_"]

    os.makedirs(REPORTS, exist_ok=True)
    path = os.path.join(REPORTS, f"{today}-radar.md")
    with open(path, "w") as f:
        f.write("\n".join(L) + "\n")
    return path


def _feishu_call(method, path, token=None, body=None):
    """飞书开放平台最小调用(标准库 urllib)。失败返回 {code:-1,...} 不抛。"""
    import urllib.request, urllib.error
    url = "https://open.feishu.cn/open-apis" + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"code": -1, "_http": e.code, "msg": e.read().decode(errors="replace")[:200]}
    except Exception as e:
        return {"code": -1, "msg": str(e)}


def _date_ms(iso_day):
    """日期字符串 -> 当地正午毫秒时间戳(取正午避免时区导致的跨日显示)。"""
    return int(datetime.fromisoformat(iso_day + "T12:00:00").timestamp() * 1000)


def push_bitable(cfg, rows):
    """当日推荐行 append 进飞书多维表格(与 CSV 同构)。状态列永远不写=运营的地盘。
    敏感凭证(app_id/secret/app_token/table_id)全在 repo 外的 credentials_path 文件里，repo 转公开安全。
    任何失败只返回错误串，不中断主链。"""
    cred_path = os.path.expanduser(cfg.get("bitable", {}).get("credentials_path", ""))
    if not cred_path or not os.path.exists(cred_path):
        return f"NotConfigured: 凭证文件不存在 {cred_path or '(未配置路径)'}"
    try:
        cred = json.load(open(cred_path))
        t = _feishu_call("POST", "/auth/v3/tenant_access_token/internal",
                         body={"app_id": cred["app_id"], "app_secret": cred["app_secret"]})
        tok = t.get("tenant_access_token")
        if not tok:
            return f"token failed: code={t.get('code')} {str(t.get('msg'))[:80]}"
        records = []
        for r in rows:
            f = {
                "日期": _date_ms(r["日期"]),
                "频道名": r["频道名"],
                "频道链接": {"link": r["频道链接"], "text": r["频道链接"]},
                schema.COL_LIGHT: r.get(schema.COL_LIGHT, ""),        # 红绿灯(cards 表保持文本列, 只写值; 🏢 照常写)
                "身份标签": r.get("身份标签", ""),
                "排名百分位": f"{r['排名百分位']}%" if r["排名百分位"] is not None else "",
                # W17: cards 表有历史 → 旧「命中主题」英文文本列**停写**(不删, 保历史); 改写新「主题标签」中文多选。
                schema.COL_THEME_TAGS: schema.theme_tags_zh(r.get("命中主题") or []),
                "是否新发现": "是" if r["是否新发现"] else "否",
                "值得签1": r["值得签1"], "值得签2": r["值得签2"], "值得签3": r["值得签3"],
                "风险": r["风险"], "首次合作建议": r["首次合作建议"],
            }
            # 订阅(双格式文本, W17): 有订阅数才写。
            if r.get(schema.COL_SUBS_TEXT):
                f[schema.COL_SUBS_TEXT] = r[schema.COL_SUBS_TEXT]
            # 数值列(新名: 对路分/在涨分/潜力分 + 证据列 起势/浪/破圈)。None 不写(留空胜过写 0 误导)。
            for k in ("排名", "订阅数", schema.COL_FIT, schema.COL_RISING, schema.COL_POTENTIAL,
                      "语义分", "甜点分", "POV标记分", "起势分", "浪层分", "破圈比"):
                if r.get(k) is not None:
                    f[k] = r[k]
            if r["排名变动"] is not None:
                f["排名变动"] = _fmt_delta(r["排名变动"])
            records.append({"fields": f})
        if not records:
            return "no rows to append"
        res = _feishu_call("POST",
                           f"/bitable/v1/apps/{cred['app_token']}/tables/{cred['table_id']}/records/batch_create",
                           token=tok, body={"records": records})
        if res.get("code") != 0:
            return f"batch_create failed: code={res.get('code')} {str(res.get('msg'))[:80]}"
        # W21 门面富化(失败安全, 绝不挡主链): 给刚追加的新卡回填 频道头像 + 完整档案关联。
        # batch_create 按序返回 record_id, 与 rows 对齐; 开关 cfg.bitable.avatar_enrich(默认开)。
        enrich_msg = _enrich_new_cards(cfg, rows, res)
        return f"appended {len(records)} rows{enrich_msg}"
    except Exception as e:
        return f"error: {e}"


def _enrich_new_cards(cfg, rows, batch_res):
    """W21: 给 push_bitable 刚追加的新卡富化头像+关联。整块 try 包住, 任何失败只返回提示串, 绝不抛。
    默认开(cfg.bitable.avatar_enrich != False); 头像与关联各自失败安全(见 cards_enrich.enrich_records)。"""
    try:
        if cfg.get("bitable", {}).get("avatar_enrich") is False:
            return " (enrich off)"
        created = (batch_res.get("data") or {}).get("records") or []
        targets = []
        for row, rec in zip(rows, created):
            rid = rec.get("record_id")
            if rid:
                targets.append({"record_id": rid, "name": row.get("频道名", ""),
                                "url": row.get("频道链接", "")})
        if not targets:
            return ""
        import cards_enrich
        r = cards_enrich.enrich_records(cfg, targets)
        if r.get("ok"):
            return f" (enrich: 头像 {r.get('avatar_ok')}/{r.get('avatar_ok', 0) + r.get('avatar_fail', 0)}, 关联 {r.get('link_ok')})"
        return f" (enrich skipped: {str(r.get('error'))[:50]})"
    except Exception as e:
        return f" (enrich err: {str(e)[:50]})"


def push_feishu_docs(cfg, report_path, stats=None):
    """飞书文档出口(原生 docx，链接永久): 日报先建/更(拿当日永久链接) -> 主页(直链当日日报) ->
    档案(导航头直链主页+当日日报) -> 日报补新频道档案链接后原地重写一遍 -> 回填表格「档案」列。
    原地更新不换 URL，顺序只决定"当天新建 id 何时可被引用"，不再有轮换死链问题。
    温和降级: 整块 try 包住，任何失败只返回错误串不中断主链(照 push_bitable 惯例)。"""
    try:
        import feishu_docs
        r_daily = feishu_docs.push_daily_report(cfg, report_path=report_path)
        r_home = feishu_docs.push_homepage(cfg, dict(stats or {}))
        r_dos = feishu_docs.push_dossiers(cfg)
        # 当天首次出现的新频道: 其档案 id 上一步才建出来，补进日报后原地重写(同 id，URL 不变)
        patched = feishu_docs.patch_report_dossier_links(report_path, cfg=cfg)
        r_daily2 = feishu_docs.push_daily_report(cfg, report_path=report_path)
        r_bf = feishu_docs.backfill_bitable_dossier_links(cfg)
        daily_ok = r_daily.get("ok") and r_daily2.get("ok")
        return (f"daily={'ok' if daily_ok else (r_daily.get('error') or r_daily2.get('error') or 'err')}"
                f" home={'ok' if r_home.get('ok') else 'err:' + str(r_home.get('error') or r_home.get('url_or_err'))[:60]}"
                f" dossiers={r_dos.get('counts', r_dos.get('error'))}"
                f" report_links={patched}"
                f" bitable_dossier_col={r_bf.get('updated', r_bf.get('error'))}")
    except Exception as e:
        return f"error: {e}"


def sync_full_ranking_table(cfg, scored, pool_by_url, dry_run=False):
    """全池榜单同步(飞书第二张表): 当日全池排序整表重写。复用 sync_full_ranking 模块。
    dry_run: 这是绕过 cfg.outputs 的独立真实推送路径(直读 bitable 凭证)，--dry-run 下必须跳过, 否则试跑仍会真灌表。
    温和降级: 模块导入或调用失败只返回错误 dict, 不中断主链。"""
    if dry_run:
        return {"ok": False, "skipped": "dry-run"}
    try:
        import sync_full_ranking as sfr
        dossier_links = sfr._load_dossier_links(cfg)
        records = sfr.build_full_ranking_records(scored, pool_by_url, dossier_links)
        return sfr.sync_ranking_table(cfg, records, "full_ranking_table_id",
                                      sfr.FULL_RANKING_TABLE_NAME, sfr.FULL_RANKING_FIELDS)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def run_bili_line(bili_cfg_path, today, budget=None, dry_run=False, main_cfg=None):
    """B站(国内侧)线: 小预算刷新采集 -> 打分排序 -> B站榜单(飞书第三张表) -> 返回摘要供日报。
    与 YouTube 线串行(主跑完再跑)。B站采集器 CLI = collect_bilibili.py --config <bili配置> [--reset]。
    dry_run: 采集(只读公开元数据)与打分照常跑，只跳过第 3 步真实灌表(同 sync_full_ranking_table 的理由)。
    温和降级: 任何阶段失败只把错误塞进返回 dict, 绝不中断主链。返回:
      {ok, pool_size, top5:[{rank,name,subs,score}...], table:<sync结果>, error?}"""
    res = {"ok": False, "pool_size": 0, "top5": [], "table": None}
    try:
        bcfg = load_config(bili_cfg_path)
        bili_pool = os.path.join(ROOT, bcfg.get("pool_path", "data/pool/bilibili_pool.jsonl"))
        # 1) 采集(小预算刷新+发现). collect_bilibili 自带断点续跑与预算封顶, 失败不致命继续用现有池。
        cmd = ["python3", os.path.join(HERE, "collect_bilibili.py"), "--config", bili_cfg_path]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=(budget or 90) * 60 + 120)
            sys.stderr.write(p.stderr[-2000:] if p.stderr else "")
            res["collect_rc"] = p.returncode
        except Exception as e:
            res["collect_err"] = str(e)
        # 2) 打分排序(用 B站 config, 吃 bilibili 池)
        if not os.path.exists(bili_pool):
            res["error"] = f"bilibili pool 不存在: {bili_pool}"
            return res
        brows = load_pool(bili_pool)
        res["pool_size"] = len(brows)
        bscored = score_pool(brows, bcfg)
        # themes_hit 富化(日报/表格命中主题列用)
        tkw = theme_keywords(bcfg)
        bpool_by_url = {r["channel_url"]: r for r in brows}
        for s in bscored:
            s["themes_hit"] = themes_hit(bpool_by_url.get(s["channel_url"], {}), tkw)
        # 身份过滤器: B站榜单也贴行动分级。identity 词典是双语共享的, bili config 没有 identity 节时
        # 借用主 config 的(main_cfg 由调用方传入)。词典中文词专治 B站那批错误(理财/玄学/带货/搬运)。
        if "identity" not in bcfg and main_cfg and "identity" in main_cfg:
            bcfg = dict(bcfg)
            bcfg["identity"] = main_cfg["identity"]
            # leak 泄漏词用 bili 自己的(含 一镜到底/全景相机x5 等 B站措辞), 已在 bcfg 里, 不覆盖。
        identity_filter.annotate_scored(bscored, bpool_by_url, bcfg)
        # AI 复核层(B站线): 同 YT, 规则灯之上语义过滤只降级不升级(StoneFPV/大鹏 这类搬运/带货正是 B站坑)。
        # B站无上传日期→无🟢, 范围退化为 fit topN; ai_review 配置借用主 config(bcfg 已并入 main identity, ai_review 走同一 cfg 节)。
        ai_cfg = bcfg if bcfg.get("ai_review") else (main_cfg or bcfg)
        res["ai_review"] = ai_review.review_scored(bscored, bpool_by_url, ai_cfg, today=today,
                                                   line_label="/BILI", progress=dry_run)
        print("AI 复核(B站):", json.dumps(res["ai_review"], ensure_ascii=False))
        res["top5"] = [{"rank": s["rank"], "name": s["channel_name"],
                        "subs": s.get("subscribers"), "score": s["score"]} for s in bscored[:5]]
        # 落一份 B站排序产物到 runs(便于复现与排查)
        bout = os.path.join(ROOT, "data", "runs", f"{today}-bilibili")
        os.makedirs(bout, exist_ok=True)
        with open(os.path.join(bout, "ranked.json"), "w") as f:
            json.dump(bscored, f, ensure_ascii=False, indent=1)
        # 3) B站榜单(飞书第三张表, 列同全池榜单去掉档案列)。dry-run 下跳过, 只在此处判断(采集/打分照跑)。
        if dry_run:
            res["table"] = {"ok": False, "skipped": "dry-run"}
        else:
            try:
                import sync_full_ranking as sfr
                brecords = sfr.build_full_ranking_records(bscored, bpool_by_url, {}, top_dossier_n=0)
                for rec in brecords:
                    rec["fields"].pop("档案", None)
                res["table"] = sfr.sync_ranking_table(bcfg, brecords, "bili_ranking_table_id",
                                                      sfr.BILI_RANKING_TABLE_NAME, sfr.BILI_RANKING_FIELDS)
            except Exception as e:
                res["table"] = {"ok": False, "error": str(e)}
        res["ok"] = True
        return res
    except Exception as e:
        res["error"] = str(e)
        return res


def append_bili_section(report_path, bili_res):
    """把「B站雷达」一节追加到日报 markdown 末尾(top5 + 池子数)。温和降级: 失败静默。"""
    try:
        L = ["", "---", "", "## B站雷达", ""]
        if not bili_res or not bili_res.get("ok"):
            L += [f"_B站线本次未产出（{(bili_res or {}).get('error', '未运行')}）。_", ""]
        else:
            L += [f"- B站池子：**{bili_res.get('pool_size', 0)}** UP 主", ""]
            top5 = bili_res.get("top5") or []
            if top5:
                L += ["| 排名 | UP 主 | 粉丝 | 对路分 |", "|---:|---|---:|---:|"]
                for t in top5:
                    L.append(f"| #{t['rank']} | **{t['name']}** | {t.get('subs')} | {t['score']:.4f} |")
            tb = bili_res.get("table") or {}
            L += ["", f"_B站榜单已同步飞书（写入 {tb.get('written', '?')} 行）。_" if tb.get("ok")
                  else "_B站榜单飞书同步未完成。_", ""]
        with open(report_path, "a") as f:
            f.write("\n".join(L) + "\n")
    except Exception:
        pass


def push(cfg, msg, report_path, bitable_rows=None, stats=None):
    """按 config.outputs 分发。report 已写盘; imessage 调 notify 脚本; bitable 灌飞书多维表格; feishu_docs 传日报+档案+主页上飞书云空间。"""
    results = {}
    for out in cfg.get("outputs", []):
        if out == "report":
            results["report"] = report_path
        elif out == "imessage":
            if os.path.exists(NOTIFY):
                p = subprocess.run([NOTIFY, msg], capture_output=True, text=True)
                results["imessage"] = "sent" if p.returncode == 0 else f"failed: {p.stderr.strip()[:120]}"
            else:
                results["imessage"] = "skipped: notify-imessage.sh not found"
        elif out == "bitable":
            results["bitable"] = push_bitable(cfg, bitable_rows or [])
        elif out == "feishu_docs":
            results["feishu_docs"] = push_feishu_docs(cfg, report_path, stats=stats)
    return results


ALL_OUTPUTS = ["report", "imessage", "bitable", "feishu_docs"]


def format_push_summary(push_res, active_outputs):
    """把 push() 的结果整理成逐行大写标注，一眼分清「空转/真发/失败」。
    active_outputs 是本次实际跑的 cfg.outputs(dry-run 下只有 report)；
    不在其中的出口标 SKIPPED(dry-run 未执行)，让人知道不是凭证问题。"""
    lines = []
    for out in ALL_OUTPUTS:
        if out not in active_outputs:
            lines.append(f"PUSH {out}: SKIPPED(dry-run 未执行)")
            continue
        val = push_res.get(out)
        if out == "report":
            lines.append(f"PUSH report: WRITTEN(本地日报) -> {val}")
        elif val is None:
            lines.append(f"PUSH {out}: SKIPPED(未在 outputs 中)")
        elif out == "imessage":
            if val == "sent":
                lines.append("PUSH imessage: SENT(真实发送)")
            elif str(val).startswith("skipped"):
                lines.append(f"PUSH imessage: SKIPPED(无凭证/脚本缺失) {val}")
            else:
                lines.append(f"PUSH imessage: FAILED {val}")
        elif out == "bitable":
            sval = str(val)
            if sval.startswith("appended"):
                lines.append(f"PUSH bitable: SENT(真实灌表) {val}")
            elif sval.startswith("NotConfigured"):
                lines.append(f"PUSH bitable: SKIPPED(无凭证) {val}")
            else:
                lines.append(f"PUSH bitable: FAILED {val}")
        elif out == "feishu_docs":
            sval = str(val)
            if sval.startswith("error"):
                lines.append(f"PUSH feishu_docs: FAILED {val}")
            else:
                lines.append(f"PUSH feishu_docs: SENT(真实推送) {val}")
        else:
            lines.append(f"PUSH {out}: {val}")
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "config", "insta360.json"))
    ap.add_argument("--budget", type=int, help="覆盖 refresh 频道数(冒烟用小值)")
    ap.add_argument("--discover-terms", type=int, help="覆盖 discover 搜索词数")
    ap.add_argument("--top-n", type=int, help="覆盖推荐卡扫描的 top N(冒烟用小值)")
    ap.add_argument("--bili-config", default=os.path.join(ROOT, "config", "insta360_bilibili.json"),
                    help="B站线品牌配置(YouTube 主跑完串行跑 B站线)")
    ap.add_argument("--skip-bili", action="store_true", help="跳过 B站线(只跑 YouTube 主线)")
    ap.add_argument("--dry-run", action="store_true",
                    help="安全试跑: 强制 outputs=[report]，跳过 iMessage/飞书真实推送与数据快照 commit/push")
    args = ap.parse_args()

    if args.dry_run:
        print("\n" + "=" * 60)
        print("DRY RUN: 只产日报，不推送不提交")
        print("=" * 60 + "\n")

    cfg = load_config(args.config)
    if args.dry_run:
        cfg["outputs"] = ["report"]  # 强制只产日报，跳过 imessage/bitable/feishu_docs 全部真实出口
    # 池子路径由 config.pool_path 决定(缺省回退到默认 creator_pool.jsonl)。
    # 反射进模块全局 POOL, 让下游各处(gen_cards/write_scoreboard_picks)统一取到同一份。
    global POOL
    if cfg.get("pool_path"):
        POOL = os.path.join(ROOT, cfg["pool_path"])
    # --top-n 冒烟时缩小扫描面; max_cards 同步收到不超过 top_n(否则 backup 补足会超采)
    smoke_top_n = args.top_n
    smoke_max_cards = None
    if args.top_n is not None:
        smoke_max_cards = min(cfg["explain"]["max_cards"], args.top_n)
    today = date.today().isoformat()
    out_dir = os.path.join(DAILY, today)

    prev = prev_ranked(today)
    first_run = prev is None

    collect_res = run_collect(args.config, args.budget, args.discover_terms)

    rows = load_pool(POOL)
    pool_by_url = {r["channel_url"]: r for r in rows}
    tkw = theme_keywords(cfg)
    scored = score_pool(rows, cfg)
    for s in scored:
        s["themes_hit"] = themes_hit(pool_by_url.get(s["channel_url"], {}), tkw)

    newcomers, jumpers = diff_ranks(scored, prev)

    os.makedirs(out_dir, exist_ok=True)
    ranked_path = os.path.join(out_dir, "ranked.json")
    with open(ranked_path, "w") as f:
        json.dump(scored, f, ensure_ascii=False, indent=1)

    # 三信号采集(起势 momentum + 浪 trend + 破圈 breakout), 都是产品路径专用。
    # momentum: RSS 采集(只读, dry-run 保留) -> 打分。trend/breakout: 趋势采集(dry-run 掐外部三路留 pool_wave) -> 打分。
    # ranked.json 已落盘(collect_rss/浪层按 fit 排名决定拉取优先级)。任一信号缺失时融合优雅降级(不冤杀)。
    momentum_path = run_rss_momentum(args.config, POOL, ranked_path, today)
    trend_path, breakout_path = run_trends_layer(args.config, POOL, today, dry_run=args.dry_run)

    # 身份过滤器(产品路径): 给每个 scored 行贴 identity_flags / action_grade / grade_reasons。
    # 必须在 fuse_rising 之前(annotate 会重置 identity_flags, ⚠️ 徽章由随后的 fuse_rising 用 setdefault+append 追加, 不被覆盖)。
    # 铁律: 只在产品展示层跑, backtest 官方指标不经此。词典可看品牌 token(already_partner 需要)。
    identity_filter.annotate_scored(scored, pool_by_url, cfg)

    # 在涨分合成 + 单段式融合(2026-07-07 简化): 三信号合成一个「在涨分」rising, potential = fit × (1 + gain × rising)。
    # 替换原两段式(momentum fuse + trend fuse)。原三信号字段照算保留进证据列/档案。
    # trend_chaser + single_video_driven ⚠️ 徽章在此追加进 identity_flags(setdefault+append, 保留 annotate 已贴的身份标签, 只标不改分)。
    # 铁律: 只改产品路径展示/排序, fit 的 score/rank 永远不动; backtest 官方指标零引用。
    fuse_rising(scored, momentum_path, trend_path, breakout_path, cfg)

    # AI 复核层(2026-07-08 Max 拍板): 规则灯之上再加一层语义过滤, **只降级不升级**。
    # 必须在 annotate/fuse 之后(需要终态 action_grade), 在 ranked.json 重写之前(降级要被下游看到)。
    # 复核范围=fit topN ∪ 全部🟢, 受 max_candidates_per_line/budget_seconds 两道闸门限流(在线自限时)。
    # 就地把 🟢 疑似搬运/机构/离题的降 🟡 + 贴 🤖AI复核 标 + 落中文理由(走拦截原因列)。
    # 铁律: 只减不加、绝不物理删、already_partner 零接触; 整层失败优雅跳过不断链; --dry-run 照跑(本地 ollama 无网络副作用)。
    ai_review_stats = ai_review.review_scored(scored, pool_by_url, cfg, today=today,
                                              line_label="/YT", progress=args.dry_run)
    print("AI 复核(YT):", json.dumps(ai_review_stats, ensure_ascii=False))

    # 融合+身份标注+AI复核后重写 ranked.json(含 momentum/trend/breakout/rising/potential/action_grade, 供下游取用)
    with open(ranked_path, "w") as f:
        json.dump(scored, f, ensure_ascii=False, indent=1)

    cards = gen_cards(args.config, ranked_path, out_dir, top_n=smoke_top_n, max_cards=smoke_max_cards)

    # 🔴 拦截的频道不进推荐卡(Max 拍板: 系统预标注辅助人工终审, 推荐卡只放可外联/待核验)。
    # 绝不物理删除数据: 全池榜单表仍保留 🔴 行 + 拦截原因列。这里只是推荐卡出口的过滤。
    grade_by_url = {s["channel_url"]: s.get("action_grade") for s in scored}
    cards = [c for c in cards if grade_by_url.get(c.get("_channel_url")) != "🔴"]

    # 今日拦截清单(给日报「今日拦截 N 个」用): 取推荐卡扫描区(top_n_scan)内被判 🔴 的频道,
    # 列出名字 + 首条拦截原因, 让运营一眼复核。这是"本会拦掉哪些否则会被推荐的候选"的诚实口径。
    scan_n = smoke_top_n or cfg.get("explain", {}).get("top_n_scan", 100)
    blocked = [{"name": s.get("channel_name"), "rank": s.get("rank"),
                "flags": identity_filter.flags_zh(s.get("identity_flags")),
                "reason": (s.get("grade_reasons") or [""])[0]}
               for s in scored[:scan_n] if s.get("action_grade") == "🔴"]

    # explain 之后的三件富化: 跨平台重扫 -> 当日推荐卡频道档案 -> top50 评论采样。
    # 全部温和降级(失败只记 log 不中断主链)。评论采样的 top_n 跟随 --top-n 冒烟参数。
    cross_res = run_cross_platform(args.config) if cfg.get("cross_platform", {}).get("enabled", True) else {"skipped": True}
    dossier_res = run_dossiers(args.config) if cfg.get("dossiers", {}).get("enabled", True) else {"skipped": True}
    comments_res = run_comments(args.config, top_n=smoke_top_n)
    transcripts_res = run_transcripts(args.config, today, top_n=smoke_top_n)

    # 互链: 日报/表格里放该达人档案的飞书链接(映射读不到就温和省略; 上传前 push 流程还会按最新映射刷新)
    dossier_links = {}
    try:
        import feishu_docs
        _m = feishu_docs._load_map(cfg.get("feishu_docs", {}).get("map_path", feishu_docs.DEFAULT_MAP_PATH))
        dossier_links = {k[len("dossier/"):]: v.get("url") for k, v in _m.items()
                        if k.startswith("dossier/") and isinstance(v, dict) and v.get("url")}
    except Exception:
        pass

    table_rows = build_table_rows(today, cards, scored, pool_by_url, dossier_links=dossier_links)
    csv_path = write_cards_table(today, table_rows, out_dir)

    if date.today().weekday() == 0:  # 周一存本周 picks(可证伪预测)
        write_scoreboard_picks(today, scored, cards, pool_by_url)
    scoreboard_results = check_scoreboard(today, scored, cfg)

    report_path = write_report(today, collect_res, scored, newcomers, jumpers, cards, pool_by_url, first_run,
                               csv_path, scoreboard_results, dossier_links=dossier_links, blocked=blocked,
                               ai_review_stats=ai_review_stats)

    n = len(scored)
    dtxt = ""
    if collect_res.get("pool_before") is not None:
        dtxt = f"+{collect_res['pool_after'] - collect_res['pool_before']}"
    msg = (f"📡 达人雷达日报：池子{n}({dtxt})，今天建议联系 {len(cards)} 个 → reports/{today}-radar.md")
    # 主页仪表盘大数字随日报刷新的实时口径
    sb_status = ("首份 picks 已存档，2026-08-04 结算" if not scoreboard_results
                 else f"最近结算 {len(scoreboard_results)} 期")
    docs_stats = {"pool_size": n, "cards_today": len(cards), "blind_test_multiple": "4.6",
                  "scoreboard_status": sb_status}
    push_res = push(cfg, msg, report_path, bitable_rows=table_rows, stats=docs_stats)

    # 全池榜单同步(飞书第二张表): 每日跑完用当日全池排序整表重写(先清后写, 表 id 稳定)。
    # 这条路径直读 bitable 凭证, 不受 cfg.outputs 管控, dry-run 下单独传 dry_run 跳过, 否则试跑仍会真灌表。
    # 温和降级: 失败只记结果不中断主链(照 push_bitable 惯例)。
    full_rank_res = sync_full_ranking_table(cfg, scored, pool_by_url, dry_run=args.dry_run)

    # B站线: YouTube 主线跑完后串行跑(小预算刷新+打分+B站榜单+日报追加一节)。
    # 只在主 config 非 bilibili 时跑(避免自递归); --skip-bili 可关。温和降级不中断主链。
    # dry-run 下采集/打分照常跑(只读公开元数据), 只跳过榜单真实灌表(同上一条理由)。
    bili_res = None
    if not args.skip_bili and cfg.get("platform") != "bilibili" and os.path.exists(args.bili_config):
        bili_res = run_bili_line(args.bili_config, today, budget=args.budget, dry_run=args.dry_run, main_cfg=cfg)
        append_bili_section(report_path, bili_res)

    git_res = "skipped: dry-run" if args.dry_run else commit_snapshots(today)  # 数据快照入库(先存后洗的"存"落到异地)

    os.makedirs(LOGS, exist_ok=True)
    cross_txt = f"{cross_res.get('with_any', '?')}/{cross_res.get('total', '?')}" if "error" not in cross_res else "err"
    comm_txt = (f"{comments_res.get('channels_ok', '?')}ok/{comments_res.get('comments_written', '?')}c"
                if "error" not in comments_res else "err")
    trans_txt = (f"{transcripts_res.get('videos_ok', '?')}ok/{transcripts_res.get('videos_no_subs', '?')}nosubs"
                 if "error" not in transcripts_res else "err")
    if full_rank_res.get("ok"):
        full_rank_txt = f"{full_rank_res.get('written', '?')}/{full_rank_res.get('total', '?')}"
    elif full_rank_res.get("skipped"):
        full_rank_txt = f"skipped:{full_rank_res['skipped']}"
    else:
        full_rank_txt = f"err:{str(full_rank_res.get('error'))[:40]}"
    if bili_res is None:
        bili_txt = "skip"
    elif bili_res.get("ok"):
        _bt = bili_res.get("table") or {}
        _bt_txt = _bt.get("written", f"skipped:{_bt['skipped']}" if _bt.get("skipped") else "err")
        bili_txt = f"pool={bili_res.get('pool_size', 0)},table={_bt_txt}"
    else:
        bili_txt = f"err:{str(bili_res.get('error'))[:40]}"
    summary = (f"{datetime.now().isoformat(timespec='seconds')} pool={n} {dtxt or '+?'} "
               f"refreshed={collect_res.get('refreshed', 0)} discovered={collect_res.get('discovered', 0)} "
               f"newcomers={len(newcomers)} jumpers={len(jumpers)} cards={len(cards)} "
               f"cross={cross_txt} dossiers={dossier_res.get('generated', 'err')} comments={comm_txt} "
               f"transcripts={trans_txt} "
               f"settled={len(scoreboard_results)} push={push_res} full_rank={full_rank_txt} "
               f"bili={bili_txt} git={git_res}")
    with open(os.path.join(LOGS, "radar.log"), "a") as f:
        f.write(summary + "\n")

    print("\n=== RUN SUMMARY ===")
    print(summary)
    print("report:", report_path)
    print("\n--- PUSH RESULTS ---")
    for line in format_push_summary(push_res, cfg.get("outputs", [])):
        print(line)
    # 这两条不受 cfg.outputs 管控(独立直读 bitable 凭证的真实推送路径)，单独醒目标注。
    if full_rank_res.get("skipped"):
        print(f"PUSH full_rank_table: SKIPPED({full_rank_res['skipped']})")
    elif full_rank_res.get("ok"):
        print(f"PUSH full_rank_table: SENT(真实灌表) {full_rank_txt}")
    else:
        print(f"PUSH full_rank_table: FAILED {full_rank_txt}")
    if bili_res is None:
        print("PUSH bili_table: SKIPPED(B站线未跑)")
    else:
        _bt = bili_res.get("table") or {}
        if _bt.get("skipped"):
            print(f"PUSH bili_table: SKIPPED({_bt['skipped']})")
        elif _bt.get("ok"):
            print(f"PUSH bili_table: SENT(真实灌表) written={_bt.get('written', '?')}")
        else:
            print(f"PUSH bili_table: FAILED {_bt.get('error', 'unknown')}")
    if args.dry_run:
        print("PUSH git_commit_push: SKIPPED(dry-run)")
    elif git_res == "committed+pushed":
        print(f"PUSH git_commit_push: SENT(真实提交并推送) {git_res}")
    else:
        print(f"PUSH git_commit_push: FAILED/PARTIAL {git_res}")
    if args.dry_run:
        print("\n" + "=" * 60)
        print("DRY RUN 完成: 只产日报，不推送不提交")
        print("=" * 60)


if __name__ == "__main__":
    main()
