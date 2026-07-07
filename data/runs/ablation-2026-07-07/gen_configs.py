#!/usr/bin/env python3
"""生成删除实验的 10 份 config 变体。不改 src/，只产 config 副本。
归一化规则:
- signal_weights 三路(semantic/sweet_spot/platform_markers)不被代码自动归一，
  删某路后手动把余下路按原比例重归一化到和=1。
- themes 六主题的 weight 在 radar_lib 里已被 theme_weight_sum 除掉(等价自动归一)，
  但为让 config 自解释，删某主题后仍显式把余下主题重归一化到和=1。
"""
import json, copy, os

BASE = "/Users/max/Documents/creator-radar/config/insta360.json"
OUT = "/Users/max/Documents/creator-radar/data/runs/ablation-2026-07-07/configs"

base = json.load(open(BASE))


def renorm_signals(d):
    """把 signal_weights 里非零项按当前比例重归一化到和=1。"""
    sw = d["signal_weights"]
    tot = sum(v for v in sw.values())
    for k in sw:
        sw[k] = round(sw[k] / tot, 6) if tot else 0.0
    # 修正舍入使和精确=1（把残差补到最大项）
    resid = round(1.0 - sum(sw.values()), 6)
    if abs(resid) > 0:
        kmax = max(sw, key=lambda k: sw[k])
        sw[kmax] = round(sw[kmax] + resid, 6)
    return d


def renorm_themes(d):
    """把 themes 各 weight 按当前比例重归一化到和=1。"""
    th = d["themes"]
    tot = sum(th[q]["weight"] for q in th)
    for q in th:
        th[q]["weight"] = round(th[q]["weight"] / tot, 6) if tot else 0.0
    resid = round(1.0 - sum(th[q]["weight"] for q in th), 6)
    if abs(resid) > 0:
        qmax = max(th, key=lambda q: th[q]["weight"])
        th[qmax]["weight"] = round(th[qmax]["weight"] + resid, 6)
    return d


def write(name, cfg):
    p = os.path.join(OUT, name + ".json")
    json.dump(cfg, open(p, "w"), ensure_ascii=False, indent=2)
    # 打印这份变体的关键旋钮，便于人工核对
    sw = cfg["signal_weights"]
    tw = {q: cfg["themes"][q]["weight"] for q in cfg["themes"]}
    print(f"{name:24s} signals={sw}  themes_sum={round(sum(tw.values()),4)}  themes={tw}")


configs = {}

# 1. fresh-baseline: 原样
configs["fresh-baseline"] = copy.deepcopy(base)

# 2. no-sweet: sweet_spot 置 0, semantic+markers 重归一化
c = copy.deepcopy(base)
c["signal_weights"]["sweet_spot"] = 0.0
configs["no-sweet"] = renorm_signals(c)

# 3. no-markers: platform_markers 置 0, semantic+sweet 重归一化
c = copy.deepcopy(base)
c["signal_weights"]["platform_markers"] = 0.0
configs["no-markers"] = renorm_signals(c)

# 4. semantic-only: 只留 semantic=1.0
c = copy.deepcopy(base)
c["signal_weights"] = {"semantic": 1.0, "sweet_spot": 0.0, "platform_markers": 0.0}
configs["semantic-only"] = c

# 5. theme-drop-<名> x6
for q in list(base["themes"].keys()):
    c = copy.deepcopy(base)
    del c["themes"][q]
    configs[f"theme-drop-{q}"] = renorm_themes(c)

for name, cfg in configs.items():
    write(name, cfg)

print(f"\n共写出 {len(configs)} 份 config 到 {OUT}")
