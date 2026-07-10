#!/usr/bin/env python3
"""达人雷达 · 驾驶舱(W23): 读当日真实产物 -> 聚合 -> 渲染单文件自包含 HTML。

背景: 飞书 Base 原生仪表盘 API 不开放程序化建图, 所以自建这页静态驾驶舱,
给 Max 每天扫一眼 + 比赛演示当门面。

铁律(与主链一致):
  · 只读聚合, 绝不内嵌 2430 原始行——只嵌聚合数字 + 今日 15 张卡 + 5 个 picks。
  · 纯本地文件, 零外发; 无任何飞书 URL/app_token/table_id(页面里只允许 deck 公开地址)。
  · 对外文字不用破折号; 署名只写 Max。
  · 冻结口径数字(盲测/拦截/密度)直接引用 docs/challenges.md, 不重算。

用法:
  python3 src/dashboard.py                 # 用最新一天的产物, 输出 reports/dashboard.html
  python3 src/dashboard.py --date 2026-07-10
  python3 src/dashboard.py --out /tmp/x.html
"""
import argparse
import base64
import glob
import html as _html
import json
import os
import subprocess
import tempfile
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
import sys
sys.path.insert(0, HERE)
import schema  # 单一来源: 订阅双格式 / 中文主题标签 / 平台字面量

REPORTS = os.path.join(ROOT, "reports")
DATA = os.path.join(ROOT, "data")
AVATARS = os.path.join(DATA, "avatars")

# ---------------------------------------------------------------------------
# 冻结口径(直接引用 docs/challenges.md; 禁止重算)。改这里前先回源。
# ---------------------------------------------------------------------------
FROZEN = {
    "blind_test": "4.62×",   # W6: 五主题权重±20% 四召回格全同; 前5%富集; 考试池 1106
    "intercept_rule": "76%",  # W15/R2-13: 规则 v1.1 ❌集拦截 25/33
    "intercept_ai": "94%",    # W18: 规则+AI ❌集拦截 31/33
    "green_density": "86%",   # W15/R2-13: 🟢队列密度 18/21
    "exam_pool": "1,106",
}
# 甜点区间正例富集(W6 + R2-1, 考试池 1106 回测, 非今日池)。
SWEET = {"core": "1.54×", "peak": "2.23×", "band": "10-30万"}
DECK_URL = "https://maxi-max-dev.github.io/creator-radar-deck/"

# 红绿灯四态 -> 状态色 + 中文。三真态用固定状态色, 🏢 官方联动(转别家 BD)用中性色。
LIGHT_META = [
    ("🟢", "可联系", "good"),
    ("🟡", "再看看", "warning"),
    ("🔴", "别碰", "critical"),
    ("🏢", "官方联动", "neutral"),
]
# 六主题 -> 固定分类色槽(schema THEME 顺序, 不循环不按值)。
# 色槽 = 参考调色板 blue/aqua/yellow/green/orange(t1-t5): 原 slot5 紫在暗色下与蓝在
#   protan 视觉塌缩(ΔE 2.5), 且横条按值排序天天变邻接, 故换 orange, 使 --pairs all
#   在 light+dark 双跑 exit-0 PASS(worst 10.3 落 8-12 floor 带, 靠每条名字+数值直标满足次级编码)。
# 极限挑战=B站专属主题, YT 管线不打标, 今日 0 命中 -> 渲染为中性(t6), 不占用状态红。
THEME_ORDER = list(schema.THEME_LABELS.values())  # POV原生/真实vlog/长途纪录/器材玩法/硬核技术/极限挑战
THEME_TOKEN = {lbl: f"t{i+1}" for i, lbl in enumerate(THEME_ORDER)}

# 订阅分桶(与 docs/challenges.md 甜点叙事对齐; 10-30万=甜点核区)。
SUB_BUCKETS = [
    (0, 10000, "<1万"),
    (10000, 30000, "1-3万"),
    (30000, 100000, "3-10万"),
    (100000, 300000, "10-30万"),   # 甜点核区
    (300000, 1000000, "30-100万"),
    (1000000, 3000000, "100-300万"),
    (3000000, 10**15, ">300万"),
]
SWEET_BUCKET = "10-30万"

# ---- CSS 变量单一来源(亮/暗独立取色, 各自 dataviz 校验通过) ----
LIGHT_VARS = {
    "page": "#f9f9f7", "surface": "#fcfcfb", "card": "#ffffff",
    "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781",
    "grid": "#e1e0d9", "axis": "#c3c2b7", "border": "rgba(11,11,11,0.10)",
    "shadow": "rgba(11,11,11,0.07)",
    "good": "#0ca30c", "warning": "#fab219", "critical": "#d03b3b", "neutral": "#898781",
    "blue": "#2a78d6", "blueStrong": "#1c5cab", "blueWash": "rgba(42,120,214,0.12)",
    "t1": "#2a78d6", "t2": "#1baf7a", "t3": "#eda100", "t4": "#008300", "t5": "#eb6834", "t6": "#898781",
}
DARK_VARS = {
    "page": "#0d0d0d", "surface": "#1a1a19", "card": "#201f1d",
    "ink": "#ffffff", "ink2": "#c3c2b7", "muted": "#898781",
    "grid": "#2c2c2a", "axis": "#383835", "border": "rgba(255,255,255,0.10)",
    "shadow": "rgba(0,0,0,0.45)",
    "good": "#0ca30c", "warning": "#fab219", "critical": "#d03b3b", "neutral": "#898781",
    "blue": "#3987e5", "blueStrong": "#6da7ec", "blueWash": "rgba(57,135,229,0.18)",
    "t1": "#3987e5", "t2": "#199e70", "t3": "#c98500", "t4": "#008300", "t5": "#d95926", "t6": "#898781",
}


