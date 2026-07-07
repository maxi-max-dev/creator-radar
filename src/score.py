#!/usr/bin/env python3
"""达人雷达产品路径: 对全池打分排序，产出 ranked.json，终端打印前 30 名。

可选 --momentum: 给定 momentum_scores.json 时融合起势层, 额外产出 potential 分与 potential 排名
(独立列 + 融合列)。不给则退化为纯 fit 排名(向后兼容, 老行为不变)。
起势层只进本产品路径, 绝不进 backtest 官方指标。
"""
import argparse, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from radar_lib import load_config, load_pool, score_pool, fuse_momentum


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--pool", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--momentum", default=None, help="momentum_scores.json; 给了就融合起势层")
    ap.add_argument("--rank-by", choices=["fit", "potential"], default="fit",
                    help="终端打印按哪个排(默认 fit, 保持 ranked.json 的 rank 语义不变)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    rows = load_pool(args.pool)
    print(f"pool={len(rows)}", file=sys.stderr)

    scored = score_pool(rows, cfg)
    if args.momentum:
        fuse_momentum(scored, args.momentum, cfg)

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "ranked.json"), "w") as f:
        json.dump(scored, f, ensure_ascii=False, indent=1)

    if args.momentum and args.rank_by == "potential":
        view = sorted(scored, key=lambda x: x["potential_rank"])[:30]
        print("\n--- 前 30 名 (按 potential = fit × 起势) ---")
        for s in view:
            print(f"  P#{s['potential_rank']:>4} score#{s['rank']:>4} pot={s['potential']:.4f} "
                  f"[fit={s['score']:.4f} mom={s['momentum']:.3f}/{s['momentum_cov']}] {s['channel_name']}")
    else:
        print("\n--- 前 30 名 (按 fit) ---")
        for s in scored[:30]:
            extra = (f" mom={s['momentum']:.3f}/{s['momentum_cov']} pot#{s['potential_rank']}"
                     if "momentum" in s else "")
            print(f"  #{s['rank']:>4} ({s['pct']:>5}%) score={s['score']:.4f} "
                  f"[sem={s['sem']:.3f} sweet={s['sweet']:.3f} pov={s['pov']:.3f}]{extra} "
                  f"{s['channel_name']}  subs={s['subscribers']}")


if __name__ == "__main__":
    main()
