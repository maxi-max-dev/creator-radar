#!/usr/bin/env python3
"""达人雷达采集器(YouTube 适配器): 刷新池内频道元数据 + 发现新频道入池。
用 yt-dlp 抓 channel/videos 页的 flat JSON。每次运行有预算封顶防失控，节流见 config.collect。
所有产出留仓库目录(池子/history)，不碰 iCloud 路径。
"""
import argparse, json, os, subprocess, sys, time, tempfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from radar_lib import load_config, load_pool, leak_pattern

POOL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "pool", "creator_pool.jsonl")
HISTDIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "history")


def _run_ytdlp(target, playlist_items=None):
    """跑 yt-dlp 取单个 target 的 flat JSON。失败返回 None，不抛。"""
    cmd = ["yt-dlp", target, "-J", "--flat-playlist", "--skip-download", "--no-warnings", "--no-update"]
    if playlist_items:
        cmd += ["--playlist-items", playlist_items]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if p.returncode != 0 or not p.stdout.strip():
            return None
        return json.loads(p.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        return None


def fetch_channel(channel_url, n_videos):
    """抓一个频道的元数据，返回可写入池子的字段字典(不含 is_positive 等标注)。"""
    d = _run_ytdlp(channel_url.rstrip("/") + "/videos", playlist_items=f"1-{n_videos}")
    if not d:
        return None
    ents = d.get("entries") or []
    titles = [e.get("title") for e in ents if e.get("title")]
    ts = [e.get("timestamp") for e in ents if e.get("timestamp")]
    last_upload = date.fromtimestamp(max(ts)).isoformat() if ts else None
    return {
        "channel_name": d.get("channel") or d.get("uploader"),
        "channel_url": d.get("channel_url") or channel_url,
        "channel_id": d.get("channel_id") or d.get("id"),
        "subscribers": d.get("channel_follower_count"),
        "description": d.get("description"),
        "country": None,
        "recent_video_titles": titles,
        "last_upload_date": last_upload,
    }


def atomic_write_pool(rows):
    """临时文件 + rename 原子替换池子，防写一半崩掉损坏数据。"""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(POOL), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, POOL)


def append_history(rows, today):
    """当日快照: 每频道一行 (channel_url, subscribers)，给成长斜率信号留数据。"""
    os.makedirs(HISTDIR, exist_ok=True)
    path = os.path.join(HISTDIR, f"{today}.jsonl")
    with open(path, "a") as f:
        for r in rows:
            f.write(json.dumps({"channel_url": r.get("channel_url"), "subscribers": r.get("subscribers")}) + "\n")


def do_refresh(rows, cfg, budget, throttle, today):
    """轮转刷新: 挑 last_refreshed 最旧的 budget 个频道重抓元数据，就地更新。"""
    n_vid = cfg["collect"]["videos_per_channel"]
    # 从没刷过的(None)排最前，其余按 last_refreshed 升序; 稳定排序保留原顺序
    order = sorted(range(len(rows)), key=lambda i: rows[i].get("last_refreshed") or "")
    touched = []
    for i in order[:budget]:
        r = rows[i]
        fresh = fetch_channel(r["channel_url"], n_vid)
        r["last_refreshed"] = today
        if fresh:
            # 只覆盖会变的字段，保留 is_positive/source/first_seen 等标注
            for k in ("subscribers", "recent_video_titles", "last_upload_date", "channel_name"):
                if fresh.get(k) is not None:
                    r[k] = fresh[k]
            touched.append(r)
            print(f"  refresh ok: {r['channel_name']} subs={r.get('subscribers')}", file=sys.stderr)
        else:
            print(f"  refresh miss: {r['channel_name']}", file=sys.stderr)
        time.sleep(throttle)
    return touched


def do_discover(rows, cfg, n_terms, throttle, today):
    """搜索发现: 跑前 n_terms 个搜索词，收集不在池里的新频道并抓元数据入池。"""
    ccfg = cfg["collect"]
    leak_re = leak_pattern(cfg)
    have = {r.get("channel_url") for r in rows} | {r.get("channel_id") for r in rows}
    terms = ccfg["search_terms"][:n_terms]
    per = ccfg["discover_results_per_term"]
    n_vid = ccfg["videos_per_channel"]

    # 先扫搜索结果集齐候选频道 id/url，去重去已有去品牌自营号
    candidates = {}
    for t in terms:
        d = _run_ytdlp(f"ytsearch{per}:{t}")
        for e in (d.get("entries") or []) if d else []:
            cid, curl = e.get("channel_id"), e.get("channel_url")
            name = e.get("channel") or ""
            if not curl or cid in have or curl in have or cid in candidates:
                continue
            if leak_re.search(name):  # 品牌自营号(如 Insta360 Moto)不入池
                continue
            candidates[cid] = curl
        print(f"  discover term done: {t!r} -> pool candidates so far {len(candidates)}", file=sys.stderr)
        time.sleep(throttle)

    max_new = ccfg["discover_max_new_per_run"]
    added = []
    for curl in candidates.values():
        if len(added) >= max_new:  # 单次入池封顶，防运行时长失控
            print(f"  discover cap hit: {max_new} new channels, stopping", file=sys.stderr)
            break
        fresh = fetch_channel(curl, n_vid)
        time.sleep(throttle)
        if not fresh or not fresh.get("channel_name"):
            continue
        if fresh["channel_url"] in have or fresh.get("channel_id") in have:
            continue
        if leak_re.search(fresh["channel_name"] or ""):
            continue
        fresh.update({
            "source": "auto-discover", "is_positive": False, "positive_source": None,
            "first_seen": today, "last_refreshed": today,
        })
        rows.append(fresh)
        have.add(fresh["channel_url"]); have.add(fresh.get("channel_id"))
        added.append(fresh)
        print(f"  discover NEW: {fresh['channel_name']} subs={fresh.get('subscribers')}", file=sys.stderr)
    return added


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--refresh", action="store_true", help="刷新池内现有频道元数据(轮转)")
    ap.add_argument("--discover", action="store_true", help="搜索发现新频道入池")
    ap.add_argument("--budget", type=int, help="覆盖 refresh 频道数")
    ap.add_argument("--discover-terms", type=int, help="覆盖 discover 搜索词数")
    args = ap.parse_args()

    cfg = load_config(args.config)
    ccfg = cfg["collect"]
    rows = load_pool(POOL)
    today = date.today().isoformat()
    n0 = len(rows)
    print(f"pool={n0}", file=sys.stderr)

    throttle = ccfg["throttle_seconds"]
    touched = []
    if args.refresh:
        budget = args.budget if args.budget is not None else ccfg["refresh_per_run"]
        touched = do_refresh(rows, cfg, budget, throttle, today)
    added = []
    if args.discover:
        n_terms = args.discover_terms if args.discover_terms is not None else ccfg["discover_terms_per_run"]
        added = do_discover(rows, cfg, n_terms, throttle, today)

    atomic_write_pool(rows)
    append_history(rows, today)

    result = {"pool_before": n0, "pool_after": len(rows), "discovered": len(added),
              "refreshed": len(touched), "discovered_names": [a["channel_name"] for a in added]}
    print(json.dumps(result, ensure_ascii=False))
    return result


if __name__ == "__main__":
    main()
