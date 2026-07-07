#!/usr/bin/env python3
"""达人雷达验证路径: 用池内正例标注做盲测回测，产出 backtest_scores.json + backtest_metrics.json。
标签泄漏防护: 打分前从所有文本中剔除品牌相关 token (见 radar_lib.build_doc)。

子集口径说明(2026-07-07 W2 修正):
  正例带两类标签, 含义正交, 不可混用:
    - positive_source(来源标签): 这个正例是怎么被挖出来的。
        "star"    = 名册组(roster): 影石官方合作名册/大使名单里的公开成员。
        "midtail" = 声明组(disclosure): 从视频赞助声明/带货口径里挖出的合作方。
      来源 ≠ 规模: 名册组里有 1,540 粉的小号, 声明组里有 125 万粉的大号。
      故来源标签**只描述发现渠道**, 不参与任何规模口径的召回统计。
    - 规模切分(size tier): 按订阅数分档, 从正例订阅分布的自然断点取阈值
        (<10万 / 10万~100万 / >100万; 断点位于 55k→73k 之内与 591k→125万 之间, 无正例卡边界)。
      这才是回答「引擎会不会只认大号」的正确切分。
  官方 headline 指标 = 全部正例(26)的召回, 与子集口径无关, 永不受本次改名影响。
"""
import argparse, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from radar_lib import load_config, load_pool, score_pool

# 来源标签(positive_source 原始值) → 人话来源语义。原始值冻结在考试池里(改动会破坏 md5),
# 故只在展示层做映射, 不回写数据。
SOURCE_LABELS = {
    "star": "名册组(roster)",
    "midtail": "声明组(disclosure)",
}

# 规模档: 订阅数阈值来自正例分布自然断点(见模块 docstring)。
SIZE_TIERS = [
    ("small", "小规模<10万", lambda s: (s or 0) > 0 and s < 100_000),
    ("mid", "中规模10万~100万", lambda s: (s or 0) >= 100_000 and s <= 1_000_000),
    ("giant", "巨星>100万", lambda s: (s or 0) > 1_000_000),
]


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

    def recall_at(frac, pred):
        """pred(row)->bool 圈定子集; 官方 headline 用 pred=全部正例。"""
        k = int(n * frac)
        top = scored[:k]
        hit = [s for s in top if s["is_positive"] and pred(s)]
        total = sum(1 for s in scored if s["is_positive"] and pred(s))
        return len(hit), total

    def median_pct(pred):
        ranks = sorted(s["pct"] for s in scored if s["is_positive"] and pred(s))
        return ranks[len(ranks) // 2] if ranks else None

    metrics = {}
    # 官方 headline = 全部正例(26), 口径与叫法无关, 永远第一顺位。
    for frac in (0.05, 0.10, 0.20):
        h, t = recall_at(frac, lambda s: True)
        metrics[f"recall@top{int(frac*100)}%_all"] = f"{h}/{t} = {h/t*100:.0f}%"
    metrics["median_pct_all"] = f"{median_pct(lambda s: True):.1f}%"

    # 规模切分(回答「会不会只认大号」)。来源切分不进召回统计, 只在明细里标注。
    for key, label, pred in SIZE_TIERS:
        for frac in (0.05, 0.10, 0.20):
            h, t = recall_at(frac, lambda s, p=pred: p(s["subscribers"]))
            metrics[f"recall@top{int(frac*100)}%_size_{key}"] = (
                f"{h}/{t} = {h/t*100:.0f}%" if t else "0/0 = n/a"
            )
        m = median_pct(lambda s, p=pred: p(s["subscribers"]))
        metrics[f"median_pct_size_{key}"] = f"{m:.1f}%" if m is not None else "n/a"

    metrics["chance_baseline"] = "top5%=5%, top10%=10%, top20%=20%"

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "backtest_scores.json"), "w") as f:
        json.dump(scored, f, ensure_ascii=False, indent=1)
    with open(os.path.join(args.out, "backtest_metrics.json"), "w") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=1)

    print(json.dumps(metrics, ensure_ascii=False, indent=1))
    print("\n--- 正例排名明细(前=好); [来源组] size档 ---")
    for s in scored:
        if s["is_positive"]:
            src = SOURCE_LABELS.get(s["positive_source"], s["positive_source"] or "?")
            subs = s["subscribers"] or 0
            tier = next((lbl for _k, lbl, p in SIZE_TIERS if p(subs)), "?")
            print(f"  #{s['rank']:>4} ({s['pct']:>5}%) [{src}] <{tier}> {s['channel_name']}  subs={subs}")


if __name__ == "__main__":
    main()
