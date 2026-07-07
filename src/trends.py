#!/usr/bin/env python3
"""达人雷达 第四层「对的浪」打分器(trend scoring)。

命题: 在特定领域发掘下一个爆火的人, 一个信号是「他在乘一个正在升温的浪」。
铁律(Max 已认可的纠偏): 趋势是放大器不是保票。
  浪层信号 = 浪在涨(该词升温) × 进得早(他早于大众进场) × 他的浪上内容跑赢他自己的基线。
  晚进场蹭热点是负信号不是正信号 → 打低分甚至标记 trend_chaser。

输入:
  - data/trends/<date>.jsonl (collect_trends.py 产出; 池内浪权重最高, 外部源做佐证)
  - data/pool/creator_pool.jsonl (vertical)
  - data/rss/<date>.jsonl (视频级 views/天, 复用 momentum 同思路, 只看命中浪词的视频)
  - data/tags/ (如有则用作子题材标签; 无则用视频标题关键词)

产出: data/runs/trends-v1/trend_scores.json + 升温词表 rising_terms.json

trend_score 三因子(相乘, 选型理由见下):
  A 匹配度 match ∈ [0,1]: 该频道近期视频命中「本垂类升温词表」的强度(命中词的升温权重之和, 归一)。
  B 进场早晚 early ∈ [0,1]: 命中的升温词在池内首次出现日期的分位, 越早越高。
  C 浪上表现 onwave ∈ [0,1]: 只看命中浪词的近期视频, 其 views/天 相对该频道自身基线的爆发比
                             (log 压缩+封顶归一, 与 momentum 同思路)。
  trend_score = A × B × C(乘性)。
  选乘性: 三者缺一浪层信号就不成立——
    命中但晚进场且平庸(A 高 B 低 C 低) → 分自然趋零, 且若 A>0 而 (B 低 且 C 低) 则打 trend_chaser 标记;
    没命中任何浪词(A=0) → trend_score=0, 不是负分(他只是没在任何已知浪上, 不代表差)。
  数据不足频道给中性分 + data_coverage 标记, 绝不冤杀。

外部源(google/bilibili/reddit)当前只做「佐证」: 若某垂类升温词也在外部源标题里出现, 给该词
  corroborated=True 徽章(小幅加权), 但不主导评分(它们是大盘/社区不是垂类精调)。

铁律: 本打分器只喂产品路径(展示层), 绝不进 backtest 官方指标(学 momentum.py)。

用法:
  python3 src/trends.py \
    --trends data/trends/2026-07-07.jsonl \
    --pool data/pool/creator_pool.jsonl \
    --rss data/rss/2026-07-07.jsonl \
    --out-scores data/runs/trends-v1/trend_scores.json \
    --out-terms data/runs/trends-v1/rising_terms.json
"""
import argparse, json, math, os, sys
from collections import defaultdict
from datetime import datetime, timezone
from statistics import median

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 复用采集器里的分词/停用词/tag 加载, 保证浪词口径一致
from collect_trends import tokenize, load_tags, load_pool_verticals, _parse_dt, dedup_rss  # noqa: E402

# ---- 缺省旋钮(本工具唯一配置源) ----
DEFAULTS = {
    "recent_window_days": 14,        # 与采集器窗口一致
    "min_days": 2.0,                 # views/天 分母地板
    "min_videos_for_baseline": 4,    # 频道历史视频少于此 → 中性分 + low coverage
    "neutral_score": 0.3,            # 数据不足中性分(与 momentum 一致口径)
    "burst_log_base": 2.0,
    "burst_cap": 8.0,                # 8x 起势封顶
    "short_weight": 0.5,
    # 匹配度归一: 命中升温权重之和达到 match_saturation 即视为满命中
    "match_saturation": 2.0,
    # 进场早晚: 用词在池内 first_seen 的分位(越早越高); 无 first_seen 给中性
    "early_neutral": 0.5,
    # trend_chaser 判定: 命中了浪(A>=chaser_min_match) 但 早 与 表现 双低
    "chaser_min_match": 0.15,
    "chaser_early_max": 0.35,        # 进场分位低于此 = 晚进场
    "chaser_onwave_max": 0.15,       # 浪上表现低于此 = 平庸/跑不赢自己
    # 外部源佐证徽章加权(温和, 不主导)
    "corroboration_bonus": 0.15,     # corroborated 词的升温权重 ×(1+bonus)
    "top_terms_per_vertical": 40,
}


