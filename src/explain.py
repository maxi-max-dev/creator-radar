#!/usr/bin/env python3
"""达人雷达推荐卡生成: 对打分结果里的 top 候选用本地 ollama chat 模型精读，产可解释推荐卡。
纪律: 只喂真实元数据字段，提示词明令"不知道的不要编"; 单卡失败跳过不炸整批。
产出 data/runs/daily/YYYY-MM-DD/cards.json。
"""
import argparse, json, os, sys, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from radar_lib import load_config, load_pool

SYS_PROMPT = (
    "你是品牌达人营销的选人分析师，服务的品牌方是{brand_context}。"
    "给你一个 YouTube 创作者的真实元数据和雷达打分信号，"
    "输出一张 JSON 推荐卡。\n\n"
    "信号语义字典(解释任何信号时必须严格按这里的含义，不许自由发挥因果关系):\n"
    "- subscribers: 频道订阅数\n"
    "- radar_rank / radar_percentile: 雷达全池排名和排名百分位，百分位越小排名越靠前。引用它时只能陈述位置事实(如'全池排名第2、前0.2%')，不许引申为内容质量、竞争力或同质化\n"
    "- semantic_score: 频道文本与品牌主题查询的语义相似度(0-1)\n"
    "- sweet_spot_score: 订阅规模落在品牌偏好的中腰部甜点区的程度(0-1)，越接近1表示订阅规模越接近理想值(是正中甜点，不是上限或下限)。它只关于粉丝规模，与内容质量、平台算法、受众匹配都无关\n"
    "- subs_vs_brand_ideal: 订阅数在品牌理想规模的低侧还是高侧。谈规模方向(上限/下限/增长空间)时必须与这个字段一致，不许反着说\n"
    "- pov_marker_score: 标题和简介里第一视角拍摄标记(POV/onboard/helmet cam等)的密度(0-1)\n"
    "- top_themes_hit: 频道文本关键词命中的品牌主题名列表\n"
    "- is_new_discovery / first_seen: 是否本轮搜索新发现入池，及入池日期\n"
    "- rank_delta: 相比上次运行排名上升的位数，null 表示无历史可比\n\n"
    "铁律: 只能用我给你的字段，任何我没给的事实(具体合作过谁、真实观看量、私人信息)都不许编，不确定就不写。"
    "风险点只允许写数据里真实可推断的角度(如订阅规模上限、垂类宽窄、缺少历史排名数据、近期表现在数据里看不到)，"
    "禁止写'平台算法''内容同质化''算法匹配度''竞争力'这类数据推不出来的判断。"
    "first_collab 必须是该品牌方与这位创作者的首次合作形式，禁止点名其他厂商的设备或品牌名，禁止建议与第三方品牌联名。/no_think"
)

CARD_SCHEMA_HINT = (
    '严格输出这个 JSON 结构，不要多余文字:\n'
    '{"channel_name":"...",'
    '"why_worth_signing":["理由1(必须引用真实信号:命中的主题/订阅规模/POV标记/是否新发现)","理由2","理由3"],'
    '"risk":"一条风险点",'
    '"first_collab":"建议的首次合作形式，一条"}'
)


def subs_direction(subs, ideal):
    """订阅数相对理想规模的方向，确定性给出，防模型把'高分'误读成'接近上限'。±25% 算正中。"""
    if not subs or subs <= 0:
        return "未知"
    r = subs / ideal
    if r < 0.8:
        return "低于品牌理想规模"
    if r > 1.25:
        return "高于品牌理想规模"
    return "接近品牌理想规模(正中甜点)"


