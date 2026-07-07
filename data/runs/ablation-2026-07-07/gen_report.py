#!/usr/bin/env python3
"""从 _summary.json 生成 ABLATION.md。数字全从 summary 来，杜绝手抄误差。
判词与结论段由本脚本按判读规则拼装，人工再润色语气。"""
import json, os, datetime

ADIR = "/Users/max/Documents/creator-radar/data/runs/ablation-2026-07-07"
S = json.load(open(os.path.join(ADIR, "_summary.json")))
base = S["base"]
V = S["variants"]

ORDER = [
    "fresh-baseline", "no-sweet", "no-markers", "semantic-only",
    "theme-drop-pov_native", "theme-drop-adventure_bold", "theme-drop-authentic_vlog",
    "theme-drop-journey_narrative", "theme-drop-gear_native", "theme-drop-vertical_craft",
]
LABEL = {
    "fresh-baseline": "fresh-baseline（对照组）",
    "no-sweet": "no-sweet（删甜点函数）",
    "no-markers": "no-markers（删平台标记）",
    "semantic-only": "semantic-only（只留语义）",
    "theme-drop-pov_native": "theme-drop · pov_native（第一视角）",
    "theme-drop-adventure_bold": "theme-drop · adventure_bold（冒险大胆）",
    "theme-drop-authentic_vlog": "theme-drop · authentic_vlog（真实vlog）",
    "theme-drop-journey_narrative": "theme-drop · journey_narrative（长途叙事）",
    "theme-drop-gear_native": "theme-drop · gear_native（器材原生）",
    "theme-drop-vertical_craft": "theme-drop · vertical_craft（垂类技艺）",
}


def cell(d):
    return f"{d['hit']}/{d['tot']} = {d['pct']:.0f}%"


def dstr(x):
    if x == 0:
        return "±0.0"
    return f"{x:+.1f}"


lines = []
w = lines.append

w("# 删除实验 · 盲测裁判")
w("")
w(f"> 生成日期 {datetime.date.today().isoformat()}｜引擎 达人雷达 Creator Radar｜品牌配置 insta360")
w(f"> 池子 `data/pool/creator_pool.jsonl`（{base['n']} 频道，26 正例 = 20 中腰部 + 6 巨星）｜本地 bge-m3 embedding｜串行跑，零 API")
w("")
w("## 一、方法")
w("")
w("每个变体 = 一份改过的 config 副本，只动打分旋钮，不动 `src/` 一行代码（`backtest.py` 吃 `--config` 就够）。全池零样本重跑，正例做隐藏标准答案。所有变体与本轮对照组 **fresh-baseline** 比，不与老基线（2026-07-06-pilot）比，以吸收 ollama embedding 的浮点漂移与池子 +21 频道的影响。")
w("")
w("信号三路（semantic / sweet_spot / platform_markers）代码里不自动归一，删某路后按原比例把余下路重归一化到和 = 1。主题六个在代码里已被权重和除掉（等价自动归一），删某主题后仍显式把余下主题重归一化到和 = 1，让 config 自解释。")
w("")
w("**判读规则**（对比 fresh-baseline）：")
w("")
w("- 召回掉 ≥ 2 个百分点，或 正例中位百分位恶化 ≥ 2pp（中位数变大）→ **承重墙**")
w("- 三指标变化全部 < 0.5pp → **白重量候选**")
w("- 介于两者之间 → **边缘**（列证据，不下死判）")
w("")
w("中位百分位越低越好（正例排得越靠前）。漂移噪声约 ±0.1pp 量级，0.1–0.2pp 的抖动一律读作噪声不读作信号。")
w("")
w("## 二、实验矩阵")
w("")
w("三指标 × 两套正例（all = 全部 26；mid = 中腰部 20 子集）。召回单元格 = 命中/总数。")
w("")
w("| 变体 | 前5% all | 前5% mid | 前10% all | 前10% mid | 中位% all | 中位% mid | 判决 |")
w("|---|---|---|---|---|---|---|---|")
for v in ORDER:
    if v not in V:
        w(f"| {LABEL[v]} | — 未跑成 — |||||||")
        continue
    m = V[v]["metrics"]
    row = (f"| {LABEL[v]} | {cell(m['r5_all'])} | {cell(m['r5_midtail'])} | "
           f"{cell(m['r10_all'])} | {cell(m['r10_midtail'])} | "
           f"{m['med_all']:.1f}% | {m['med_midtail']:.1f}% | {V[v]['verdict']} |")
    w(row)
