#!/usr/bin/env python3
"""达人雷达验证路径: 用池内正例标注做盲测回测，产出 backtest_scores.json + backtest_metrics.json。
标签泄漏防护: 打分前从所有文本中剔除品牌相关 token (见 radar_lib.build_doc)。
"""
import argparse, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from radar_lib import load_config, load_pool, score_pool


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--pool", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    cfg = load_config(args.config)
    rows = load_pool(args.pool)
    print(f"pool={len(rows)}", file=sys.stderr)

    scored = score_pool(rows, cfg)
    n = len(scored)

    def recall_at(frac, subset):
        k = int(n * frac)
        top = scored[:k]
        hit = [s for s in top if s["is_positive"] and (subset is None or s["positive_source"] == subset)]
        total = sum(1 for s in scored if s["is_positive"] and (subset is None or s["positive_source"] == subset))
        return len(hit), total

    metrics = {}
    for frac in (0.05, 0.10, 0.20):
        for sub, tag in ((None, "all"), ("midtail", "midtail")):
            h, t = recall_at(frac, sub)
            metrics[f"recall@top{int(frac*100)}%_{tag}"] = f"{h}/{t} = {h/t*100:.0f}%"
    pos_ranks = [s["pct"] for s in scored if s["is_positive"]]
    mid_ranks = [s["pct"] for s in scored if s["positive_source"] == "midtail"]
    metrics["median_pct_all"] = f"{sorted(pos_ranks)[len(pos_ranks)//2]:.1f}%"
    metrics["median_pct_midtail"] = f"{sorted(mid_ranks)[len(mid_ranks)//2]:.1f}%"
    metrics["chance_baseline"] = "top5%=5%, top10%=10%, top20%=20%"

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "backtest_scores.json"), "w") as f:
        json.dump(scored, f, ensure_ascii=False, indent=1)
    with open(os.path.join(args.out, "backtest_metrics.json"), "w") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=1)

    print(json.dumps(metrics, ensure_ascii=False, indent=1))
    print("\n--- 正例排名明细(前=好) ---")
    for s in scored:
        if s["is_positive"]:
            print(f"  #{s['rank']:>4} ({s['pct']:>5}%) [{s['positive_source']}] {s['channel_name']}  subs={s['subscribers']}")


if __name__ == "__main__":
    main()
