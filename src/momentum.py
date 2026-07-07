#!/usr/bin/env python3
"""达人雷达 起势层打分器(momentum)。

核心思路(一句话): 某频道最近视频的 播放速度(views/天) 显著超过它自己所有已知视频的
views/天 中位数 = 起势信号。只比频道对自己历史的相对爆发, 绝不跨频道横向比绝对播放量
(大频道天然高, 那是 fit 层甜点函数管的事)。

每个频道输出 momentum_score ∈ [0,1] + data_coverage 标记。管道:
  1. 每条视频算 views_per_day = view_count / max(天龄, 地板天数)。
  2. 频道历史基线 = 所有已知视频 views_per_day 的中位数(shorts 降权后)。
  3. 近期窗口(默认 45 天)内每条视频算 爆发比 = views_per_day / baseline_median。
  4. log 压缩(底 2) + burst_cap 封顶 + 归一化到 [0,1]。
  5. 近期视频按发布距今天数指数衰减(半衰期)。
  6. 频道 momentum = 近期视频里 加权爆发分 的最大值(取最亮那条爆款代表起势)。
  7. 已知视频不足的频道给中性分, 打 data_coverage 标记, 绝不缺数据=0 分冤杀。

铁律: 本打分器只喂产品路径(score/run_radar 展示层), 绝不进 backtest 官方指标。

用法:
  python3 src/momentum.py --config config/insta360.json \
      --pool data/pool/creator_pool.jsonl \
      --rss data/rss/2026-07-07.jsonl \
      --out data/runs/momentum-v1/momentum_scores.json
"""
import argparse, json, math, os, sys
from datetime import datetime, timezone
from statistics import median

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from radar_lib import load_config, load_pool


def _parse_dt(s):
    """解析 RSS 的 ISO 时间(带时区), 返回 aware datetime 或 None。"""
    if not s:
        return None
    try:
        # RSS: 2026-07-05T14:03:11+00:00
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def channel_momentum(videos, mconf, now):
    """对一个频道的视频列表算 momentum。返回 dict:
       score / data_coverage / n_usable / baseline_vpd / top 证据字段。"""
    floor_days = mconf["min_days_since_upload"]
    maturity_days = mconf["min_maturity_days"]
    win_days = mconf["recent_window_days"]
    half_life = mconf["half_life_days"]
    min_vids = mconf["min_videos_for_baseline"]
    neutral = mconf["neutral_score"]
    log_base = mconf["burst_log_base"]
    cap = mconf["burst_cap"]
    short_w = mconf["short_weight"]

    # 1. 每条视频算 views/天 + 天龄。shorts 记权重(降权)。
    items = []
    for v in videos:
        dt = _parse_dt(v.get("published"))
        views = v.get("view_count")
        if dt is None or views is None:
            continue
        age = (now - dt).total_seconds() / 86400.0
        eff_age = max(age, floor_days)  # 地板天数防新视频分母虚小
        vpd = views / eff_age
        w = short_w if v.get("is_short") else 1.0
        items.append({
            "title": v.get("title", ""), "views": views, "age_days": age,
            "vpd": vpd, "is_short": v.get("is_short", False), "weight": w,
        })

    n_usable = len(items)
    if n_usable < min_vids:
        return {"momentum_score": round(neutral, 4), "data_coverage": "low",
                "n_usable": n_usable, "baseline_vpd": None,
                "top_burst_ratio": None, "top_video": None}

    # 2. 频道历史基线 = views/天 的中位数。
    #    shorts 降权体现在: 用加权样本近似——把每条 vpd 按权重复制不现实, 改用
    #    "把 shorts 的 vpd 乘权重后再取中位"(等价于把 shorts 的势能打折进基线与突破两侧)。
    adj_vpd = [it["vpd"] * it["weight"] for it in items]
    baseline = median(adj_vpd)
    if baseline <= 0:
        return {"momentum_score": round(neutral, 4), "data_coverage": "low",
                "n_usable": n_usable, "baseline_vpd": 0.0,
                "top_burst_ratio": None, "top_video": None}

    # 3-6. 近期窗口内每条算 爆发比 → log 压缩 → 封顶归一 → 衰减 → 取最大。
    best = 0.0
    best_ev = None
    log_cap = math.log(cap, log_base)  # 归一化分母
    n_eligible = 0
    for it in items:
        # 起势窗口 = [maturity, window]: 太新的(未成熟, views/天 虚高)不当突破点, 太老的(窗口外)不算近期。
        # 两端外的视频都只进基线, 不参与突破评分。
        if it["age_days"] < maturity_days or it["age_days"] > win_days:
            continue
        n_eligible += 1
        ratio = (it["vpd"] * it["weight"]) / baseline
        if ratio <= 1.0:
            burst01 = 0.0
        else:
            burst01 = min(math.log(ratio, log_base), log_cap) / log_cap  # → [0,1]
        # 时间衰减: 半衰期 half_life 天。刚发的满权, 越老越轻。
        decay = 0.5 ** (max(it["age_days"], 0) / half_life)
        score = burst01 * decay
        if score > best:
            best = score
            best_ev = {"title": it["title"], "views": it["views"],
                       "age_days": round(it["age_days"], 1),
                       "burst_ratio": round(ratio, 2), "is_short": it["is_short"]}

    # 有历史但近期窗口内没有可评的新视频 = 频道自然沉寂, momentum 归零(它确实没在起势)。
    coverage = "ok" if n_eligible > 0 else "stale"
    return {"momentum_score": round(best, 4), "data_coverage": coverage,
            "n_usable": n_usable, "n_eligible_recent": n_eligible,
            "baseline_vpd": round(baseline, 1),
            "top_burst_ratio": best_ev["burst_ratio"] if best_ev else None,
            "top_video": best_ev}


