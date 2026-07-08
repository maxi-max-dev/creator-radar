#!/usr/bin/env python3
"""达人雷达 · cards 表「门面富化」(W21 2026-07-08 Max「表格太单调」批)。

给 cards(达人推荐)表两列上料, 二者都**失败安全**(抓不到/传超时/匹配不上 → 静默留空, 绝不挡主链):
  1) 频道头像(附件列): 只取**公开元数据**头像图片(YouTube 频道页 og:image, 兜底 yt-dlp 频道缩略图;
     B站行走用户 card API), 下到 data/avatars/ 缓存, 经 drive/v1/medias/upload_all 传成 file_token 写入。
     ⚠️铁律: 只取头像图片这种公开元数据, **绝不下载视频/音频本体**。
  2) 完整档案(单向关联 type 18 → 全池榜单表): 按频道名匹配全池表 record_id 回填。匹配不上留空, 不硬凑。

两种用法:
  A) 一次性回填现有全部 cards 行: `python3 src/cards_enrich.py --config config/insta360.json`
  B) run_radar 每日主链: push_bitable 追加当日新卡后, 调 enrich_records(...) 给新卡带头像+关联。
     单卡 15s 超时, 任一步失败静默跳过。开关: cfg["bitable"]["avatar_enrich"](默认 True)。

复用全仓惯例: 标准库 urllib, 失败返回/记录不抛, 温和降级。敏感物(app_token/table_id)只从 credentials 文件读。
"""
import io, json, os, re, sys, time, uuid
import urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import schema

AVATAR_DIR = os.path.join(ROOT, "data", "avatars")
BILI_THROTTLE_S = 3.5          # B站用户 card API 节流(铁律 >=3.5s)
DEFAULT_ITEM_TIMEOUT_S = 15    # 单卡整体超时兜底
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"


def _http_get(url, timeout=12, binary=False, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read() if binary else r.read().decode("utf-8", "replace")


def _feishu_call(method, path, token=None, body=None):
    url = "https://open.feishu.cn/open-apis" + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"code": -1, "_http": e.code, "msg": e.read().decode(errors="replace")[:200]}
    except Exception as e:
        return {"code": -1, "msg": str(e)}


def _get_token(cred):
    t = _feishu_call("POST", "/auth/v3/tenant_access_token/internal",
                     body={"app_id": cred["app_id"], "app_secret": cred["app_secret"]})
    return t.get("tenant_access_token")


# ---------------------------------------------------------------------------
# 头像 URL 发现(只公开元数据; 绝不下载视频/音频本体)。
# ---------------------------------------------------------------------------
def _yt_avatar_url(channel_url, timeout=12):
    """YouTube 频道头像 URL: 先扒频道页 og:image, 兜底页内 avatar.thumbnails。都失败返回 None。"""
    try:
        html = _http_get(channel_url, timeout=timeout)
    except Exception:
        return None
    m = (re.search(r'<meta property="og:image" content="([^"]+)"', html)
         or re.search(r'<link rel="image_src" href="([^"]+)"', html)
         or re.search(r'"avatar":\{"thumbnails":\[\{"url":"([^"]+)"', html))
    if m:
        return m.group(1).replace("\\/", "/")
    return None


