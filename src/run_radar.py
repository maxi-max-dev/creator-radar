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


def write_report(today, collect_res, scored, newcomers, jumpers, cards, pool_by_url, first_run, csv_path=None,
                 scoreboard_results=None):
    n = len(scored)
    delta = ""
    if collect_res.get("pool_before") is not None:
        d = collect_res["pool_after"] - collect_res["pool_before"]
        delta = f"（+{d}）" if d else "（+0）"
    L = [f"# 达人雷达日报 · {today}", ""]
    L += ["## 今日池子", "",
          f"- 池子规模：**{n}** 频道 {delta}",
          f"- 本次刷新：{collect_res.get('refreshed', 0)} 个频道元数据",
          f"- 新发现入池：{collect_res.get('discovered', 0)} 个"]
    if collect_res.get("discovered_names"):
        L.append(f"  - {', '.join(collect_res['discovered_names'][:15])}")
    L.append("")

    if first_run:
        L += ["## 排名 diff", "", "_首次运行，无历史排名可对比。下次运行起产出新进/窜升榜。_", ""]
    else:
        L += ["## 新进前 100", ""]
        if newcomers:
            for s in newcomers[:20]:
                L.append(f"- #{s['rank']} **{s['channel_name']}** · {s.get('subscribers')} 订阅 · score={s['score']}")
        else:
            L.append("_本次无新面孔进入前 100。_")
        L += ["", "## 窜升榜（排名上升 ≥200 位）", ""]
        if jumpers:
            for s in jumpers[:20]:
                L.append(f"- **{s['channel_name']}** ↑{s['rank_delta']} 位 → 现 #{s['rank']} · {s.get('subscribers')} 订阅")
        else:
            L.append("_本次无频道大幅窜升。_")
        L.append("")

    L += ["## 推荐卡", ""]
    if cards:
        for c in cards:
            L.append(f"### {c.get('channel_name', '?')}  ·  #{c.get('_rank', '?')}")
            for r in (c.get("why_worth_signing") or []):
                L.append(f"- 值得签：{r}")
            if c.get("risk"):
                L.append(f"- ⚠️ 风险：{c['risk']}")
            if c.get("first_collab"):
                L.append(f"- 🤝 首次合作：{c['first_collab']}")
            L.append("")
    else:
        L += ["_本次未生成推荐卡（无符合条件的新面孔/窜升候选，或模型不可用）。_", ""]

    L += ["## 记分板", ""]
    if scoreboard_results:
        for r in scoreboard_results:
            L.append(f"- **{r['picked_date']} 那期预测到期**（{r['window_days']} 天窗口，全池中位增速 {r['pool_median_growth_pct']}%）：")
            for v in r["verdicts"]:
                if v["verdict"] == "数据缺失":
                    L.append(f"  - {v.get('channel_name', '?')}：数据缺失")
                else:
                    L.append(f"  - **{v['channel_name']}** 订阅 {v['subscribers_baseline']} → {v['subscribers_now']}"
                             f"（{v['growth_pct']:+.2f}%）：{v['verdict']}")
    else:
        L.append("记分板：无到期预测")
    L.append("")

    L += ["## 运行统计", "",
          f"- 时间：{datetime.now().isoformat(timespec='seconds')}",
          f"- 推荐卡：{len(cards)} 张",
          f"- 新进前 100：{len(newcomers)} 个" if not first_run else "- 新进前 100：n/a（首次运行）"]
    if csv_path:
        L.append(f"- 当日推荐表格：data/runs/daily/{today}/cards_table.csv")

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


def push(cfg, msg, report_path, bitable_rows=None):
    """按 config.outputs 分发。report 已写盘; imessage 调 notify 脚本; bitable 灌飞书多维表格。"""
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
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "config", "insta360.json"))
    ap.add_argument("--budget", type=int, help="覆盖 refresh 频道数(冒烟用小值)")
    ap.add_argument("--discover-terms", type=int, help="覆盖 discover 搜索词数")
    ap.add_argument("--top-n", type=int, help="覆盖推荐卡扫描的 top N(冒烟用小值)")
    args = ap.parse_args()

    cfg = load_config(args.config)
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

    table_rows = build_table_rows(today, cards, scored, pool_by_url)
    csv_path = write_cards_table(today, table_rows, out_dir)

    if date.today().weekday() == 0:  # 周一存本周 picks(可证伪预测)
        write_scoreboard_picks(today, scored, cards, pool_by_url)
    scoreboard_results = check_scoreboard(today, scored, cfg)

    report_path = write_report(today, collect_res, scored, newcomers, jumpers, cards, pool_by_url, first_run,
                               csv_path, scoreboard_results)

    n = len(scored)
    dtxt = ""
    if collect_res.get("pool_before") is not None:
        dtxt = f"+{collect_res['pool_after'] - collect_res['pool_before']}"
    msg = (f"📡 达人雷达日报：池子{n}({dtxt})，新进前100 {len(newcomers)} 个，"
           f"推荐卡 {len(cards)} 张 → reports/{today}-radar.md")
    push_res = push(cfg, msg, report_path, bitable_rows=table_rows)

    git_res = commit_snapshots(today)  # 数据快照入库(先存后洗的"存"落到异地)

    os.makedirs(LOGS, exist_ok=True)
    cross_txt = f"{cross_res.get('with_any', '?')}/{cross_res.get('total', '?')}" if "error" not in cross_res else "err"
    comm_txt = (f"{comments_res.get('channels_ok', '?')}ok/{comments_res.get('comments_written', '?')}c"
                if "error" not in comments_res else "err")
    summary = (f"{datetime.now().isoformat(timespec='seconds')} pool={n} {dtxt or '+?'} "
               f"refreshed={collect_res.get('refreshed', 0)} discovered={collect_res.get('discovered', 0)} "
               f"newcomers={len(newcomers)} jumpers={len(jumpers)} cards={len(cards)} "
               f"cross={cross_txt} dossiers={dossier_res.get('generated', 'err')} comments={comm_txt} "
               f"settled={len(scoreboard_results)} push={push_res} git={git_res}")
    with open(os.path.join(LOGS, "radar.log"), "a") as f:
        f.write(summary + "\n")

    print("\n=== RUN SUMMARY ===")
    print(summary)
    print("report:", report_path)
    print("push:", push_res)


if __name__ == "__main__":
    main()