def cfg_get(cfg, key):
    tconf = (cfg or {}).get("trends", {}) if cfg else {}
    return tconf.get(key, DEFAULTS[key])


# =========================================================================
# 升温词表: 从 data/trends 提炼(池内浪主导, 外部源佐证)
# =========================================================================

def build_rising_terms(trend_records, cfg):
    """从 trends jsonl 提炼每垂类升温词表。
    - 池内浪(source=pool_wave)直接给词 + 升温倍数(metric)+ first_seen(extra)。
    - 同一词同一天可能多条快照(append), 取升温倍数最高的一条。
    - 外部源(google/bili/reddit)聚合成一个「外部热词集合」(小写标题分词), 用于给池内词打
      corroborated 徽章(该词出现在外部热词里)。
    返回: {vertical: {term: {heat, first_seen, hits, corroborated, evidence}}}
    """
    top_n = cfg_get(cfg, "top_terms_per_vertical")
    # 1) 外部热词集合(所有外部源标题分词的并集; 只用于佐证)
    external_tokens = set()
    for r in trend_records:
        if r.get("source") == "pool_wave":
            continue
        title = r.get("term_or_title", "")
        for t in tokenize(title, set()):
            external_tokens.add(t)

    # 2) 池内浪词(按 vertical 聚合, 同词取最高 heat 的快照)
    by_vert = defaultdict(dict)  # vert -> term -> best record
    for r in trend_records:
        if r.get("source") != "pool_wave":
            continue
        vert = r.get("region_or_scope")
        term = r.get("term_or_title")
        heat = r.get("metric")
        if not vert or not term or heat is None:
            continue
        ex = r.get("extra", {}) or {}
        cur = by_vert[vert].get(term)
        if cur is None or heat > cur["heat"]:
            by_vert[vert][term] = {
                "heat": float(heat),
                "first_seen": ex.get("term_first_seen"),
                "hits": ex.get("hits_recent"),
                "baseline_vpd": ex.get("baseline_vpd"),
                "evidence": ex.get("evidence"),
            }

    # 3) 徽章 + first_seen 分位(进场早晚基础): 每垂类内按 first_seen 排序算分位
    out = {}
    for vert, terms in by_vert.items():
        # first_seen 分位: 越早(日期越小)分位越高
        dated = [(t, _parse_dt(v["first_seen"])) for t, v in terms.items() if v.get("first_seen")]
        dated.sort(key=lambda z: z[1])  # 早的在前
        pct = {}
        n = len(dated)
        for i, (t, _) in enumerate(dated):
            # 最早的一批分位接近 1, 最晚接近 0
            pct[t] = 1.0 - (i / max(n - 1, 1)) if n > 1 else cfg_get(cfg, "early_neutral")
        ranked = sorted(terms.items(), key=lambda kv: -kv[1]["heat"])[:top_n]
        vd = {}
        for term, v in ranked:
            v = dict(v)
            v["corroborated"] = term in external_tokens
            v["early_pct"] = pct.get(term, cfg_get(cfg, "early_neutral"))
            vd[term] = v
        out[vert] = vd
    return out


# =========================================================================
# 频道级 trend_score
# =========================================================================

def _channel_recent_videos(videos, now, min_days):
    """标准化频道视频 -> [{vpd, age_days, views, title, tokens, weight}]。"""
    out = []
    for v in videos:
        dt = _parse_dt(v.get("published"))
        views = v.get("view_count")
        if dt is None or views is None:
            continue
        age = (now - dt).total_seconds() / 86400.0
        vpd = views / max(age, min_days)
        out.append({
            "vpd": vpd, "age_days": age, "views": views, "title": v.get("title", ""),
            "video_id": v.get("video_id"), "is_short": v.get("is_short", False),
        })
    return out