# ===========================================================================
# 数据装载与聚合
# ===========================================================================
def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _latest_daily(preferred=None):
    """最新一天的 YT daily 目录(含 ranked.json)。preferred 命中优先。"""
    dirs = {}
    for d in glob.glob(os.path.join(DATA, "runs", "daily", "*")):
        r = os.path.join(d, "ranked.json")
        if os.path.exists(r):
            dirs[os.path.basename(d)] = d
    if not dirs:
        return None, None
    if preferred and preferred in dirs:
        return preferred, dirs[preferred]
    key = max(dirs)
    return key, dirs[key]


def _latest_bili():
    files = sorted(glob.glob(os.path.join(DATA, "runs", "*-bilibili", "ranked.json")))
    return files[-1] if files else None


def _latest_picks():
    files = sorted(glob.glob(os.path.join(DATA, "scoreboard", "picks-*.json")))
    return files[0] if files else None  # 最早一份 = 首期记分板


def _grade_dist(rows):
    counts = {m[0]: 0 for m in LIGHT_META}
    for r in rows:
        g = r.get("action_grade")
        if g in counts:
            counts[g] += 1
        elif g:
            counts.setdefault(g, 0)
            counts[g] += 1
    return counts


def aggregate(date_pref=None):
    """把当日真实产物聚合成一个渲染就绪的 dict(全是小数字 + 今日榜单, 无原始行)。"""
    day_key, day_dir = _latest_daily(date_pref)
    yt = _load(os.path.join(day_dir, "ranked.json")) if day_dir else None
    yt = yt or []
    bili_path = _latest_bili()
    bili = _load(bili_path) if bili_path else None
    bili = bili or []
    bili_date = os.path.basename(os.path.dirname(bili_path)).replace("-bilibili", "") if bili_path else "?"

    # 红绿灯分布
    yt_grade = _grade_dist(yt)
    bili_grade = _grade_dist(bili)

    # 六主题命中(YT; 一个频道可多命中)
    theme_counts = {lbl: 0 for lbl in THEME_ORDER}
    for r in yt:
        for k in (r.get("themes_hit") or []):
            lbl = schema.THEME_LABELS.get(k)
            if lbl in theme_counts:
                theme_counts[lbl] += 1

    # 订阅分桶(YT 全池)
    buckets = {lab: 0 for *_, lab in SUB_BUCKETS}
    no_sub = 0
    for r in yt:
        s = r.get("subscribers")
        if not isinstance(s, (int, float)):
            no_sub += 1
            continue
        for lo, hi, lab in SUB_BUCKETS:
            if lo <= s < hi:
                buckets[lab] += 1
                break

    # 今日推荐卡: YT(cards.json) + B站(seen 账本里当天 carded)
    yt_by_url = {r.get("channel_url"): r for r in yt}
    cards_raw = _load(os.path.join(day_dir, "cards.json")) if day_dir else None
    cards_raw = cards_raw or []
    yt_cards = []
    for c in cards_raw:
        url = c.get("_channel_url") or c.get("channel_url") or ""
        cid = url.rsplit("/", 1)[-1] if url else ""
        rec = yt_by_url.get(url, {})
        why = c.get("why_worth_signing") or []
        yt_cards.append({
            "name": c.get("channel_name", "?"),
            "cid": cid,
            "grade": rec.get("action_grade", ""),
            "subs": rec.get("subscribers"),
            "fit": rec.get("score"),
            "rank": c.get("_rank"),
            "evidence": (why[0] if why else "").strip(),
        })

    bili_by_url = {r.get("channel_url"): r for r in bili}
    seen = _load(os.path.join(DATA, "scoreboard", "bili_cards_seen.json")) or {}
    carded = [(u, v) for u, v in seen.items() if v.get("status") == "carded"]
    today_bili = [u for u, v in carded if v.get("date") == day_key]
    if not today_bili:  # 兜底: 若当天无, 用账本里最近一天 carded
        if carded:
            latest_d = max(v.get("date", "") for _, v in carded)
            today_bili = [u for u, v in carded if v.get("date") == latest_d]
    bili_cards = []
    for u in today_bili:
        rec = bili_by_url.get(u, {})
        cid = u.rsplit("/", 1)[-1]
        bili_cards.append({
            "name": rec.get("channel_name", "?"),
            "cid": cid,
            "grade": rec.get("action_grade", "🟡"),
            "subs": rec.get("subscribers"),
            "fit": rec.get("score"),
            "evidence": "命中影石选人内核，待补活性数据（平台无上传日期）",
        })

    # 推荐卡累计(YT 跨日去重 + B站账本 carded 累计)
    yt_urls = set()
    for f in glob.glob(os.path.join(DATA, "runs", "daily", "*", "cards.json")):
        for c in (_load(f) or []):
            u = c.get("_channel_url") or c.get("channel_url")
            if u:
                yt_urls.add(u)
    cards_cum = len(yt_urls) + len(carded)
    cards_today = len(yt_cards) + len(bili_cards)

    # 记分板(首期 picks)
    picks_path = _latest_picks()
    picks_data = _load(picks_path) if picks_path else {}
    picks_data = picks_data or {}
    picked_date = picks_data.get("picked_date", "")
    reveal = ""
    days_left = None
    if picked_date:
        try:
            pd = datetime.strptime(picked_date, "%Y-%m-%d").date()
            rv = date.fromordinal(pd.toordinal() + 28)  # 结算周期 = 建仓 + 28 天 = 8/4
            reveal = rv.isoformat()
            days_left = (rv - date.today()).days
        except Exception:
            pass
    picks = []
    for p in (picks_data.get("picks") or [])[:5]:
        picks.append({
            "name": p.get("channel_name", "?"),
            "rank": p.get("rank_at_pick"),
            "subs": p.get("subscribers_baseline"),
        })

    return {
        "day_key": day_key or "?",
        "bili_date": bili_date,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "yt_total": len(yt),
        "bili_total": len(bili),
        "pool_total": len(yt) + len(bili),
        "yt_grade": yt_grade,
        "bili_grade": bili_grade,
        "green_total": yt_grade.get("🟢", 0) + bili_grade.get("🟢", 0),
        "theme_counts": theme_counts,
        "buckets": buckets,
        "no_sub": no_sub,
        "sub_plotted": sum(buckets.values()),
        "yt_cards": yt_cards,
        "bili_cards": bili_cards,
        "cards_cum": cards_cum,
        "cards_today": cards_today,
        "picks": picks,
        "picked_date": picked_date,
        "reveal": reveal,
        "days_left": days_left,
        "pool_at_pick": picks_data.get("pool_size"),
    }


