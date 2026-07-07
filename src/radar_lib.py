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


def rising_evidence_lines(scored_row):
    """把 rising_evidence(fuse_rising 存的原始证据) 翻成三条人话, 供日报/档案「证据」节 + 推荐卡提示词。
    只列有证据的那几条(缺的不硬凑)。返回 list[str]。三条口径:
      破自己纪录 = 最近一条视频 views/天 冲到自己历史 Nx; 骑在热点上 = 命中升温词 X 的视频跑赢自己基线;
      播放远超粉丝盘 = 一条视频播放到粉丝盘的 Nx(算法在把他推给圈外人)。"""
    ev = (scored_row or {}).get("rising_evidence") or {}
    lines = []
    m = ev.get("momentum")
    if m and m.get("burst_ratio"):
        title = (m.get("title") or "").strip()
        tail = f"（《{title[:40]}》）" if title else ""
        lines.append(f"破自己纪录：最近一条视频播放速度冲到自己历史的 {m['burst_ratio']:.1f} 倍{tail}")
    t = ev.get("trend")
    if t and t.get("matched_terms"):
        terms = "、".join(t["matched_terms"][:3])
        br = f"，跑赢自己基线 {t['burst_ratio']:.1f} 倍" if t.get("burst_ratio") else ""
        lines.append(f"骑在热点上：内容命中正在升温的「{terms}」{br}")
    b = ev.get("breakout")
    if b and b.get("top_ratio"):
        lines.append(f"播放远超粉丝盘：一条视频播放数达到粉丝总盘的 {b['top_ratio']:.1f} 倍（算法在把他推给圈外人）")
    return lines


def rising_score(momentum, m_cov, onwave, t_cov, breakout_score, b_cov, cfg):
    """在涨分: 三信号合成一个数 ∈[0,1] = 「是不是正在被越来越多陌生人看到」。

    选型论证(Max 2026-07-07 简化拍板):
      三信号里 起势(momentum) 与 浪的 onwave 分量是**同一根轴**——两者都是「近期视频
      views/天 相对该频道自身历史基线的爆发比(log 压缩归一)」, 只是 onwave 只看命中浪词
      的视频。若把 momentum + trend_score 相加会**重复计数**这根「相对自己在加速」的轴。
      故加速轴 = max(momentum, onwave)(取更亮的那个证据, 不叠加)。
      破圈(breakout = 近期 views ÷ 粉丝盘) 是**独立的第二根轴**: 它衡量「算法把他推给了多少
      圈外陌生人」, 与「他比自己更火」正交(大频道易 momentum 难破圈, 小频道一条爆款反之)。

      两轴合成用 noisy-or: rising = 1 - (1-accel)×(1-breakout)。
      为何 noisy-or 而非加权 max 或加权和:
        - 两轴是同一潜变量(被更多陌生人看到)的**独立证据**。任一轴发火都应抬高在涨分
          (加权 max 会让第二根轴完全无贡献, 浪费了独立信号)。
        - 但不该像加权和那样可以冲破 1 或线性叠加(两个弱信号 ≠ 一个强信号)。
          noisy-or 天然把两个独立概率式证据合成且封顶在 1, 正是这个语义。

    数据不足处理(不冤杀): 某轴 coverage 非 ok 时该轴贡献取 0(只由有证据的轴抬升);
    两轴都无证据时退回 neutral(未知不是差, 但也拿不出证据说在涨)。
    """
    sconf = cfg.get("rising", {}).get("synthesis", {})
    neutral = sconf.get("neutral", 0.3)

    # 加速轴: momentum 与 onwave 同源, 取 max(不叠加)。仅 coverage=ok 的分量参与。
    accel_parts = []
    if m_cov == "ok" and momentum is not None:
        accel_parts.append(momentum)
    if t_cov == "ok" and onwave is not None:
        accel_parts.append(onwave)
    accel = max(accel_parts) if accel_parts else None

    # 破圈轴: 独立。仅 coverage=ok 参与。
    brk = breakout_score if (b_cov == "ok" and breakout_score is not None) else None

    if accel is None and brk is None:
        return round(neutral, 4), "none"      # 两轴都无证据 → 中性
    a = accel if accel is not None else 0.0
    b = brk if brk is not None else 0.0
    rising = 1.0 - (1.0 - a) * (1.0 - b)      # noisy-or
    return round(rising, 4), "ok"


def _single_video_driven(m, t, b, cfg):
    """单视频驱动判定(Max 拍板采纳, 只标注不改分): 在涨证据是否 >80% 来自同一条视频。

    加速轴的证据本就取自单条最亮视频(momentum.top_video / trend.top_evidence),
    破圈轴证据也取自单条最亮视频(breakout.top_video)。因此判定逻辑:
      - 只有一根轴有证据 → 该轴的最亮单条视频 = 100% 的在涨证据 → 触发。
      - 两根轴都有证据但**指向同一条视频**(video_id 相同) → 仍是同一条视频承载 100% → 触发。
      - 两根轴指向不同视频 → 证据分散在 >1 条视频上 → 不触发。
    accel 轴的代表视频取 momentum.top_video 与 trend.top_evidence 里更亮的那条(与 rising 的
    max 选择一致)。dominance_threshold 目前语义为「是否单条独占」, 保留为旋钮供将来细化。
    """
    m_ok = m and m.get("data_coverage") == "ok" and (m.get("momentum_score") or 0) > 0
    t_ok = t and t.get("data_coverage") == "ok" and (t.get("onwave") or 0) > 0
    b_ok = b and b.get("data_coverage") == "ok" and (b.get("breakout_score") or 0) > 0

    # 加速轴代表视频 = momentum 与 trend 里 onwave/momentum 更大的那条
    accel_vid = None
    if m_ok or t_ok:
        mv = (m or {}).get("top_video") or {}
        tv = (t or {}).get("top_evidence") or {}
        m_val = (m.get("momentum_score") or 0) if m_ok else -1
        t_val = (t.get("onwave") or 0) if t_ok else -1
        accel_vid = (mv.get("video_id") if m_val >= t_val else tv.get("video_id"))
    brk_vid = ((b or {}).get("top_video") or {}).get("video_id") if b_ok else None

    axes = [v for v, ok in ((accel_vid, m_ok or t_ok), (brk_vid, b_ok)) if ok]
    if not axes:
        return False
    if len(axes) == 1:
        return True                            # 唯一有证据的轴 = 单条视频独占
    # 两轴都有证据: 指向同一条视频才算单视频驱动(video_id 都拿得到时才能判)
    if accel_vid and brk_vid:
        return accel_vid == brk_vid
    return False                               # 拿不到 id 无法证明同源 → 保守不标