def _yt_avatar_url_ytdlp(channel_url, timeout=20):
    """兜底: yt-dlp 只取频道元数据(--skip-download)拿 thumbnails 最大图。绝不下视频本体。"""
    import subprocess
    try:
        p = subprocess.run(["yt-dlp", "--skip-download", "--playlist-items", "0",
                            "--dump-single-json", "--no-warnings", channel_url],
                           capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0 or not p.stdout.strip():
            return None
        meta = json.loads(p.stdout)
        thumbs = meta.get("thumbnails") or []
        # 频道 avatar 常带 id="avatar_uncropped" 或取分辨率最大者
        av = next((t for t in thumbs if t.get("id") == "avatar_uncropped"), None)
        if not av and thumbs:
            av = max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
        return av.get("url") if av else None
    except Exception:
        return None


def _bili_avatar_url(channel_url, timeout=12):
    """B站 UP 主头像: 从频道链接取 uid, 走用户 card API(公开元数据)拿 face。节流由调用方保证。"""
    m = re.search(r"space\.bilibili\.com/(\d+)", channel_url) or re.search(r"/(\d{3,})/?$", channel_url)
    if not m:
        return None
    uid = m.group(1)
    try:
        j = json.loads(_http_get("https://api.bilibili.com/x/web-interface/card?mid=%s" % uid,
                                 timeout=timeout, headers={"User-Agent": _UA, "Referer": "https://space.bilibili.com/"}))
    except Exception:
        return None
    if j.get("code") != 0:
        return None
    return ((j.get("data") or {}).get("card") or {}).get("face") or None


def avatar_url_for(channel_url, use_ytdlp_fallback=True):
    """按平台选头像 URL 来源。返回 (url|None, is_bili)。"""
    is_bili = "bilibili.com" in (channel_url or "")
    if is_bili:
        return _bili_avatar_url(channel_url), True
    url = _yt_avatar_url(channel_url)
    if not url and use_ytdlp_fallback:
        url = _yt_avatar_url_ytdlp(channel_url)
    return url, False


# ---------------------------------------------------------------------------
# 下载 + 上传 到飞书媒体, 返回 file_token。
# ---------------------------------------------------------------------------
def _download_avatar(img_url, cid, timeout=12):
    """下头像图片到 data/avatars/<cid>.jpg, 返回 (path, bytes)。失败返回 (None, None)。
    只下图片: content-type 非 image/* 则弃(防误抓到 HTML/视频)。"""
    try:
        req = urllib.request.Request(img_url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ctype = r.headers.get("Content-Type", "")
            if "image" not in ctype:
                return None, None
            data = r.read()
        if not data or len(data) < 512:      # 太小多半是占位/错误图
            return None, None
        os.makedirs(AVATAR_DIR, exist_ok=True)
        path = os.path.join(AVATAR_DIR, cid + ".jpg")
        with open(path, "wb") as f:
            f.write(data)
        return path, data
    except Exception:
        return None, None


def _upload_media(token, app_token, cid, data, timeout=60):
    """multipart 传头像到 drive/v1/medias/upload_all(parent_type=bitable_image, parent_node=app_token)。
    返回 file_token|None。"""
    boundary = "----ce" + uuid.uuid4().hex
    fname = cid + ".jpg"

    def _field(name, val):
        return ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                % (boundary, name, val)).encode()
    buf = io.BytesIO()
    buf.write(_field("file_name", fname))
    buf.write(_field("parent_type", "bitable_image"))
    buf.write(_field("parent_node", app_token))
    buf.write(_field("size", str(len(data))))
    buf.write(('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
               'Content-Type: image/jpeg\r\n\r\n' % (boundary, fname)).encode())
    buf.write(data)
    buf.write(("\r\n--%s--\r\n" % boundary).encode())
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/drive/v1/medias/upload_all",
        data=buf.getvalue(), method="POST",
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "multipart/form-data; boundary=" + boundary})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            j = json.loads(r.read())
        return (j.get("data") or {}).get("file_token") if j.get("code") == 0 else None
    except Exception:
        return None


def fetch_avatar_token(token, app_token, channel_url, use_ytdlp_fallback=True, deadline=None):
    """一条频道: 发现头像 URL → 下载 → 上传 → file_token。任一步失败或超时返回 None(失败安全)。"""
    if deadline and time.time() > deadline:
        return None
    cid = (channel_url or "").rstrip("/").rsplit("/", 1)[-1] or "unknown"
    img_url, _is_bili = avatar_url_for(channel_url, use_ytdlp_fallback=use_ytdlp_fallback)
    if not img_url:
        return None
    if deadline and time.time() > deadline:
        return None
    path, data = _download_avatar(img_url, cid)
    if not data:
        return None
    if deadline and time.time() > deadline:
        return None
    return _upload_media(token, app_token, cid, data)


