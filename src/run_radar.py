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


# 表格列序固定，也是将来 bitable 出口的 schema 母版
CSV_COLUMNS = ["日期", "排名", "频道名", "频道链接", "订阅数", "排名百分位", "总分", "语义分", "甜点分",
               "POV标记分", "命中主题", "是否新发现", "排名变动", "值得签1", "值得签2", "值得签3",
               "风险", "首次合作建议", "状态"]


def write_cards_table(today, cards, scored, pool_by_url, out_dir):
    """推荐卡表格化: 一行一个推荐人。utf-8-sig 保 Numbers/Excel 中文不乱码，状态列留空给运营填。"""
    s_by_url = {s["channel_url"]: s for s in scored}
    path = os.path.join(out_dir, "cards_table.csv")
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        for c in cards:
            s = s_by_url.get(c.get("_channel_url"), {})
            p = pool_by_url.get(c.get("_channel_url"), {})
            why = (c.get("why_worth_signing") or []) + ["", "", ""]
            delta = s.get("rank_delta")
            w.writerow([
                today,
                s.get("rank", c.get("_rank", "")),
                s.get("channel_name", c.get("channel_name", "")),
                c.get("_channel_url", ""),
                s.get("subscribers", ""),
                f"{s['pct']}%" if s.get("pct") is not None else "",
                f"{s['score']:.4f}" if s.get("score") is not None else "",
                f"{s['sem']:.4f}" if s.get("sem") is not None else "",
                f"{s['sweet']:.4f}" if s.get("sweet") is not None else "",
                f"{s['pov']:.4f}" if s.get("pov") is not None else "",
                "、".join(s.get("themes_hit") or []),
                "是" if p.get("source") == "auto-discover" else "否",
                (f"+{delta}" if delta > 0 else str(delta)) if delta is not None else "",
                why[0], why[1], why[2],
                c.get("risk", ""),
                c.get("first_collab", ""),
                "",
            ])
    return path


def write_report(today, collect_res, scored, newcomers, jumpers, cards, pool_by_url, first_run, csv_path=None):
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


def push(cfg, msg, report_path):
    """按 config.outputs 分发。report 已写盘; imessage 调 notify 脚本; bitable 留占位。"""
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
            # NotConfigured: 飞书多维表格出口留接口不实现，等 app_token/table_id 凭证接入。
            # 灌表字段 = CSV_COLUMNS(cards_table.csv 同一套 schema): 日期/排名/频道名/频道链接/订阅数/
            # 排名百分位/总分/语义分/甜点分/POV标记分/命中主题/是否新发现/排名变动/值得签1-3/风险/首次合作建议/状态
            results["bitable"] = "NotConfigured"
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

    csv_path = write_cards_table(today, cards, scored, pool_by_url, out_dir)

    report_path = write_report(today, collect_res, scored, newcomers, jumpers, cards, pool_by_url, first_run, csv_path)

    n = len(scored)
    dtxt = ""
    if collect_res.get("pool_before") is not None:
        dtxt = f"+{collect_res['pool_after'] - collect_res['pool_before']}"
    msg = (f"📡 达人雷达日报：池子{n}({dtxt})，新进前100 {len(newcomers)} 个，"
           f"推荐卡 {len(cards)} 张 → reports/{today}-radar.md")
    push_res = push(cfg, msg, report_path)

    os.makedirs(LOGS, exist_ok=True)
    summary = (f"{datetime.now().isoformat(timespec='seconds')} pool={n} {dtxt or '+?'} "
               f"refreshed={collect_res.get('refreshed', 0)} discovered={collect_res.get('discovered', 0)} "
               f"newcomers={len(newcomers)} jumpers={len(jumpers)} cards={len(cards)} push={push_res}")
    with open(os.path.join(LOGS, "radar.log"), "a") as f:
        f.write(summary + "\n")

    print("\n=== RUN SUMMARY ===")
    print(summary)
    print("report:", report_path)
    print("push:", push_res)


if __name__ == "__main__":
    main()
