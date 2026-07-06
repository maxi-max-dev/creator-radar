#!/usr/bin/env python3
"""达人雷达公共库: 配置加载 + embedding + 打分。
策略数值(权重/阈值/查询词)一律来自 config，本文件不允许出现硬编码。
"""
import json, math, re, sys, urllib.request, time


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
