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
    # approximate_date: 让频道页 flat 条目带上近似上传时间戳，零额外请求(只作用于 youtubetab)
    cmd = ["yt-dlp", target, "-J", "--flat-playlist", "--skip-download", "--no-warnings", "--no-update",
           "--extractor-args", "youtubetab:approximate_date"]
    if playlist_items:
        cmd += ["--playlist-items", playlist_items]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if p.returncode != 0 or not p.stdout.strip():
            return None
        return json.loads(p.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        return None


# flat entry 里的管道字段，快照不存(thumbnails 体积大无分析价值)；其余字段全量透传，宁多勿少
VIDEO_PLUMBING = {"_type", "ie_key", "__x_forwarded_for_ip", "thumbnails"}


def _upload_precision(d, ref=None):
    """W14(2026-07-08) 日期精度标记: yt-dlp flat 模式对老视频只给相对时间("1 year ago"),
    经 extract_relative_time → datetime_from_str('now-1year') 换算成绝对日期后, 会把当前月-日/日
    保留、只回退年(或月), 导致老视频停更天数落在抓取日附近= 伪精度(见 REPORT.md 数据可信度发现)。

    换算规律(yt-dlp _base.py extract_relative_time 已核实):
      年级("N years ago") → 月与日都等于抓取日的月-日, 年更早;
      月级("N months ago") → 日等于抓取日的日, 年-月更早;
      日/周级或精确日期 → 与抓取日月-日不同(除非真的同月同日)。
    据此按'与抓取日的月-日重合度'反推精度下界。ref=抓取参照日(默认今天)。
    保守方向: 宁可判粗(→ 下游 🟡)不判细, 真·同月同日上传的极少数按粗算不冤(只影响活性核验的松紧)。"""
    if d is None:
        return None
    ref = ref or date.today()
    if (d.month, d.day) == (ref.month, ref.day) and d.year < ref.year:
        return "year"
    if d.day == ref.day and (d.year, d.month) < (ref.year, ref.month):
        return "month"
    return "day"


def _video_record(e, ref=None):
    """flat entry -> 原始视频记录: 非管道字段全透传 + video_id/upload_date/upload_precision/is_short。
    注: 当前 yt-dlp 版本 flat 模式无单视频 view_count，将来版本有就自动带上(透传)，不为它加请求。"""
    v = {k: val for k, val in e.items() if k not in VIDEO_PLUMBING}
    v["video_id"] = e.get("id")
    ts = e.get("timestamp")
    d = date.fromtimestamp(ts) if ts else None
    v["upload_date"] = d.isoformat() if d else None
    v["upload_precision"] = _upload_precision(d, ref)  # W14: day/month/year 精度标记
    dur = e.get("duration")
    v["is_short"] = bool((dur is not None and dur <= 60) or "/shorts/" in (e.get("url") or ""))
    return v


def fetch_channel(channel_url, n_videos):
    """抓一个频道的元数据，返回池子字段 + _snapshot(视频层原始数据，入池前剥离转 history)。"""
    d = _run_ytdlp(channel_url.rstrip("/") + "/videos", playlist_items=f"1-{n_videos}")
    if not d:
        return None
    today = date.today()
    videos = [_video_record(e, ref=today) for e in (d.get("entries") or [])]
    titles = [v.get("title") for v in videos if v.get("title")]
    ts = [v.get("timestamp") for v in videos if v.get("timestamp")]
    # W14: last_upload_precision = 最近一条视频(=last_upload_date 指向的那条)的精度标记。
    last_prec = None
    if ts:
        last_d = date.fromtimestamp(max(ts))
        last_prec = _upload_precision(last_d, today)
    return {
        "channel_name": d.get("channel") or d.get("uploader"),
        "channel_url": d.get("channel_url") or channel_url,
        "channel_id": d.get("channel_id") or d.get("id"),
        "subscribers": d.get("channel_follower_count"),
        "channel_view_count": d.get("view_count"),
        "description": d.get("description"),
        "country": None,
        "recent_video_titles": titles,
        "last_upload_date": date.fromtimestamp(max(ts)).isoformat() if ts else None,
        "last_upload_precision": last_prec,
        "_snapshot": {"videos": videos},
    }


def make_snapshot(fresh, today, event):
    """一条完整原始快照(data/history/ 起势层燃料)。结构开放，内容层(字幕/打标)后续按 video_id 外挂。"""
    return {
        "date": today, "event": event,
        "channel_id": fresh.get("channel_id"), "channel_name": fresh.get("channel_name"),
        "channel_url": fresh.get("channel_url"), "subscribers": fresh.get("subscribers"),
        "channel_view_count": fresh.get("channel_view_count"),
        "videos": fresh["_snapshot"]["videos"],
    }


def atomic_write_pool(rows):
    """临时文件 + rename 原子替换池子，防写一半崩掉损坏数据。"""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(POOL), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, POOL)


