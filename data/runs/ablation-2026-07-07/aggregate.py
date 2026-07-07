#!/usr/bin/env python3
"""从每个变体的 backtest_scores.json 重算指标(全精度)，与 fresh-baseline 比 delta，
按判读规则给每个零件判决，产出 _summary.json + 控制台汇总表。
判读规则(对比 fresh-baseline):
  召回掉 >=2 个百分点 或 中位百分位恶化 >=2pp   -> 承重(load-bearing)
  三指标变化都 <0.5pp                          -> 白重量候选(white-weight)
  介于两者之间                                  -> 边缘(marginal)
指标定义(与 backtest.py 完全一致):
  recall@topX% = rank<=floor(n*X) 的正例数 / 该子集正例总数
  median_pct   = 正例 pct 的中位数(取 sorted[len//2]，与 backtest.py 同)
中位百分位越低越好，所以"恶化"=median 变大。
"""
import json, os, math

ADIR = "/Users/max/Documents/creator-radar/data/runs/ablation-2026-07-07"
VARIANTS = [
    "fresh-baseline", "no-sweet", "no-markers", "semantic-only",
    "theme-drop-pov_native", "theme-drop-adventure_bold", "theme-drop-authentic_vlog",
    "theme-drop-journey_narrative", "theme-drop-gear_native", "theme-drop-vertical_craft",
]


def metrics_from_scores(path):
    s = json.load(open(path))
    n = len(s)
    pos = [r for r in s if r["is_positive"]]
    mid = [r for r in pos if r["positive_source"] == "midtail"]

    def recall(frac, subset):
        k = int(n * frac)
        pool = pos if subset == "all" else mid
        hit = sum(1 for r in pool if r["rank"] <= k)
        return hit, len(pool)

    def median_pct(subset):
        pool = pos if subset == "all" else mid
        vals = sorted(r["pct"] for r in pool)
        return vals[len(vals) // 2]

    m = {}
    for frac in (0.05, 0.10):
        for sub in ("all", "midtail"):
            h, t = recall(frac, sub)
            m[f"r{int(frac*100)}_{sub}"] = {"hit": h, "tot": t, "pct": round(h / t * 100, 1)}
    m["med_all"] = round(median_pct("all"), 1)
    m["med_midtail"] = round(median_pct("midtail"), 1)
    m["n"] = n
    return m


def verdict(delta):
    """delta 是本变体相对 fresh-baseline 的差值字典(见下)。返回判决字符串。
    d_r5_all/d_r10_all/d_r5_mid/d_r10_mid: 召回百分点差(本-基, 负=掉)
    d_med_all/d_med_mid: 中位百分位差(本-基, 正=恶化)
    """
    recall_drops = [-delta["d_r5_all"], -delta["d_r10_all"], -delta["d_r5_mid"], -delta["d_r10_mid"]]
    med_worse = [delta["d_med_all"], delta["d_med_mid"]]
    max_recall_drop = max(recall_drops)      # 最严重的召回下降(正数=掉了)
    max_med_worse = max(med_worse)           # 最严重的中位恶化(正数=变差)
    # 承重: 任一召回掉>=2pp 或 任一中位恶化>=2pp
    if max_recall_drop >= 2.0 or max_med_worse >= 2.0:
        return "承重"
    # 白重量: 所有变化绝对值 <0.5pp
    all_moves = [abs(delta[k]) for k in ("d_r5_all", "d_r10_all", "d_r5_mid", "d_r10_mid", "d_med_all", "d_med_mid")]
    if max(all_moves) < 0.5:
        return "白重量"
    return "边缘"


rows = {}
for v in VARIANTS:
    p = os.path.join(ADIR, v, "backtest_scores.json")
    if not os.path.exists(p):
        print(f"[MISSING] {v}: {p} 不存在，可能该变体没跑成")
        continue
    rows[v] = metrics_from_scores(p)

base = rows["fresh-baseline"]
summary = {"base": base, "variants": {}}

for v in VARIANTS:
    if v not in rows:
        continue
    m = rows[v]
    if v == "fresh-baseline":
        summary["variants"][v] = {"metrics": m, "verdict": "(对照组)"}
        continue
    delta = {
        "d_r5_all": round(m["r5_all"]["pct"] - base["r5_all"]["pct"], 1),
        "d_r10_all": round(m["r10_all"]["pct"] - base["r10_all"]["pct"], 1),
        "d_r5_mid": round(m["r5_midtail"]["pct"] - base["r5_midtail"]["pct"], 1),
        "d_r10_mid": round(m["r10_midtail"]["pct"] - base["r10_midtail"]["pct"], 1),
        "d_med_all": round(m["med_all"] - base["med_all"], 1),
        "d_med_mid": round(m["med_midtail"] - base["med_midtail"], 1),
    }
    summary["variants"][v] = {"metrics": m, "delta": delta, "verdict": verdict(delta)}

json.dump(summary, open(os.path.join(ADIR, "_summary.json"), "w"), ensure_ascii=False, indent=2)

# 控制台汇总表
hdr = f"{'变体':28s} {'r5_all':>10s} {'r10_all':>10s} {'r5_mid':>10s} {'r10_mid':>10s} {'med_all':>8s} {'med_mid':>8s}  判决"
print(hdr)
print("-" * len(hdr))
for v in VARIANTS:
    if v not in rows:
        continue
    m = rows[v]
    def cell(d):
        return f"{d['hit']}/{d['tot']}={d['pct']:.0f}%"
    line = (f"{v:28s} {cell(m['r5_all']):>10s} {cell(m['r10_all']):>10s} "
            f"{cell(m['r5_midtail']):>10s} {cell(m['r10_midtail']):>10s} "
            f"{m['med_all']:>7.1f}% {m['med_midtail']:>7.1f}%  {summary['variants'][v]['verdict']}")
    print(line)

print("\n_summary.json 已写出")
