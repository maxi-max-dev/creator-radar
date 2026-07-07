#!/usr/bin/env python3
"""达人雷达 · B站(国内侧)采集适配器 v0。

只拉公开元数据(UP 主名/UID/粉丝数/签名/近期视频标题+时长)，绝不下载任何视频/音频。
入口两件套(策略在 config.collect)：
  - 种子发现 + 近期标题：本机 RSSHub 的 bilibili 视频搜索路由 vsearch/<关键词>
    (带 guest buvid 自动续、412 风控自愈；返回 XML，每条含 AuthorID/author/title/Length)
  - 逐 UP 主补料：api.bilibili.com 的 card 端点(免签名，一次拿 name+fans+sign)

产出 data/pool/bilibili_pool.jsonl，行字段与 data/pool/creator_pool.jsonl 对齐
(channel_name/channel_url/subscribers/description/recent_video_titles)，附加 platform:"bilibili"/uid 等。
断点续跑：state 文件记已跑过的关键词与已入库 uid；重跑跳过已完成关键词、已 enrich 的 uid。
节流≥config.throttle_seconds(默认 3.5s)，单点失败跳过不中断，预算(分钟)封顶。

用法：
  python3 src/collect_bilibili.py --config config/insta360_bilibili.json
  python3 src/collect_bilibili.py --config config/insta360_bilibili.json --reset   # 清空断点重跑
"""
import argparse, html, json, os, re, sys, time, urllib.parse, urllib.request
from email.utils import parsedate_to_datetime

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

ITEM_RE = re.compile(r"<item>(.*?)</item>", re.S)
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
AUTHOR_RE = re.compile(r"<author>(.*?)</author>", re.S)
AID_RE = re.compile(r"AuthorID:\s*(\d+)")
LEN_RE = re.compile(r"Length:\s*([\d:]+)")
PUBDATE_RE = re.compile(r"<pubDate>(.*?)</pubDate>", re.S)


def _pubdate_to_iso(s):
    """RSSHub 的 RFC822 pubDate(如 'Tue, 07 Jul 2026 07:16:27 GMT') -> 'YYYY-MM-DD'。解析不了返回 None。"""
    if not s:
        return None
    try:
        return parsedate_to_datetime(s.strip()).date().isoformat()
    except (TypeError, ValueError, IndexError):
        return None


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://www.bilibili.com"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def clean(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def parse_vsearch(xml):
    """从 RSSHub vsearch XML 解析出 [(uid, author_name, video_title, duration, pub_iso)]。
    pub_iso = 该视频发布日 'YYYY-MM-DD'(从 <pubDate> 解析, 零额外请求)。用于便宜地补 last_upload_date。"""
    out = []
    for it in ITEM_RE.findall(xml):
        aid = AID_RE.search(it)
        if not aid:
            continue
        title = TITLE_RE.search(it)
        author = AUTHOR_RE.search(it)
        length = LEN_RE.search(it)
        pub = PUBDATE_RE.search(it)
        out.append((
            aid.group(1),
            clean(author.group(1)) if author else "",
            clean(title.group(1)) if title else "",
            length.group(1) if length else "",
            _pubdate_to_iso(pub.group(1)) if pub else None,
        ))
    return out


def fetch_card(uid, card_ep, timeout=15):
    """card 端点(免签名)：返回 (name, fans, sign) 或 None(失败/风控)。"""
    try:
        d = json.loads(http_get(card_ep + str(uid), timeout=timeout))
    except Exception as e:
        return None, f"req:{type(e).__name__}"
    if d.get("code") != 0:
        return None, f"code:{d.get('code')}"
    c = d.get("data", {}).get("card", {})
    fans = c.get("fans")
    try:
        fans = int(fans) if fans is not None else None
    except (TypeError, ValueError):
        fans = None
    return {"name": c.get("name") or "", "fans": fans, "sign": c.get("sign") or ""}, "ok"


def load_state(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"done_terms": [], "authors": {}}