def fuse_rising(scored, momentum_path, trend_path, breakout_path, cfg):
    """产品路径专用(2026-07-07 简化): 三信号合成「在涨分」, 单段式融合出 potential。

    替换原两段式(fuse_momentum 的 momentum fuse + fuse_trends 的 trend fuse)。
      potential = fit × (1 + gain × rising)
    对外只暴露一个「在涨分」(rising), 但原三个字段(momentum/trend/breakout 及其分量)照算
    保留进 scored 行(供工程证据列与档案)。单视频驱动 ⚠️ 追加进 identity_flags(只标不改分)。

    铁律(与 fuse_momentum/fuse_trends 同): 只改产品路径展示/排序, backtest 绝不 import 本函数,
    冻结考试池官方指标零影响。任一信号文件缺失 → 该轴按无证据优雅降级(不冤杀)。

    就地给每个 scored 行加:
      momentum / momentum_cov         (起势, 原始值保留)
      trend / trend_cov / trend_chaser(浪, 原始值保留)
      breakout / breakout_score       (破圈, 原始值保留)
      rising / rising_cov             (合成的在涨分)
      potential / potential_rank / potential_pct
      identity_flags 追加 trend_chaser / single_video_driven(如触发)
    返回 scored。
    """
    fconf = cfg.get("rising", {}).get("fusion", {})
    gain = fconf.get("gain", 0.75)

    def _index(path):
        d = {}
        if path and os.path.exists(path):
            for x in json.load(open(path)):
                d[x["channel_url"]] = x
        return d

    m_by = _index(momentum_path)
    t_by = _index(trend_path)
    b_by = _index(breakout_path)

    for s in scored:
        url = s["channel_url"]
        m = m_by.get(url)
        t = t_by.get(url)
        b = b_by.get(url)

        mval = m["momentum_score"] if m else cfg["momentum"]["neutral_score"]
        mcov = m["data_coverage"] if m else "none"
        s["momentum"] = round(mval, 4)
        s["momentum_cov"] = mcov

        tval = t["trend_score"] if t else 0.0
        tcov = t["data_coverage"] if t else "none"
        onwave = t.get("onwave", 0.0) if t else 0.0
        chaser = bool(t and t.get("trend_chaser"))
        s["trend"] = round(tval, 4)
        s["trend_cov"] = tcov
        s["trend_chaser"] = chaser

        s["breakout"] = round(b["top_ratio"], 3) if (b and b.get("top_ratio") is not None) else None
        bscore = b.get("breakout_score") if b else None
        bcov = b.get("data_coverage") if b else "none"
        s["breakout_score"] = bscore

        rising, rcov = rising_score(mval, mcov, onwave, tcov, bscore, bcov, cfg)
        s["rising"] = rising
        s["rising_cov"] = rcov
        s["potential"] = round(s["score"] * (1 + gain * rising), 5)

        # 在涨的三条人话证据(供日报/档案的「证据」节人话化, 只在有证据时带):
        #   破自己纪录 = 起势(momentum) 的最亮爆发视频; 骑热点 = 浪(trend) 的命中浪词视频; 播放远超粉丝盘 = 破圈(breakout)。
        ev = {}
        if mcov == "ok" and m and m.get("top_video") and (m.get("momentum_score") or 0) > 0:
            ev["momentum"] = m["top_video"]           # {title, views, age_days, burst_ratio, ...}
        if tcov == "ok" and t and t.get("top_evidence") and (t.get("onwave") or 0) > 0:
            ev["trend"] = t["top_evidence"]            # {title, matched_terms, burst_ratio, ...}
        if bcov == "ok" and b and b.get("top_video") and (b.get("top_ratio") or 0) > 1:
            ev["breakout"] = {"top_ratio": b.get("top_ratio"), **(b.get("top_video") or {})}
        s["rising_evidence"] = ev

        # ⚠️ 展示层徽章(不进分级判定, 只进身份标签列)
        flags = s.setdefault("identity_flags", [])
        if chaser and "trend_chaser" not in flags:
            flags.append("trend_chaser")
        if _single_video_driven(m, t, b, cfg) and "single_video_driven" not in flags:
            flags.append("single_video_driven")

    order = sorted(range(len(scored)), key=lambda i: -scored[i]["potential"])
    n = len(scored)
    for rank, i in enumerate(order, 1):
        scored[i]["potential_rank"] = rank
        scored[i]["potential_pct"] = round(rank / n * 100, 1)
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