def channel_trend(videos, vert_terms, cfg, now, tags):
    """算单频道 trend_score。vert_terms = 该频道所属垂类的升温词表(可能为空)。"""
    win = cfg_get(cfg, "recent_window_days")
    min_days = cfg_get(cfg, "min_days")
    min_vids = cfg_get(cfg, "min_videos_for_baseline")
    neutral = cfg_get(cfg, "neutral_score")
    log_base = cfg_get(cfg, "burst_log_base")
    cap = cfg_get(cfg, "burst_cap")
    short_w = cfg_get(cfg, "short_weight")
    sat = cfg_get(cfg, "match_saturation")
    corr_bonus = cfg_get(cfg, "corroboration_bonus")

    items = _channel_recent_videos(videos, now, min_days)
    n = len(items)
    base = {"trend_score": round(neutral, 4), "match": 0.0, "early": 0.0, "onwave": 0.0,
            "trend_chaser": False, "n_usable": n, "hit_terms": [], "top_evidence": None}
    if n < min_vids:
        base["data_coverage"] = "low"
        return base
    if not vert_terms:
        # 该垂类没有任何升温词(可能外部/池内都没检出) → 没有可乘的浪, 给中性分
        base["data_coverage"] = "no_terms"
        base["trend_score"] = round(neutral, 4)
        return base

    baseline = median([it["vpd"] * (short_w if it["is_short"] else 1.0) for it in items]) or 0.0
    if baseline <= 0:
        base["data_coverage"] = "low"
        return base

    # 近期视频(窗口内)按命中升温词聚合
    recent = [it for it in items if it["age_days"] <= win]
    log_cap = math.log(cap, log_base)

    hit_weight_sum = 0.0            # A 的原料: 命中升温词权重(heat, 佐证词加成)之和
    hit_terms = set()
    onwave_best = 0.0              # C: 命中浪词的近期视频里, 相对自身基线爆发比的最大(归一)
    early_weighted_num = 0.0       # B: 命中词的 early_pct 按 heat 加权
    early_weighted_den = 0.0
    best_ev = None

    for it in recent:
        vid = it["video_id"]
        toks = set(str(t).lower() for t in (tags.get(vid) if vid in tags else tokenize(it["title"], set())))
        matched = toks & set(vert_terms.keys())
        if not matched:
            continue
        # 该视频命中的最高 heat 词决定它的浪权重
        for term in matched:
            info = vert_terms[term]
            w = info["heat"] * (1 + corr_bonus if info.get("corroborated") else 1.0)
            hit_weight_sum += w
            hit_terms.add(term)
            early_weighted_num += info.get("early_pct", 0.5) * info["heat"]
            early_weighted_den += info["heat"]
        # 这条命中浪词的视频, 算它相对频道基线的爆发比 → onwave
        w_v = short_w if it["is_short"] else 1.0
        ratio = (it["vpd"] * w_v) / baseline
        burst01 = 0.0 if ratio <= 1.0 else min(math.log(ratio, log_base), log_cap) / log_cap
        if burst01 > onwave_best:
            onwave_best = burst01
            best_ev = {"title": it["title"], "views": it["views"],
                       "age_days": round(it["age_days"], 1), "burst_ratio": round(ratio, 2),
                       "matched_terms": sorted(matched), "video_id": vid}

    match = min(hit_weight_sum / sat, 1.0) if sat > 0 else 0.0
    early = (early_weighted_num / early_weighted_den) if early_weighted_den > 0 else 0.0
    onwave = onwave_best
    score = match * early * onwave

    # trend_chaser: 命中了浪但 早+表现 双低 = 晚进场蹭热点
    chaser = (match >= cfg_get(cfg, "chaser_min_match")
              and early <= cfg_get(cfg, "chaser_early_max")
              and onwave <= cfg_get(cfg, "chaser_onwave_max"))

    return {
        "trend_score": round(score, 4),
        "match": round(match, 4), "early": round(early, 4), "onwave": round(onwave, 4),
        "trend_chaser": bool(chaser),
        "data_coverage": "ok",
        "n_usable": n, "n_recent": len(recent),
        "hit_terms": sorted(hit_terms),
        "baseline_vpd": round(baseline, 1),
        "top_evidence": best_ev,
    }


