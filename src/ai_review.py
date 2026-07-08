#!/usr/bin/env python3
"""AI 复核层 (2026-07-08, Max 拍板): 规则红绿灯之上再加一层语义过滤。

Max 原话:「判断最后碰的还是人。先用目前的数据做判断，再加一层 AI 过滤掉(减掉)，
把那部分数据退掉。」翻车实证=StoneFPV(搬运合集)凭对路分排 B站 #2，规则层只给了
🟡 no_upload_date 没认出搬运；大鹏快开车(车评带货)同理。规则词典治标，语义判断缺一层。

这一层做的事(**只减不加，保守铁律**):
  输入 = annotate 后的 scored rows(已带 action_grade / identity_flags / grade_reasons)。
  复核范围 = 每平台 fit top N(ai_review.top_n) ∪ 全部 🟢(去重合并)，再受两道闸门限流
            (max_candidates_per_line 单线候选上限 + budget_seconds 单线墙钟预算)，
            让在线主链自限时不拖过 20 分钟；离线验收可把闸门开大复核全量。
  每个候选喂本地 ollama qwen3:8b(素材=频道名/简介/近期标题/命中主题/订阅/已有 tags)，
  产严格 JSON: real_person / repost_or_compilation / org_or_brand / off_topic / confidence / reason。

裁决融合(AI 只有降级权, 没有升级权):
  🟢 → 🟡      + flag "ai_review_flagged" + 中文理由(落 grade_reasons 走拦截原因列)。
  🟡 加 flag    不改灯(已经在人工核验队列, AI 只多贴一条语义疑点让终审看)。
  🔴 / 🏢       零接触(已经拦了/已转 BD, 不重复动)。
  绝不自动 🔴、绝不升级任何一级、绝不物理删除。already_partner / 正例逻辑零接触
  (命中 already_partner 的行连碰都不碰)。

工程约束: 单候选超时(timeout_seconds); 整层失败=优雅跳过不断链(reviewed=0 照常返回);
  ai_review.enabled 开关; 结果缓存 data/ai_review/YYYY-MM-DD.jsonl(gitignore, 按
  channel_id 幂等, 当天已复核不重打)。解析失败重试 1 次后跳过(宁可漏不可炸)。

铁律: 只在产品展示层跑, backtest 官方指标不经此; 纯标准库 + 本地 ollama /api/chat。
"""
import json, os, sys, time, urllib.error, urllib.request
from datetime import date, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

REVIEW_DIR = os.path.join(ROOT, "data", "ai_review")
TAGS_FOR_SCORING = os.path.join(ROOT, "data", "tags", "tags_for_scoring.jsonl")

# AI 复核降级时贴的机器键(展示名在 identity_filter.FLAG_LABELS 里 = "🤖AI复核")。
AI_FLAG = "ai_review_flagged"

# 本模块缺省(config.ai_review 覆盖)。top_n 大是为了「全部🟢+topN」的语义完整;
# 真正约束在线时长的是 max_candidates_per_line + budget_seconds 两道闸门。
DEFAULTS = {
    "enabled": True,
    "model": "qwen3:8b",
    "endpoint": "http://127.0.0.1:11434/api/chat",
    "temperature": 0.0,
    # 单候选超时: 实测 qwen3:8b 思考态(catch StoneFPV 必须开思考)单候选 ~40-62s, 30s 会把每个
    # 候选都掐死(=零 verdict, 层白跑), 故上调到 90s 给 p95 留头; 真正约束在线时长的是下面两道闸门。
    "timeout_seconds": 90,
    "top_n": 40,                     # 每平台 fit top-N 纳入复核范围(∪ 全部 🟢)
    "max_candidates_per_line": 12,   # 单线(YT/B站各一)复核候选硬上限, 防在线主链超时
    "budget_seconds": 600,           # 单线墙钟预算, 到点收工(已复核的算数, 未及的优雅跳过)
    "confidence_threshold": 0.70,    # 只有 confidence 达标的负面判定才降级(防误退)。0.70: 搬运类真判
                                     # conf 常落 0.7(StoneFPV), 误退好人靠 prompt 把 bool 判对而非靠高阈值卡
    "description_chars": 500,
    "titles_max": 8,                 # 近期标题最多喂几条(搬运号的他人名字信号靠标题列)
    "title_chars": 90,               # 单条标题截断
}

