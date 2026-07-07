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
from radar_lib import load_config, load_pool, score_pool

POOL = os.path.join(ROOT, "data", "pool", "creator_pool.jsonl")
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


# 表格列序固定，也是将来 bitable 出口的 schema 母版
CSV_COLUMNS = ["日期", "排名", "频道名", "频道链接", "订阅数", "排名百分位", "总分", "语义分", "甜点分",
               "POV标记分", "命中主题", "是否新发现", "排名变动", "值得签1", "值得签2", "值得签3",
               "风险", "首次合作建议", "状态"]


def build_table_rows(today, cards, scored, pool_by_url):
    """推荐行的单一数据源(原始类型值)。CSV 与 bitable 两个出口都从这里取数，保证同构不漂移。"""
    s_by_url = {s["channel_url"]: s for s in scored}
    rows = []
    for c in cards:
        s = s_by_url.get(c.get("_channel_url"), {})
        p = pool_by_url.get(c.get("_channel_url"), {})
        why = (c.get("why_worth_signing") or []) + ["", "", ""]
        rows.append({
            "日期": today,
            "排名": s.get("rank", c.get("_rank")),
            "频道名": s.get("channel_name", c.get("channel_name", "")),
            "频道链接": c.get("_channel_url", ""),
            "订阅数": s.get("subscribers"),
            "排名百分位": s.get("pct"),
            "总分": s.get("score"), "语义分": s.get("sem"),
            "甜点分": s.get("sweet"), "POV标记分": s.get("pov"),
            "命中主题": s.get("themes_hit") or [],
            "是否新发现": p.get("source") == "auto-discover",
            "排名变动": s.get("rank_delta"),
            "值得签1": why[0], "值得签2": why[1], "值得签3": why[2],
            "风险": c.get("risk", ""), "首次合作建议": c.get("first_collab", ""),
        })
    return rows


def _fmt_delta(d):
    return (f"+{d}" if d > 0 else str(d)) if d is not None else ""


def write_cards_table(today, rows, out_dir):
    """推荐卡表格化: 一行一个推荐人。utf-8-sig 保 Numbers/Excel 中文不乱码，状态列留空给运营填。"""
    path = os.path.join(out_dir, "cards_table.csv")
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        for r in rows:
            w.writerow([
                r["日期"],
                r["排名"] if r["排名"] is not None else "",
                r["频道名"],
                r["频道链接"],
                r["订阅数"] if r["订阅数"] is not None else "",
                f"{r['排名百分位']}%" if r["排名百分位"] is not None else "",
                f"{r['总分']:.4f}" if r["总分"] is not None else "",
                f"{r['语义分']:.4f}" if r["语义分"] is not None else "",
                f"{r['甜点分']:.4f}" if r["甜点分"] is not None else "",
                f"{r['POV标记分']:.4f}" if r["POV标记分"] is not None else "",
                "、".join(r["命中主题"]),
                "是" if r["是否新发现"] else "否",
                _fmt_delta(r["排名变动"]),
                r["值得签1"], r["值得签2"], r["值得签3"],
                r["风险"], r["首次合作建议"],
                "",
            ])
    return path


def write_scoreboard_picks(today, scored, cards, pool_by_url, note=None):
    """记分板 picks 存档(每周一): 当日推荐 + top50 快照 + 全池订阅基线。
    全池基线让文件自足: 到期结算时不依赖任何外部历史。幂等，同日已存在则跳过。"""
    os.makedirs(SCOREBOARD, exist_ok=True)
    path = os.path.join(SCOREBOARD, f"picks-{today}.json")
    if os.path.exists(path):
        return path
    s_by = {s["channel_url"]: s for s in scored}
    picks = []
    for c in cards:
        s = s_by.get(c.get("_channel_url"), {})
        picks.append({
            "channel_url": c.get("_channel_url"),
            "channel_name": s.get("channel_name") or c.get("channel_name"),
            "rank_at_pick": s.get("rank", c.get("_rank")),
            "score_at_pick": s.get("score"),
            "subscribers_baseline": s.get("subscribers"),
            "channel_view_count_baseline": pool_by_url.get(c.get("_channel_url"), {}).get("channel_view_count"),
        })
    doc = {
        "picked_date": today,
        "note": note,
        "picks": picks,
        "top50": [{"rank": s["rank"], "channel_url": s["channel_url"], "channel_name": s["channel_name"],
                   "score": s["score"], "subscribers": s.get("subscribers")} for s in scored[:50]],
        "pool_size": len(scored),
        "pool_subscribers_baseline": {s["channel_url"]: s.get("subscribers") for s in scored},
    }
    with open(path, "w") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    return path