# ---------------------------------------------------------------------------
# 完整档案关联(type 18 → 全池榜单): 按频道名匹配 record_id。
# ---------------------------------------------------------------------------
def _rec_text(v):
    if isinstance(v, list):
        return "".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in v)
    return v if isinstance(v, str) else (str(v) if v is not None else "")


def build_fullranking_name_index(token, app_token, full_tid):
    """全池榜单表: {频道名(strip): record_id}。名字重复时保留排名靠前(第一次见)的一条。
    失败返回空 dict(关联全留空)。"""
    idx = {}
    pt = None
    for _ in range(60):
        body = {"page_size": 500, "field_names": ["频道名"]}
        if pt:
            body["page_token"] = pt
        r = _feishu_call("POST", "/bitable/v1/apps/%s/tables/%s/records/search" % (app_token, full_tid),
                         token=token, body=body)
        if r.get("code") != 0:
            break
        d = r.get("data", {})
        for it in d.get("items", []):
            nm = _rec_text((it.get("fields") or {}).get("频道名")).strip()
            if nm and nm not in idx:
                idx[nm] = it["record_id"]
        pt = d.get("page_token")
        if not d.get("has_more"):
            break
        time.sleep(0.15)
    return idx


# ---------------------------------------------------------------------------
# 主入口: 给一批 cards 记录(record_id/name/url)富化两列。
# ---------------------------------------------------------------------------
def _load_tables_ledger():
    p = os.path.expanduser("~/.config/creator-radar/feishu_tables.json")
    try:
        return json.load(open(p)) if os.path.exists(p) else {}
    except Exception:
        return {}


