#!/usr/bin/env python3
"""达人雷达 第四层「对的浪」采集器(trend collection)。

铁律(Max 已认可的纠偏): 趋势是放大器不是保票。浪层信号 = 浪在涨 × 进得早 ×
他的浪上内容跑赢他自己的基线。晚进场蹭热点是负信号不是正信号。本采集器只负责
「机械地把四路浪的原始快照记下来」, 打分是 trends.py 的事。

四路来源(按性价比排序, 每路独立 try, 失败只记原因绝不中断整批):
  1) 池内浪(最重要, 零新增网络): 用已有 data/rss/<date>.jsonl(视频级 views+发布时间)
     + data/pool/creator_pool.jsonl(vertical) + data/tags/(有则用, 无则退回标题关键词),
     按子题材词聚合近 14 天视频的 views/天 相对该垂类历史基线的倍数, 找「正在升温的子题材词」。
     这是我们自己领域内的浪, 决策价值最高。
  2) Google 每日热搜 RSS(大盘浪): trends.google.com/trending/rss?geo=US|GB|JP|CN。
  3) B站分区热门(国内浪): 本地 RSSHub /bilibili/ranking/<rid>。
  4) Reddit 社区热帖(海外社区浪): r/<sub>/hot(.json 优先, 403 时退回 .rss)。

产出: data/trends/<date>.jsonl, 每行一条统一 schema:
  {source, region_or_scope, term_or_title, metric, captured_at, extra?}
  append 友好(每天可重复跑, 同一天多次跑就多几条快照, 打分层按 captured_at 去重/取最新)。

网络纪律(硬性):
  - 绝不下载任何音视频。
  - 本地 RSSHub 的 B站请求节流 >= 3.5s 硬底。
  - Reddit 公共接口加 User-Agent 头且 >= 1s 节流。
  - Google 的 RSS 正常请求即可(仍留 0.5s 礼貌间隔)。
  - 纯 python 标准库。

配置: 全部旋钮走模块内 DEFAULTS(学 transcripts.py: insta360.json 由他人维护, 本工具不写它;
      若 config 里出现 trends 节则覆盖 DEFAULTS, 但当前不依赖它存在)。
"""
import argparse, json, os, sys, time, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

RSS_DIR = os.path.join(ROOT, "data", "rss")
POOL = os.path.join(ROOT, "data", "pool", "creator_pool.jsonl")
TAGS_DIR = os.path.join(ROOT, "data", "tags")
TRENDS_DIR = os.path.join(ROOT, "data", "trends")

# ---- 缺省旋钮(本工具唯一配置源; 不读也不写他人维护的 insta360.json) ----
DEFAULTS = {
    # 池内浪
    "pool_window_days": 14,          # 「近期」窗口: 近 N 天发布的视频算正在发生的浪
    "pool_min_days": 2.0,            # views/天 的分母地板, 防新视频分母虚小顶穿
    "pool_min_vertical_videos": 8,   # 一个垂类历史视频少于这么多条, 基线不可信, 跳过该垂类
    "pool_min_term_hits": 2,         # 一个子题材词近期至少命中这么多条视频才纳入(防单条噪声)
    "pool_top_terms": 40,            # 每垂类最多记这么多升温词
    "pool_stopwords_extra": [],      # 领域外补充停用词(默认空)
    # Google 热搜
    "google_geos": ["US", "GB", "JP", "CN"],
    "google_throttle_seconds": 0.5,
    # B站排行
    "bili_rsshub_base": "http://127.0.0.1:1200",
    "bili_rids": [234, 223, 250],    # 234 运动 / 223 汽车 / 250 出行(217 动物圈按指令不用)
    "bili_rid_names": {"234": "sport", "223": "car", "250": "travel"},
    "bili_throttle_seconds": 3.5,    # 硬底 >= 3.5s
    "bili_timeout_seconds": 25,
    "bili_max_items": 60,
    # Reddit
    "reddit_subs": ["motorcycles", "MTB", "fpv", "skiing", "surfing", "climbing"],
    "reddit_throttle_seconds": 1.2,  # >= 1s
    "reddit_timeout_seconds": 20,
    "reddit_limit": 25,
    "reddit_user_agent": "creator-radar-trends/0.1 (research; contact via github maxi-max-dev)",
    # 通用
    "http_timeout_seconds": 20,
}


