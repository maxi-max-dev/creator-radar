#!/usr/bin/env python3
"""达人雷达产品路径: 对全池打分排序，产出 ranked.json，终端打印前 30 名。"""
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

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "ranked.json"), "w") as f:
        json.dump(scored, f, ensure_ascii=False, indent=1)

    print("\n--- 前 30 名 ---")
    for s in scored[:30]:
        print(f"  #{s['rank']:>4} ({s['pct']:>5}%) score={s['score']:.4f} "
              f"[sem={s['sem']:.3f} sweet={s['sweet']:.3f} pov={s['pov']:.3f}] "
              f"{s['channel_name']}  subs={s['subscribers']}")


if __name__ == "__main__":
    main()