def compute(pool, rss_recs, cfg, now=None):
    """对全池算 momentum。有 RSS 的按视频算; 没 RSS 的给中性分 data_coverage='none'。
    now 可显式传入(默认 RSS 的 fetched_at, 保证同一份快照重跑分数字节一致; 都没有则用当前时钟)。"""
    mconf = cfg["momentum"]
    if now is None:
        # 用快照抓取时刻做参考'当下', 让同一份 RSS 重跑结果确定可复现(age 不随重跑时钟漂移)。
        fa = next((r.get("fetched_at") for r in rss_recs if r.get("fetched_at")), None)
        now = _parse_dt(fa) if fa else datetime.now(timezone.utc)
    rss_by_cid = {r["channel_id"]: r for r in rss_recs}

    out = []
    for r in pool:
        cid = r.get("channel_id") or ""
        name = r.get("channel_name")
        url = r.get("channel_url")
        rec = rss_by_cid.get(cid)
        if rec is None:
            out.append({"channel_id": cid, "channel_name": name, "channel_url": url,
                        "momentum_score": round(mconf["neutral_score"], 4),
                        "data_coverage": "none", "n_usable": 0,
                        "baseline_vpd": None, "top_burst_ratio": None, "top_video": None,
                        "fit_rank": None})
            continue
        m = channel_momentum(rec.get("videos", []), mconf, now)
        m.update({"channel_id": cid, "channel_name": name, "channel_url": url,
                  "fit_rank": rec.get("fit_rank")})
        out.append(m)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--pool", required=True)
    ap.add_argument("--rss", required=True, help="data/rss/<date>.jsonl")
    ap.add_argument("--out", required=True, help="momentum_scores.json 输出路径")
    args = ap.parse_args()

    cfg = load_config(args.config)
    pool = load_pool(args.pool)
    rss_recs = [json.loads(l) for l in open(args.rss)]

    scores = compute(pool, rss_recs, cfg)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(scores, f, ensure_ascii=False, indent=1)

    # 汇总打印
    from collections import Counter
    cov = Counter(s["data_coverage"] for s in scores)
    ok = [s["momentum_score"] for s in scores if s["data_coverage"] == "ok"]
    print(f"pool={len(scores)}  coverage={dict(cov)}", file=sys.stderr)
    if ok:
        ok.sort()
        print(f"momentum(ok频道) min={min(ok):.3f} median={ok[len(ok)//2]:.3f} max={max(ok):.3f}", file=sys.stderr)
    print(json.dumps({"n": len(scores), "coverage": dict(cov)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