def save_state(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def write_pool(path, authors):
    """把 state.authors(已 enrich 的)落成 jsonl，字段对齐 creator_pool.jsonl。幂等整写。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        for uid, a in authors.items():
            if not a.get("enriched"):
                continue
            row = {
                "channel_name": a.get("name") or a.get("search_name") or f"UP{uid}",
                "channel_url": f"https://space.bilibili.com/{uid}",
                "channel_id": f"bili:{uid}",
                "subscribers": a.get("fans"),
                "description": a.get("sign") or "",
                "country": "CN",
                "recent_video_titles": a.get("titles", [])[:10],
                "source": "bilibili_vsearch",
                "is_positive": a.get("is_positive", False),
                "positive_source": a.get("positive_source"),
                "platform": "bilibili",
                "uid": int(uid) if uid.isdigit() else uid,
                "video_durations": a.get("durations", [])[:10],
                "found_terms": sorted(a.get("terms", [])),
                "last_refreshed": time.strftime("%Y-%m-%d"),
                # 从 vsearch pubDate 便宜拿到的最近上传日(可能缺, 缺时身份过滤器走 🟡 data_coverage 不足)
                "last_upload_date": a.get("last_upload_date"),
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--reset", action="store_true", help="清空断点从头跑")
    args = ap.parse_args()

    with open(args.config) as f:
        cfg = json.load(f)
    cc = cfg["collect"]
    root = repo_root()
    state_path = os.path.join(root, cc["state_file"])
    pool_path = os.path.join(root, "data/pool/bilibili_pool.jsonl")
    base = cc["rsshub_base"].rstrip("/")
    vroute = cc["vsearch_route"]
    card_ep = cc["card_endpoint"]
    throttle = cc.get("throttle_seconds", 3.5)
    budget_s = cc.get("budget_minutes", 90) * 60
    cap = cc.get("target_max_authors", 800)

    if args.reset and os.path.exists(state_path):
        os.remove(state_path)
    state = load_state(state_path)
    authors = state["authors"]  # uid -> {search_name,name,fans,sign,titles[],durations[],terms[],enriched,is_positive,positive_source}
    t0 = time.time()

    def budget_left():
        return budget_s - (time.time() - t0)

    # ---- 阶段一：搜索发现，累积每个 UP 主的标题/时长/命中词 ----
    print(f"[discover] {len(cc['search_terms'])} terms via RSSHub vsearch", file=sys.stderr)
    for term in cc["search_terms"]:
        if term in state["done_terms"]:
            print(f"  skip(done) {term}", file=sys.stderr)
            continue
        if budget_left() < 90:
            print("  budget nearly gone, stop discovery", file=sys.stderr)
            break
        url = base + vroute + urllib.parse.quote(term)
        try:
            xml = http_get(url, timeout=30)
            rows = parse_vsearch(xml)
        except Exception as e:
            print(f"  FAIL {term}: {type(e).__name__} (skip)", file=sys.stderr)
            time.sleep(throttle)
            continue
        new_here = 0
        for uid, aname, title, dur, pub_iso in rows:
            a = authors.setdefault(uid, {"titles": [], "durations": [], "terms": [], "enriched": False})
            if aname and not a.get("search_name"):
                a["search_name"] = aname
                new_here += 1
            if title and title not in a["titles"]:
                a["titles"].append(title)
                a["durations"].append(dur)
            # last_upload_date = 该 UP 主在搜索结果里见过的最新发布日(零额外请求, 便宜补活性字段)。
            # vsearch 按 pubdate 排, 同一 UP 主可能多条, 取最大日期。
            if pub_iso and pub_iso > (a.get("last_upload_date") or ""):
                a["last_upload_date"] = pub_iso
            if term not in a["terms"]:
                a["terms"].append(term)
        state["done_terms"].append(term)
        save_state(state_path, state)
        print(f"  {term}: {len(rows)} items, +{new_here} new authors (pool={len(authors)})", file=sys.stderr)
        time.sleep(throttle)

    # ---- 阶段二：逐 UP 主 enrich(card 端点补粉丝/签名/正式名) ----
    todo = [uid for uid, a in authors.items() if not a.get("enriched")][:cap]
    print(f"[enrich] {len(todo)} authors need card lookup", file=sys.stderr)
    ok = fail = 0
    for i, uid in enumerate(todo):
        if budget_left() < 20:
            print(f"  budget gone at {i}/{len(todo)}, stop (resume later)", file=sys.stderr)
            break
        card, status = fetch_card(uid, card_ep)
        if card:
            a = authors[uid]
            a["name"] = card["name"]
            a["fans"] = card["fans"]
            a["sign"] = card["sign"]
            a["enriched"] = True
            ok += 1
        else:
            fail += 1
            if fail <= 8 or fail % 20 == 0:
                print(f"  card fail {uid}: {status}", file=sys.stderr)
        if (i + 1) % 15 == 0:
            save_state(state_path, state)
            print(f"  enrich {i+1}/{len(todo)} ok={ok} fail={fail} ({budget_left()/60:.0f}min left)", file=sys.stderr)
        time.sleep(throttle)

    save_state(state_path, state)
    write_pool(pool_path, authors)
    enriched_n = sum(1 for a in authors.values() if a.get("enriched"))
    print(f"\n[done] discovered={len(authors)} enriched={enriched_n} "
          f"card_ok={ok} card_fail={fail} elapsed={(time.time()-t0)/60:.1f}min", file=sys.stderr)
    print(f"[done] pool written: {pool_path} ({enriched_n} rows)", file=sys.stderr)


if __name__ == "__main__":
    main()
