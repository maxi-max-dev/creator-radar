#!/usr/bin/env python3
"""达人雷达 · B站(国内侧)进推荐卡(W22 2026-07-08 Max「还不错, 目前只有国外的, 国内的呢」)。

背景: cards(达人推荐)表历史 17 行全是 YouTube; B站 319 人只躺在 B站榜单表, 从没进过推荐卡。
本模块让 B站达人每天也出 2-3 张推荐卡, 写进同一张 cards 表(平台列=B站, 一眼分清)。

铁律(与主链一致的失败安全 + 诚实口径):
  · 只给 🟡 出卡: 绝不给 🔴(别碰)/🏢(转官方)出卡。按现有 fit 排名口径取前几名。
  · 跨日去重: data/scoreboard/bili_cards_seen.json = {channel_url: 上次出卡日期}。
    出过卡的频道**永不**再进推荐卡(明天链再跑不会把同 3 个频道再推一遍)。这是去重的唯一事实源。
  · B站无上传日期(平台不给)→ 全体 🟡/🔴, 结构性零 🟢。证据摘要如实写「活性待人工核一眼」, 不绕过。
  · 完整档案(单向关联)只能指向全池(YouTube)表 → B站卡此列**留空**, 不硬凑(enrich 时 do_link=False)。
  · 档案(dossier)链接列: B站线无 explain/档案管线 → **留空**(不为此扩建)。
  · 数量开关: cfg["bilibili"]["cards_per_day"](默认 2)。
  · 失败安全: 本模块任何一步炸了都只返回错误 dict/串, 绝不抛, 绝不影响 YouTube 卡或主链。
    调用方(run_radar)用 bare try 再包一层, 双保险。

两种用法:
  A) run_radar 每日主链: B站线跑完拿到 bscored, 调 push_bili_cards(cfg, bscored, today)。
  B) 今天先回填(T4): python3 src/bili_cards.py --config config/insta360_bilibili.json \
        --ranked data/runs/2026-07-08-bilibili/ranked.json --date 2026-07-08
     (读今日已有 ranked 产物, 不跑 radar 链)。--dry-run 只打印选中卡不真发。

复用全仓惯例: 标准库 urllib, 敏感物只从 credentials 文件读, 温和降级。
"""
import argparse, json, os, sys, time
import urllib.request, urllib.error
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import schema

SEEN_LEDGER = os.path.join(ROOT, "data", "scoreboard", "bili_cards_seen.json")
DEFAULT_CARDS_PER_DAY = 2


# ---------------------------------------------------------------------------
# 跨日去重账本: {channel_url: 上次出卡日期}。出过卡的频道永不再进推荐卡。
# ---------------------------------------------------------------------------
def load_seen(path=SEEN_LEDGER):
    try:
        return json.load(open(path)) if os.path.exists(path) else {}
    except Exception:
        return {}


