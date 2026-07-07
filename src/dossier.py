#!/usr/bin/env python3
"""达人档案 MD 生成: 对当日推荐卡的频道各产一页 data/dossiers/<channel_id>.md。
会进公开仓库 + 飞书文档，格式要干净。纯机械拼装既有产物，不碰 LLM。

一页含: 频道基本盘 / 订阅快照史(从 data/history/*.jsonl) / 近期视频表 / 评论区概况(只写统计) /
跨平台矩阵(cross_platform.jsonl) / 当日推荐卡。顶部 YAML frontmatter(channel_id/channel_name/updated)。
幂等: 重跑覆盖。

隐私红线: 评论区只写统计(采样条数/点赞中位数/平均长度)，禁止引用评论原文; 原文只在本地 data/comments/。
对外文字不用破折号。
"""
import argparse, glob, json, os, statistics, sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from radar_lib import load_config

POOL = os.path.join(ROOT, "data", "pool", "creator_pool.jsonl")
DAILY = os.path.join(ROOT, "data", "runs", "daily")
HISTORY = os.path.join(ROOT, "data", "history")
COMMENTS = os.path.join(ROOT, "data", "comments")
CROSS = os.path.join(ROOT, "data", "pool", "cross_platform.jsonl")
DOSSIERS = os.path.join(ROOT, "data", "dossiers")

PLATFORM_LABEL = {"instagram": "Instagram", "tiktok": "TikTok", "facebook": "Facebook",
                  "twitter": "X / Twitter", "website": "个人网站"}
FIELD_LABEL = {"description": "简介", "recent_video_titles": "视频标题", "channel_name": "频道名"}


def _fmt_int(n):
    return f"{n:,}" if isinstance(n, (int, float)) and n is not None else "未知"


def sub_timeline(channel_id, channel_url):
    """从 data/history/*.jsonl 提取该频道的订阅快照时间线。按 channel_id 或 channel_url 匹配。
    只取带日期+订阅数的记录(全池基线那种无日期的轻量行不算一个时间点)。返回 [(date, subs), ...] 升序去重。"""
    points = {}  # date -> subs(同日多条取最后一条)
    for path in sorted(glob.glob(os.path.join(HISTORY, "*.jsonl"))):
        for line in open(path):
            try:
                r = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if r.get("channel_id") != channel_id and r.get("channel_url") != channel_url:
                continue
            d, subs = r.get("date"), r.get("subscribers")
            if d and subs is not None:
                points[d] = subs
    return sorted(points.items())


def comment_stats(channel_id, today):
    """读 data/comments/<today>/<channel_id>.jsonl 出统计(采样条数/点赞中位数/平均长度)。
    只算统计，绝不返回任何原文。无文件返回 None。"""
    path = os.path.join(COMMENTS, today, f"{channel_id}.jsonl")
    if not os.path.exists(path):
        return None
    likes, lengths, n, videos = [], [], 0, set()
    for line in open(path):
        try:
            c = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        n += 1
        if c.get("video_id"):
            videos.add(c["video_id"])
        lk = c.get("like_count")
        if isinstance(lk, (int, float)):
            likes.append(lk)
        t = c.get("text")
        if t:
            lengths.append(len(t))
    if n == 0:
        return None
    return {
        "sampled": n,
        "videos": len(videos),
        "like_median": round(statistics.median(likes), 1) if likes else None,
        "avg_len": round(statistics.mean(lengths), 1) if lengths else None,
    }


def load_cross(cross_path=CROSS):
    """cross_platform.jsonl -> {channel_url: record} 与 {channel_id: record} 双索引。"""
    by_url, by_id = {}, {}
    if not os.path.exists(cross_path):
        return by_url, by_id
    for line in open(cross_path):
        try:
            r = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if r.get("channel_url"):
            by_url[r["channel_url"]] = r
        if r.get("channel_id"):
            by_id[r["channel_id"]] = r
    return by_url, by_id