def enrich_records(cfg, targets, do_avatar=True, do_link=True,
                   item_timeout_s=DEFAULT_ITEM_TIMEOUT_S, verbose=False):
    """给 cards 表若干行富化头像 + 完整档案关联。

    targets: [{"record_id":..., "name":..., "url":...}, ...]
    返回摘要 dict(不抛)。任一行任一列失败 = 静默留空, 不影响其它行, 绝不中断主链。
    - do_avatar: 抓头像图并写附件列(公开元数据; 绝不下视频/音频)。
    - do_link:   按频道名匹配全池表 record_id, 写单向关联列。
    B站行头像走 card API, 请求间节流 >=3.5s。
    """
    cred_path = os.path.expanduser(cfg.get("bitable", {}).get("credentials_path", ""))
    if not cred_path or not os.path.exists(cred_path):
        return {"ok": False, "error": "凭证缺失"}
    try:
        cred = json.load(open(cred_path))
        token = _get_token(cred)
        if not token:
            return {"ok": False, "error": "token failed"}
        app_token, cards_tid = cred.get("app_token"), cred.get("table_id")
        full_tid = _load_tables_ledger().get("full_ranking_table_id")

        name_idx = {}
        if do_link and full_tid:
            name_idx = build_fullranking_name_index(token, app_token, full_tid)

        updates = []
        avatar_ok = avatar_fail = link_ok = link_miss = 0
        no_avatar = []
        last_bili = 0.0
        for t in targets:
            rid, name, url = t.get("record_id"), (t.get("name") or "").strip(), t.get("url") or ""
            if not rid:
                continue
            f = {}
            if do_avatar:
                is_bili = "bilibili.com" in url
                if is_bili:                       # B站节流
                    wait = BILI_THROTTLE_S - (time.time() - last_bili)
                    if wait > 0:
                        time.sleep(wait)
                    last_bili = time.time()
                deadline = time.time() + item_timeout_s
                ft = fetch_avatar_token(token, app_token, url,
                                        use_ytdlp_fallback=not is_bili, deadline=deadline)
                if ft:
                    f["频道头像"] = [{"file_token": ft}]
                    avatar_ok += 1
                else:
                    avatar_fail += 1
                    no_avatar.append(name or url)
            if do_link:
                rec_id = name_idx.get(name)
                if rec_id:
                    f["完整档案"] = [rec_id]
                    link_ok += 1
                else:
                    link_miss += 1
            if f:
                updates.append({"record_id": rid, "fields": f})
            if verbose:
                print("  · %-22s avatar=%s link=%s" % (
                    (name or url)[:22], "✓" if f.get("频道头像") else "—",
                    "✓" if f.get("完整档案") else "—"))

        written = 0
        for i in range(0, len(updates), 100):
            batch = updates[i:i + 100]
            r = _feishu_call("POST",
                             "/bitable/v1/apps/%s/tables/%s/records/batch_update" % (app_token, cards_tid),
                             token=token, body={"records": batch})
            if r.get("code") == 0:
                written += len(batch)
            else:
                # 写失败也不抛: 记录错误, 已富化的算数, 其余留空
                return {"ok": False, "error": "batch_update code=%s %s" % (r.get("code"), str(r.get("msg"))[:80]),
                        "avatar_ok": avatar_ok, "avatar_fail": avatar_fail,
                        "link_ok": link_ok, "link_miss": link_miss, "written": written}
            time.sleep(0.3)
        return {"ok": True, "targets": len(targets), "written_rows": written,
                "avatar_ok": avatar_ok, "avatar_fail": avatar_fail,
                "avatar_rate": round(avatar_ok / max(1, avatar_ok + avatar_fail), 3),
                "link_ok": link_ok, "link_miss": link_miss,
                "link_rate": round(link_ok / max(1, link_ok + link_miss), 3),
                "no_avatar": no_avatar}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _fetch_all_cards_targets(token, app_token, cards_tid):
    """一次性回填用: 取 cards 表全部行的 (record_id, 频道名, 频道链接)。"""
    out, pt = [], None
    for _ in range(200):
        body = {"page_size": 500, "field_names": ["频道名", "频道链接"]}
        if pt:
            body["page_token"] = pt
        r = _feishu_call("POST", "/bitable/v1/apps/%s/tables/%s/records/search" % (app_token, cards_tid),
                         token=token, body=body)
        if r.get("code") != 0:
            break
        d = r.get("data", {})
        for it in d.get("items", []):
            f = it.get("fields") or {}
            u = f.get("频道链接")
            url = (u.get("link") if isinstance(u, dict)
                   else (u[0].get("link") if isinstance(u, list) and u and isinstance(u[0], dict) else ""))
            out.append({"record_id": it["record_id"], "name": _rec_text(f.get("频道名")), "url": url or ""})
        pt = d.get("page_token")
        if not d.get("has_more"):
            break
        time.sleep(0.2)
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser(description="cards 表门面富化: 回填现有全部行的头像 + 完整档案关联")
    ap.add_argument("--config", required=True)
    ap.add_argument("--no-avatar", action="store_true", help="跳过头像(只补关联)")
    ap.add_argument("--no-link", action="store_true", help="跳过关联(只补头像)")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 行(调试用)")
    args = ap.parse_args()

    cfg = json.load(open(args.config))
    cred = json.load(open(os.path.expanduser(cfg["bitable"]["credentials_path"])))
    token = _get_token(cred)
    if not token:
        print(json.dumps({"ok": False, "error": "token failed"}, ensure_ascii=False)); return
    targets = _fetch_all_cards_targets(token, cred["app_token"], cred["table_id"])
    if args.limit:
        targets = targets[:args.limit]
    print("回填目标 %d 行 ..." % len(targets))
    res = enrich_records(cfg, targets, do_avatar=not args.no_avatar, do_link=not args.no_link, verbose=True)
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