# 复核员系统提示词: 只补规则漏掉的语义坑, 给两类翻车原型的显式判据(搬运号=标题挂满他人名字;
# 机构号=简介商务合作+联系方式)。**不加 /no_think**: 实测 qwen3:8b 关思考会漏判 StoneFPV,
# 开思考才认出「标题反复出现 andre/El Gringo 等他人 ID = 搬运」(bench 4/4 全中, 零误退好人)。
SYS_PROMPT = (
    "你是运动相机品牌的达人合作复核员。频道已用规则初筛过，你只补规则漏掉的语义坑——"
    "用常识判断这个频道是不是「品牌其实不该发首次达人合作邀请」的那种。看频道名 / 简介 / "
    "近期视频标题 / 订阅数 / 命中主题，判断四件事:\n"
    "1) repost_or_compilation(搬运号/合集号/二次剪辑搬运): 判据——近期视频标题里如果反复出现"
    "【别人的名字 / 别的创作者 ID / 外国人名】(例如 andre、El Gringo、Franz Meyer 这种一看就是"
    "在搬运别人作品的信号)，或简介自称「合集 / 搬运 / 图书馆 / 仓库 / 录播 / 全网」，就判 true。"
    "真·原创作者的标题写的是自己的行程 / 内容，不会挂满一堆别人的名字。\n"
    "2) org_or_brand(机构号 / 品牌官方号 / 厂商 / 器材店 / 主业是带货的商务号): 判 true 的是"
    "【频道本身就是一个品牌 / 公司 / 店铺 / 机构在运营】——官方旗舰店、器材店、厂商官号、"
    "或主业就是接商单带货 / 续航评测卖货的号。"
    "⚠️关键区分: 一个**真人创作者**在简介里留【商务合作邮箱 / 联系方式 / business email / 商务 vx】"
    "只是方便品牌找他恰饭，这是**正常创作者标配，不算 org_or_brand**；创作者被某品牌**赞助 / supported by / "
    "拿到品牌支持**、或围绕某个车型 / 器材做**评测**，只要出镜的是他本人、拍的是他自己的体验，"
    "**仍是 real_person，不判 org_or_brand**。只有当频道明显是【一个组织 / 店铺 / 官方账号在发声】"
    "而非某个具体真人在拍自己时，才判 org_or_brand=true。\n"
    "3) off_topic(主业与「运动影像」无关): 这个品牌要找的是户外 / 运动场景的第一视角与全景拍摄创作者。"
    "**相关**的是: 摩托骑行 / 自行车 / 赛车 / 攀岩 / 冲浪 / 滑雪 / 潜水 / 跑步 / 徒步 / 无人机穿越 等运动的 POV 拍摄与随身器材玩法。"
    "**off_topic=true** 的是: 主轴是**汽车 / 电动车续航评测**(四轮车评不是运动影像)、理财 / 股票 / 玄学算命 / "
    "纯带货卖货直播 / 美妆 / 游戏 / 与户外运动无关的题材。判断看频道主轴内容是什么。\n"
    "4) real_person(真人出镜的原创创作者，本人拍本人剪，哪怕留了商务邮箱 / 被品牌赞助): 判 true。\n\n"
    "confidence 是你对上面判定的把握(0~1)。铁律: 只依据我给的真实语料判断，不许因为品牌语境脑补，"
    "不确定就给低 confidence 别硬判。严格只输出这个 JSON，不要多余文字、不要思考过程泄漏进 JSON:\n"
    '{"real_person":true/false,"repost_or_compilation":true/false,"org_or_brand":true/false,'
    '"off_topic":true/false,"confidence":0.0,"reason":"一句话中文说明"}'
)


# ---------- 已有标签(可选加料) ----------

def load_scoring_tags():
    """data/tags/tags_for_scoring.jsonl -> {channel_id: {vertical, content_themes, content_forms}}。
    脱敏视图(无品牌痕迹), 给复核员当额外语料。缺文件返回空 dict(优雅降级)。"""
    out = {}
    if not os.path.exists(TAGS_FOR_SCORING):
        return out
    for line in open(TAGS_FOR_SCORING):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        cid = r.get("channel_id")
        if cid:
            out[cid] = r
    return out


# ---------- 输入装配 ----------

def _theme_labels(themes_hit):
    """命中主题 key -> 中文短标签(复核员看得懂; 未知 key 原样)。import schema 失败则原样返回。"""
    try:
        import schema
        return schema.theme_tags_zh(themes_hit or [])
    except Exception:
        return list(themes_hit or [])


