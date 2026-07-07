#!/usr/bin/env python3
"""评论采样(有预算与隐私红线): 对当日排序产物 top N 频道抓热门评论，只留文本+计数。
受众质量信号 + 内容打标语料 + 将来喂飞书 AI 总结。打标是下一棒的事，本工具纯机械提取，不碰 LLM。

隐私红线(硬性):
  评论作者名/头像/频道链接一律不入库。要区分作者只存 author_id 的 sha256 前 8 位。只留文本与点赞数。
预算红线(硬性):
  每天总请求 ≤ budget_requests_per_day(config)，单频道失败跳过不中断整批。
去重账本 data/comments/fetched_index.json:
  N 天(config.dedup_days)内抓过的频道跳过。
存储:
  data/comments/YYYY-MM-DD/<channel_id>.jsonl，原始 append-only，每条含 video_id / text / like_count / author_hash / comment_id。
  ⚠️ data/comments/ 必须保持 gitignore(第三方用户内容不进仓库)。
禁止下载视频/音频本体(--skip-download 恒开)。
"""
import argparse, hashlib, json, os, subprocess, sys, time
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from radar_lib import load_config

POOL = os.path.join(ROOT, "data", "pool", "creator_pool.jsonl")
DAILY = os.path.join(ROOT, "data", "runs", "daily")
COMMENTS = os.path.join(ROOT, "data", "comments")
INDEX = os.path.join(COMMENTS, "fetched_index.json")


def _author_hash(author_id):
    """作者标识只保留 sha256 前 8 位(能区分作者，不可还原为账号/链接)。无 id 返回 None。"""
    if not author_id:
        return None
    return hashlib.sha256(str(author_id).encode()).hexdigest()[:8]


def load_index():
    if os.path.exists(INDEX):
        try:
            return json.load(open(INDEX))
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def save_index(idx):
    os.makedirs(COMMENTS, exist_ok=True)
    tmp = INDEX + ".tmp"
    with open(tmp, "w") as f:
        json.dump(idx, f, ensure_ascii=False, indent=1)
    os.replace(tmp, INDEX)


def recently_fetched(channel_id, idx, dedup_days, today):
    """channel_id 在 dedup_days 天内抓过则 True。"""
    last = idx.get(channel_id)
    if not last:
        return False
    try:
        age = (date.fromisoformat(today) - date.fromisoformat(last)).days
    except ValueError:
        return False
    return age < dedup_days


