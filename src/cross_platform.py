#!/usr/bin/env python3
"""跨平台矩阵检测(零网络请求): 正则扫描频道文本字段里的他平台链接/handle。
会经营多平台矩阵的创作者商业成熟度更高，这是一条选人信号，后续进达人档案。

只读 data/pool/creator_pool.jsonl 的既有文本字段(description / recent_video_titles / channel_name)，
不发任何请求。每行产一条: channel_url + platforms 字典({instagram: handle, ...}) + detected_from(命中来自哪个字段)。
幂等: 每次重写整个 data/pool/cross_platform.jsonl(池子才 ~1100 行，全量重扫即可)。
"""
import argparse, json, os, re, sys, collections

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POOL = os.path.join(ROOT, "data", "pool", "creator_pool.jsonl")
OUT = os.path.join(ROOT, "data", "pool", "cross_platform.jsonl")

# 扫描的文本字段(按优先级)，detected_from 记第一个命中该平台的字段名
TEXT_FIELDS = ["description", "recent_video_titles", "channel_name"]

# handle 允许的字符(YouTube/IG/TikTok/X 通用): 字母数字下划线点，1-30 长
_H = r"[A-Za-z0-9._]{1,30}"

# 每平台一组模式: 先试完整链接(拿 handle)，再试裸 @handle 前缀词。
# 用 finditer 逐条取第一个非空捕获组当 handle。域名边界用 \b 和显式 host，避免子串误伤。
PLATFORM_PATTERNS = {
    "instagram": [
        re.compile(r"(?:https?://)?(?:www\.)?instagram\.com/(" + _H + r")", re.I),
        re.compile(r"(?:^|[\s|·•\-（(])(?:ig|insta|instagram)\s*[:：@/]\s*@?(" + _H + r")", re.I),
    ],
    "tiktok": [
        re.compile(r"(?:https?://)?(?:www\.)?tiktok\.com/@(" + _H + r")", re.I),
        re.compile(r"(?:^|[\s|·•\-（(])tiktok\s*[:：@/]\s*@?(" + _H + r")", re.I),
    ],
    "facebook": [
        # facebook.com/<handle> 或 fb.com/<handle>；排除纯数字 profile.php 之外的常见页
        re.compile(r"(?:https?://)?(?:www\.)?(?:facebook|fb)\.com/(" + _H + r")", re.I),
    ],
    "twitter": [
        re.compile(r"(?:https?://)?(?:www\.)?(?:twitter|x)\.com/(" + _H + r")", re.I),
        re.compile(r"(?:^|[\s|·•\-（(])(?:twitter|x\.com)\s*[:：@/]\s*@?(" + _H + r")", re.I),
    ],
    "website": [
        # 个人网站: 显式 http(s) 链接，排除已知社媒/视频平台域名(下方 _SOCIAL_HOSTS 过滤)
        re.compile(r"https?://(?:www\.)?([A-Za-z0-9.\-]+\.[A-Za-z]{2,})(?:/[^\s]*)?", re.I),
    ],
}

# 归一化 handle 时剔除的尾随标点(URL 后常粘 ) 、 。 等)
_TRAIL = ".,)/」』】>|、。 \t"

# website 检测里要排除的域名(社媒/视频/聚合/打赏/店铺/短链/品牌自身，都不算"个人网站")
_SOCIAL_HOSTS = re.compile(
    r"(?:youtube|youtu\.be|instagram|tiktok|facebook|fb\.com|twitter|x\.com|"
    r"linktr\.ee|linktree|beacons\.ai|bio\.link|dot\.cards|costa\.so|t\.me|telegram|discord|"
    r"twitch|kick\.com|patreon|paypal|amazon|amzn|gmail|reddit|snapchat|threads\.net|"
    r"vk\.com|b23\.tv|bilibili|weibo|"
    r"buymeacoffee|ko-?fi|shopee|\.stores\.jp|\.shop\b|"
    r"insta\s*360|insta360|"  # 品牌自身域名不当个人网站(这条工具不过打分层的泄漏词表)
    r"bit\.ly|goo\.gl|whatsapp|wa\.me|sites\.google\.com|blogspot)",
    re.I,
)

# facebook 常见的非 handle 路径段(sharer/pages/groups 等)，命中就不当 handle
_FB_NONHANDLE = {"sharer", "pages", "groups", "profile.php", "watch", "reel", "story.php", "events", "hashtag"}


def _clean_handle(h):
    return h.strip(_TRAIL)


def _field_texts(row):
    """产 (字段名, 文本) 序列; recent_video_titles 是列表，拼成一段。"""
    for f in TEXT_FIELDS:
        v = row.get(f)
        if not v:
            continue
        if isinstance(v, list):
            yield f, " \n ".join(str(x) for x in v if x)
        else:
            yield f, str(v)


def detect_row(row):
    """扫一个频道行，返回 (platforms 字典, detected_from 字典)。无命中返回两个空字典。
    platforms[平台]=handle(第一个命中)，detected_from[平台]=命中所在字段名。"""
    platforms, detected_from = {}, {}
    for field, text in _field_texts(row):
        for plat, patterns in PLATFORM_PATTERNS.items():
            if plat in platforms:  # 已在更高优先字段命中，保留第一个
                continue
            for pat in patterns:
                hit = None
                for m in pat.finditer(text):
                    cand = _clean_handle(m.group(1))
                    if not cand:
                        continue
                    if plat == "website":
                        host = cand.lower()
                        if _SOCIAL_HOSTS.search(host) or "." not in host:
                            continue
                    if plat == "facebook" and cand.split("/")[0].lower() in _FB_NONHANDLE:
                        continue
                    # handle 类不接受纯平台名自身(如 x.com/ 后什么都没有已被长度挡掉)
                    hit = cand
                    break
                if hit:
                    platforms[plat] = hit
                    detected_from[plat] = field
                    break
    return platforms, detected_from


def scan_pool(pool_path=POOL, out_path=OUT):
    """全量扫描池子，幂等重写整个 cross_platform.jsonl。返回统计字典。"""
    with open(pool_path) as f:
        rows = [json.loads(l) for l in f]
    total = len(rows)
    counts = collections.Counter()
    records = []
    for row in rows:
        platforms, detected_from = detect_row(row)
        if not platforms:
            continue
        for plat in platforms:
            counts[plat] += 1
        records.append({
            "channel_url": row.get("channel_url"),
            "channel_name": row.get("channel_name"),
            "channel_id": row.get("channel_id"),
            "platforms": platforms,
            "detected_from": detected_from,
        })
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return {"total": total, "with_any": len(records), "counts": dict(counts), "out": out_path}


def _print_stats(stats):
    total = stats["total"]
    print(f"跨平台矩阵检测: 全池 {total} 频道，{stats['with_any']} 个含至少一个他平台信号 "
          f"({stats['with_any']/total*100:.1f}%)", file=sys.stderr)
    order = ["instagram", "tiktok", "facebook", "twitter", "website"]
    for plat in order:
        c = stats["counts"].get(plat, 0)
        print(f"  {plat:10s}: {c:4d}  ({c/total*100:.1f}%)", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="跨平台矩阵检测(零网络，纯正则扫池子文本)")
    ap.add_argument("--pool", default=POOL)
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()
    stats = scan_pool(args.pool, args.out)
    _print_stats(stats)
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