def append_history(snapshots, today):
    """当日原始快照 append-only: 每个被刷新/新发现的频道一条完整记录，先存后洗。
    全池订阅面貌不再重复存这里，池子本身每日入库(git)即为全量快照。"""
    os.makedirs(HISTDIR, exist_ok=True)
    path = os.path.join(HISTDIR, f"{today}.jsonl")
    with open(path, "a") as f:
        for s in snapshots:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")


def do_refresh(rows, cfg, budget, throttle, today):
    """轮转刷新: 挑 last_refreshed 最旧的 budget 个频道重抓元数据，就地更新。"""
    n_vid = cfg["collect"]["videos_per_channel"]
    # 从没刷过的(None)排最前，其余按 last_refreshed 升序; 稳定排序保留原顺序
    order = sorted(range(len(rows)), key=lambda i: rows[i].get("last_refreshed") or "")
    touched, snapshots = [], []
    for i in order[:budget]:
        r = rows[i]
        fresh = fetch_channel(r["channel_url"], n_vid)
        r["last_refreshed"] = today
        if fresh:
            snapshots.append(make_snapshot(fresh, today, "refresh"))
            fresh.pop("_snapshot")
            # 只覆盖会变的字段，保留 is_positive/source/first_seen 等标注
            # W14: last_upload_precision 随 last_upload_date 一起刷新(精度字段从采集自然长出, 不回填历史)
            for k in ("subscribers", "recent_video_titles", "last_upload_date", "last_upload_precision", "channel_name", "channel_view_count"):
                if fresh.get(k) is not None:
                    r[k] = fresh[k]
            touched.append(r)
            print(f"  refresh ok: {r['channel_name']} subs={r.get('subscribers')}", file=sys.stderr)
        else:
            print(f"  refresh miss: {r['channel_name']}", file=sys.stderr)
        time.sleep(throttle)
    return touched, snapshots


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
    added, snapshots = [], []
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
        snapshots.append(make_snapshot(fresh, today, "discover"))
        fresh.pop("_snapshot")
        fresh.update({
            "source": "auto-discover", "is_positive": False, "positive_source": None,
            "first_seen": today, "last_refreshed": today,
        })
        rows.append(fresh)
        have.add(fresh["channel_url"]); have.add(fresh.get("channel_id"))
        added.append(fresh)
        print(f"  discover NEW: {fresh['channel_name']} subs={fresh.get('subscribers')}", file=sys.stderr)
    return added, snapshots


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
    touched, snapshots = [], []
    if args.refresh:
        budget = args.budget if args.budget is not None else ccfg["refresh_per_run"]
        touched, snaps = do_refresh(rows, cfg, budget, throttle, today)
        snapshots.extend(snaps)
    added = []
    if args.discover:
        n_terms = args.discover_terms if args.discover_terms is not None else ccfg["discover_terms_per_run"]
        added, snaps = do_discover(rows, cfg, n_terms, throttle, today)
        snapshots.extend(snaps)

    atomic_write_pool(rows)
    append_history(snapshots, today)

    result = {"pool_before": n0, "pool_after": len(rows), "discovered": len(added),
              "refreshed": len(touched), "snapshots": len(snapshots),
              "discovered_names": [a["channel_name"] for a in added]}
    print(json.dumps(result, ensure_ascii=False))
    return result


if __name__ == "__main__":
    main()
