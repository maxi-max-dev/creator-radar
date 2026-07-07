#!/usr/bin/env python3
"""达人雷达 RSS 采集器: 起势层(momentum)的视频级播放数据来源。

为什么需要它: 池子记录只有 recent_video_titles(纯标题) 和 last_upload_date(单日期),
没有任何单条视频的播放数。要判断"某频道最近视频跑得比自己历史快",必须拿到
每条近期视频的 播放数 + 发布日期。YouTube 官方频道 RSS 免费无 key 就给这两样:

  https://www.youtube.com/feeds/videos.xml?channel_id=UCxxxx

每个频道返回最近 ~15 条视频, media:community/media:statistics@views 是播放数。

红线: 只读 RSS(纯 urllib+xml.etree, 无第三方库), 绝不 yt-dlp 批量刷、绝不下载任何
音视频本体、绝不碰评论。节流按 config.momentum.rss.throttle_seconds。拉取失败的频道
跳过记数, 不中断。产出 append 友好的 jsonl 到 data/rss/。

用法:
  python3 src/collect_rss.py --config config/insta360.json \
      --pool data/pool/creator_pool.jsonl \
      --ranked data/runs/momentum-v1/fit/ranked.json \
      --out data/rss
"""
import argparse, json, os, sys, time, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from radar_lib import load_config, load_pool

NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}
FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"


def parse_feed(raw):
    """解析一份频道 RSS, 返回 (channel_title, [video dict,...])。
    每个 video: video_id / title / published(ISO) / view_count(int|None) / duration_sec(int|None) / is_short(bool)。
    view_count 缺失时留 None(部分频道关闭了公开播放数), 绝不当 0。"""
    root = ET.fromstring(raw)
    ch_title = root.findtext("a:title", default="", namespaces=NS)
    videos = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", default="", namespaces=NS)
        title = e.findtext("a:title", default="", namespaces=NS)
        pub = e.findtext("a:published", default="", namespaces=NS)
        views = None
        grp = e.find("media:group", NS)
        if grp is not None:
            comm = grp.find("media:community", NS)
            if comm is not None:
                stat = comm.find("media:statistics", NS)
                if stat is not None and stat.get("views") is not None:
                    try:
                        views = int(stat.get("views"))
                    except ValueError:
                        views = None
        # RSS 不带时长/shorts 标记。用启发式: 标题或链接含 #shorts / /shorts/ 判为短视频。
        # (真实时长要另调 API, 本采集器守零下载红线, 故用文本启发式, 在 momentum 里已足够降权用。)
        link = ""
        le = e.find("a:link", NS)
        if le is not None:
            link = le.get("href") or ""
        low = (title + " " + link).lower()
        is_short = ("#shorts" in low) or ("/shorts/" in low) or ("#short" in low and "#shorts" not in low)
        videos.append({
            "video_id": vid,
            "title": title,
            "published": pub,
            "view_count": views,
            "is_short": is_short,
        })
    return ch_title, videos


def fetch_one(cid, timeout):
    url = FEED_URL.format(cid=cid)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (creator-radar RSS)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def select_channels(pool, ranked, top_n):
    """按 fit 排名取前 top_n 个有 UC channel_id 的频道。
    ranked.json 里是 channel_url + rank; 与 pool 按 url join 拿 channel_id。"""
    by_url = {r["channel_url"]: r for r in pool}
    picked = []
    seen = set()
    for s in ranked:  # ranked 已按 rank 升序
        r = by_url.get(s["channel_url"])
        if not r:
            continue
        cid = r.get("channel_id") or ""
        if not cid.startswith("UC") or cid in seen:
            continue
        seen.add(cid)
        picked.append({
            "channel_id": cid,
            "channel_name": r.get("channel_name"),
            "channel_url": r.get("channel_url"),
            "fit_rank": s.get("rank"),
        })
        if len(picked) >= top_n:
            break
    return picked


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--pool", required=True)
    ap.add_argument("--ranked", required=True, help="fit ranked.json, 决定拉取优先级")
    ap.add_argument("--out", required=True, help="输出目录 data/rss")
    ap.add_argument("--limit", type=int, default=None, help="覆盖 config.momentum.rss.top_channels(调试用)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    rconf = cfg.get("momentum", {}).get("rss", {})
    top_n = args.limit if args.limit is not None else rconf.get("top_channels", 350)
    throttle = rconf.get("throttle_seconds", 0.5)
    timeout = rconf.get("timeout_seconds", 20)

    pool = load_pool(args.pool)
    ranked = json.load(open(args.ranked))
    targets = select_channels(pool, ranked, top_n)
    print(f"选中 {len(targets)} 个频道(按 fit 排名), 节流 {throttle}s/请求", file=sys.stderr)

    os.makedirs(args.out, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fetched_at = datetime.now(timezone.utc).isoformat()
    out_path = os.path.join(args.out, f"{stamp}.jsonl")

    ok = fail = 0
    fails = []
    with open(out_path, "a") as f:
        for i, t in enumerate(targets, 1):
            cid = t["channel_id"]
            try:
                raw = fetch_one(cid, timeout)
                _, videos = parse_feed(raw)
                rec = {
                    "channel_id": cid,
                    "channel_name": t["channel_name"],
                    "channel_url": t["channel_url"],
                    "fit_rank": t["fit_rank"],
                    "fetched_at": fetched_at,
                    "n_videos": len(videos),
                    "videos": videos,
                }
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                ok += 1
            except (urllib.error.HTTPError, urllib.error.URLError, ET.ParseError, TimeoutError, Exception) as e:
                fail += 1
                fails.append((cid, t["channel_name"], type(e).__name__))
            if i % 25 == 0 or i == len(targets):
                print(f"  {i}/{len(targets)}  ok={ok} fail={fail}", file=sys.stderr)
            time.sleep(throttle)

    print(f"\n完成: ok={ok} fail={fail} -> {out_path}", file=sys.stderr)
    if fails:
        print(f"失败频道({len(fails)}): " + ", ".join(f"{n}[{e}]" for _, n, e in fails[:20]), file=sys.stderr)
    # 一行机器可读汇总
    print(json.dumps({"ok": ok, "fail": fail, "out": out_path, "fetched_at": fetched_at}, ensure_ascii=False))


if __name__ == "__main__":
    main()