def cfg_get(cfg, key):
    """trends 节覆盖 DEFAULTS(节不存在也能跑)。"""
    tconf = (cfg or {}).get("trends", {}) if cfg else {}
    return tconf.get(key, DEFAULTS[key])


# =========================================================================
# 通用 HTTP(标准库)
# =========================================================================

def http_get(url, timeout, headers=None):
    """GET 返回 bytes。失败抛异常(由调用方 try 捕获记原因)。"""
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# =========================================================================
# 停用词(池内浪的标题分词用; 只做粗过滤, 不追求完美)
# =========================================================================

_STOP = set("""
a an the of to in on at for and or but with without from into onto over under up down off out
is are was were be been being do does did has have had will would can could should may might must
i you he she it we they me him her them my your his our their this that these those there here
new full best top how why what when where who which vs versus first last part ep episode
day days week weeks year years time times day1 official video vlog short shorts clip clips footage
my the a and 2024 2025 2026 s01 e01 pt live watch subscribe channel more get got go goes going
one two three four five ft feat feat. amp
""".split())


def tokenize(title, extra_stop):
    """标题 -> 归一化词/双词(bigram)列表。粗分词: 小写、留字母数字、去停用词与超短词。
    同时产出 bigram(相邻两词)以捕捉「gravel bike」「trail run」这类子题材短语。"""
    if not title:
        return []
    import re
    words = re.findall(r"[a-z0-9]+", title.lower())
    toks = [w for w in words if len(w) >= 3 and w not in _STOP and w not in extra_stop and not w.isdigit()]
    grams = list(toks)
    for i in range(len(toks) - 1):
        grams.append(toks[i] + " " + toks[i + 1])
    return grams


# =========================================================================
# 数据加载(池内浪)
# =========================================================================

def _parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def load_pool_verticals():
    """channel_id -> vertical(可能 None)。"""
    out = {}
    if not os.path.exists(POOL):
        return out
    with open(POOL) as f:
        for line in f:
            try:
                r = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            out[r.get("channel_id")] = r.get("vertical")
    return out


def latest_rss_path():
    """data/rss/ 里最新一份 <date>.jsonl 的路径(按文件名排序)。"""
    if not os.path.isdir(RSS_DIR):
        return None
    files = sorted(f for f in os.listdir(RSS_DIR) if f.endswith(".jsonl"))
    return os.path.join(RSS_DIR, files[-1]) if files else None


def load_rss(path):
    recs = []
    with open(path) as f:
        for line in f:
            try:
                recs.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue
    return recs


def dedup_rss(recs):
    """按 channel_id 去重(留视频最多的那条)。data/rss/<date>.jsonl 由主链工兵实时 append,
    同一频道可能出现多行(两批采集), 消费方读活文件时先去重防重复计数。保序无 id 的行照留。"""
    best = {}
    order = []
    passthrough = []
    for r in recs:
        cid = r.get("channel_id")
        if not cid:
            passthrough.append(r)
            continue
        cur = best.get(cid)
        if cur is None:
            best[cid] = r
            order.append(cid)
        elif len(r.get("videos", [])) > len(cur.get("videos", [])):
            best[cid] = r
    return [best[c] for c in order] + passthrough


