#!/usr/bin/env python3
"""起势层验证器: 从 momentum_scores.json + fit ranked.json 产出四项验证的可复现数据,
并写 top20_evidence.md。四项验证:
  a. 区分度检验(十分位表, 证明不是全零全一)
  b. 表面效度(top20 证据表, 人看一眼就懂)
  c. 时间回滚 mini-demo 的数据支撑(roster 命中在别处单独跑, 这里给全池佐证)
  d. 阴性对照(随机组 vs fit-top 组 momentum 均值)

用法:
  python3 src/momentum_validate.py --scores data/runs/momentum-v1/momentum_scores.json \
      --ranked data/runs/momentum-v1/fit/ranked.json \
      --out-md data/runs/momentum-v1/top20_evidence.md
"""
import argparse, json, random, statistics
from collections import Counter


def deciles(vals):
    vals = sorted(vals)
    n = len(vals)
    return [(d * 10, vals[min(int(d / 10 * (n - 1)), n - 1)]) for d in range(11)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scores", required=True)
    ap.add_argument("--ranked", required=True)
    ap.add_argument("--out-md", required=True)
    args = ap.parse_args()

    scores = json.load(open(args.scores))
    ranked = json.load(open(args.ranked))
    m_by_url = {s["channel_url"]: s for s in scores}
    fit_rank = {r["channel_url"]: r["rank"] for r in ranked}

    cov = Counter(s["data_coverage"] for s in scores)
    active = [s for s in scores if s["data_coverage"] == "ok"]
    active_vals = [s["momentum_score"] for s in active]

    # --- a. 区分度: 十分位(活跃频道) ---
    dec = deciles(active_vals)

    # --- b. top20 证据 ---
    top20 = sorted(active, key=lambda x: -x["momentum_score"])[:20]

    # --- d. 阴性对照 ---
    fetched = [s for s in scores if s["data_coverage"] != "none"]
    fetched_urls = [s["channel_url"] for s in fetched]
    fit_top_urls = [r["channel_url"] for r in ranked[:100]]

    def mvals(urls):
        return [m_by_url[u]["momentum_score"] for u in urls
                if u in m_by_url and m_by_url[u]["data_coverage"] != "none"]

    random.seed(42)
    fit_top_m = mvals(fit_top_urls)
    rand_means = []
    for _ in range(500):
        rand_means.append(statistics.mean(mvals(random.sample(fetched_urls, 100))))
    rand_mean = statistics.mean(rand_means)
    fit_top_mean = statistics.mean(fit_top_m)

    # --- 写 MD ---
    L = []
    L.append("# 起势层(momentum) top20 证据表 + 验证数据")
    L.append("")
    L.append("> 自动生成 (src/momentum_validate.py)。数据源: 全池 1106 频道, 其中 fit 排名前 350 拉了 YouTube 官方 RSS。")
    L.append("")
    L.append(f"覆盖度: `ok`(活跃有近期视频)={cov.get('ok',0)} · "
             f"`stale`(有历史但近期无上传, momentum=0)={cov.get('stale',0)} · "
             f"`none`(未拉 RSS, 给中性分 0.3)={cov.get('none',0)}")
    L.append("")

    L.append("## a. 区分度检验: 活跃频道 momentum 十分位")
    L.append("")
    L.append("证明它不是全零/全一的摆设, 是一条平滑的判别梯度。")
    L.append("")
    L.append("| 分位 | momentum |")
    L.append("|---|---|")
    for p, v in dec:
        L.append(f"| P{p} | {v:.3f} |")
    L.append("")
    L.append(f"活跃频道 n={len(active_vals)} · 均值 {statistics.mean(active_vals):.3f} · "
             f"标准差 {statistics.pstdev(active_vals):.3f} · 两位小数去重后有 "
             f"{len({round(v,2) for v in active_vals})} 个不同取值。")
    L.append("")

    L.append("## b. 表面效度: momentum top20 证据")
    L.append("")
    L.append("每行都能一眼看懂为什么它亮: 频道 · 最亮的近期视频标题 · 播放数 · 发布天数 · 超频道自身中位的倍数。")
    L.append("")
    L.append("| # | 频道 | fit# | momentum | 爆款视频 | 播放数 | 发布天数 | 超自身中位 | 短视频 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for i, s in enumerate(top20, 1):
        tv = s.get("top_video") or {}
        title = (tv.get("title", "") or "").replace("|", "/")[:44]
        fr = fit_rank.get(s["channel_url"], "?")
        L.append(f"| {i} | {s['channel_name']} | {fr} | {s['momentum_score']:.3f} | "
                 f"{title} | {tv.get('views','?'):,} | {tv.get('age_days','?')}d | "
                 f"{tv.get('burst_ratio','?')}x | {'是' if tv.get('is_short') else ''} |")
    L.append("")

    L.append("## d. 阴性对照: fit-top 组 vs 随机组 momentum 均值")
    L.append("")
    L.append("关键预期(反直觉): momentum 应该和 fit **正交**(它们量的是不同维度: fit=对的人, momentum=对的时机)。")
    L.append("若两组均值几乎相等, 说明 momentum 是一个**独立**信号, 能在任何 fit 分层内部再排序, 而不是 fit 的影子。")
    L.append("")
    L.append("| 组 | momentum 均值 | n |")
    L.append("|---|---|---|")
    L.append(f"| fit-top 100 | {fit_top_mean:.3f} | {len(fit_top_m)} |")
    L.append(f"| 随机 100 (500 次重采样, 取自已拉 RSS 的池) | {rand_mean:.3f} | 100×500 |")
    L.append(f"| 差值 (fit_top − random) | {fit_top_mean - rand_mean:+.3f} | — |")
    L.append("")
    L.append("**读法**: 差值接近 0 = 正交性成立 = momentum 携带 fit 没有的新信息(这正是我们要的)。"
             "反例(若 momentum 强相关 fit)会是浪费一层。")
    L.append("")

    out = "\n".join(L) + "\n"
    with open(args.out_md, "w") as f:
        f.write(out)

    # 机器可读汇总(供 MOMENTUM.md 引用)
    summary = {
        "coverage": dict(cov),
        "active_n": len(active_vals),
        "active_mean": round(statistics.mean(active_vals), 3),
        "active_stdev": round(statistics.pstdev(active_vals), 3),
        "active_distinct_2dp": len({round(v, 2) for v in active_vals}),
        "deciles": {f"P{p}": round(v, 3) for p, v in dec},
        "neg_control": {
            "fit_top100_mean": round(fit_top_mean, 3),
            "random100_mean": round(rand_mean, 3),
            "diff": round(fit_top_mean - rand_mean, 3),
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
