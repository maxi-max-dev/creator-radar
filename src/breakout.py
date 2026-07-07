#!/usr/bin/env python3
"""达人雷达 破圈比(breakout ratio)。白纸推演补的便宜缺口, 顺手做掉。

一句话: 近期视频的 views ÷ 频道订阅数, 取近期最大值, log 压缩到 [0,1]。
破圈比 > 1 说明这条视频的观看数超过了他的粉丝总盘, 也就是算法把它推给了大量「陌生人」,
是「平台在替他投票」的直接代理信号。

与 momentum 的区别(写进备忘, 展示层两列并列不重复):
  - momentum(起势) = 视频 views/天 相对『他自己的历史中位数』的爆发比 → 「他比自己更火了吗」。
  - breakout(破圈) = 视频 views 相对『他自己的粉丝盘(订阅数)』的倍数 → 「算法在把他推给圈外人吗」。
  两者可背离: 大频道发新片轻松破 momentum 却难破圈(粉丝基数大);
  小频道一条爆款可能远超粉丝盘(高 breakout)但相对自身历史未必是异常(它一贯高产)。
  两列一起看 = 「相对自己在加速」× 「相对粉丝盘在外溢」, 互补。

输入: data/rss/<date>.jsonl(视频 views) + data/pool/creator_pool.jsonl(subscribers)。
产出: data/runs/trends-v1/breakout_scores.json。

只用近期窗口内的视频(与 trends 同口径, 默认 14 天), 避免历史爆款把「当前是否破圈」污染。
订阅数缺失/为 0 的频道给 data_coverage 标记, 不冤杀(标 no_subs, breakout=None)。
纯标准库。
"""
import argparse, json, math, os, sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collect_trends import _parse_dt, dedup_rss  # noqa: E402

DEFAULTS = {
    "recent_window_days": 14,   # 只看近 N 天视频是否破圈(与 trends 一致)
    "breakout_cap": 4.0,        # 破圈倍数封顶: 4x 粉丝盘已是强破圈, log2 后 =2 个单位归一
    "log_base": 2.0,
    "include_shorts": True,     # shorts 也算破圈(短视频破圈是真信号, 不像 momentum 会污染基线)
    "min_recent_videos": 1,     # 近期至少 1 条视频才给分
}


def cfg_get(cfg, key):
    tconf = (cfg or {}).get("trends", {}) if cfg else {}
    return tconf.get(key, DEFAULTS[key])


def channel_breakout(videos, subs, cfg, now):
    """单频道破圈比。返回 dict(score/coverage/证据)。"""
    win = cfg_get(cfg, "recent_window_days")
    cap = cfg_get(cfg, "breakout_cap")
    log_base = cfg_get(cfg, "log_base")
    incl_short = cfg_get(cfg, "include_shorts")
    min_recent = cfg_get(cfg, "min_recent_videos")

    if not subs or subs <= 0:
        return {"breakout_score": None, "data_coverage": "no_subs",
                "subs": subs, "top_ratio": None, "top_video": None, "n_recent": 0}

    recent = []
    for v in videos:
        dt = _parse_dt(v.get("published"))
        views = v.get("view_count")
        if dt is None or views is None:
            continue
        if (now - dt).total_seconds() / 86400.0 > win:
            continue
        if v.get("is_short") and not incl_short:
            continue
        recent.append(v)

    if len(recent) < min_recent:
        return {"breakout_score": 0.0, "data_coverage": "stale",
                "subs": subs, "top_ratio": None, "top_video": None, "n_recent": len(recent)}

    log_cap = math.log(cap, log_base)
    best_ratio = 0.0
    best_v = None
    for v in recent:
        ratio = v["view_count"] / subs
        if ratio > best_ratio:
            best_ratio = ratio
            best_v = v
    # log 压缩到 [0,1]: ratio<=1(没破圈)→0; ratio>=cap → 1
    if best_ratio <= 1.0:
        score = 0.0
    else:
        score = min(math.log(best_ratio, log_base), log_cap) / log_cap

    ev = None
    if best_v is not None:
        ev = {"title": best_v.get("title", ""), "views": best_v.get("view_count"),
              "is_short": best_v.get("is_short", False), "video_id": best_v.get("video_id")}
    return {"breakout_score": round(score, 4), "data_coverage": "ok",
            "subs": subs, "top_ratio": round(best_ratio, 3), "top_video": ev,
            "n_recent": len(recent)}


def compute(pool, rss_recs, cfg, now=None):
    if now is None:
        fa = next((r.get("fetched_at") for r in rss_recs if r.get("fetched_at")), None)
        now = _parse_dt(fa) if fa else datetime.now(timezone.utc)
    subs_by_cid = {r.get("channel_id"): r.get("subscribers") for r in pool}
    out = []
    for r in rss_recs:
        cid = r.get("channel_id")
        subs = subs_by_cid.get(cid)
        b = channel_breakout(r.get("videos", []), subs, cfg, now)
        b.update({"channel_id": cid, "channel_name": r.get("channel_name"),
                  "channel_url": r.get("channel_url"), "fit_rank": r.get("fit_rank")})
        out.append(b)
    return out


def main():
    ap = argparse.ArgumentParser(description="破圈比: 近期 views ÷ 订阅数, log 归一")
    ap.add_argument("--pool", required=True)
    ap.add_argument("--rss", required=True, help="data/rss/<date>.jsonl")
    ap.add_argument("--config", help="可选 trends 节覆盖 DEFAULTS")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    cfg = None
    if args.config and os.path.exists(args.config):
        try:
            cfg = json.load(open(args.config))
        except (json.JSONDecodeError, ValueError):
            cfg = None
    pool = [json.loads(l) for l in open(args.pool)]
    rss_recs = []
    with open(args.rss) as f:
        for line in f:
            try:
                rss_recs.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue
    rss_recs = dedup_rss(rss_recs)

    scores = compute(pool, rss_recs, cfg)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    json.dump(scores, open(args.out, "w"), ensure_ascii=False, indent=1)

    from collections import Counter
    cov = Counter(s["data_coverage"] for s in scores)
    ok = sorted(s["breakout_score"] for s in scores if s.get("data_coverage") == "ok")
    broke = sum(1 for s in scores if (s.get("top_ratio") or 0) > 1.0)
    print(f"rss={len(scores)} coverage={dict(cov)} broke_circle(ratio>1)={broke}", file=sys.stderr)
    if ok:
        print(f"breakout(ok) min={ok[0]:.3f} median={ok[len(ok)//2]:.3f} max={ok[-1]:.3f}", file=sys.stderr)
    print(json.dumps({"n": len(scores), "coverage": dict(cov), "broke_circle": broke}, ensure_ascii=False))


if __name__ == "__main__":
    main()