def compute(trend_records, pool_verts, rss_recs, cfg, now=None):
    """全池算 trend_score。返回 (scores_list, rising_terms)。"""
    if now is None:
        fa = next((r.get("fetched_at") for r in rss_recs if r.get("fetched_at")), None)
        now = _parse_dt(fa) if fa else datetime.now(timezone.utc)
    rising = build_rising_terms(trend_records, cfg)
    tags = load_tags()
    rss_by_cid = {r.get("channel_id"): r for r in rss_recs}

    out = []
    for cid, r in rss_by_cid.items():
        vert = pool_verts.get(cid)
        vert_terms = rising.get(vert, {}) if vert else {}
        m = channel_trend(r.get("videos", []), vert_terms, cfg, now, tags)
        m.update({"channel_id": cid, "channel_name": r.get("channel_name"),
                  "channel_url": r.get("channel_url"), "vertical": vert,
                  "fit_rank": r.get("fit_rank")})
        out.append(m)
    # 没有 RSS 的池内频道也补一行中性(展示层不冤杀)
    have = set(rss_by_cid.keys())
    for cid, vert in pool_verts.items():
        if cid in have:
            continue
        out.append({"channel_id": cid, "channel_name": None, "channel_url": None,
                    "vertical": vert, "fit_rank": None,
                    "trend_score": round(cfg_get(cfg, "neutral_score"), 4),
                    "match": 0.0, "early": 0.0, "onwave": 0.0, "trend_chaser": False,
                    "data_coverage": "none", "n_usable": 0, "hit_terms": [], "top_evidence": None})
    return out, rising


def main():
    ap = argparse.ArgumentParser(description="第四层浪打分(match×early×onwave, trend_chaser 标记)")
    ap.add_argument("--trends", required=True, help="data/trends/<date>.jsonl")
    ap.add_argument("--pool", required=True)
    ap.add_argument("--rss", required=True, help="data/rss/<date>.jsonl")
    ap.add_argument("--config", help="可选 trends 节覆盖 DEFAULTS")
    ap.add_argument("--out-scores", required=True)
    ap.add_argument("--out-terms", required=True)
    args = ap.parse_args()

    cfg = None
    if args.config and os.path.exists(args.config):
        try:
            cfg = json.load(open(args.config))
        except (json.JSONDecodeError, ValueError):
            cfg = None

    trend_records = []
    with open(args.trends) as f:
        for line in f:
            try:
                trend_records.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue
    pool_verts = load_pool_verticals()
    rss_recs = []
    with open(args.rss) as f:
        for line in f:
            try:
                rss_recs.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue
    rss_recs = dedup_rss(rss_recs)

    scores, rising = compute(trend_records, pool_verts, rss_recs, cfg)
    for p in (args.out_scores, args.out_terms):
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    json.dump(scores, open(args.out_scores, "w"), ensure_ascii=False, indent=1)
    json.dump(rising, open(args.out_terms, "w"), ensure_ascii=False, indent=1)

    from collections import Counter
    cov = Counter(s["data_coverage"] for s in scores)
    ok = sorted(s["trend_score"] for s in scores if s.get("data_coverage") == "ok")
    chasers = sum(1 for s in scores if s.get("trend_chaser"))
    print(f"pool={len(scores)} coverage={dict(cov)} chasers={chasers}", file=sys.stderr)
    if ok:
        print(f"trend(ok) min={ok[0]:.3f} median={ok[len(ok)//2]:.3f} max={ok[-1]:.3f}", file=sys.stderr)
    print(json.dumps({"n": len(scores), "coverage": dict(cov), "trend_chasers": chasers,
                      "verticals_with_terms": {k: len(v) for k, v in rising.items()}},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
