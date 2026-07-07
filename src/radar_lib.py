#!/usr/bin/env python3
"""达人雷达公共库: 配置加载 + embedding + 打分。
策略数值(权重/阈值/查询词)一律来自 config，本文件不允许出现硬编码。
"""
import json, math, os, re, sys, urllib.request, time


def load_config(path):
    with open(path) as f:
        return json.load(f)


def load_pool(path):
    with open(path) as f:
        return [json.loads(l) for l in f]


def embed(texts, cfg):
    """按 config 里的 batch_size 分批调用本地 ollama embedding 接口。"""
    econf = cfg["embedding"]
    batch = econf["batch_size"]
    out = []
    for i in range(0, len(texts), batch):
        body = json.dumps({"model": econf["model"], "input": texts[i:i + batch]}).encode()
        req = urllib.request.Request(econf["endpoint"], data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            out.extend(json.loads(r.read())["embeddings"])
        print(f"  embed {min(i + batch, len(texts))}/{len(texts)}", file=sys.stderr)
    return out


def cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def sweet_spot(subs, cfg):
    """中腰部甜点: 对数钟形, 两端衰减。峰值/宽度/未知值分数全部来自 config。"""
    sc = cfg["sweet_spot"]
    if not subs or subs <= 0:
        return sc["unknown_subs_score"]
    x = math.log10(subs)
    return math.exp(-((x - sc["log10_mu"]) ** 2) / (2 * sc["log10_sigma"] ** 2))


def leak_pattern(cfg):
    return re.compile(cfg["leak_tokens_pattern"], re.I)


def build_doc(row, cfg, leak_re):
    """拼接频道名+简介+近期视频标题为一段文本，截断长度来自 config，并剔除泄漏词。"""
    df = cfg["doc_fields"]
    txt = " | ".join(filter(None, [
        row.get("channel_name") or "",
        (row.get("description") or "")[:df["description_chars"]],
        " ; ".join(row.get("recent_video_titles") or [])[:df["recent_titles_chars"]],
    ]))
    return leak_re.sub(" ", txt)


def score_pool(rows, cfg):
    """对全池打分并按分数降序排名，返回打分明细列表(含 rank/pct)。"""
    leak_re = leak_pattern(cfg)
    docs = [build_doc(r, cfg, leak_re) for r in rows]

    themes = cfg["themes"]
    qnames = list(themes)  # 主题顺序=配置文件里的顺序
    flat_queries, q_spans = [], {}
    for q in qnames:
        variants = themes[q]["variants"]
        q_spans[q] = (len(flat_queries), len(flat_queries) + len(variants))
        flat_queries.extend(variants)

    t0 = time.time()
    qvecs = embed(flat_queries, cfg)
    dvecs = embed(docs, cfg)
    print(f"embedding done in {time.time()-t0:.0f}s", file=sys.stderr)

    sw_conf = cfg["signal_weights"]
    pm_conf = cfg["platform_markers"]
    pm_re = re.compile(pm_conf["pattern"], re.I)
    theme_weight_sum = sum(themes[q]["weight"] for q in qnames)

    scored = []
    for r, d, dv in zip(rows, docs, dvecs):
        sem = sum(
            themes[q]["weight"] * max(cos(dv, qvecs[i]) for i in range(*q_spans[q]))
            for q in qnames
        ) / theme_weight_sum
        sw = sweet_spot(r.get("subscribers"), cfg)
        pm = min(len(pm_re.findall(d)) / pm_conf["cap"], 1.0)
        score = sw_conf["semantic"] * sem + sw_conf["sweet_spot"] * sw + sw_conf["platform_markers"] * pm
        scored.append({
            "channel_name": r["channel_name"], "channel_url": r["channel_url"],
            "subscribers": r.get("subscribers"), "is_positive": r.get("is_positive", False),
            "positive_source": r.get("positive_source"),
            "score": round(score, 5), "sem": round(sem, 4), "sweet": round(sw, 4), "pov": round(pm, 4),
        })

    scored.sort(key=lambda x: -x["score"])
    n = len(scored)
    for i, s in enumerate(scored):
        s["rank"] = i + 1
        s["pct"] = round((i + 1) / n * 100, 1)
    return scored


def fuse_momentum(scored, momentum_path, cfg):
    """产品路径专用: 把起势层(momentum)融进 fit 分, 产出 potential 分与 potential 排名。

    铁律: 只改产品路径的展示/排序, 绝不碰 backtest。backtest.py 直接调 score_pool 不经此函数,
    冻结考试池官方数字因此不受任何影响。

    融合公式(乘性放大器): potential = score × (1 + gain × (momentum − pivot))
      - momentum 高于 pivot(=neutral) 才正向放大 fit; 低于则轻微压制。
      - 数据不足频道 momentum=neutral(pivot), 放大量=0, potential==score, 排名不动。
      - 选乘性不选加性: '对的时机'是对'对的人'的放大器, 一个 fit 极低的人再火也不该被这层捞进来。

    就地给每个 scored 行加 momentum / momentum_cov / potential 字段, 并加 potential_rank/potential_pct。
    momentum_path 不存在时: momentum 全填中性, potential==score(优雅降级, 排名不变)。
    返回 scored(已加字段, 原 score 排名不变, potential 排名另存字段)。
    """
    fconf = cfg["momentum"]["fusion"]
    gain = fconf["gain"]
    pivot = fconf["momentum_pivot"]
    neutral = cfg["momentum"]["neutral_score"]

    m_by_url = {}
    if momentum_path and os.path.exists(momentum_path):
        for m in json.load(open(momentum_path)):
            m_by_url[m["channel_url"]] = m

    for s in scored:
        m = m_by_url.get(s["channel_url"])
        mval = m["momentum_score"] if m else neutral
        mcov = m["data_coverage"] if m else "none"
        s["momentum"] = round(mval, 4)
        s["momentum_cov"] = mcov
        s["potential"] = round(s["score"] * (1 + gain * (mval - pivot)), 5)

    # potential 排名(独立于 score 排名, 只进产品展示)
    order = sorted(range(len(scored)), key=lambda i: -scored[i]["potential"])
    n = len(scored)
    for rank, i in enumerate(order, 1):
        scored[i]["potential_rank"] = rank
        scored[i]["potential_pct"] = round(rank / n * 100, 1)
    return scored


def fuse_trends(scored, trend_path, breakout_path, cfg):
    """产品路径专用: 浪层(trend)+破圈比(breakout)并进 scored 行, 照 TRENDS.md 第7节接线。

    铁律(与 fuse_momentum 同一保护逻辑, Max 已认可"趋势是放大器不是保票"):
      - 浪层是放大器不是独立加分: potential *= (1 + gain × trend), 乘性温和放大。
      - 只有 data_coverage=ok 且 trend>0(真上浪有证据)才放大; trend=0(没上浪≠差)与
        数据不足(no_terms/none 的中性 0.3)一律不动 potential, 绝不压 fit。
      - trend_chaser(晚进场蹭热点)只作展示层 ⚠️ 徽章: 追加进 identity_flags, 不改任何分数。
      - backtest.py 绝不 import 本函数, 冻结考试池官方指标零影响。

    就地给每个 scored 行加:
      trend(浪层分, 原始 trend_score 含中性) / trend_cov / trend_chaser
      breakout(破圈比, 原始 views÷粉丝盘 倍数, >1=破圈; 缺数据 None) / breakout_score(log 归一)
    并在放大后重算 potential / potential_rank / potential_pct(fit 的 score/rank 永远不动)。
    trend_path/breakout_path 缺失时: 字段全填中性/None, potential 不动(优雅降级)。
    """
    fconf = cfg.get("trends", {}).get("fusion", {})
    gain = fconf.get("gain", 0.0)

    t_by_url = {}
    if trend_path and os.path.exists(trend_path):
        for t in json.load(open(trend_path)):
            t_by_url[t["channel_url"]] = t
    b_by_url = {}
    if breakout_path and os.path.exists(breakout_path):
        for b in json.load(open(breakout_path)):
            b_by_url[b["channel_url"]] = b

    for s in scored:
        t = t_by_url.get(s["channel_url"])
        tval = t["trend_score"] if t else 0.0
        tcov = t["data_coverage"] if t else "none"
        chaser = bool(t and t.get("trend_chaser"))
        s["trend"] = round(tval, 4)
        s["trend_cov"] = tcov
        s["trend_chaser"] = chaser
        b = b_by_url.get(s["channel_url"])
        s["breakout"] = round(b["top_ratio"], 3) if (b and b.get("top_ratio") is not None) else None
        s["breakout_score"] = b.get("breakout_score") if b else None
        # 放大: 仅真上浪(coverage=ok 且 trend>0)。基底是 fuse_momentum 融合后的 potential;
        # momentum 未跑时 potential 缺失, 以 score 为基底(浪层独立可用)。
        base = s.get("potential", s["score"])
        eff = tval if (tcov == "ok" and tval > 0) else 0.0
        s["potential"] = round(base * (1 + gain * eff), 5)
        # 蹭热点 ⚠️ 徽章并进身份标签列(展示层, 不进分级判定)
        if chaser:
            flags = s.setdefault("identity_flags", [])
            if "trend_chaser" not in flags:
                flags.append("trend_chaser")

    # 放大改变了 potential, 重算 potential 排名(仍只进产品展示)
    order = sorted(range(len(scored)), key=lambda i: -scored[i]["potential"])
    n = len(scored)
    for rank, i in enumerate(order, 1):
        scored[i]["potential_rank"] = rank
        scored[i]["potential_pct"] = round(rank / n * 100, 1)
    return scored