# ===========================================================================
# 渲染工具
# ===========================================================================
def esc(s):
    return _html.escape(str(s if s is not None else ""), quote=True)


def fmt_int(n):
    try:
        return format(int(n), ",")
    except Exception:
        return "?"


def subs_dual(n):
    return schema.subs_dual(n) if n is not None else "订阅未知"


def pct(n, total):
    return 0.0 if not total else n / total * 100.0


def pct_txt(p):
    if p == 0:
        return "0%"
    if p < 0.1:
        return "<0.1%"
    return f"{p:.1f}%"


def _lum(hexv):
    hexv = hexv.lstrip("#")
    r, g, b = (int(hexv[i:i + 2], 16) / 255 for i in (0, 2, 4))
    def lin(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def ink_on(hexv):
    """填充色上的文字取白或墨(按亮度), 保证可读。"""
    return "#0b0b0b" if _lum(hexv) > 0.42 else "#ffffff"


_AVATAR_CACHE = {}
def avatar_data_uri(cid):
    """data/avatars/<cid>.jpg -> sips 缩 64px -> base64 data URI。缺图返回 None(走字母兜底)。"""
    if not cid:
        return None
    if cid in _AVATAR_CACHE:
        return _AVATAR_CACHE[cid]
    src = os.path.join(AVATARS, cid + ".jpg")
    uri = None
    if os.path.exists(src):
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(suffix=".jpg")
            os.close(fd)
            r = subprocess.run(["sips", "-Z", "64", src, "--out", tmp],
                               capture_output=True, timeout=25)
            if r.returncode == 0 and os.path.getsize(tmp) > 0:
                with open(tmp, "rb") as f:
                    uri = "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()
        except Exception:
            uri = None
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
    _AVATAR_CACHE[cid] = uri
    return uri


# ===========================================================================
# 各区块 HTML
# ===========================================================================
def _kpi_row(a):
    g_share = pct_txt(pct(a["green_total"], a["pool_total"]))
    tiles = [
        ("全池规模", fmt_int(a["pool_total"]),
         f"YouTube {fmt_int(a['yt_total'])} · B站 {fmt_int(a['bili_total'])}", ""),
        ("可直接联系", f"🟢 {fmt_int(a['green_total'])}",
         f"占全池 {g_share}（B站 0，见下）", "good"),
        ("推荐卡累计", fmt_int(a["cards_cum"]),
         f"今日 +{a['cards_today']}（YT {len(a['yt_cards'])} · B站 {len(a['bili_cards'])}）", ""),
        ("盲测命中", FROZEN["blind_test"],
         f"前 5% 富集（考试池 {FROZEN['exam_pool']}）", ""),
        ("AI 复核拦截", FROZEN["intercept_ai"],
         f"规则 {FROZEN['intercept_rule']} 加 AI 到 {FROZEN['intercept_ai']}", ""),
    ]
    out = ['<div class="kpi-row">']
    for label, value, sub, accent in tiles:
        cls = "kpi" + (" kpi-good" if accent == "good" else "")
        out.append(
            f'<div class="{cls}"><div class="kpi-label">{esc(label)}</div>'
            f'<div class="kpi-value">{esc(value)}</div>'
            f'<div class="kpi-sub">{esc(sub)}</div></div>'
        )
    out.append("</div>")
    return "".join(out)


def _stacked_bar(counts, total):
    """一条 100% 堆叠红绿灯条(段间 2px 缝, 段内 icon+数字若放得下)。"""
    segs = []
    for emoji, label, token in LIGHT_META:
        n = counts.get(emoji, 0)
        if n <= 0:
            continue
        p = pct(n, total)
        fill = f"var(--{token})"
        txt = ink_on({"good": LIGHT_VARS["good"], "warning": LIGHT_VARS["warning"],
                      "critical": LIGHT_VARS["critical"], "neutral": LIGHT_VARS["neutral"]}[token])
        inner = ""
        if p >= 9:  # 放得下才写段内标签, 否则留给 tooltip + 图例(不裁字)
            inner = f'<span style="color:{txt}">{esc(emoji)} {fmt_int(n)}</span>'
        tip = f"{emoji} {label}：{fmt_int(n)}（{pct_txt(p)}）"
        segs.append(
            f'<div class="seg hit" style="flex:{max(p,0.35)} 0 0;background:{fill}" '
            f'data-tip="{esc(tip)}">{inner}</div>'
        )
    return f'<div class="stack">{"".join(segs)}</div>'


def _redlight_section(a):
    legend = []
    for emoji, label, token in LIGHT_META:
        legend.append(
            f'<span class="lg"><span class="sw" style="background:var(--{token})"></span>'
            f'{esc(emoji)} {esc(label)}</span>'
        )
    # 表视图孪生
    rows = []
    for name, counts, total in [("YouTube", a["yt_grade"], a["yt_total"]),
                                ("B站", a["bili_grade"], a["bili_total"])]:
        for emoji, label, _ in LIGHT_META:
            n = counts.get(emoji, 0)
            rows.append(f"<tr><td>{esc(name)}</td><td>{esc(emoji)} {esc(label)}</td>"
                        f"<td class='num'>{fmt_int(n)}</td><td class='num'>{pct_txt(pct(n,total))}</td></tr>")
    table = ("<table class='tbl'><thead><tr><th>平台</th><th>红绿灯</th><th class='num'>频道数</th>"
             "<th class='num'>占比</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>")

    return f"""
<section class="panel">
  <div class="panel-head">
    <h2>红绿灯分布</h2>
    <p class="sub">能不能直接联系。🟢队列密度 {FROZEN['green_density']}（规则 v1.1 复核，考试池回测）。</p>
  </div>
  <div class="legend">{"".join(legend)}</div>
  <div class="bar-block">
    <div class="bar-label"><span>YouTube</span><span class="muted">{fmt_int(a['yt_total'])} 频道</span></div>
    {_stacked_bar(a['yt_grade'], a['yt_total'])}
  </div>
  <div class="bar-block">
    <div class="bar-label"><span>B站</span><span class="muted">{fmt_int(a['bili_total'])} 频道</span></div>
    {_stacked_bar(a['bili_grade'], a['bili_total'])}
  </div>
  <p class="note">B站 0 个 🟢：平台不提供上传日期，活性无法自动判断，一律保守落 🟡 待人工核，不是真的没有可联系的人。</p>
  <details class="twin"><summary>数据表</summary>{table}</details>
</section>"""


def _theme_section(a):
    counts = a["theme_counts"]
    order = sorted(THEME_ORDER, key=lambda l: counts[l], reverse=True)
    mx = max(counts.values()) or 1
    rows = []
    for lbl in order:
        n = counts[lbl]
        token = THEME_TOKEN[lbl]
        w = n / mx * 100
        if n == 0:
            bar = ('<div class="hbar-track"><div class="hbar" style="width:0"></div></div>'
                   f'<div class="hbar-val muted">0 · B站专属主题</div>')
        else:
            bar = (f'<div class="hbar-track hit" data-tip="{esc(lbl)}：{fmt_int(n)} 个频道命中">'
                   f'<div class="hbar" style="width:{w:.1f}%;background:var(--{token})"></div></div>'
                   f'<div class="hbar-val">{fmt_int(n)}</div>')
        rows.append(f'<div class="hrow"><div class="hbar-name">{esc(lbl)}</div>{bar}</div>')
    tbl_rows = "".join(f"<tr><td>{esc(l)}</td><td class='num'>{fmt_int(counts[l])}</td></tr>"
                       for l in THEME_ORDER)
    table = ("<table class='tbl'><thead><tr><th>主题</th><th class='num'>命中频道数</th></tr></thead><tbody>"
             + tbl_rows + "</tbody></table>")
    return f"""
<section class="panel">
  <div class="panel-head">
    <h2>六主题命中分布</h2>
    <p class="sub">命中该内容主题的 YouTube 频道数（一个频道可命中多个）。主题内核来自影石官方选人逻辑逆向工程。</p>
  </div>
  <div class="hbars">{"".join(rows)}</div>
  <details class="twin"><summary>数据表</summary>{table}</details>
</section>"""


def _histogram_section(a):
    buckets = a["buckets"]
    labels = [lab for *_, lab in SUB_BUCKETS]
    vals = [buckets[l] for l in labels]
    mx = max(vals) or 1
    # SVG 几何
    W, H = 680, 250
    padL, padR, padT, padB = 44, 16, 24, 44
    plotW = W - padL - padR
    plotH = H - padT - padB
    n = len(labels)
    slot = plotW / n
    barW = min(24.0, slot * 0.5)
    base_y = padT + plotH
    parts = [f'<svg class="hist" viewBox="0 0 {W} {H}" role="img" '
             f'aria-label="订阅量分布直方图" preserveAspectRatio="xMidYMid meet">']
    # 基线
    parts.append(f'<line x1="{padL}" y1="{base_y}" x2="{W-padR}" y2="{base_y}" '
                 f'stroke="var(--axis)" stroke-width="1"/>')
    # 一条 max 网格
    gy = padT
    parts.append(f'<line x1="{padL}" y1="{gy}" x2="{W-padR}" y2="{gy}" stroke="var(--grid)" stroke-width="1"/>')
    parts.append(f'<text x="{padL-8}" y="{gy+4}" text-anchor="end" class="ax">{fmt_int(mx)}</text>')
    parts.append(f'<text x="{padL-8}" y="{base_y+4}" text-anchor="end" class="ax">0</text>')
    for i, lab in enumerate(labels):
        cx = padL + slot * i + slot / 2
        v = vals[i]
        bh = v / mx * plotH
        by = base_y - bh
        is_sweet = (lab == SWEET_BUCKET)
        if is_sweet:  # 甜点核区: 半透明色带 + 更强蓝(同色系加重)
            parts.append(f'<rect x="{cx-slot/2:.1f}" y="{padT}" width="{slot:.1f}" height="{plotH}" '
                         f'fill="var(--blueWash)"/>')
        fill = "var(--blueStrong)" if is_sweet else "var(--blue)"
        # 圆角数据端(顶), 底部方
        parts.append(
            f'<path d="M{cx-barW/2:.1f},{by+min(4,bh):.1f} '
            f'q0,-4 4,-4 h{barW-8:.1f} q4,0 4,4 v{bh-min(4,bh):.1f} h{-barW:.1f} z" '
            f'fill="{fill}"/>' if bh > 4 else
            f'<rect x="{cx-barW/2:.1f}" y="{by:.1f}" width="{barW:.1f}" height="{max(bh,1):.1f}" fill="{fill}"/>'
        )
        # 透明命中区(整列, 便于 hover)
        parts.append(f'<rect class="hit" x="{cx-slot/2:.1f}" y="{padT}" width="{slot:.1f}" height="{plotH}" '
                     f'fill="transparent" data-tip="{esc(lab)}：{fmt_int(v)} 个频道"/>')
        # 顶部数值 + 底部桶标签
        parts.append(f'<text x="{cx:.1f}" y="{by-6:.1f}" text-anchor="middle" class="val">{fmt_int(v)}</text>')
        parts.append(f'<text x="{cx:.1f}" y="{base_y+16:.1f}" text-anchor="middle" class="ax">{esc(lab)}</text>')
    # 甜点富集标注(在色带上方)
    sweet_i = labels.index(SWEET_BUCKET)
    scx = padL + slot * sweet_i + slot / 2
    parts.append(f'<text x="{scx:.1f}" y="{base_y+32:.1f}" text-anchor="middle" class="sweet-tag">'
                 f'甜点核区 · 正例富集 {SWEET["peak"]}</text>')
    parts.append("</svg>")
    tbl_rows = "".join(f"<tr><td>{esc(l)}</td><td class='num'>{fmt_int(buckets[l])}</td></tr>" for l in labels)
    table = ("<table class='tbl'><thead><tr><th>订阅区间</th><th class='num'>频道数</th></tr></thead><tbody>"
             + tbl_rows + f"<tr><td>无订阅数据</td><td class='num'>{fmt_int(a['no_sub'])}</td></tr></tbody></table>")
    return f"""
<section class="panel">
  <div class="panel-head">
    <h2>订阅量分布</h2>
    <p class="sub">今日全池 {fmt_int(a['sub_plotted'])} 个有订阅数据的频道。</p>
  </div>
  {"".join(parts)}
  <p class="note">10-30万 是甜点核区，正例在此富集 {SWEET['peak']}；核区 [1万,30万) 整体富集 {SWEET['core']}
  （考试池 {FROZEN['exam_pool']} 回测，非今日池实时值）。另有 {fmt_int(a['no_sub'])} 个频道无订阅数据未计入分桶。</p>
  <details class="twin"><summary>数据表</summary>{table}</details>
</section>"""


def _grade_chip(grade):
    for emoji, label, token in LIGHT_META:
        if grade == emoji:
            return f'<span class="chip chip-{token}">{esc(emoji)} {esc(label)}</span>'
    return f'<span class="chip">{esc(grade)}</span>' if grade else ""


def _card(c, platform):
    uri = avatar_data_uri(c["cid"])
    if uri:
        av = f'<img class="av" src="{uri}" alt="" loading="lazy"/>'
    else:
        initial = esc((c["name"] or "?")[0])
        av = f'<div class="av av-mono">{initial}</div>'
    subs = subs_dual(c["subs"])
    fit = f'对路 {c["fit"]:.2f}' if isinstance(c.get("fit"), (int, float)) else ""
    plat_tag = f'<span class="ptag">{esc(platform)}</span>'
    ev = esc(c["evidence"])[:120]
    return f"""<div class="card">
  <div class="card-top">{av}<div class="card-id">
    <div class="card-name">{esc(c['name'])}</div>
    <div class="card-meta">{_grade_chip(c['grade'])}{plat_tag}</div>
  </div></div>
  <div class="card-subs">{esc(subs)}</div>
  <div class="card-fit">{esc(fit)}</div>
  <div class="card-ev">{ev}</div>
</div>"""


def _cards_section(a):
    yt = "".join(_card(c, "YouTube") for c in a["yt_cards"])
    bili = "".join(_card(c, "B站") for c in a["bili_cards"])
    bili_block = ""
    if a["bili_cards"]:
        bili_block = f"""
  <div class="cards-subhead">B站 · {len(a['bili_cards'])} 张</div>
  <p class="note">B站卡均为 🟡：平台无上传日期，先当补活性数据的候选进人工池，不是 🟢 直联。</p>
  <div class="cards-grid">{bili}</div>"""
    return f"""
<section class="panel">
  <div class="panel-head">
    <h2>今日推荐卡</h2>
    <p class="sub">当天筛出、最值得先联系的账号。头像为公开频道图，缩至 64px 内嵌。</p>
  </div>
  <div class="cards-subhead">YouTube · {len(a['yt_cards'])} 张</div>
  <div class="cards-grid">{yt}</div>{bili_block}
</section>"""


def _scoreboard_section(a):
    picks = a["picks"]
    if picks:
        rows = "".join(
            f'<tr><td class="num">#{esc(p["rank"])}</td><td>{esc(p["name"])}</td>'
            f'<td class="num">{esc(subs_dual(p["subs"]))}</td></tr>'
            for p in picks
        )
        picks_tbl = ("<table class='tbl picks'><thead><tr><th class='num'>排名</th><th>频道</th>"
                     "<th class='num'>建仓订阅</th></tr></thead><tbody>" + rows + "</tbody></table>")
    else:
        picks_tbl = "<p class='note'>暂无记分板存档。</p>"
    dl = a["days_left"]
    cd_val = f"{dl}" if isinstance(dl, int) and dl >= 0 else "已开奖"
    cd_unit = "天后开奖" if isinstance(dl, int) and dl >= 0 else ""
    return f"""
<section class="panel">
  <div class="panel-head">
    <h2>记分板 · 首期</h2>
    <p class="sub">{esc(a['picked_date'])} 建仓 5 位，{esc(a['reveal'])} 结算（建仓 28 天）。</p>
  </div>
  <div class="sb-wrap">
    <div class="sb-count">
      <div class="sb-num">{esc(cd_val)}</div>
      <div class="sb-unit">{esc(cd_unit)}</div>
    </div>
    <div class="sb-body">
      {picks_tbl}
      <p class="note">结算时比这 5 位的订阅增速与播放速度，是否跑赢全池中位。建仓时 view 基线当日未留（记为空），故只做订阅口径回看加播放增速定性判断。建仓池 {fmt_int(a['pool_at_pick'])}。</p>
    </div>
  </div>
</section>"""


def _footer(a):
    lines = [
        "数据来源：YouTube 与 B站公开元数据（订阅数、公开视频标题与计数），非私有数据。",
        "日期精度：B站不提供上传日期，活性判断保守落 🟡 待人工核，故 B站 0 🟢。",
        "AI 复核只降级不升级：🟢 降 🟡 加旗，绝不自动 🔴、绝不物理删除，退回仍留全池榜单待人工终审。",
        f"冻结口径（盲测 {FROZEN['blind_test']} / 拦截 {FROZEN['intercept_ai']} / 密度 {FROZEN['green_density']}）"
        f"来自考试池 {FROZEN['exam_pool']} 回测，非今日池实时值。",
    ]
    li = "".join(f"<li>{esc(x)}</li>" for x in lines)
    return f"""
<footer class="foot">
  <ul>{li}</ul>
  <div class="foot-bar">
    <span>生成于 {esc(a['generated'])} · 引擎 v1.2 · 数据 {esc(a['day_key'])}（B站 {esc(a['bili_date'])}）</span>
    <a href="{DECK_URL}" target="_blank" rel="noopener">完整方法与迭代史 →</a>
  </div>
  <div class="foot-sign">Max · 达人雷达</div>
</footer>"""


# ===========================================================================
# 页面组装
# ===========================================================================
def _css():
    def block(d):
        return "\n".join(f"  --{k}: {v};" for k, v in d.items())
    return CSS_TEMPLATE.replace("/*__LIGHT__*/", block(LIGHT_VARS)) \
                       .replace("/*__DARK__*/", block(DARK_VARS)) \
                       .replace("/*__DARK2__*/", block(DARK_VARS))


def render(a):
    header = f"""
<header class="hero">
  <div class="hero-top">
    <div>
      <h1>达人雷达 · 驾驶舱</h1>
      <p class="hero-sub">影石潜力达人每日快照 · 数据截至 {esc(a['generated'])}</p>
    </div>
    <div class="hero-right">
      <div class="badges">
        <span class="badge">YouTube {fmt_int(a['yt_total'])}</span>
        <span class="badge">B站 {fmt_int(a['bili_total'])}</span>
      </div>
      <button class="theme-btn" type="button" onclick="__toggleTheme()" aria-label="切换深浅色">
        <span id="theme-ico">◐</span> <span id="theme-txt">深色</span>
      </button>
    </div>
  </div>
</header>"""
    body = "".join([
        header,
        _kpi_row(a),
        _redlight_section(a),
        _theme_section(a),
        _histogram_section(a),
        _cards_section(a),
        _scoreboard_section(a),
        _footer(a),
    ])
    return (
        "<!doctype html>\n<html lang=\"zh-CN\">\n<head>\n"
        "<meta charset=\"utf-8\"/>\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/>\n"
        "<title>达人雷达 · 驾驶舱</title>\n"
        f"<style>\n{_css()}\n</style>\n</head>\n<body>\n"
        f'<main class="wrap">\n{body}\n</main>\n'
        '<div id="tip" class="tip" role="tooltip"></div>\n'
        f"<script>\n{JS}\n</script>\n</body>\n</html>\n"
    )


def generate(date=None, out=None):
    a = aggregate(date_pref=date)
    html_str = render(a)
    out = out or os.path.join(REPORTS, "dashboard.html")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html_str)
    return out