def load_tags():
    """data/tags/ 若打标工兵已产出, 读进来: video_id -> [subtopic tags]。
    宽松兼容多种可能格式(jsonl 每行一条, 或 json map)。没有就返回空 dict, 调用方退回标题关键词。"""
    tags = {}
    if not os.path.isdir(TAGS_DIR):
        return tags
    for fn in sorted(os.listdir(TAGS_DIR)):
        p = os.path.join(TAGS_DIR, fn)
        if not os.path.isfile(p):
            continue
        try:
            if fn.endswith(".jsonl"):
                for line in open(p):
                    line = line.strip()
                    if not line:
                        continue
                    r = json.loads(line)
                    vid = r.get("video_id") or r.get("id")
                    tg = r.get("tags") or r.get("subtopics") or r.get("labels")
                    if vid and tg:
                        tags[vid] = tg if isinstance(tg, list) else [tg]
            elif fn.endswith(".json"):
                obj = json.load(open(p))
                if isinstance(obj, dict):
                    for vid, tg in obj.items():
                        if isinstance(tg, dict):
                            tg = tg.get("tags") or tg.get("subtopics") or []
                        if tg:
                            tags[vid] = tg if isinstance(tg, list) else [tg]
        except (json.JSONDecodeError, ValueError, OSError):
            continue
    return tags


# =========================================================================
# 来源 1: 池内浪检测(最重要, 零新增网络)
# =========================================================================

def collect_pool_waves(cfg, now):
    """按垂类聚合近 window 天视频, 找相对该垂类历史基线在升温的子题材词。
    产出 records(schema) + 采集统计。

    逻辑:
      - 每条视频算 views/天(分母地板 min_days)。
      - 每垂类历史基线 = 该垂类所有视频 views/天 的中位数。
      - 对每个「子题材词」(优先 tags, 无则标题分词): 计算「近期命中该词的视频」的
        views/天 中位数 / 该垂类基线 = 该词的升温倍数; 记命中条数、首次出现日期。
      - 升温倍数 > 1 且命中数 >= min_term_hits 才纳入升温词表。
    """
    from statistics import median
    stat = {"source": "pool_wave", "ok": False, "verticals": 0, "terms": 0, "note": ""}
    recs = []
    rss_path = latest_rss_path()
    if not rss_path:
        stat["note"] = "no rss file"
        return recs, stat
    window = cfg_get(cfg, "pool_window_days")
    min_days = cfg_get(cfg, "pool_min_days")
    min_vert = cfg_get(cfg, "pool_min_vertical_videos")
    min_hits = cfg_get(cfg, "pool_min_term_hits")
    top_terms = cfg_get(cfg, "pool_top_terms")
    extra_stop = set(cfg_get(cfg, "pool_stopwords_extra"))

    verts = load_pool_verticals()
    tags = load_tags()
    rss = dedup_rss(load_rss(rss_path))
    stat["rss_file"] = os.path.basename(rss_path)
    stat["tags_used"] = len(tags) > 0

    # 按垂类收集视频(带 views/天 与 tokens)
    per_vert = defaultdict(list)  # vertical -> list of video dicts
    for r in rss:
        vert = verts.get(r.get("channel_id"))
        if not vert:
            continue
        for v in r.get("videos", []):
            dt = _parse_dt(v.get("published"))
            views = v.get("view_count")
            if dt is None or views is None:
                continue
            age = (now - dt).total_seconds() / 86400.0
            vpd = views / max(age, min_days)
            vid = v.get("video_id")
            toks = tags.get(vid) if vid in tags else tokenize(v.get("title", ""), extra_stop)
            per_vert[vert].append({
                "vpd": vpd, "age_days": age, "views": views,
                "title": v.get("title", ""), "video_id": vid,
                "published": v.get("published"), "tokens": [str(t).lower() for t in (toks or [])],
                "channel_name": r.get("channel_name"),
            })

    captured = now.isoformat(timespec="seconds")
    for vert, vids in per_vert.items():
        if len(vids) < min_vert:
            continue
        baseline = median([x["vpd"] for x in vids]) or 0.0
        if baseline <= 0:
            continue
        stat["verticals"] += 1

        # 每个词: 近期(窗口内)命中视频的 vpd 列表 + 首次出现日期(全量, 判断进场早晚)
        recent = [x for x in vids if x["age_days"] <= window]
        term_recent_vpd = defaultdict(list)      # term -> [vpd of recent hits]
        term_recent_ev = defaultdict(list)       # term -> [evidence video dicts]
        term_first_seen = {}                     # term -> earliest published dt across ALL videos
        for x in vids:
            dt = _parse_dt(x["published"])
            for t in set(x["tokens"]):
                if dt and (t not in term_first_seen or dt < term_first_seen[t]):
                    term_first_seen[t] = dt
        for x in recent:
            for t in set(x["tokens"]):
                term_recent_vpd[t].append(x["vpd"])
                term_recent_ev[t].append(x)

        scored_terms = []
        for t, vpds in term_recent_vpd.items():
            if len(vpds) < min_hits:
                continue
            heat = median(vpds) / baseline
            if heat <= 1.0:
                continue
            fs = term_first_seen.get(t)
            # 证据: 该词近期 vpd 最高的一条视频
            ev = max(term_recent_ev[t], key=lambda z: z["vpd"])
            scored_terms.append({
                "term": t, "heat": heat, "hits": len(vpds),
                "first_seen": fs.isoformat() if fs else None,
                "ev_title": ev["title"], "ev_views": ev["views"],
                "ev_vpd": ev["vpd"], "ev_age_days": ev["age_days"],
                "ev_channel": ev["channel_name"], "ev_video_id": ev["video_id"],
            })
        scored_terms.sort(key=lambda z: -z["heat"])
        for st in scored_terms[:top_terms]:
            stat["terms"] += 1
            recs.append({
                "source": "pool_wave",
                "region_or_scope": vert,
                "term_or_title": st["term"],
                "metric": round(st["heat"], 3),   # 升温倍数(相对该垂类历史基线)
                "captured_at": captured,
                "extra": {
                    "baseline_vpd": round(baseline, 2),
                    "hits_recent": st["hits"],
                    "term_first_seen": st["first_seen"],
                    "window_days": window,
                    "evidence": {
                        "title": st["ev_title"], "channel": st["ev_channel"],
                        "views": st["ev_views"], "vpd": round(st["ev_vpd"], 1),
                        "age_days": round(st["ev_age_days"], 1), "video_id": st["ev_video_id"],
                    },
                },
            })
    stat["ok"] = stat["terms"] > 0 or stat["verticals"] > 0
    stat["note"] = f"{stat['verticals']} verticals, {stat['terms']} rising terms" if stat["ok"] else "no rising terms"
    return recs, stat