def _yaml_escape(s):
    """frontmatter 里的字符串值最小转义(引号包裹，内部双引号转义)。"""
    return '"' + str(s or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_md(card, scored_row, pool_row, cross_rec, timeline, cstats, today):
    """拼一页档案 markdown。所有对外文字不用破折号。"""
    cid = pool_row.get("channel_id") or scored_row.get("channel_id") or ""
    name = scored_row.get("channel_name") or pool_row.get("channel_name") or card.get("channel_name") or "?"
    url = scored_row.get("channel_url") or pool_row.get("channel_url") or ""
    L = []

    # --- frontmatter ---
    L += ["---",
          f"channel_id: {_yaml_escape(cid)}",
          f"channel_name: {_yaml_escape(name)}",
          f"updated: {today}",
          "---", ""]

    # --- 标题 + 基本盘 ---
    L += [f"# {name}", "",
          "## 频道基本盘", "",
          f"- 频道链接：{url}",
          f"- 订阅数：{_fmt_int(scored_row.get('subscribers'))}",
          f"- 雷达排名：第 {scored_row.get('rank', '?')} 名 / 全池 {scored_row.get('_pool_size', '?')}"
          f"（前 {scored_row.get('pct', '?')}%）",
          f"- 总分：{scored_row.get('score', '?')}"
          f"（语义 {scored_row.get('sem', '?')} · 甜点 {scored_row.get('sweet', '?')} · POV标记 {scored_row.get('pov', '?')}）"]
    themes = scored_row.get("themes_hit") or []
    L.append(f"- 命中主题：{('、'.join(themes)) if themes else '无'}")
    L.append("")

    # --- 订阅快照史 ---
    L += ["## 订阅快照史", ""]
    if timeline:
        L += ["| 日期 | 订阅数 |", "|---|---|"]
        for d, subs in timeline:
            L.append(f"| {d} | {_fmt_int(subs)} |")
        if len(timeline) == 1:
            L.append("")
            L.append("_目前仅一天快照，随每日运行积累时间线。_")
    else:
        L.append("_暂无带日期的订阅快照（随每日运行开始积累）。_")
    L.append("")

    # --- 近期视频表 ---
    L += ["## 近期视频", ""]
    titles = pool_row.get("recent_video_titles") or []
    videos = _recent_videos(pool_row, timeline_hint=titles)
    if videos:
        L += ["| 标题 | 时长 | 类型 | 发布日期 |", "|---|---|---|---|"]
        for v in videos:
            L.append(f"| {_md_cell(v['title'])} | {v['dur']} | {v['kind']} | {v['date']} |")
    else:
        L.append("_暂无近期视频数据。_")
    L.append("")

    # --- 评论区概况(只写统计) ---
    L += ["## 评论区概况", ""]
    if cstats:
        L += [f"- 采样评论：{cstats['sampled']} 条（覆盖 {cstats['videos']} 个视频）",
              f"- 点赞中位数：{cstats['like_median'] if cstats['like_median'] is not None else '未知'}",
              f"- 平均评论长度：{cstats['avg_len'] if cstats['avg_len'] is not None else '未知'} 字符",
              "",
              "_仅统计口径，评论原文不入库（隐私红线，原文只留本地采样目录）。_"]
    else:
        L.append("_本轮未采样到该频道评论。_")
    L.append("")

    # --- 跨平台矩阵 ---
    L += ["## 跨平台矩阵", ""]
    plats = (cross_rec or {}).get("platforms") or {}
    dfrom = (cross_rec or {}).get("detected_from") or {}
    if plats:
        L += ["| 平台 | 账号 / 链接 | 检出来源 |", "|---|---|---|"]
        for key in ["instagram", "tiktok", "facebook", "twitter", "website"]:
            if key in plats:
                src = FIELD_LABEL.get(dfrom.get(key, ""), dfrom.get(key, ""))
                L.append(f"| {PLATFORM_LABEL[key]} | {_md_cell(plats[key])} | {src} |")
        L += ["", "_检出自频道公开文本的链接或 handle，作为多平台经营信号，未逐一人工核验。_"]
    else:
        L.append("_未检出其他平台链接（不代表没有，仅代表频道公开文本里没写）。_")
    L.append("")

    # --- 当日推荐卡(仅当有卡时才出这一节; 无卡的档案温和省略，绝不伪造推荐语) ---
    card = card or {}
    has_card = bool(card.get("why_worth_signing") or card.get("risk") or card.get("first_collab"))
    if has_card:
        L += ["## 当日推荐卡", "", f"_生成于 {today}。_", ""]
        whys = card.get("why_worth_signing") or []
        if whys:
            L.append("**值得签：**")
            for w in whys:
                L.append(f"- {w}")
            L.append("")
        if card.get("risk"):
            L += ["**风险：**", f"- {card['risk']}", ""]
        if card.get("first_collab"):
            L += ["**首次合作建议：**", f"- {card['first_collab']}", ""]

    return "\n".join(L).rstrip() + "\n"


def _md_cell(s):
    """表格单元格里的文本: 去竖线/换行，防破表。"""
    return str(s or "").replace("|", "／").replace("\n", " ").strip()


def _recent_videos(pool_row, timeline_hint=None):
    """近期视频行。优先用当日 history 快照里的视频层(带时长/发布日/短或长)，
    没有结构化视频层就退化到 recent_video_titles(只有标题)。"""
    # 优先: 当日 history 里该频道的 videos(结构化)
    cid = pool_row.get("channel_id")
    url = pool_row.get("channel_url")
    best = None
    for path in sorted(glob.glob(os.path.join(HISTORY, "*.jsonl")), reverse=True):
        for line in open(path):
            try:
                r = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if (r.get("channel_id") == cid or r.get("channel_url") == url) and r.get("videos"):
                vids = r["videos"]
                if any(v.get("duration") is not None or v.get("upload_date") for v in vids):
                    best = vids
                    break
        if best:
            break
    out = []
    if best:
        for v in best:
            dur = v.get("duration")
            is_short = v.get("is_short")
            out.append({
                "title": v.get("title") or "?",
                "dur": _fmt_dur(dur),
                "kind": "短视频" if is_short else ("长视频" if dur else "未知"),
                "date": v.get("upload_date") or "未知",
            })
        return out
    # 退化: 只有标题
    for t in (pool_row.get("recent_video_titles") or [])[:8]:
        out.append({"title": t, "dur": "未知", "kind": "未知", "date": "未知"})
    return out


def _fmt_dur(sec):
    if sec is None:
        return "未知"
    sec = int(sec)
    m, s = divmod(sec, 60)
    return f"{m}:{s:02d}" if m else f"0:{s:02d}"


def generate(cfg, today, out_dir=DOSSIERS):
    """对当日 cards.json 里每个频道生成一页档案。返回统计。"""
    day = os.path.join(DAILY, today)
    cards_path = os.path.join(day, "cards.json")
    ranked_path = os.path.join(day, "ranked.json")
    if not os.path.exists(cards_path) or not os.path.exists(ranked_path):
        return {"generated": 0, "reason": "缺 cards.json 或 ranked.json", "files": []}

    cards = json.load(open(cards_path))
    scored = json.load(open(ranked_path))
    pool_size = len(scored)
    scored_by_url = {s["channel_url"]: s for s in scored}
    pool_by_url = {}
    for l in open(POOL):
        r = json.loads(l)
        pool_by_url[r.get("channel_url")] = r
    cross_by_url, cross_by_id = load_cross()

    os.makedirs(out_dir, exist_ok=True)
    written = []
    for card in cards:
        url = card.get("_channel_url")
        s = dict(scored_by_url.get(url, {}))
        s["_pool_size"] = pool_size
        p = pool_by_url.get(url, {})
        cid = p.get("channel_id")
        if not cid:
            print(f"  dossier skip (no channel_id): {card.get('channel_name')}", file=sys.stderr)
            continue
        cross_rec = cross_by_url.get(url) or cross_by_id.get(cid)
        timeline = sub_timeline(cid, url)
        cstats = comment_stats(cid, today)
        md = build_md(card, s, p, cross_rec, timeline, cstats, today)
        path = os.path.join(out_dir, f"{cid}.md")  # 幂等: 覆盖
        with open(path, "w") as f:
            f.write(md)
        written.append(path)
        print(f"  dossier ok: {s.get('channel_name')} -> {os.path.basename(path)}", file=sys.stderr)

    return {"generated": len(written), "files": written}


def generate_topn(cfg, today, top_n, out_dir=DOSSIERS):
    """对当日 ranked.json 前 top_n 频道各生成一页档案。
    有推荐卡的频道(前若干名)带推荐卡节; 无卡的省略该节(build_md 里判定，不伪造推荐语)。
    其余数据(视频表/评论/跨平台/快照史)与 generate() 完全同源。幂等: 覆盖。"""
    day = os.path.join(DAILY, today)
    ranked_path = os.path.join(day, "ranked.json")
    if not os.path.exists(ranked_path):
        return {"generated": 0, "reason": "缺 ranked.json", "files": [], "with_card": 0}
    scored = json.load(open(ranked_path))
    pool_size = len(scored)

    cards_path = os.path.join(day, "cards.json")
    cards_by_url = {}
    if os.path.exists(cards_path):
        for c in json.load(open(cards_path)):
            if c.get("_channel_url"):
                cards_by_url[c["_channel_url"]] = c

    pool_by_url = {}
    for l in open(POOL):
        r = json.loads(l)
        pool_by_url[r.get("channel_url")] = r
    cross_by_url, cross_by_id = load_cross()

    os.makedirs(out_dir, exist_ok=True)
    written, with_card, no_cid = [], 0, 0
    for s in scored[:top_n]:
        url = s.get("channel_url")
        p = pool_by_url.get(url, {})
        cid = p.get("channel_id")
        if not cid:
            no_cid += 1
            print(f"  dossier skip (no channel_id): {s.get('channel_name')}", file=sys.stderr)
            continue
        srow = dict(s)
        srow["_pool_size"] = pool_size
        card = cards_by_url.get(url, {})
        if card:
            with_card += 1
        cross_rec = cross_by_url.get(url) or cross_by_id.get(cid)
        timeline = sub_timeline(cid, url)
        cstats = comment_stats(cid, today)
        md = build_md(card, srow, p, cross_rec, timeline, cstats, today)
        path = os.path.join(out_dir, f"{cid}.md")  # 幂等: 覆盖
        with open(path, "w") as f:
            f.write(md)
        written.append(path)
        print(f"  dossier ok: {s.get('channel_name')} -> {os.path.basename(path)}"
              f"{' [card]' if card else ''}", file=sys.stderr)

    return {"generated": len(written), "with_card": with_card,
            "skipped_no_cid": no_cid, "files": written}


def main():
    ap = argparse.ArgumentParser(description="达人档案 MD 生成(纯拼装既有产物，不碰 LLM)")
    ap.add_argument("--config", required=True)
    ap.add_argument("--date", help="目标日期(默认今天)")
    ap.add_argument("--out", default=DOSSIERS)
    ap.add_argument("--top-n", type=int, help="给当日排序前 N 频道各出一页(有卡带卡节，无卡省略)。不给则只对当日推荐卡频道生成。")
    args = ap.parse_args()
    cfg = load_config(args.config)
    today = args.date or date.today().isoformat()
    if args.top_n:
        stats = generate_topn(cfg, today, args.top_n, out_dir=args.out)
        print(f"达人档案生成(top{args.top_n}): {stats['generated']} 份"
              f"(其中带推荐卡 {stats.get('with_card', 0)} 份) -> {args.out}", file=sys.stderr)
        print(json.dumps(stats, ensure_ascii=False))
    else:
        stats = generate(cfg, today, out_dir=args.out)
        print(f"达人档案生成: {stats['generated']} 份 -> {args.out}", file=sys.stderr)
        print(json.dumps({"generated": stats["generated"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