# ===========================================================================
# 静态资源(CSS / JS)
# ===========================================================================
CSS_TEMPLATE = """
:root {
/*__LIGHT__*/
}
:root[data-theme="dark"] {
/*__DARK__*/
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
/*__DARK2__*/
  }
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; overflow-x: hidden; max-width: 100%; }
body {
  background: var(--page); color: var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  line-height: 1.5; -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 1080px; margin: 0 auto; padding: 24px 20px 48px; }
h1, h2 { margin: 0; font-weight: 650; letter-spacing: -0.01em; }
h1 { font-size: 26px; }
h2 { font-size: 18px; }
.muted { color: var(--muted); }
.num { font-variant-numeric: tabular-nums; text-align: right; }

/* hero */
.hero { margin-bottom: 20px; }
.hero-top { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; flex-wrap: wrap; }
.hero-sub { margin: 6px 0 0; color: var(--ink2); font-size: 14px; }
.hero-right { display: flex; flex-direction: column; align-items: flex-end; gap: 10px; }
.badges { display: flex; gap: 8px; }
.badge {
  font-size: 12px; font-weight: 600; padding: 4px 10px; border-radius: 999px;
  background: var(--surface); border: 1px solid var(--border); color: var(--ink2);
  font-variant-numeric: tabular-nums;
}
.theme-btn {
  font: inherit; font-size: 13px; cursor: pointer; padding: 5px 12px; border-radius: 8px;
  background: var(--surface); border: 1px solid var(--border); color: var(--ink2);
}
.theme-btn:hover { border-color: var(--axis); }

/* kpi */
.kpi-row { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 22px; }
.kpi {
  background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
  padding: 14px 16px; box-shadow: 0 1px 2px var(--shadow); border-top: 2px solid var(--blue);
}
.kpi-good { border-top-color: var(--good); }
.kpi-label { font-size: 12px; color: var(--muted); }
.kpi-value { font-size: 30px; font-weight: 680; margin: 4px 0 2px; letter-spacing: -0.02em; }
.kpi-sub { font-size: 11.5px; color: var(--ink2); }

/* panels */
.panel {
  background: var(--surface); border: 1px solid var(--border); border-radius: 14px;
  padding: 20px 22px; margin-bottom: 18px; box-shadow: 0 1px 2px var(--shadow);
}
.panel-head { margin-bottom: 14px; }
.panel-head .sub { margin: 5px 0 0; font-size: 13px; color: var(--ink2); }
.note { font-size: 12.5px; color: var(--ink2); margin: 12px 0 0; }

/* stacked red-light */
.legend { display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 12px; }
.lg { font-size: 12.5px; color: var(--ink2); display: inline-flex; align-items: center; gap: 6px; }
.sw { width: 12px; height: 12px; border-radius: 3px; display: inline-block; }
.bar-block { margin: 10px 0 4px; }
.bar-label { display: flex; justify-content: space-between; font-size: 13px; font-weight: 600; margin-bottom: 5px; }
.bar-label .muted { font-weight: 400; font-variant-numeric: tabular-nums; }
.stack {
  display: flex; gap: 2px; height: 38px; background: var(--surface);
  border-radius: 8px; overflow: hidden;
}
.seg {
  display: flex; align-items: center; justify-content: center; min-width: 3px;
  font-size: 13px; font-weight: 600; font-variant-numeric: tabular-nums;
  transition: filter .12s; cursor: default; white-space: nowrap;
}
.seg:hover { filter: brightness(1.06); }

/* horizontal theme bars */
.hbars { display: flex; flex-direction: column; gap: 9px; }
.hrow { display: grid; grid-template-columns: 76px 1fr auto; align-items: center; gap: 10px; }
.hbar-name { font-size: 13px; color: var(--ink2); }
.hbar-track { background: transparent; border-radius: 5px; min-height: 20px; display: flex; align-items: center; }
.hbar { height: 20px; border-radius: 0 4px 4px 0; min-width: 2px; }
.hbar-val { font-size: 13px; font-variant-numeric: tabular-nums; color: var(--ink); min-width: 40px; }

/* histogram svg */
.hist { width: 100%; max-width: 100%; height: auto; display: block; margin-top: 4px; }
.hist .ax { fill: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.hist .val { fill: var(--ink); font-size: 11.5px; font-weight: 600; font-variant-numeric: tabular-nums; }
.hist .sweet-tag { fill: var(--blueStrong); font-size: 11.5px; font-weight: 600; }
.hist .hit { cursor: default; }

/* cards */
.cards-subhead { font-size: 13px; font-weight: 650; margin: 6px 0 8px; color: var(--ink); }
.cards-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(224px, 1fr)); gap: 12px; }
.card {
  background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 12px 13px;
}
.card-top { display: flex; gap: 10px; align-items: center; margin-bottom: 8px; }
.av { width: 40px; height: 40px; border-radius: 50%; object-fit: cover; flex: 0 0 40px; background: var(--grid); }
.av-mono {
  display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 18px;
  color: #fff; background: var(--blue);
}
.card-id { min-width: 0; }
.card-name { font-size: 14px; font-weight: 640; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.card-meta { display: flex; gap: 6px; align-items: center; margin-top: 3px; }
.chip { font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 999px; border: 1px solid var(--border); white-space: nowrap; }
.chip-good { background: rgba(12,163,12,0.13); color: var(--good); border-color: rgba(12,163,12,0.30); }
.chip-warning { background: rgba(250,178,25,0.16); color: #9a6a00; border-color: rgba(250,178,25,0.40); }
.chip-critical { background: rgba(208,59,59,0.13); color: var(--critical); border-color: rgba(208,59,59,0.30); }
.chip-neutral { background: rgba(137,135,129,0.15); color: var(--ink2); border-color: var(--border); }
.ptag { font-size: 10.5px; color: var(--muted); border: 1px solid var(--border); border-radius: 5px; padding: 1px 6px; }
.card-subs { font-size: 12.5px; color: var(--ink); font-variant-numeric: tabular-nums; }
.card-fit { font-size: 12px; color: var(--ink2); margin-top: 1px; font-variant-numeric: tabular-nums; }
.card-ev { font-size: 12px; color: var(--ink2); margin-top: 7px; line-height: 1.45;
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
  overflow-wrap: anywhere; word-break: break-word; }
.card-subs, .kpi-sub, .card-fit { overflow-wrap: anywhere; }

/* scoreboard */
.sb-wrap { display: flex; gap: 20px; align-items: stretch; flex-wrap: wrap; }
.sb-count {
  flex: 0 0 140px; background: var(--card); border: 1px solid var(--border); border-radius: 12px;
  display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 16px;
}
.sb-num { font-size: 46px; font-weight: 700; line-height: 1; letter-spacing: -0.02em; }
.sb-unit { font-size: 12.5px; color: var(--muted); margin-top: 6px; }
.sb-body { flex: 1 1 300px; min-width: 260px; }

/* tables (twin views) */
.twin { margin-top: 12px; }
.twin summary { cursor: pointer; font-size: 12.5px; color: var(--muted); }
.tbl { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 12.5px; }
.tbl th, .tbl td { text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--border); }
.tbl th { color: var(--muted); font-weight: 600; }
.tbl td.num, .tbl th.num { text-align: right; }
.picks td, .picks th { padding: 6px 8px; }

/* footer */
.foot { margin-top: 26px; padding-top: 16px; border-top: 1px solid var(--border); }
.foot ul { margin: 0; padding-left: 18px; }
.foot li { font-size: 11.5px; color: var(--muted); margin: 3px 0; }
.foot-bar { display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap;
  font-size: 11.5px; color: var(--muted); margin-top: 12px; font-variant-numeric: tabular-nums; }
.foot-bar a { color: var(--blue); text-decoration: none; }
.foot-bar a:hover { text-decoration: underline; }
.foot-sign { font-size: 12px; color: var(--ink2); margin-top: 8px; font-weight: 600; }

/* tooltip */
.tip {
  position: fixed; z-index: 50; pointer-events: none; opacity: 0; transition: opacity .1s;
  background: var(--ink); color: var(--page); font-size: 12px; padding: 5px 9px; border-radius: 6px;
  max-width: 260px; box-shadow: 0 2px 8px var(--shadow);
}

@media (max-width: 860px) {
  .kpi-row { grid-template-columns: repeat(2, 1fr); }
  .kpi-value { font-size: 26px; }
}
@media (max-width: 520px) {
  .wrap { padding: 16px 12px 36px; }
  .kpi-row { grid-template-columns: 1fr 1fr; gap: 8px; }
  .panel { padding: 16px 14px; }
  .hrow { grid-template-columns: 64px 1fr auto; }
  .hero-right { align-items: flex-start; }
}
"""

