#!/usr/bin/env python3
"""达人雷达 pilot v1: 零样本打分 + 盲测回测
维度来自影石选人逻辑逆向工程(8主题), 权重手工设定, 未用正例标签调参。
标签泄漏防护: 打分前从所有文本中剔除品牌相关 token。
"""
import json, math, re, sys, urllib.request, time

S = "/private/tmp/claude-501/-/be4de55d-405e-48d0-a5bd-0350b12c3bb0/scratchpad"
OLLAMA = "http://127.0.0.1:11434/api/embed"
EMB_MODEL = "bge-m3"

# ---- 标签泄漏防护: 品牌 token 黑名单(打分前从文本剔除) ----
LEAK = re.compile(r"insta\s*360|insta360|影石|antigravity|think\s*bold", re.I)

# ---- 8主题 → 语义查询(不含品牌词) ----
THEME_QUERIES = {
    # v1.2: 每主题两个垂类变体(摩托/骑行), 打分取 max, 模拟真实产品"分垂类打分"形态
    "pov_native": [
        "first-person POV onboard action camera footage, helmet cam riding perspective, immersive 360 degree motorcycle video",
        "first-person POV mountain bike trail footage, chest mount cycling perspective, immersive 360 degree riding video",
    ],
    "adventure_bold": [
        "bold adventurous challenge, extreme stunts, pushing limits, epic motorcycle adventure in wild terrain",
        "bold adventurous challenge, extreme downhill stunts, pushing limits, epic bicycle adventure in wild terrain",
    ],
    "authentic_vlog": [
        "authentic self-filmed motovlog, personal storytelling, solo creator sharing real daily riding life",
        "authentic self-filmed cycling vlog, personal storytelling, solo creator sharing real daily cycling life",
    ],
    "journey_narrative": [
        "long distance motorcycle travel journey across countries, road trip documentary, overcoming hardship",
        "long distance bicycle touring journey across countries, bikepacking documentary, overcoming hardship",
    ],
    "gear_native": [
        "camera gear mounting review for motorcycle riding, filming technique tutorial for riders",
        "camera gear mounting review for cycling, filming technique tutorial for mountain bikers",
    ],
    "vertical_craft": [
        "professional riding skill, motorcycle racing track technique, expert level riding",
        "professional cycling skill, downhill mountain bike racing technique, expert level bike handling",
    ],
}
THEME_WEIGHTS = {
    "pov_native": 0.25, "adventure_bold": 0.15, "authentic_vlog": 0.20,
    "journey_narrative": 0.15, "gear_native": 0.15, "vertical_craft": 0.10,
}
# 启发式特征权重(与语义分相加前各自归一)
W_SEMANTIC, W_SWEET, W_POVMARK = 0.70, 0.18, 0.12

POV_MARKERS = re.compile(r"\bpov\b|onboard|helmet cam|first person|raw (?:audio|sound)|no music|4k ride", re.I)

def embed(texts):
    out = []
    B = 32
    for i in range(0, len(texts), B):
        body = json.dumps({"model": EMB_MODEL, "input": texts[i:i+B]}).encode()
        req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            out.extend(json.loads(r.read())["embeddings"])
        print(f"  embed {min(i+B, len(texts))}/{len(texts)}", file=sys.stderr)
    return out

def cos(a, b):
    dot = sum(x*y for x, y in zip(a, b))
    na = math.sqrt(sum(x*x for x in a)); nb = math.sqrt(sum(x*x for x in b))
    return dot/(na*nb) if na and nb else 0.0

def sweet_spot(subs):
    """中腰部甜点: 1万-100万 高分, 两端衰减(对数钟形)"""
    if not subs or subs <= 0: return 0.3  # 未知给中性偏低
    x = math.log10(subs)
    return math.exp(-((x - 4.9) ** 2) / (2 * 1.1 ** 2))  # 峰值≈8万

def main():
    rows = [json.loads(l) for l in open(f"{S}/creator_pool.jsonl")]
    print(f"pool={len(rows)}", file=sys.stderr)

    docs = []
    for r in rows:
        txt = " | ".join(filter(None, [
            r.get("channel_name") or "",
            (r.get("description") or "")[:600],
            " ; ".join(r.get("recent_video_titles") or [])[:600],
        ]))
        txt_clean = LEAK.sub(" ", txt)
        docs.append(txt_clean)

    t0 = time.time()
    qnames = list(THEME_QUERIES)
    flat_queries, q_spans = [], {}
    for q in qnames:
        variants = THEME_QUERIES[q]
        q_spans[q] = (len(flat_queries), len(flat_queries) + len(variants))
        flat_queries.extend(variants)
    qvecs = embed(flat_queries)
    dvecs = embed(docs)
    print(f"embedding done in {time.time()-t0:.0f}s", file=sys.stderr)

    scored = []
    for r, d, dv in zip(rows, docs, dvecs):
        sem = sum(
            THEME_WEIGHTS[q] * max(cos(dv, qvecs[i]) for i in range(*q_spans[q]))
            for q in qnames
        ) / sum(THEME_WEIGHTS.values())
        sw = sweet_spot(r.get("subscribers"))
        pm = min(len(POV_MARKERS.findall(d)) / 3.0, 1.0)
        score = W_SEMANTIC * sem + W_SWEET * sw + W_POVMARK * pm
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

    json.dump(scored, open(f"{S}/backtest_scores.json", "w"), ensure_ascii=False, indent=1)
    json.dump(metrics, open(f"{S}/backtest_metrics.json", "w"), ensure_ascii=False, indent=1)
    print(json.dumps(metrics, ensure_ascii=False, indent=1))
    print("\n--- 正例排名明细(前=好) ---")
    for s in scored:
        if s["is_positive"]:
            print(f"  #{s['rank']:>4} ({s['pct']:>5}%) [{s['positive_source']}] {s['channel_name']}  subs={s['subscribers']}")

if __name__ == "__main__":
    main()
