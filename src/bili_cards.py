#!/usr/bin/env python3
"""达人雷达 · B站(国内侧)进推荐卡(W22 2026-07-08 Max「还不错, 目前只有国外的, 国内的呢」)。

背景: cards(达人推荐)表历史 17 行全是 YouTube; B站 319 人只躺在 B站榜单表, 从没进过推荐卡。
本模块让 B站达人每天也出 2-3 张推荐卡, 写进同一张 cards 表(平台列=B站, 一眼分清)。

铁律(与主链一致的失败安全 + 诚实口径):
  · 只给 🟡 出卡: 绝不给 🔴(别碰)/🏢(转官方)出卡。按现有 fit 排名口径取前几名。
  · **AI 复核闸门(W22.1 2026-07-08 总控验收抓刺)**: 光看 🟡+fit 排名会把自家审计判过 ❌ 的
    翻车货推上门面(实证=W22 首批 3 张里 StoneFPV 搬运 conf0.95 / 大鹏快开车 离题 conf0.80,
    均为 W18 审计 ❌ 名单成员; 病根=当日 ranked 产物早于 W18 主链复核, 不带 ai_review 标记)。
    修法=候选出卡前逐个过 src/ai_review.py 复核, 判退证据按序认三个来源:
      ① bscored 行已带 ai_review_flagged(明晨起主链 W18 层在 B站线内先跑, 行上自带标记);
      ② data/ai_review/<date>.jsonl 当日缓存命中 → 复用 verdict(幂等不重打);
      ③ 现场真跑 qwen3:8b 思考态(单候选 timeout 90s, 结果落缓存)。
    高置信负面判定(搬运/机构/离题, conf>=confidence_threshold 默认 0.70)一律不出卡,
    顺位补下一个 🟡; 闸门扫描窗 = min(需求数+GATE_BUFFER, GATE_SCAN_CAP=6) 个候选。
    **宁缺不错**: ollama/复核整体不可用时当天 B站卡整批跳过(日报如实写一行), 推荐门面
    质量优先于可用性。单候选拿不到 verdict(解析失败)也不出卡, 只跳过不记永久拒。
  · 跨日去重账本 data/scoreboard/bili_cards_seen.json(**v2, W22.1 语义升级**):
      {channel_url: {"date":..., "status":"carded"|"rejected", "reason":...}}
    carded=出过卡, 永不再推; rejected=AI 复核判退, 永不出卡(也不再重复复核)。
    旧 v1 格式({url: 日期串})读入时自动归一为 carded。两种状态都从候选里排除。
  · 按日补足(top-up, W22.1): 今天表里已有 K 张 B站卡则只补 (cards_per_day - K) 张;
    K>=配额时整批跳过。同日重跑零副作用, 删错卡换卡后可补位。
  · B站无上传日期(平台不给)→ 全体 🟡/🔴, 结构性零 🟢。证据摘要如实写「活性待人工核一眼」。
  · 完整档案(单向关联)只能指向全池(YouTube)表 → B站卡此列**留空**, 不硬凑(enrich 时 do_link=False)。
  · 档案(dossier)链接列: B站线无 explain/档案管线 → **留空**(不为此扩建)。
  · 数量开关: cfg["bilibili"]["cards_per_day"](默认 2)。
  · 失败安全: 本模块任何一步炸了都只返回错误 dict/串, 绝不抛, 绝不影响 YouTube 卡或主链。
    调用方(run_radar)用 bare try 再包一层, 双保险。

两种用法:
  A) run_radar 每日主链: B站线跑完拿到 bscored(已带主链 AI 复核标记), 调 push_bili_cards(...)。
  B) 回填/换卡: python3 src/bili_cards.py --config config/insta360_bilibili.json \
        --ranked data/runs/<date>-bilibili/ranked.json --date <date>
     (读已有 ranked 产物, 不跑 radar 链)。--dry-run 只选卡+过闸门, 不写表不记账本。

复用全仓惯例: 标准库 urllib, 敏感物只从 credentials 文件读, 温和降级。
"""
import argparse, json, os, sys, time
import urllib.request, urllib.error
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import schema
import ai_review as _ai

SEEN_LEDGER = os.path.join(ROOT, "data", "scoreboard", "bili_cards_seen.json")
DEFAULT_CARDS_PER_DAY = 2
GATE_BUFFER = 3      # 闸门扫描窗 = 需求数 + 缓冲(被判退就顺位补)
GATE_SCAN_CAP = 6    # 扫描窗封顶(总控定的上限, 防复核无限烧时间)
UNAVAILABLE_NOTE = "B站卡今日跳过：AI 复核不可用(宁缺不错)"