def fetch_recent_video_ids(channel_url, n_videos, throttle):
    """列频道最近 n_videos 个视频 id(flat，不下载)。这一步算 1 次请求。失败返回 []。"""
    cmd = ["yt-dlp", channel_url.rstrip("/") + "/videos", "-J", "--flat-playlist",
           "--skip-download", "--no-warnings", "--no-update", "--playlist-items", f"1-{n_videos}"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if p.returncode != 0 or not p.stdout.strip():
            return []
        d = json.loads(p.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        return []
    finally:
        time.sleep(throttle)
    ids = []
    for e in (d.get("entries") or []):
        vid = e.get("id")
        if vid:
            ids.append(vid)
    return ids


def fetch_comments(video_id, top_k, throttle):
    """抓单个视频前 top_k 条热门评论(comment_sort=top)，禁止下载本体。这一步算 1 次请求。
    返回精简评论列表(隐私红线: 只留 video_id/text/like_count/author_hash/comment_id)。失败返回 None。"""
    # max_comments=<总数>,<每根>,<回复>: 只要顶层热门，不要回复层
    cmd = ["yt-dlp", f"https://www.youtube.com/watch?v={video_id}",
           "--skip-download", "--write-comments", "--no-warnings", "--no-update",
           "--extractor-args", f"youtube:max_comments={top_k},{top_k},0;comment_sort=top",
           "--dump-single-json"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if p.returncode != 0 or not p.stdout.strip():
            return None
        d = json.loads(p.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        return None
    finally:
        time.sleep(throttle)
    out = []
    for c in (d.get("comments") or [])[:top_k]:
        text = c.get("text")
        if text is None:
            continue
        out.append({
            "video_id": video_id,
            "text": text,
            "like_count": c.get("like_count"),
            "author_hash": _author_hash(c.get("author_id")),  # 只存 hash，绝不存作者名/头像/链接
            "comment_id": c.get("id"),                          # 不透明 id，用于 append-only 去重
        })
    return out


def _existing_comment_ids(path):
    """已落盘该频道文件里的 comment_id 集合(append-only 去重，避免重跑重复行)。"""
    ids = set()
    if not os.path.exists(path):
        return ids
    for line in open(path):
        try:
            ids.add(json.loads(line).get("comment_id"))
        except (json.JSONDecodeError, ValueError):
            continue
    return ids


def append_comments(channel_id, records, today):
    """append-only 落盘 data/comments/YYYY-MM-DD/<channel_id>.jsonl，跳过已存在的 comment_id。返回新写条数。"""
    day_dir = os.path.join(COMMENTS, today)
    os.makedirs(day_dir, exist_ok=True)
    path = os.path.join(day_dir, f"{channel_id}.jsonl")
    seen = _existing_comment_ids(path)
    written = 0
    with open(path, "a") as f:
        for r in records:
            if r.get("comment_id") in seen:
                continue
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            seen.add(r.get("comment_id"))
            written += 1
    return written


def load_ranked_channels(today, top_n):
    """从当日 daily/<today>/ranked.json 取前 top_n 频道(带 channel_id，来自池子回填)。"""
    ranked_path = os.path.join(DAILY, today, "ranked.json")
    if not os.path.exists(ranked_path):
        return []
    scored = json.load(open(ranked_path))
    # ranked.json 不带 channel_id，从池子按 url 补
    pool_by_url = {}
    for l in open(POOL):
        r = json.loads(l)
        pool_by_url[r.get("channel_url")] = r
    out = []
    for s in scored[:top_n]:
        p = pool_by_url.get(s["channel_url"], {})
        cid = p.get("channel_id")
        if not cid:
            continue
        out.append({"channel_id": cid, "channel_url": s["channel_url"], "channel_name": s["channel_name"]})
    return out


def sample(cfg, today, top_n=None):
    """主流程: top_n 频道 × 每频道最近 videos_per_channel 视频 × 每视频 comments_per_video 条热门评论。
    受预算(budget_requests_per_day)与去重账本(dedup_days)约束; 单频道失败跳过不中断。"""
    ccfg = cfg.get("comments", {})
    top_n = top_n if top_n is not None else ccfg.get("top_channels", 50)
    n_vid = ccfg.get("videos_per_channel", 2)
    top_k = ccfg.get("comments_per_video", 20)
    dedup_days = ccfg.get("dedup_days", 7)
    budget = ccfg.get("budget_requests_per_day", 120)
    throttle = cfg.get("collect", {}).get("throttle_seconds", 2.5)

    idx = load_index()
    channels = load_ranked_channels(today, top_n)

    requests_used = 0
    stats = {"channels_considered": len(channels), "channels_attempted": 0, "channels_ok": 0,
             "channels_skipped_dedup": 0, "channels_failed": 0, "videos_sampled": 0,
             "comments_written": 0, "requests_used": 0, "budget": budget}

    for ch in channels:
        cid = ch["channel_id"]
        if recently_fetched(cid, idx, dedup_days, today):
            stats["channels_skipped_dedup"] += 1
            continue
        if requests_used >= budget:
            print(f"comments: 预算 {budget} 用尽，停止(还剩 "
                  f"{len(channels) - stats['channels_attempted'] - stats['channels_skipped_dedup']} 频道未抓)", file=sys.stderr)
            break

        stats["channels_attempted"] += 1
        try:
            # 1 次请求列视频
            if requests_used >= budget:
                break
            vids = fetch_recent_video_ids(ch["channel_url"], n_vid, throttle)
            requests_used += 1
            if not vids:
                stats["channels_failed"] += 1
                print(f"  comments miss (no videos): {ch['channel_name']}", file=sys.stderr)
                continue

            ch_records = []
            for vid in vids[:n_vid]:
                if requests_used >= budget:
                    break
                recs = fetch_comments(vid, top_k, throttle)  # 1 次请求/视频
                requests_used += 1
                if recs is None:
                    print(f"  comments miss (video {vid}): {ch['channel_name']}", file=sys.stderr)
                    continue
                ch_records.extend(recs)
                stats["videos_sampled"] += 1

            if ch_records:
                w = append_comments(cid, ch_records, today)
                stats["comments_written"] += w
                stats["channels_ok"] += 1
                idx[cid] = today  # 去重账本按频道记最近抓取日
                print(f"  comments ok: {ch['channel_name']} -> {w} 条 (req used {requests_used}/{budget})", file=sys.stderr)
            else:
                stats["channels_failed"] += 1
        except Exception as e:  # 单频道任何异常都不中断整批
            stats["channels_failed"] += 1
            print(f"  comments SKIP {ch['channel_name']}: {e}", file=sys.stderr)

    stats["requests_used"] = requests_used
    save_index(idx)
    return stats


def main():
    ap = argparse.ArgumentParser(description="评论采样(有预算+隐私红线，禁下载本体)")
    ap.add_argument("--config", required=True)
    ap.add_argument("--date", help="目标日期(默认今天)")
    ap.add_argument("--top-n", type=int, help="覆盖 config.comments.top_channels(冒烟用小值)")
    args = ap.parse_args()
    cfg = load_config(args.config)
    today = args.date or date.today().isoformat()
    stats = sample(cfg, today, top_n=args.top_n)
    print(f"评论采样完成: 尝试 {stats['channels_attempted']} 频道，成功 {stats['channels_ok']}，"
          f"去重跳过 {stats['channels_skipped_dedup']}，失败 {stats['channels_failed']}，"
          f"写入 {stats['comments_written']} 条评论，用请求 {stats['requests_used']}/{stats['budget']}", file=sys.stderr)
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