def build_meta(scored_row, pool_by_url, ideal_subs):
    """把打分行 + 池子原始行拼成喂给模型的真实事实块。"""
    p = pool_by_url.get(scored_row["channel_url"], {})
    themes = scored_row.get("themes_hit", [])
    titles = (p.get("recent_video_titles") or [])[:6]
    return {
        "channel_name": scored_row["channel_name"],
        "subscribers": scored_row.get("subscribers"),
        "subs_vs_brand_ideal": subs_direction(scored_row.get("subscribers"), ideal_subs),
        "radar_rank": scored_row.get("rank"),
        "radar_percentile": scored_row.get("pct"),
        "semantic_score": scored_row.get("sem"),
        "sweet_spot_score": scored_row.get("sweet"),
        "pov_marker_score": scored_row.get("pov"),
        "top_themes_hit": themes,
        "is_new_discovery": bool(p.get("source") == "auto-discover"),
        "first_seen": p.get("first_seen"),
        "rank_delta": scored_row.get("rank_delta"),
        "description": (p.get("description") or "")[:600],
        "recent_video_titles": titles,
    }


def call_ollama(meta, ecfg):
    sys_prompt = SYS_PROMPT.format(brand_context=ecfg.get("brand_context", "一家消费品牌"))
    prompt = f"{CARD_SCHEMA_HINT}\n\n创作者真实元数据(只能用这些):\n{json.dumps(meta, ensure_ascii=False, indent=1)}"
    body = json.dumps({
        "model": ecfg["model"],
        "messages": [{"role": "system", "content": sys_prompt}, {"role": "user", "content": prompt}],
        "stream": False, "format": "json",
        "options": {"temperature": ecfg["temperature"]},
    }).encode()
    req = urllib.request.Request(ecfg["endpoint"], data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        content = json.loads(r.read())["message"]["content"]
    return json.loads(content)


def select_candidates(scored, pool_by_url, top_n, max_cards):
    """从前 top_n 名里挑: 非正例 且 (新发现 或 排名大幅上升)。不足则按分数补足非正例。"""
    pool = pool_by_url
    picks, backup = [], []
    for s in scored[:top_n]:
        if s.get("is_positive"):
            continue
        p = pool.get(s["channel_url"], {})
        is_new = p.get("source") == "auto-discover"
        big_jump = (s.get("rank_delta") or 0) >= 200
        if is_new or big_jump:
            picks.append(s)
        else:
            backup.append(s)
    picks = picks[:max_cards]
    if len(picks) < max_cards:
        picks += backup[:max_cards - len(picks)]
    return picks


def generate_cards(scored, pool_rows, cfg, out_dir):
    ecfg = cfg["explain"]
    ideal_subs = round(10 ** cfg["sweet_spot"]["log10_mu"])  # 理想规模来自甜点函数峰值，不另设数
    pool_by_url = {r["channel_url"]: r for r in pool_rows}
    cands = select_candidates(scored, pool_by_url, ecfg["top_n_scan"], ecfg["max_cards"])
    print(f"explain: {len(cands)} candidates", file=sys.stderr)

    cards = []
    for s in cands:
        meta = build_meta(s, pool_by_url, ideal_subs)
        try:
            card = call_ollama(meta, ecfg)
            card["_channel_url"] = s["channel_url"]
            card["_rank"] = s.get("rank")
            cards.append(card)
            print(f"  card ok: {s['channel_name']}", file=sys.stderr)
        except Exception as e:
            print(f"  card SKIP {s['channel_name']}: {e}", file=sys.stderr)

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "cards.json"), "w") as f:
        json.dump(cards, f, ensure_ascii=False, indent=1)
    return cards


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--pool", required=True)
    ap.add_argument("--ranked", required=True, help="score.py 产的 ranked.json")
    ap.add_argument("--out", required=True)
    ap.add_argument("--top-n", type=int, help="覆盖 config.explain.top_n_scan")
    ap.add_argument("--max-cards", type=int, help="覆盖 config.explain.max_cards")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.top_n is not None:
        cfg["explain"]["top_n_scan"] = args.top_n
    if args.max_cards is not None:
        cfg["explain"]["max_cards"] = args.max_cards
    pool_rows = load_pool(args.pool)
    scored = json.load(open(args.ranked))
    cards = generate_cards(scored, pool_rows, cfg, args.out)
    print(json.dumps({"cards": len(cards)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