w("")
w("## 三、相对 fresh-baseline 的 delta（承重判据看这张）")
w("")
w("负数 = 召回掉了；中位列正数 = 恶化（排名变差）。红线：召回 −2.0 或中位 +2.0。")
w("")
w("| 变体 | Δ前5% all | Δ前5% mid | Δ前10% all | Δ前10% mid | Δ中位 all | Δ中位 mid | 判决 |")
w("|---|---|---|---|---|---|---|---|")
for v in ORDER:
    if v == "fresh-baseline" or v not in V or "delta" not in V[v]:
        continue
    d = V[v]["delta"]
    row = (f"| {LABEL[v]} | {dstr(d['d_r5_all'])} | {dstr(d['d_r5_mid'])} | "
           f"{dstr(d['d_r10_all'])} | {dstr(d['d_r10_mid'])} | "
           f"{dstr(d['d_med_all'])} | {dstr(d['d_med_mid'])} | {V[v]['verdict']} |")
    w(row)
w("")

# 分组判决
load = [v for v in ORDER if v != "fresh-baseline" and V.get(v, {}).get("verdict") == "承重"]
white = [v for v in ORDER if v != "fresh-baseline" and V.get(v, {}).get("verdict") == "白重量"]
marg = [v for v in ORDER if v != "fresh-baseline" and V.get(v, {}).get("verdict") == "边缘"]

w("## 四、每零件判决")
w("")
w("### 承重墙（删了系统显著变差，必须留）")
w("")
if load:
    for v in load:
        d = V[v]["delta"]
        worst_r = min(d["d_r5_all"], d["d_r10_all"], d["d_r5_mid"], d["d_r10_mid"])
        worst_m = max(d["d_med_all"], d["d_med_mid"])
        w(f"- **{LABEL[v]}**：最大召回下降 {worst_r:+.1f}pp，最大中位恶化 {worst_m:+.1f}pp。")
else:
    w("- （本轮无零件触发承重红线。见「结论」对此的解读。）")
w("")
w("### 白重量候选（删了几乎无损，可删以简化）")
w("")
if white:
    for v in white:
        d = V[v]["delta"]
        w(f"- **{LABEL[v]}**：六项 delta 绝对值全 < 0.5pp（最大 {max(abs(x) for x in d.values()):.1f}pp），在噪声带内。")
else:
    w("- （无零件落进 <0.5pp 全静默带。）")
w("")
w("### 边缘（有变化但未过红线，列证据不下死判）")
w("")
if marg:
    for v in marg:
        d = V[v]["delta"]
        w(f"- **{LABEL[v]}**：最大召回变动 {min(d['d_r5_all'],d['d_r10_all'],d['d_r5_mid'],d['d_r10_mid']):+.1f}pp，"
          f"最大中位变动 {max(d['d_med_all'],d['d_med_mid']):+.1f}pp。介于噪声与红线之间。")
else:
    w("- （无边缘件。）")
w("")
w("## 五、删删删结论")
w("")
w("<!-- 结论段由人工按数据润色，脚本先摆事实骨架 -->")
w(f"- 承重墙：{('、'.join(LABEL[v].split('（')[0] for v in load)) if load else '无（阈值内）'}")
w(f"- 白重量可删：{('、'.join(LABEL[v].split('（')[0] for v in white)) if white else '无'}")
w(f"- 边缘待观察：{('、'.join(LABEL[v].split('（')[0] for v in marg)) if marg else '无'}")
w("")

open(os.path.join(ADIR, "ABLATION.md"), "w").write("\n".join(lines) + "\n")
print("ABLATION.md 骨架已生成（数字部分完成，结论段待人工润色）")
print(f"承重={load}")
print(f"白重量={white}")
print(f"边缘={marg}")