JS = """
(function () {
  var root = document.documentElement;
  var url = new URL(location.href);
  var q = url.searchParams.get('theme');
  var saved = null;
  try { saved = localStorage.getItem('radar-theme'); } catch (e) {}
  var t = (q === 'dark' || q === 'light') ? q : saved;
  if (t === 'dark' || t === 'light') root.setAttribute('data-theme', t);
  function curTheme() {
    var a = root.getAttribute('data-theme');
    if (a) return a;
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  function paintBtn() {
    var d = curTheme() === 'dark';
    var ico = document.getElementById('theme-ico');
    var txt = document.getElementById('theme-txt');
    if (ico) ico.textContent = d ? '☀' : '◐';
    if (txt) txt.textContent = d ? '浅色' : '深色';
  }
  window.__toggleTheme = function () {
    var next = curTheme() === 'dark' ? 'light' : 'dark';
    root.setAttribute('data-theme', next);
    try { localStorage.setItem('radar-theme', next); } catch (e) {}
    paintBtn();
  };
  // tooltip: 读 data-tip(纯属性), 用 textContent 写入(不信任数据不进 innerHTML)
  var tip = document.getElementById('tip');
  function show(e) {
    var el = e.target.closest('.hit');
    if (!el || !el.dataset.tip) { hide(); return; }
    tip.textContent = el.dataset.tip;
    tip.style.opacity = '1';
    var x = (e.clientX || 0) + 12, y = (e.clientY || 0) + 14;
    var w = tip.offsetWidth, vw = window.innerWidth;
    if (x + w > vw - 8) x = vw - w - 8;
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
  }
  function hide() { tip.style.opacity = '0'; }
  document.addEventListener('pointermove', show);
  document.addEventListener('pointerleave', hide);
  document.addEventListener('DOMContentLoaded', paintBtn);
  paintBtn();
})();
"""


def main():
    ap = argparse.ArgumentParser(description="达人雷达驾驶舱 HTML 生成器")
    ap.add_argument("--date", default=None, help="指定 YT daily 日期(YYYY-MM-DD), 默认最新")
    ap.add_argument("--out", default=None, help="输出路径, 默认 reports/dashboard.html")
    args = ap.parse_args()
    path = generate(date=args.date, out=args.out)
    size = os.path.getsize(path)
    print(f"驾驶舱已生成: {path} ({size/1024:.0f} KB)")


if __name__ == "__main__":
    main()