# ---------------------------------------------------------------------------
# 跨日去重账本 v2: {url: {"date", "status": carded|rejected, "reason"?}}。
# carded=出过卡永不再推; rejected=AI 判退永不出卡。v1(纯日期串)自动归一为 carded。
# ---------------------------------------------------------------------------
def load_seen(path=SEEN_LEDGER):
    try:
        raw = json.load(open(path)) if os.path.exists(path) else {}
    except Exception:
        return {}
    out = {}
    for url, v in raw.items():
        if isinstance(v, dict):
            out[url] = v
        else:  # v1 兼容: 值是日期串 = 出过卡
            out[url] = {"date": str(v), "status": "carded"}
    return out


def save_seen(seen, path=SEEN_LEDGER):
    """写去重账本(失败静默: 账本写不成不该炸主链, 最坏情况明天可能重推一次, 可接受)。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(seen, f, ensure_ascii=False, indent=1, sort_keys=True)
        return True
    except Exception:
        return False


def mark_seen(seen, url, status, day, reason=None):
    """就地记账。rejected 永不被 carded 覆盖(判退是更强语义); carded 不降级为 rejected 之外的态。"""
    cur = seen.get(url)
    if cur and cur.get("status") == "rejected":
        return  # 已判退, 不覆盖
    rec = {"date": day, "status": status}
    if reason:
        rec["reason"] = reason
    seen[url] = rec


# ---------------------------------------------------------------------------
# 候选排序: 只 🟡, 按 fit rank, 跳过账本里(出过卡/判退)与 exclude(今天已在表)的频道。
# ---------------------------------------------------------------------------
def select_bili_cards(bscored, seen, n, exclude=()):
    """从 B站 ranked(已按 fit rank 升序)里取前 n 个合格候选(**未过 AI 闸门的原始候选**)。
    规则: action_grade 必须 == 🟡(🔴 别碰 / 🏢 转官方 / 无灯 一律不出);
    channel_url 不在 seen 账本(carded/rejected 都排除)也不在 exclude 集合里。"""
    picks = []
    ex = {u.rstrip("/") for u in exclude}
    for s in sorted(bscored, key=lambda x: x.get("rank", 10 ** 9)):
        if s.get("action_grade") != "🟡":
            continue                         # 只给黄灯出卡, 绝不 🔴/🏢
        url = s.get("channel_url") or ""
        if not url or url in seen or url.rstrip("/") in ex:
            continue                         # 账本(出过卡/判退) + 今日已在表 都排除
        picks.append(s)
        if len(picks) >= n:
            break
    return picks


# ---------------------------------------------------------------------------
# AI 复核闸门(W22.1)。
# ---------------------------------------------------------------------------
def _ollama_up(rcfg, timeout=4):
    """健康检查: ollama /api/tags 通则视为复核可用。失败=整体不可用(宁缺不错走整批跳过)。"""
    try:
        url = rcfg["endpoint"].rsplit("/api/", 1)[0] + "/api/tags"
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as r:
            r.read(64)
        return True
    except Exception:
        return False


def _flag_reason_from_row(s):
    """bscored 行已带 ai_review_flagged 时, 从 grade_reasons 里捞出 AI 复核那条中文理由。"""
    for r in (s.get("grade_reasons") or []):
        if "AI复核" in str(r):
            return str(r)
    return "主链AI复核已标记(ai_review_flagged)"


def gated_select(bscored, seen, need, pool_by_url, cfg, today, exclude=(), progress=False):
    """带 AI 闸门的选卡(懒式: 凑够 need 就停, 最多扫 min(need+GATE_BUFFER, GATE_SCAN_CAP) 个候选)。

    返回 dict(绝不抛):
      picks:      通过闸门的 scored 行(<= need)
      rejected:   [(row, reason, source)] 被判退的候选(source: flag|cache|live)
      no_verdict: [name] 拿不到 verdict 的候选(解析失败, 跳过不出卡也不永久拒)
      unavailable: True=复核整体不可用(ollama 不通), 调用方应整批跳过
      reviewed:   实际过闸门的候选数
    """
    out = {"picks": [], "rejected": [], "no_verdict": [], "unavailable": False, "reviewed": 0}
    try:
        rcfg = dict(_ai.DEFAULTS)
        rcfg.update(cfg.get("ai_review", {}) or {})
        scan_n = min(need + GATE_BUFFER, GATE_SCAN_CAP)
        candidates = select_bili_cards(bscored, seen, scan_n, exclude=exclude)
        if not candidates:
            return out
        cache = _ai._load_cache(today)
        tags = _ai.load_scoring_tags()
        health_checked = ollama_ok = False
        for s in candidates:
            if len(out["picks"]) >= need:
                break
            out["reviewed"] += 1
            name = s.get("channel_name") or "?"
            url = s.get("channel_url") or ""
            # 来源①: 主链复核已在行上贴标(明晨起 bscored 自带) → 直接判退
            if _ai.AI_FLAG in (s.get("identity_flags") or []):
                out["rejected"].append((s, _flag_reason_from_row(s), "flag"))
                if progress:
                    print("  [bili_gate] REJECT(行标) %s" % name, file=sys.stderr)
                continue
            # 来源②: 当日缓存
            cid = s.get("channel_id") or url
            cached = cache.get(cid)
            if cached and "verdict" in cached:
                verdict, src = cached["verdict"], "cache"
            else:
                # 来源③: 现场真跑(先健康检查一次; ollama 不通 = 整体不可用, 宁缺不错)
                if not health_checked:
                    ollama_ok = _ollama_up(rcfg)
                    health_checked = True
                if not ollama_ok:
                    out["unavailable"] = True
                    return out
                verdict = _ai.review_candidate(s, dict(pool_by_url.get(url, {})), tags.get(cid), rcfg)
                src = "live"
                if verdict is not None:
                    _ai._append_cache(today, {
                        "channel_id": cid, "channel_name": name,
                        "grade_before": s.get("action_grade"),
                        "verdict": verdict, "reviewed_by": "bili_cards_gate",
                        "reviewed_at": datetime.now().isoformat(timespec="seconds"),
                    })
            if verdict is None:
                out["no_verdict"].append(name)   # 拿不到判决 = 不出卡(宁缺不错), 但不永久拒
                if progress:
                    print("  [bili_gate] NO-VERDICT(跳过不出卡) %s" % name, file=sys.stderr)
                continue
            do_reject, reason = _ai.should_downgrade(verdict, rcfg)
            if do_reject:
                out["rejected"].append((s, reason, src))
                if progress:
                    print("  [bili_gate] REJECT(%s) %s | %s" % (src, name, reason), file=sys.stderr)
            else:
                s.setdefault("_gate", {})["verdict"] = verdict   # 留复核通过记录(报告/存证用)
                out["picks"].append(s)
                if progress:
                    print("  [bili_gate] PASS(%s) %s conf=%.2f" % (src, name, verdict.get("confidence", 0)),
                          file=sys.stderr)
        return out
    except Exception as e:
        # 闸门自身炸了 = 视作复核不可用(宁缺不错), 绝不带病出卡
        out["unavailable"] = True
        out["error"] = str(e)
        return out


def _load_pool_by_url(cfg):
    """读 B站池子(闸门复核语料: 简介/近期标题)。失败返回空 dict(复核仍可跑, 语料少一点)。"""
    try:
        from radar_lib import load_pool
        pool_path = os.path.join(ROOT, cfg.get("pool_path", "data/pool/bilibili_pool.jsonl"))
        return {r["channel_url"]: r for r in load_pool(pool_path)}
    except Exception:
        return {}


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
    平台=B站; 红绿灯照搬(必为 🟡); 完整档案/档案留空; 在涨/潜力/起势/浪/破圈 B站结构性无 → 不带。
    W22.1: 通过闸门的卡, 风险列如实注明「已过AI复核」+ conf, 让运营知道这张卡的复核状态。"""
    rows = []
    for s in picks:
        why = _evidence_lines(s)
        gate_v = (s.get("_gate") or {}).get("verdict")
        risk = "B站无上传日期无法判活性, 联系前先人工看主页近一个月是否更新"
        if gate_v:
            risk += "; 已过AI复核(qwen3:8b, conf %.2f, 无搬运/机构/离题疑点)" % gate_v.get("confidence", 0)
        else:
            risk += "; 身份标签若有搬运/带货疑点以榜单表拦截原因为准"
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
            "风险": risk,
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
    """查 cards 表今天(日期=today)已有的 B站频道链接集合(top-up 配额与防双写用)。
    失败返回空集(不阻塞, 靠账本兜底)。"""
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


