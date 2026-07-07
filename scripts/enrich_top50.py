#!/usr/bin/env python3
"""Task 1: top50 真实元数据补齐。
对当日 daily/<date>/ranked.json 前 N 频道，用 collect.fetch_channel 逐个拉真实近期视频元数据
(标题/时长/发布日期/is_short)，节流 config.collect.throttle_seconds。
- 更新 data/pool/creator_pool.jsonl 对应行(只覆盖会变字段，保留 is_positive/source/first_seen 标注)
- 给 data/history/<date>.jsonl 追加完整快照行(event="enrich"，append-only，别覆盖已有行)

纯复用 collect.py 现有函数(fetch_channel/make_snapshot/atomic_write_pool/append_history)，
只是把「刷新最旧 N 个」换成「刷新当日排序 top N」。失败温和降级: 单频道 miss 跳过不中断。
"""
import argparse, json, os, sys, time
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from radar_lib import load_config, load_pool
import collect

DAILY = os.path.join(ROOT, "data", "runs", "daily")


def top_urls(today, top_n):
    """当日 ranked.json 前 top_n 频道的 channel_url(有序)。"""
    p = os.path.join(DAILY, today, "ranked.json")
    scored = json.load(open(p))
    return [s["channel_url"] for s in scored[:top_n]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--date", help="目标日期(默认今天)")
    ap.add_argument("--top-n", type=int, default=50)
    args = ap.parse_args()

    cfg = load_config(args.config)
    today = args.date or date.today().isoformat()
    n_vid = cfg["collect"]["videos_per_channel"]
    throttle = cfg["collect"]["throttle_seconds"]

    rows = load_pool(collect.POOL)
    by_url = {r.get("channel_url"): r for r in rows}
    urls = top_urls(today, args.top_n)

    snapshots, touched, miss, skipped = [], [], [], []
    for i, url in enumerate(urls, 1):
        r = by_url.get(url)
        if r is None:
            skipped.append(url)
            print(f"[{i}/{len(urls)}] skip (not in pool): {url}", file=sys.stderr)
            continue
        fresh = collect.fetch_channel(url, n_vid)
        r["last_refreshed"] = today
        if fresh:
            snapshots.append(collect.make_snapshot(fresh, today, "enrich"))
            fresh.pop("_snapshot", None)
            for k in ("subscribers", "recent_video_titles", "last_upload_date",
                      "channel_name", "channel_view_count"):
                if fresh.get(k) is not None:
                    r[k] = fresh[k]
            touched.append(r.get("channel_name"))
            nv = len(snapshots[-1]["videos"])
            print(f"[{i}/{len(urls)}] enrich ok: {r.get('channel_name')} "
                  f"subs={r.get('subscribers')} videos={nv}", file=sys.stderr)
        else:
            miss.append(r.get("channel_name"))
            print(f"[{i}/{len(urls)}] enrich miss: {r.get('channel_name')}", file=sys.stderr)
        time.sleep(throttle)

    # 先写 pool(原子)，再 append history(append-only 不覆盖已有行)
    collect.atomic_write_pool(rows)
    collect.append_history(snapshots, today)

    result = {"top_n": len(urls), "enriched": len(touched), "missed": len(miss),
              "skipped": len(skipped), "snapshots_appended": len(snapshots),
              "requests_used": len(urls) - len(skipped)}
    print(json.dumps(result, ensure_ascii=False))
    return result


if __name__ == "__main__":
    main()