def check_scoreboard(today, scored, cfg):
    """结算到期 picks: 每个 pick 的订阅增长对照全池中位增速给 verdict，写 verdicts 文件(结算一次不重复)。"""
    sb = cfg.get("scoreboard", {})
    due_days = sb.get("due_days", 28)
    margin = sb.get("verdict_margin_pct", 2.0)
    now_subs = {s["channel_url"]: s.get("subscribers") for s in scored}
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
        growths = []
        for url, b in (doc.get("pool_subscribers_baseline") or {}).items():
            n = now_subs.get(url)
            if b and n:
                growths.append((n - b) / b * 100)
        median = sorted(growths)[len(growths) // 2] if growths else 0.0
        verdicts = []
        for p in doc.get("picks", []):
            b, n = p.get("subscribers_baseline"), now_subs.get(p.get("channel_url"))
            if not b or not n:
                verdicts.append({"channel_name": p.get("channel_name"), "channel_url": p.get("channel_url"),
                                 "verdict": "数据缺失"})
                continue
            g = (n - b) / b * 100
            v = "跑赢" if g > median + margin else ("跑输" if g < median - margin else "跑平")
            verdicts.append({"channel_name": p.get("channel_name"), "channel_url": p.get("channel_url"),
                             "subscribers_baseline": b, "subscribers_now": n,
                             "growth_pct": round(g, 2), "verdict": v})
        out = {"picked_date": pdate, "settled_date": today, "window_days": age,
               "pool_median_growth_pct": round(median, 2), "verdicts": verdicts}
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
    """记分板一句话摘要(给日报开头的 callout 用)。"""
    if not scoreboard_results:
        return "记分板：本次无到期预测"
    parts = []
    for r in scoreboard_results:
        won = sum(1 for v in r["verdicts"] if v.get("verdict") == "跑赢")
        parts.append(f"{r['picked_date']} 那期结算：{won}/{len(r['verdicts'])} 跑赢全池中位")
    return "记分板：" + "；".join(parts)


def write_report(today, collect_res, scored, newcomers, jumpers, cards, pool_by_url, first_run, csv_path=None,
                 scoreboard_results=None, dossier_links=None):
    """日报 markdown(设计升级版)。开头 callout 摘要(blockquote)，推荐卡每张一个小节(heading3+分数表格+bullet)，
    divider 分节。飞书 Drive 预览这份 .md 时 blockquote/表格/heading 都会渲染成样式块；纯文本降级也不丢内容。
    dossier_links: {channel_id: 档案飞书链接}(互链，拿不到就温和省略; 上传前 push 流程还会按最新映射刷新一遍)。"""
    n = len(scored)
    delta = ""
    if collect_res.get("pool_before") is not None:
        d = collect_res["pool_after"] - collect_res["pool_before"]
        delta = f"+{d}" if d else "+0"
    sb_line = _scoreboard_oneliner(scoreboard_results)

    L = [f"# 达人雷达日报 · {today}", ""]
    # 开头 callout 摘要卡(blockquote 在飞书预览里渲染成高亮引用块)
    L += ["> 📡 **今日摘要**  ",
          f"> 池子 **{n}** 频道（{delta}） · 本次刷新 **{collect_res.get('refreshed', 0)}** · 新发现入池 **{collect_res.get('discovered', 0)}**  ",
          f"> 推荐卡 **{len(cards)}** 张 · {'首次运行无排名对比' if first_run else f'新进前100 **{len(newcomers)}** 个'}  ",
          f"> {sb_line}",
          "", "---", ""]

    L += ["## 今日池子", "",
          f"- 池子规模：**{n}** 频道（{delta}）",
          f"- 本次刷新：{collect_res.get('refreshed', 0)} 个频道元数据",
          f"- 新发现入池：{collect_res.get('discovered', 0)} 个"]
    if collect_res.get("discovered_names"):
        L.append(f"  - {', '.join(collect_res['discovered_names'][:15])}")
    L += ["", "---", ""]

    if first_run:
        L += ["## 排名 diff", "", "_首次运行，无历史排名可对比。下次运行起产出新进/窜升榜。_", "", "---", ""]
    else:
        L += ["## 新进前 100", ""]
        if newcomers:
            L += ["| 排名 | 频道 | 订阅 | 总分 |", "|---|---|---:|---:|"]
            for s in newcomers[:20]:
                L.append(f"| #{s['rank']} | **{s['channel_name']}** | {s.get('subscribers')} | {s['score']} |")
        else:
            L.append("_本次无新面孔进入前 100。_")
        L += ["", "## 窜升榜（排名上升 ≥200 位）", ""]
        if jumpers:
            L += ["| 频道 | 上升 | 现排名 | 订阅 |", "|---|---:|---:|---:|"]
            for s in jumpers[:20]:
                L.append(f"| **{s['channel_name']}** | ↑{s['rank_delta']} | #{s['rank']} | {s.get('subscribers')} |")
        else:
            L.append("_本次无频道大幅窜升。_")
        L += ["", "---", ""]

    L += ["## 推荐卡", ""]
    if cards:
        s_by_url = {s["channel_url"]: s for s in scored}
        for c in cards:
            s = s_by_url.get(c.get("_channel_url"), {})
            L.append(f"### {c.get('channel_name', '?')}  ·  #{c.get('_rank', '?')}")
            L.append("")
            # 互链: 该达人的飞书档案(channel_id 取自频道链接末段; 映射里没有就省略)
            cid = (c.get("_channel_url") or "").rstrip("/").rsplit("/", 1)[-1]
            dl = (dossier_links or {}).get(cid)
            if dl:
                L += [f"[🗂️ 达人档案]({dl})", ""]
            # 分数表格(≤6 列，数据进表格不堆正文)
            L += ["| 排名 | 订阅 | 总分 | 语义 | 甜点 | POV标记 |", "|---:|---:|---:|---:|---:|---:|",
                  f"| #{s.get('rank', c.get('_rank', '?'))} | {s.get('subscribers', '?')} | "
                  f"{s.get('score', '?')} | {s.get('sem', '?')} | {s.get('sweet', '?')} | {s.get('pov', '?')} |", ""]
            for r in (c.get("why_worth_signing") or []):
                L.append(f"- ✅ 值得签：{r}")
            if c.get("risk"):
                L.append(f"- ⚠️ 风险：{c['risk']}")
            if c.get("first_collab"):
                L.append(f"- 🤝 首次合作：{c['first_collab']}")
            L.append("")
    else:
        L += ["_本次未生成推荐卡（无符合条件的新面孔/窜升候选，或模型不可用）。_", ""]
    L += ["---", ""]

    L += ["## 记分板", ""]
    if scoreboard_results:
        for r in scoreboard_results:
            L.append(f"**{r['picked_date']} 那期预测到期**（{r['window_days']} 天窗口，全池中位增速 {r['pool_median_growth_pct']}%）")
            L.append("")
            L += ["| 频道 | 订阅(下注时→现在) | 增长 | 结论 |", "|---|---|---:|---|"]
            for v in r["verdicts"]:
                if v["verdict"] == "数据缺失":
                    L.append(f"| {v.get('channel_name', '?')} | 数据缺失 | - | 数据缺失 |")
                else:
                    L.append(f"| **{v['channel_name']}** | {v['subscribers_baseline']} → {v['subscribers_now']} | "
                             f"{v['growth_pct']:+.2f}% | {v['verdict']} |")
            L.append("")
    else:
        L += ["记分板：无到期预测", ""]
    L += ["---", ""]

    L += ["## 运行统计", "",
          f"- 时间：{datetime.now().isoformat(timespec='seconds')}",
          f"- 推荐卡：{len(cards)} 张",
          f"- 新进前 100：{len(newcomers)} 个" if not first_run else "- 新进前 100：n/a（首次运行）"]
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
                "排名百分位": f"{r['排名百分位']}%" if r["排名百分位"] is not None else "",
                "命中主题": "、".join(r["命中主题"]),
                "是否新发现": "是" if r["是否新发现"] else "否",
                "值得签1": r["值得签1"], "值得签2": r["值得签2"], "值得签3": r["值得签3"],
                "风险": r["风险"], "首次合作建议": r["首次合作建议"],
            }
            for k in ("排名", "订阅数", "总分", "语义分", "甜点分", "POV标记分"):
                if r[k] is not None:
                    f[k] = r[k]
            if r["排名变动"] is not None:
                f["排名变动"] = _fmt_delta(r["排名变动"])
            records.append({"fields": f})
        if not records:
            return "no rows to append"
        res = _feishu_call("POST",
                           f"/bitable/v1/apps/{cred['app_token']}/tables/{cred['table_id']}/records/batch_create",
                           token=tok, body={"records": records})
        if res.get("code") == 0:
            return f"appended {len(records)} rows"
        return f"batch_create failed: code={res.get('code')} {str(res.get('msg'))[:80]}"
    except Exception as e:
        return f"error: {e}"


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