def save_seen(seen, path=SEEN_LEDGER):
    """写去重账本(失败静默: 账本写不成不该炸主链, 最坏情况明天可能重推一次, 可接受)。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(seen, f, ensure_ascii=False, indent=1, sort_keys=True)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 选卡: 只 🟡, 按 fit 排名取前 N, 跳过已出过卡的频道。
# ---------------------------------------------------------------------------
def select_bili_cards(bscored, seen, cards_per_day=DEFAULT_CARDS_PER_DAY):
    """从 B站 ranked(已按 fit rank 升序)里选卡。
    规则: action_grade 必须 == 🟡(🔴 别碰 / 🏢 转官方 / 无灯 一律不出); channel_url 不在 seen 账本里。
    取前 cards_per_day 个。返回选中的 scored 行列表(保持排名顺序)。"""
    picks = []
    for s in sorted(bscored, key=lambda x: x.get("rank", 10 ** 9)):
        if s.get("action_grade") != "🟡":
            continue                         # 只给黄灯出卡, 绝不 🔴/🏢
        url = s.get("channel_url") or ""
        if not url or url in seen:
            continue                         # 跨日去重: 出过卡的频道永不再推
        picks.append(s)
        if len(picks) >= cards_per_day:
            break
    return picks


# ---------------------------------------------------------------------------
# 诚实证据摘要: B站无上传日期, 明确写「活性待人工核一眼」。不夸不绕。
# ---------------------------------------------------------------------------
def _evidence_lines(s):
    """给一张 B站卡拼三条如实证据(值得签1/2/3)。全部从 scored 行真实字段取, 不编。
    第一条恒为诚实活性提示(B站结构性缺上传日期)。"""
    themes = schema.theme_tags_zh(s.get("themes_hit") or [])
    subs = s.get("subscribers")
    lines = ["B站无上传日期(平台不给), 红绿灯只到 🟡, 活性请人工核一眼近期更新再定"]
    if themes:
        lines.append("命中主题: " + "、".join(themes) + "(对路分 %.2f)" % (s.get("score") or 0))
    else:
        lines.append("对路分 %.2f, 内容方向与影石选人逻辑相符(详见 B站榜单表)" % (s.get("score") or 0))
    if subs is not None:
        lines.append("粉丝 %s, 处于可发现的中腰部区间" % schema.subs_dual(subs))
    else:
        lines.append("粉丝数未采到, 需人工补")
    return (lines + ["", "", ""])[:3]


def build_bili_card_rows(picks, today):
    """把选中的 B站 scored 行转成 cards 表行(与 push 的 fields 对齐)。
    平台=B站; 红绿灯照搬(必为 🟡); 完整档案/档案留空; 在涨/潜力/起势/浪/破圈 B站结构性无 → 不带。"""
    rows = []
    for s in picks:
        why = _evidence_lines(s)
        rows.append({
            "日期": today,
            "排名": s.get("rank"),
            "频道名": s.get("channel_name", ""),
            "频道链接": s.get("channel_url", ""),
            schema.COL_PLATFORM: schema.PLATFORM_BILIBILI,   # 平台 = B站(一眼分清)
            "订阅数": s.get("subscribers"),
            schema.COL_SUBS_TEXT: schema.subs_dual(s.get("subscribers")),
            "排名百分位": s.get("pct"),
            schema.COL_FIT: s.get("score"),
            schema.COL_LIGHT: s.get("action_grade") or "🟡",  # 恒 🟡(select 已保证)
            "身份标签": _flags_zh(s.get("identity_flags")),
            "命中主题": s.get("themes_hit") or [],
            schema.COL_THEME_TAGS: schema.theme_tags_zh(s.get("themes_hit") or []),
            "语义分": s.get("sem"), "甜点分": s.get("sweet"), "POV标记分": s.get("pov"),
            "值得签1": why[0], "值得签2": why[1], "值得签3": why[2],
            "风险": "B站无上传日期无法判活性, 联系前先人工看主页近一个月是否更新; 身份标签若有搬运/带货疑点以榜单表拦截原因为准",
            "首次合作建议": "先寄一台设备做一条第一视角原生体验短片, 看真实产出与调性再谈深度合作",
            # 在涨分/潜力分/起势分/浪层分/破圈比: B站无 RSS 视频级数据 → 结构性缺失, 不写(留空胜过写 0)
            # 完整档案: 只指向全池(YouTube)表 → 留空。档案(dossier): B站无档案管线 → 留空。
        })
    return rows


def _flags_zh(flags):
    """身份标签中文化(借用 identity_filter, 失败降级为原样 join)。"""
    try:
        import identity_filter
        return identity_filter.flags_zh(flags)
    except Exception:
        return "、".join(flags or [])


# ---------------------------------------------------------------------------
# 飞书写入(复用 run_radar/cards_enrich 同款最小 urllib 调用)。
# ---------------------------------------------------------------------------
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


def _date_ms(iso_day):
    return int(datetime.fromisoformat(iso_day + "T12:00:00").timestamp() * 1000)


def _ensure_platform_field(token, app_token, tid):
    """幂等确保 cards 表有「平台」文本列(W22 新列)。已存在则跳过。返回 True=存在/建成。"""
    pt = None
    existing = set()
    for _ in range(50):
        p = "/bitable/v1/apps/%s/tables/%s/fields?page_size=100" % (app_token, tid)
        if pt:
            p += "&page_token=" + pt
        r = _feishu_call("GET", p, token=token)
        if r.get("code") != 0:
            break
        d = r.get("data", {})
        for it in d.get("items", []):
            if it.get("field_name"):
                existing.add(it["field_name"])
        pt = d.get("page_token")
        if not d.get("has_more"):
            break
    if schema.COL_PLATFORM in existing:
        return True
    r = _feishu_call("POST", "/bitable/v1/apps/%s/tables/%s/fields" % (app_token, tid),
                     token=token, body={"field_name": schema.COL_PLATFORM, "type": 1})  # 1 = 文本
    return r.get("code") == 0


def _row_to_fields(r):
    """cards 表行 -> 飞书 fields。只写有值的列(None 留空胜过写 0 误导)。与 push_bitable 同款口径。"""
    f = {
        "日期": _date_ms(r["日期"]),
        "频道名": r["频道名"],
        "频道链接": {"link": r["频道链接"], "text": r["频道链接"]},
        schema.COL_PLATFORM: r.get(schema.COL_PLATFORM, ""),
        schema.COL_LIGHT: r.get(schema.COL_LIGHT, ""),
        "身份标签": r.get("身份标签", ""),
        schema.COL_THEME_TAGS: schema.theme_tags_zh(r.get("命中主题") or []),
        "是否新发现": "否",
        "值得签1": r["值得签1"], "值得签2": r["值得签2"], "值得签3": r["值得签3"],
        "风险": r["风险"], "首次合作建议": r["首次合作建议"],
    }
    if r.get("排名百分位") is not None:
        f["排名百分位"] = "%s%%" % r["排名百分位"]
    if r.get(schema.COL_SUBS_TEXT):
        f[schema.COL_SUBS_TEXT] = r[schema.COL_SUBS_TEXT]
    for k in ("排名", "订阅数", schema.COL_FIT, "语义分", "甜点分", "POV标记分"):
        if r.get(k) is not None:
            f[k] = r[k]
    return f


def _existing_bili_urls_today(token, app_token, tid, today):
    """防双写: 查 cards 表今天(日期=today)已有的 B站频道链接集合。
    写之前用它排除已在表里的频道(哪怕账本没记, 也不重复写行)。失败返回空集(不阻塞, 靠账本兜底)。"""
    urls = set()
    pt = None
    for _ in range(200):
        b = {"page_size": 500, "field_names": ["频道链接", "日期"]}
        if pt:
            b["page_token"] = pt
        r = _feishu_call("POST", "/bitable/v1/apps/%s/tables/%s/records/search" % (app_token, tid),
                         token=token, body=b)
        if r.get("code") != 0:
            break
        d = r.get("data", {})
        for it in d.get("items", []):
            f = it.get("fields") or {}
            dv = f.get("日期")
            day = None
            if isinstance(dv, (int, float)):
                from datetime import timezone
                day = datetime.fromtimestamp(dv / 1000, tz=timezone.utc).astimezone().date().isoformat()
            if day and day != today:
                continue
            u = f.get("频道链接")
            link = (u.get("link") if isinstance(u, dict)
                    else (u[0].get("link") if isinstance(u, list) and u and isinstance(u[0], dict) else "")) or ""
            if "bilibili.com" in link:
                urls.add(link.rstrip("/"))
        pt = d.get("page_token")
        if not d.get("has_more"):
            break
        time.sleep(0.15)
    return urls


def push_bili_cards(cfg, bscored, today, dry_run=False, seen_path=SEEN_LEDGER):
    """主入口: B站 ranked -> 选 🟡 top N -> 写 cards 表(平台=B站) -> 补头像 -> 记去重账本。
    失败安全: 任何一步失败只返回 {ok:False, error/skipped}, 绝不抛。dry_run 只选卡不真发。
    返回摘要 dict(供日报/日志)。"""
    try:
        cards_per_day = int(cfg.get("bilibili", {}).get("cards_per_day", DEFAULT_CARDS_PER_DAY))
        seen = load_seen(seen_path)
        picks = select_bili_cards(bscored, seen, cards_per_day)
        if not picks:
            return {"ok": True, "picked": 0, "note": "无符合条件的 🟡 新频道(或全被跨日去重)"}
        rows = build_bili_card_rows(picks, today)
        picked_names = [r["频道名"] for r in rows]

        if dry_run:
            return {"ok": True, "picked": len(rows), "dry_run": True, "names": picked_names,
                    "urls": [r["频道链接"] for r in rows],
                    "note": "dry-run: 选中如上, 未写表未记账本"}

        cred_path = os.path.expanduser(cfg.get("bitable", {}).get("credentials_path", ""))
        if not cred_path or not os.path.exists(cred_path):
            return {"ok": False, "error": "凭证缺失: %s" % (cred_path or "(未配置)")}
        cred = json.load(open(cred_path))
        t = _feishu_call("POST", "/auth/v3/tenant_access_token/internal",
                         body={"app_id": cred["app_id"], "app_secret": cred["app_secret"]})
        token = t.get("tenant_access_token")
        if not token:
            return {"ok": False, "error": "token failed: %s" % str(t.get("msg"))[:80]}
        app_token, tid = cred["app_token"], cred["table_id"]

        # 幂等建「平台」列(W22)
        _ensure_platform_field(token, app_token, tid)

        # 防双写(按日幂等): 只要今天已经有任何 B站卡在表里, 整批跳过——不再补第二批不同频道。
        # 这让同日多跑/重试都零副作用(每天只出一批 B站卡)。跨日去重另由 seen 账本保证(明天不推同频道)。
        # 表实查失败(返回空集)时退回账本兜底(seen 已排除今天写过的 url)。
        already = _existing_bili_urls_today(token, app_token, tid, today)
        if already:
            return {"ok": True, "picked": 0, "note": "今天已有 %d 张 B站卡在表里(按日幂等, 整批跳过)" % len(already),
                    "names": picked_names}

        records = [{"fields": _row_to_fields(r)} for r in rows]
        res = _feishu_call("POST",
                           "/bitable/v1/apps/%s/tables/%s/records/batch_create" % (app_token, tid),
                           token=token, body={"records": records})
        if res.get("code") != 0:
            return {"ok": False, "error": "batch_create failed code=%s %s" % (res.get("code"), str(res.get("msg"))[:80])}

        # 头像富化(只头像, 不做关联: 完整档案只指向 YouTube 全池表, B站按名匹配会误连)
        enrich = _enrich_bili_avatars(cfg, rows, res)

        # 记去重账本(写成功才记; 记的是真正写进表的这几个 url)
        for r in rows:
            seen[r["频道链接"]] = today
        saved = save_seen(seen, seen_path)

        return {"ok": True, "picked": len(rows), "names": [r["频道名"] for r in rows],
                "urls": [r["频道链接"] for r in rows],
                "avatar": enrich, "seen_ledger_saved": saved}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _enrich_bili_avatars(cfg, rows, batch_res):
    """给刚写进的 B站卡补头像(复用 cards_enrich.enrich_records, do_link=False)。失败安全: 只返回提示 dict。"""
    try:
        if cfg.get("bitable", {}).get("avatar_enrich") is False:
            return {"skipped": "avatar_enrich off"}
        created = (batch_res.get("data") or {}).get("records") or []
        targets = []
        for row, rec in zip(rows, created):
            rid = rec.get("record_id")
            if rid:
                targets.append({"record_id": rid, "name": row.get("频道名", ""), "url": row.get("频道链接", "")})
        if not targets:
            return {"skipped": "no record ids"}
        import cards_enrich
        # do_link=False: B站卡完整档案永远留空(关联只指向 YouTube 全池表)
        return cards_enrich.enrich_records(cfg, targets, do_avatar=True, do_link=False)
    except Exception as e:
        return {"error": str(e)[:80]}


# ---------------------------------------------------------------------------
# CLI: 今天先回填(读已有 ranked 产物, 不跑 radar 链)。
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="B站进推荐卡: 读今日 ranked 产物, 选 🟡 top N 写 cards 表")
    ap.add_argument("--config", required=True, help="B站品牌配置(config/insta360_bilibili.json)")
    ap.add_argument("--ranked", required=True, help="B站 ranked.json(data/runs/<date>-bilibili/ranked.json)")
    ap.add_argument("--date", default=date.today().isoformat(), help="推荐日期(默认今天)")
    ap.add_argument("--dry-run", action="store_true", help="只打印选中卡, 不写表不记账本")
    args = ap.parse_args()

    cfg = json.load(open(args.config))
    bscored = json.load(open(args.ranked))
    res = push_bili_cards(cfg, bscored, args.date, dry_run=args.dry_run)
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