def build_user_prompt(row, pool_row, tag, rcfg):
    """拼单候选的复核语料。row=scored 行, pool_row=池子原始行(简介/标题), tag=脱敏标签(可空)。"""
    name = row.get("channel_name") or pool_row.get("channel_name") or "?"
    desc = (pool_row.get("description") or "")[:rcfg["description_chars"]]
    titles = (pool_row.get("recent_video_titles") or [])[:rcfg["titles_max"]]
    titles = [str(t)[:rcfg["title_chars"]] for t in titles if t]
    subs = row.get("subscribers")
    themes_zh = _theme_labels(row.get("themes_hit"))

    parts = [f"频道名: {name}"]
    if desc:
        parts.append(f"简介: {desc}")
    if titles:
        parts.append("近期视频标题: " + " ; ".join(titles))
    else:
        parts.append("近期视频标题: (无)")
    parts.append(f"订阅数: {subs if subs is not None else '未知'}")
    if themes_zh:
        parts.append("命中主题: " + "、".join(themes_zh))
    # 脱敏标签里的垂类/内容形态(有就补, 帮复核员判 off_topic; 绝无品牌痕迹)
    if tag:
        extra = []
        if tag.get("vertical"):
            extra.append(f"垂类={tag['vertical']}")
        cf = tag.get("content_forms")
        if cf:
            extra.append("内容形态=" + "、".join(str(x) for x in cf[:4]))
        if extra:
            parts.append("已有标签: " + " ; ".join(extra))
    return "\n".join(parts)


# ---------- ollama 调用 + 解析 ----------