def _gate_summary(g):
    """闸门结果 -> 报告用摘要(判退名单带理由, 复核通过带 conf)。"""
    return {
        "rejected": [{"name": s.get("channel_name"), "url": s.get("channel_url"),
                      "reason": reason, "source": src} for s, reason, src in g.get("rejected", [])],
        "no_verdict": g.get("no_verdict", []),
        "reviewed": g.get("reviewed", 0),
    }


def push_bili_cards(cfg, bscored, today, dry_run=False, seen_path=SEEN_LEDGER, pool_by_url=None):
    """主入口: B站 ranked -> 🟡 候选过 AI 闸门 -> 写 cards 表(平台=B站) -> 补头像 -> 记账本。
    失败安全: 任何一步失败只返回 {ok:False, error/skipped}, 绝不抛。dry_run 只选卡+过闸门不真发。
    返回摘要 dict(供日报/日志)。"""
    try:
        cards_per_day = int(cfg.get("bilibili", {}).get("cards_per_day", DEFAULT_CARDS_PER_DAY))
        seen = load_seen(seen_path)
        if pool_by_url is None:
            pool_by_url = _load_pool_by_url(cfg)

        # dry-run: 不碰飞书(配额按满额算, exclude 空), 闸门照过(本地 ollama 无网络副作用), 不记账本。
        if dry_run:
            g = gated_select(bscored, seen, cards_per_day, pool_by_url, cfg, today, progress=True)
            if g.get("unavailable"):
                return {"ok": True, "picked": 0, "dry_run": True, "note": UNAVAILABLE_NOTE,
                        "gate": _gate_summary(g)}
            picks = g["picks"]
            return {"ok": True, "picked": len(picks), "dry_run": True,
                    "names": [s.get("channel_name") for s in picks],
                    "urls": [s.get("channel_url") for s in picks],
                    "gate": _gate_summary(g),
                    "note": "dry-run: 选中如上(已过AI闸门), 未写表未记账本"}

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

        # 按日补足配额(W22.1): 今天已有 K 张 → 只补 cards_per_day-K 张; K>=配额整批跳过。
        already = _existing_bili_urls_today(token, app_token, tid, today)
        remaining = cards_per_day - len(already)
        if remaining <= 0:
            return {"ok": True, "picked": 0,
                    "note": "今天已有 %d 张 B站卡在表里(配额 %d 已满, 整批跳过)" % (len(already), cards_per_day)}

        # AI 闸门选卡(宁缺不错: 复核不可用整批跳过)
        g = gated_select(bscored, seen, remaining, pool_by_url, cfg, today,
                         exclude=already, progress=True)
        if g.get("unavailable"):
            return {"ok": True, "picked": 0, "note": UNAVAILABLE_NOTE, "gate": _gate_summary(g)}
        picks = g["picks"]
        # 判退的候选记账本(rejected, 永不出卡也不再重复复核) —— 无论本批是否凑够卡都要记。
        for s, reason, _src in g.get("rejected", []):
            mark_seen(seen, s.get("channel_url") or "", "rejected", today, reason=reason)
        if not picks:
            save_seen(seen, seen_path)
            return {"ok": True, "picked": 0, "gate": _gate_summary(g),
                    "note": "扫描窗内无通过 AI 闸门的 🟡 新频道(宁缺不错)"}
        rows = build_bili_card_rows(picks, today)

        records = [{"fields": _row_to_fields(r)} for r in rows]
        res = _feishu_call("POST",
                           "/bitable/v1/apps/%s/tables/%s/records/batch_create" % (app_token, tid),
                           token=token, body={"records": records})
        if res.get("code") != 0:
            save_seen(seen, seen_path)   # 判退记录仍落盘(判退与写卡失败无关)
            return {"ok": False, "error": "batch_create failed code=%s %s" % (res.get("code"), str(res.get("msg"))[:80]),
                    "gate": _gate_summary(g)}

        # 头像富化(只头像, 不做关联: 完整档案只指向 YouTube 全池表, B站按名匹配会误连)
        enrich = _enrich_bili_avatars(cfg, rows, res)

        # 记账本: 写成功的卡 = carded
        for r in rows:
            mark_seen(seen, r["频道链接"], "carded", today)
        saved = save_seen(seen, seen_path)

        return {"ok": True, "picked": len(rows), "names": [r["频道名"] for r in rows],
                "urls": [r["频道链接"] for r in rows],
                "gate": _gate_summary(g),
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
# CLI: 回填/换卡(读已有 ranked 产物, 不跑 radar 链)。
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="B站进推荐卡: 读 ranked 产物, 🟡 候选过 AI 闸门后写 cards 表")
    ap.add_argument("--config", required=True, help="B站品牌配置(config/insta360_bilibili.json)")
    ap.add_argument("--ranked", required=True, help="B站 ranked.json(data/runs/<date>-bilibili/ranked.json)")
    ap.add_argument("--date", default=date.today().isoformat(), help="推荐日期(默认今天)")
    ap.add_argument("--dry-run", action="store_true", help="只选卡+过AI闸门, 不写表不记账本")
    args = ap.parse_args()

    cfg = json.load(open(args.config))
    bscored = json.load(open(args.ranked))
    res = push_bili_cards(cfg, bscored, args.date, dry_run=args.dry_run)
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