# =========================================================================
# 来源 2: Google 每日热搜 RSS(大盘浪)
# =========================================================================

def collect_google(cfg, now):
    geos = cfg_get(cfg, "google_geos")
    throttle = cfg_get(cfg, "google_throttle_seconds")
    timeout = cfg_get(cfg, "http_timeout_seconds")
    captured = now.isoformat(timespec="seconds")
    recs = []
    stat = {"source": "google_trends", "ok": False, "geos_ok": [], "geos_fail": {}, "count": 0}
    ns = {"ht": "https://trends.google.com/trending/rss"}
    for geo in geos:
        url = f"https://trends.google.com/trending/rss?geo={geo}"
        try:
            raw = http_get(url, timeout)
            root = ET.fromstring(raw)
            items = root.findall(".//item")
            n = 0
            for it in items:
                title_el = it.find("title")
                title = (title_el.text or "").strip() if title_el is not None else ""
                if not title:
                    continue
                traf_el = it.find("ht:approx_traffic", ns)
                traffic = (traf_el.text or "").strip() if traf_el is not None else None
                recs.append({
                    "source": "google_trends",
                    "region_or_scope": geo,
                    "term_or_title": title,
                    "metric": traffic,             # 原文如 "1000+"(字符串, 打分层解析)
                    "captured_at": captured,
                    "extra": {"approx_traffic": traffic},
                })
                n += 1
            stat["geos_ok"].append(geo)
            stat["count"] += n
        except Exception as e:  # noqa: BLE001 单 geo 失败只记原因
            stat["geos_fail"][geo] = f"{type(e).__name__}: {str(e)[:80]}"
        finally:
            time.sleep(throttle)
    stat["ok"] = stat["count"] > 0
    return recs, stat