def _call_ollama_once(user_prompt, rcfg):
    body = json.dumps({
        "model": rcfg["model"],
        "messages": [
            {"role": "system", "content": SYS_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False, "format": "json",
        "options": {"temperature": rcfg["temperature"]},
    }).encode()
    req = urllib.request.Request(rcfg["endpoint"], data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=rcfg["timeout_seconds"]) as r:
        content = json.loads(r.read())["message"]["content"]
    return json.loads(content)  # 可能抛 JSONDecodeError(空串/截断) -> 上层重试一次


def _normalize_verdict(raw):
    """把模型输出归一化成稳定 schema, 越界/缺字段容错。confidence 夹到 [0,1]。"""
    def _b(v):
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ("true", "1", "yes", "是")
        return bool(v)
    try:
        conf = float(raw.get("confidence"))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    reason = raw.get("reason")
    reason = str(reason).strip() if reason not in (None, "") else ""
    return {
        "real_person": _b(raw.get("real_person")),
        "repost_or_compilation": _b(raw.get("repost_or_compilation")),
        "org_or_brand": _b(raw.get("org_or_brand")),
        "off_topic": _b(raw.get("off_topic")),
        "confidence": round(conf, 3),
        "reason": reason,
    }


def review_candidate(row, pool_row, tag, rcfg):
    """单候选复核。解析失败重试 1 次后返回 None(宁可漏不可炸)。成功返回归一化 verdict dict。"""
    user_prompt = build_user_prompt(row, pool_row, tag, rcfg)
    for attempt in (1, 2):
        try:
            raw = _call_ollama_once(user_prompt, rcfg)
            return _normalize_verdict(raw)
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            if attempt == 2:  # 重试后仍解析失败 -> 跳过(漏)
                return None
            continue
        except (urllib.error.URLError, TimeoutError, OSError):
            return None  # 网络/超时 -> 跳过该候选, 不重试(省时), 整层继续
        except Exception:
            return None
    return None


# ---------- 降级判定(保守铁律) ----------

def _flag_reason(verdict):
    """把负面 verdict 翻成一句「拦截原因」同款中文理由。命中优先级: 搬运 > 机构 > 离题。
    返回 (should_flag, reason_str)。real_person 单独为 false 不作为降级依据(信息量弱, 防误退)。"""
    v = verdict
    conf = v.get("confidence", 0.0)
    tag = None
    if v.get("repost_or_compilation"):
        tag = "搬运/合集"
    elif v.get("org_or_brand"):
        tag = "机构/品牌/带货"
    elif v.get("off_topic"):
        tag = "内容离题"
    if not tag:
        return False, ""
    r = (v.get("reason") or "").strip()
    reason = "🤖AI复核疑似%s(conf %.2f)" % (tag, conf)
    if r:
        reason += ": " + r
    return True, reason


def should_downgrade(verdict, rcfg):
    """是否应触发降级: 有负面判定 且 confidence 达阈值。conf 阈值防低置信误退好人。"""
    should_flag, reason = _flag_reason(verdict)
    if not should_flag:
        return False, ""
    if verdict.get("confidence", 0.0) < rcfg["confidence_threshold"]:
        return False, ""
    return True, reason


def apply_downgrade(row, reason):
    """保守裁决融合(就地改 row)。AI 只有降级权:
      🟢 → 🟡  (加 flag + 理由); 🟡 加 flag 不改灯; 🔴/🏢 零接触。
    already_partner 行零接触(连不进候选, 双保险再挡一次)。返回 'green_to_yellow'/'yellow_flag'/None。"""
    grade = row.get("action_grade")
    flags = row.get("identity_flags")
    if not isinstance(flags, list):
        flags = []
    if "already_partner" in flags:      # 正例/已合作逻辑零接触(双保险)
        return None
    if grade == "🟢":
        row["action_grade"] = "🟡"
        outcome = "green_to_yellow"
    elif grade == "🟡":
        outcome = "yellow_flag"
    else:                                # 🔴 / 🏢 / 其他 -> 不动
        return None
    if AI_FLAG not in flags:
        flags.append(AI_FLAG)
    row["identity_flags"] = flags
    # 理由落 grade_reasons(**契约=list[str] 纯字符串**, 见 identity_filter.annotate 第 377-388 行:
    # 最终 grade_reasons 是抽出的 label 字符串列表, 不是 (grade,label) 元组)。sync_full_ranking 的
    # 「拦截原因」列取 grade_reasons[0], 故把 AI 理由(纯串)插到最前, 保证 [0] 是最新语义疑点。
    gr = row.get("grade_reasons")
    if not isinstance(gr, list):
        gr = []
    gr.insert(0, reason)
    row["grade_reasons"] = gr
    row["ai_review"] = {"flagged": True, "reason": reason}
    return outcome


# ---------- 候选选取(范围 = fit topN ∪ 全部🟢, 受闸门限流) ----------

def _select_candidates(scored, rcfg):
    """按 fit 顺序(scored 已按 rank 排)取 top_n, 并入全部 🟢(去重), 保持 fit 顺序。
    再砍到 max_candidates_per_line(按 fit 优先, 高 fit 先复核)。返回候选 row 列表。
    跳过: 已 🔴/🏢(拦过/已转 BD) 与 already_partner(正例零接触)。"""
    top_n = rcfg["top_n"]
    seen = set()
    ordered = []
    for i, s in enumerate(scored):
        url = s.get("channel_url")
        grade = s.get("action_grade")
        flags = s.get("identity_flags") or []
        if grade in ("🔴", "🏢"):        # 已拦 / 已转官方 BD, 不复核
            continue
        if "already_partner" in flags:   # 正例/已合作零接触
            continue
        in_topn = i < top_n
        is_green = grade == "🟢"
        if not (in_topn or is_green):
            continue
        key = url or id(s)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(s)
    # 高 fit 优先(ordered 已是 fit 序), 砍到单线候选上限
    cap = rcfg["max_candidates_per_line"]
    if cap is not None and cap >= 0:
        ordered = ordered[:cap]
    return ordered


# ---------- 结果缓存(按日期, 按 channel_id 幂等) ----------

def _cache_path(today):
    return os.path.join(REVIEW_DIR, "%s.jsonl" % today)


def _load_cache(today):
    """{channel_id: verdict_record}。当天已复核过的候选不重打。"""
    path = _cache_path(today)
    out = {}
    if not os.path.exists(path):
        return out
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        cid = r.get("channel_id")
        if cid:
            out[cid] = r
    return out


def _append_cache(today, record):
    os.makedirs(REVIEW_DIR, exist_ok=True)
    with open(_cache_path(today), "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------- 主入口(给 run_radar 调, 也可 CLI 单跑) ----------

def review_scored(scored, pool_by_url, cfg, today=None, line_label="", use_cache=True,
                  progress=False):
    """对一条线(YT 或 B站)的 scored 就地跑 AI 复核。返回统计 dict。
    整层任何异常都优雅吞掉(reviewed=0), 绝不断主链。"""
    stats = {"line": line_label, "enabled": False, "candidates": 0, "reviewed": 0,
             "skipped_parse": 0, "downgraded_green": 0, "flagged_yellow": 0,
             "seconds": 0.0}
    rcfg = dict(DEFAULTS)
    rcfg.update(cfg.get("ai_review", {}) or {})
    if not rcfg.get("enabled", True):
        return stats
    stats["enabled"] = True
    today = today or date.today().isoformat()

    try:
        tags = load_scoring_tags()
        cache = _load_cache(today) if use_cache else {}
        candidates = _select_candidates(scored, rcfg)
        stats["candidates"] = len(candidates)
        # 供降级回填的 url->row 引用(scored 里的同一对象, 就地改即生效)
        t0 = time.time()
        for idx, s in enumerate(candidates, 1):
            if time.time() - t0 > rcfg["budget_seconds"]:
                if progress:
                    print("  [ai_review%s] 墙钟预算到点, 已复核 %d/%d, 其余优雅跳过"
                          % (line_label, idx - 1, len(candidates)), file=sys.stderr)
                break
            cid = s.get("channel_id") or (s.get("channel_url") or "")
            pool_row = dict(pool_by_url.get(s.get("channel_url"), {}))
            tag = tags.get(cid)

            # 缓存命中(当天已复核): 直接复用 verdict, 不重打模型
            cached = cache.get(cid)
            if cached and "verdict" in cached:
                verdict = cached["verdict"]
            else:
                verdict = review_candidate(s, pool_row, tag, rcfg)
                if verdict is None:
                    stats["skipped_parse"] += 1
                    if progress:
                        print("  [ai_review%s] %d/%d SKIP(解析失败) %s"
                              % (line_label, idx, len(candidates), s.get("channel_name")),
                              file=sys.stderr)
                    continue
                if use_cache:
                    _append_cache(today, {
                        "channel_id": cid,
                        "channel_name": s.get("channel_name"),
                        "grade_before": s.get("action_grade"),
                        "verdict": verdict,
                        "reviewed_at": datetime.now().isoformat(timespec="seconds"),
                    })
            stats["reviewed"] += 1

            do_dg, reason = should_downgrade(verdict, rcfg)
            if do_dg:
                outcome = apply_downgrade(s, reason)
                if outcome == "green_to_yellow":
                    stats["downgraded_green"] += 1
                elif outcome == "yellow_flag":
                    stats["flagged_yellow"] += 1
                if progress:
                    print("  [ai_review%s] %d/%d DOWNGRADE %s -> %s | %s"
                          % (line_label, idx, len(candidates), s.get("channel_name"),
                             s.get("action_grade"), reason), file=sys.stderr)
            elif progress:
                print("  [ai_review%s] %d/%d ok %s conf=%.2f"
                      % (line_label, idx, len(candidates), s.get("channel_name"),
                         verdict.get("confidence", 0)), file=sys.stderr)
        stats["seconds"] = round(time.time() - t0, 1)
    except Exception as e:
        # 整层失败=优雅跳过不断链
        stats["error"] = str(e)
        print("  [ai_review%s] 整层异常, 优雅跳过: %s" % (line_label, e), file=sys.stderr)
    return stats


def _main():
    """CLI: 对一个 ranked.json + pool 跑复核(离线验收 / 冒烟)。
    用法: python3 ai_review.py --ranked <ranked.json> --pool <pool.jsonl> [--all] [--top-n N]
    --all: 把闸门开到最大(复核范围内全量), 供 80 盲投集验收出数。"""
    import argparse
    from radar_lib import load_config, load_pool
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "config", "insta360.json"))
    ap.add_argument("--ranked", required=True, help="带 action_grade/identity_flags 的 ranked.json")
    ap.add_argument("--pool", required=True, help="对应池子 jsonl")
    ap.add_argument("--top-n", type=int, help="覆盖 ai_review.top_n")
    ap.add_argument("--all", action="store_true", help="闸门开到最大(复核范围内全量, 验收用)")
    ap.add_argument("--no-cache", action="store_true", help="不读写当天缓存(纯净重跑)")
    ap.add_argument("--label", default="", help="行标(日志用)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    cfg.setdefault("ai_review", {})
    if args.top_n is not None:
        cfg["ai_review"]["top_n"] = args.top_n
    if args.all:
        cfg["ai_review"]["max_candidates_per_line"] = 10 ** 9
        cfg["ai_review"]["budget_seconds"] = 10 ** 9
        cfg["ai_review"].setdefault("top_n", 0)  # top_n=0 时范围=纯 🟢(∪ top0); --all 常配大 top_n

    scored = json.load(open(args.ranked))
    pool = load_pool(args.pool)
    pool_by_url = {r["channel_url"]: r for r in pool}
    stats = review_scored(scored, pool_by_url, cfg, line_label=args.label,
                          use_cache=not args.no_cache, progress=True)
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    _main()