def run_bili_line(bili_cfg_path, today, budget=None, dry_run=False):
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
                L += ["| 排名 | UP 主 | 粉丝 | 总分 |", "|---:|---|---:|---:|"]
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

    cards = gen_cards(args.config, ranked_path, out_dir, top_n=smoke_top_n, max_cards=smoke_max_cards)

    # explain 之后的三件富化: 跨平台重扫 -> 当日推荐卡频道档案 -> top50 评论采样。
    # 全部温和降级(失败只记 log 不中断主链)。评论采样的 top_n 跟随 --top-n 冒烟参数。
    cross_res = run_cross_platform(args.config) if cfg.get("cross_platform", {}).get("enabled", True) else {"skipped": True}
    dossier_res = run_dossiers(args.config) if cfg.get("dossiers", {}).get("enabled", True) else {"skipped": True}
    comments_res = run_comments(args.config, top_n=smoke_top_n)
    transcripts_res = run_transcripts(args.config, today, top_n=smoke_top_n)

    table_rows = build_table_rows(today, cards, scored, pool_by_url)
    csv_path = write_cards_table(today, table_rows, out_dir)

    if date.today().weekday() == 0:  # 周一存本周 picks(可证伪预测)
        write_scoreboard_picks(today, scored, cards, pool_by_url)
    scoreboard_results = check_scoreboard(today, scored, cfg)

    # 互链: 日报卡片里放该达人档案的飞书链接(映射读不到就温和省略; 上传前 push 流程还会按最新映射刷新)
    dossier_links = {}
    try:
        import feishu_docs
        _m = feishu_docs._load_map(cfg.get("feishu_docs", {}).get("map_path", feishu_docs.DEFAULT_MAP_PATH))
        dossier_links = {k[len("dossier/"):]: v.get("url") for k, v in _m.items()
                        if k.startswith("dossier/") and isinstance(v, dict) and v.get("url")}
    except Exception:
        pass

    report_path = write_report(today, collect_res, scored, newcomers, jumpers, cards, pool_by_url, first_run,
                               csv_path, scoreboard_results, dossier_links=dossier_links)

    n = len(scored)
    dtxt = ""
    if collect_res.get("pool_before") is not None:
        dtxt = f"+{collect_res['pool_after'] - collect_res['pool_before']}"
    msg = (f"📡 达人雷达日报：池子{n}({dtxt})，新进前100 {len(newcomers)} 个，"
           f"推荐卡 {len(cards)} 张 → reports/{today}-radar.md")
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
        bili_res = run_bili_line(args.bili_config, today, budget=args.budget, dry_run=args.dry_run)
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