# =========================================================================
# 来源 3: B站分区热门(国内浪) via 本地 RSSHub
# =========================================================================

def collect_bilibili(cfg, now):
    base = cfg_get(cfg, "bili_rsshub_base").rstrip("/")
    rids = cfg_get(cfg, "bili_rids")
    rid_names = cfg_get(cfg, "bili_rid_names")
    throttle = max(cfg_get(cfg, "bili_throttle_seconds"), 3.5)   # 硬底 3.5s
    timeout = cfg_get(cfg, "bili_timeout_seconds")
    max_items = cfg_get(cfg, "bili_max_items")
    captured = now.isoformat(timespec="seconds")
    recs = []
    stat = {"source": "bilibili_rank", "ok": False, "rids_ok": [], "rids_fail": {}, "count": 0}
    for rid in rids:
        url = f"{base}/bilibili/ranking/{rid}"
        try:
            raw = http_get(url, timeout)
            root = ET.fromstring(raw)
            items = root.findall(".//item")[:max_items]
            scope = rid_names.get(str(rid), str(rid))
            for pos, it in enumerate(items, 1):
                t_el = it.find("title")
                title = (t_el.text or "").strip() if t_el is not None else ""
                if not title:
                    continue
                a_el = it.find("author")
                author = (a_el.text or "").strip() if a_el is not None else None
                l_el = it.find("link")
                link = (l_el.text or "").strip() if l_el is not None else None
                recs.append({
                    "source": "bilibili_rank",
                    "region_or_scope": f"bili_{scope}",
                    "term_or_title": title,
                    "metric": pos,                  # 榜位(1=最热; RSSHub 排行不带播放量, 用位次代理)
                    "captured_at": captured,
                    "extra": {"rid": rid, "rank_pos": pos, "author": author, "link": link},
                })
            stat["rids_ok"].append(rid)
            stat["count"] += len(items)
        except Exception as e:  # noqa: BLE001
            stat["rids_fail"][str(rid)] = f"{type(e).__name__}: {str(e)[:80]}"
        finally:
            time.sleep(throttle)
    stat["ok"] = stat["count"] > 0
    stat["metric_note"] = "RSSHub 排行不含播放量, metric=榜位(越小越热)"
    return recs, stat


# =========================================================================
# 来源 4: Reddit 社区热帖(海外社区浪)
# =========================================================================

def collect_reddit(cfg, now):
    subs = cfg_get(cfg, "reddit_subs")
    throttle = max(cfg_get(cfg, "reddit_throttle_seconds"), 1.0)  # 硬底 1s
    timeout = cfg_get(cfg, "reddit_timeout_seconds")
    limit = cfg_get(cfg, "reddit_limit")
    ua = cfg_get(cfg, "reddit_user_agent")
    captured = now.isoformat(timespec="seconds")
    recs = []
    stat = {"source": "reddit_hot", "ok": False, "subs_ok": [], "subs_fail": {}, "count": 0,
            "path_used": {}}
    headers = {"User-Agent": ua, "Accept": "application/json"}
    for sub in subs:
        got = None
        # 先试官方 JSON(有 score); 403/challenge 时退回 .rss(无 score 但可拿标题+日期)
        try:
            raw = http_get(f"https://www.reddit.com/r/{sub}/hot.json?limit={limit}&raw_json=1",
                           timeout, headers)
            d = json.loads(raw)
            children = d.get("data", {}).get("children", [])
            for c in children:
                p = c.get("data", {})
                title = (p.get("title") or "").strip()
                if not title:
                    continue
                created = p.get("created_utc")
                iso = (datetime.fromtimestamp(created, timezone.utc).isoformat()
                       if created else None)
                recs.append({
                    "source": "reddit_hot",
                    "region_or_scope": f"r/{sub}",
                    "term_or_title": title,
                    "metric": p.get("score"),
                    "captured_at": captured,
                    "extra": {"score": p.get("score"), "num_comments": p.get("num_comments"),
                              "created_utc_iso": iso, "path": "json"},
                })
            got = ("json", len(children))
        except Exception as e_json:  # noqa: BLE001 JSON 被挡, 退回 rss
            time.sleep(throttle)
            try:
                raw = http_get(f"https://www.reddit.com/r/{sub}/hot.rss?limit={limit}",
                               timeout, {"User-Agent": ua})
                root = ET.fromstring(raw)
                nsa = {"a": "http://www.w3.org/2005/Atom"}
                entries = root.findall("a:entry", nsa)
                for e in entries:
                    t_el = e.find("a:title", nsa)
                    title = (t_el.text or "").strip() if t_el is not None else ""
                    if not title:
                        continue
                    u_el = e.find("a:updated", nsa)
                    pub = (u_el.text or "").strip() if u_el is not None else None
                    recs.append({
                        "source": "reddit_hot",
                        "region_or_scope": f"r/{sub}",
                        "term_or_title": title,
                        "metric": None,             # .rss 无 score
                        "captured_at": captured,
                        "extra": {"score": None, "created_utc_iso": pub, "path": "rss",
                                  "json_blocked": f"{type(e_json).__name__}"},
                    })
                got = ("rss", len(entries))
            except Exception as e_rss:  # noqa: BLE001 两条路都断
                stat["subs_fail"][sub] = (f"json={type(e_json).__name__}:{str(e_json)[:40]} | "
                                          f"rss={type(e_rss).__name__}:{str(e_rss)[:40]}")
        if got:
            stat["subs_ok"].append(sub)
            stat["count"] += got[1]
            stat["path_used"][sub] = got[0]
        time.sleep(throttle)
    stat["ok"] = stat["count"] > 0
    return recs, stat


# =========================================================================
# 落盘 + 主流程
# =========================================================================

def append_jsonl(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def run(cfg, today, only=None):
    now = datetime.now(timezone.utc)
    out_path = os.path.join(TRENDS_DIR, f"{today}.jsonl")
    all_recs = []
    report = {"date": today, "captured_at": now.isoformat(timespec="seconds"), "sources": []}

    plan = [
        ("pool_wave", collect_pool_waves),
        ("google", collect_google),
        ("bilibili", collect_bilibili),
        ("reddit", collect_reddit),
    ]
    for name, fn in plan:
        if only and name not in only:
            continue
        try:
            recs, stat = fn(cfg, now)
        except Exception as e:  # noqa: BLE001 整路兜底: 单路彻底崩也不中断其余
            recs, stat = [], {"source": name, "ok": False, "note": f"FATAL {type(e).__name__}: {str(e)[:120]}"}
        all_recs.extend(recs)
        report["sources"].append(stat)
        print(f"[{name}] ok={stat.get('ok')} count={len(recs)} {stat.get('note','')}", file=sys.stderr)

    if all_recs:
        append_jsonl(out_path, all_recs)
    report["total_records"] = len(all_recs)
    report["out_path"] = out_path
    return report


def main():
    ap = argparse.ArgumentParser(description="第四层浪采集(四路独立 try, 失败只记原因)")
    ap.add_argument("--config", help="可选; 仅当 config 里有 trends 节时覆盖 DEFAULTS(不写它)")
    ap.add_argument("--date", help="目标日期(默认今天 UTC)")
    ap.add_argument("--only", help="逗号分隔只跑某几路: pool_wave,google,bilibili,reddit")
    args = ap.parse_args()
    cfg = None
    if args.config and os.path.exists(args.config):
        try:
            cfg = json.load(open(args.config))
        except (json.JSONDecodeError, ValueError):
            cfg = None
    today = args.date or datetime.now(timezone.utc).date().isoformat()
    only = set(args.only.split(",")) if args.only else None
    report = run(cfg, today, only)
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
