#!/usr/bin/env python3
"""达人雷达 · 飞书「全池榜单」表同步。

把当日全池排序整表写进飞书多维表格的第二张表「全池榜单」(与「达人推荐」表分开)。
列结构单一来源 = schema.FULL_RANKING_COLUMNS(三词化: 对路分/在涨分/潜力分/红绿灯 + 起势/浪/破圈证据列)。

表 id 存 credentials 同目录的 feishu_tables.json (repo 外, 敏感标识符不进仓库):
  { "full_ranking_table_id": "...", "bili_ranking_table_id": "..." }

每日同步策略 = 先清后写 (clear-then-write):
  取全表 record_id -> batch_delete -> batch_create(每批<=500)。
  选它而非"删表重建": 表 id 稳定不变, 任何飞书侧的字段自动化/引用/视图不被打断,
  只有表 id 首次不存在时才建表。删表重建每次换 id, 会打断引用, 更脆。

复用 run_radar 的调用形态: 标准库 urllib, 失败返回错误 dict 不抛, 温和降级不中断主链。

命令行(手动全量重写一次):
  python3 src/sync_full_ranking.py --config config/insta360.json \
      --ranked data/runs/2026-07-07-phase2/ranked.json
"""
import argparse, json, os, sys, time
import urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import schema

# 飞书字段类型: 1=多行文本 2=数字 3=单选 4=多选 5=日期 15=超链接
FT_TEXT, FT_NUMBER, FT_SINGLE, FT_MULTI, FT_DATE, FT_URL = 1, 2, 3, 4, 5, 15

FULL_RANKING_TABLE_NAME = "全池榜单"
BILI_RANKING_TABLE_NAME = "B站榜单"

# 列结构单一来源 = schema.py。这里只把列名映射到飞书字段类型(顺序即建表顺序)。
# 三词化(2026-07-07): 总分->对路分, 行动分级->红绿灯, 新增在涨分; 起势/浪/破圈=工程证据列。
# W17(2026-07-08): 红绿灯 → 单选(带颜色 🟢🟡🔴🏢); 主题标签 → 多选(中文标签, 各色); 新增 订阅(文本双格式)+ 证据摘要(文本给 AI)。
# 建字段幂等: _ensure_table 会先查已存在字段, 只补缺的(老表也能加新列); 改名走 migrate_field_names。
_FT = {
    "排名": FT_NUMBER, "频道名": FT_TEXT, "频道链接": FT_URL,
    "订阅数": FT_NUMBER, schema.COL_SUBS_TEXT: FT_TEXT,
    schema.COL_FIT: FT_NUMBER, schema.COL_RISING: FT_NUMBER, schema.COL_POTENTIAL: FT_NUMBER,
    schema.COL_LIGHT: FT_SINGLE, "身份标签": FT_TEXT, "拦截原因": FT_TEXT,
    "起势分": FT_NUMBER, "浪层分": FT_NUMBER, "破圈比": FT_NUMBER,
    schema.COL_THEME_TAGS: FT_MULTI, "垂类": FT_TEXT, "语言": FT_TEXT,
    schema.COL_EVIDENCE: FT_TEXT, "档案": FT_URL, "入池日期": FT_TEXT,
}

# 预置选项 + 颜色(飞书 color index 0-54)。建字段时一次配好, 写入端只给选项名, 飞书自动上色。
# 红绿灯单选四态: 🟢绿 / 🟡黄 / 🔴红 / 🏢蓝(官方联动)。
_LIGHT_OPTIONS = [
    {"name": "🟢", "color": 3},    # 绿
    {"name": "🟡", "color": 1},    # 黄
    {"name": "🔴", "color": 0},    # 红
    {"name": "🏢官方联动", "color": 8},   # 蓝(区别于三色灯, Max: 别家公司变个颜色转官方 BD)
]
# 主题标签多选六项(中文标签来自 schema.THEME_LABELS), 各配一色便于一眼分辨。
_THEME_COLOR = {
    "POV原生": 5, "真实vlog": 6, "长途纪录": 7, "器材玩法": 9, "硬核技术": 10, "极限挑战": 11,
}
_THEME_OPTIONS = [{"name": lbl, "color": _THEME_COLOR.get(lbl, 4)} for lbl in schema.THEME_LABELS.values()]

# 列名 -> 预置选项(仅 select/multi 列有)。_reconcile_fields 建这些列时带上 property.options。
_FIELD_OPTIONS = {
    schema.COL_LIGHT: _LIGHT_OPTIONS,
    schema.COL_THEME_TAGS: _THEME_OPTIONS,
}

FULL_RANKING_FIELDS = [(n, _FT[n]) for n in schema.FULL_RANKING_COLUMNS]

# B站榜单列集来自 schema(去掉档案 + 视频级证据)。
_BILI_DROP = schema._BILI_DROP
BILI_RANKING_FIELDS = [(n, _FT[n]) for n in schema.BILI_RANKING_COLUMNS]

# 旧列名 -> 新列名(改名迁移用): 老表已有这些列, 用 update 字段名保住历史数据不删列。
FIELD_RENAMES = {"总分": schema.COL_FIT, "行动分级": schema.COL_LIGHT}


def _feishu_call(method, path, token=None, body=None):
    """飞书开放平台最小调用(标准库 urllib)。失败返回 {code:-1,...} 不抛。
    与 run_radar._feishu_call / feishu_docs._feishu_call 同形。"""
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
    return t.get("tenant_access_token"), t


def _tables_path(cfg):
    """表 id 账本路径: credentials 同目录的 feishu_tables.json。"""
    cred_path = os.path.expanduser(cfg.get("bitable", {}).get("credentials_path", ""))
    return os.path.join(os.path.dirname(cred_path), "feishu_tables.json")


def _load_tables(cfg):
    p = _tables_path(cfg)
    if os.path.exists(p):
        try:
            return json.load(open(p))
        except Exception:
            return {}
    return {}


def _save_tables(cfg, d):
    p = _tables_path(cfg)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def _list_field_names(token, app_token, tid):
    """列出表现有字段名集合(分页取全)。失败返回空集(调用方按'没有已知字段'处理)。"""
    names, page_token = set(), None
    for _ in range(50):
        path = "/bitable/v1/apps/%s/tables/%s/fields?page_size=100" % (app_token, tid)
        if page_token:
            path += "&page_token=" + page_token
        r = _feishu_call("GET", path, token=token)
        if r.get("code") != 0:
            break
        data = r.get("data", {})
        for it in data.get("items", []):
            if it.get("field_name"):
                names.add(it["field_name"])
        page_token = data.get("page_token")
        if not data.get("has_more"):
            break
    return names


def _list_fields(token, app_token, tid):
    """列出表现有字段的 {field_name: field_id} 映射(分页取全)。失败返回空 dict。
    改名迁移要拿 field_id 调 update API, 故比 _list_field_names 多带 id。"""
    out, page_token = {}, None
    for _ in range(50):
        path = "/bitable/v1/apps/%s/tables/%s/fields?page_size=100" % (app_token, tid)
        if page_token:
            path += "&page_token=" + page_token
        r = _feishu_call("GET", path, token=token)
        if r.get("code") != 0:
            break
        data = r.get("data", {})
        for it in data.get("items", []):
            if it.get("field_name") and it.get("field_id"):
                out[it["field_name"]] = it["field_id"]
        page_token = data.get("page_token")
        if not data.get("has_more"):
            break
    return out


def _migrate_field_names(token, app_token, tid, renames):
    """列改名迁移(幂等): 老表若还叫旧名(总分/行动分级), 用字段 update API 原地改成新名,
    **保住该列历史数据**(改名不动数据), 不删列、不加重复列。
    幂等三态: 只有旧名在 -> 改名; 新名已在(旧名不在) -> 已迁过, 跳过; 旧名新名都在 ->
    别人已手建了新列, 保守不动(避免冲突, 留给人工); 两者都无 -> 全新表由 _reconcile 建新列。
    醒目日志: 每次真发出改名请求都打 RENAME 大写横幅(便于明早心跳日志一眼可查)。
    构造正确性(旧名->新名 + field_id)由离线单测 test_field_rename.py 校验, 不发真请求。"""
    existing = _list_fields(token, app_token, tid)   # {name: field_id}
    migrated = []
    for old_name, new_name in renames.items():
        if old_name not in existing:
            continue                                  # 旧名不在: 已迁过 or 全新表, 无需动
        if new_name in existing:
            print("  [RENAME-SKIP] 「%s」与「%s」同表并存, 保守不动(留人工)" % (old_name, new_name))
            continue                                  # 两者并存: 保守不动
        fid = existing[old_name]
        print("  >>> RENAME 飞书列改名: 「%s」-> 「%s」 (field_id=%s, 保历史数据不删列)" % (old_name, new_name, fid))
        r = _feishu_call("PUT", "/bitable/v1/apps/%s/tables/%s/fields/%s" % (app_token, tid, fid),
                         token=token, body={"field_name": new_name})
        if r.get("code") == 0:
            print("  >>> RENAME 成功: 「%s」现名「%s」" % (old_name, new_name))
            migrated.append((old_name, new_name))
        else:
            print("  >>> RENAME 失败 code=%s %s (下次心跳重试)" % (r.get("code"), str(r.get("msg"))[:80]))
        time.sleep(0.1)
    return migrated


def _reconcile_fields(token, app_token, tid, fields):
    """幂等补字段: 查现有字段名, 只建缺的。返回本次新建的字段名列表。
    W17: select/multi 字段(红绿灯/主题标签)建时带 property.options(预置选项 + 颜色), 飞书写入端只给选项名即可。"""
    existing = _list_field_names(token, app_token, tid)
    added = []
    for name, ftype in fields:
        if name in existing:
            continue
        body = {"field_name": name, "type": ftype}
        if name in _FIELD_OPTIONS:
            body["property"] = {"options": _FIELD_OPTIONS[name]}
        c = _feishu_call("POST", "/bitable/v1/apps/%s/tables/%s/fields" % (app_token, tid),
                         token=token, body=body)
        if c.get("code") == 0:
            added.append(name)
        time.sleep(0.1)
    return added


def _migrate_select_fields(token, app_token, tid):
    """W17 类型迁移(仅 先清后写 表 = 全池/B站, 有历史的 cards 表**不走此路**):
    把老的**文本** 红绿灯 列换成 单选(带颜色), 并删除废弃的英文 命中主题 文本列(由中文多选 主题标签 取代)。

    先清后写表每天整表重写, 删列/换类型不损历史(反正当天会清空重灌), 故允许:
      - 红绿灯: 若现存为文本(type 1) → 删旧列; 下一步 _reconcile 会以单选(type 3)重建并配色。
      - 命中主题: 存在即删(不再要英文 key 文本列, 中文 主题标签 多选替位)。
    幂等: 只在'旧形态存在'时动手; 已是目标形态则跳过。返回操作记录 dict。"""
    fields_meta = {}
    page_token = None
    for _ in range(50):
        path = "/bitable/v1/apps/%s/tables/%s/fields?page_size=100" % (app_token, tid)
        if page_token:
            path += "&page_token=" + page_token
        r = _feishu_call("GET", path, token=token)
        if r.get("code") != 0:
            break
        data = r.get("data", {})
        for it in data.get("items", []):
            if it.get("field_name") and it.get("field_id") is not None:
                fields_meta[it["field_name"]] = (it["field_id"], it.get("type"))
        page_token = data.get("page_token")
        if not data.get("has_more"):
            break
    ops = {"deleted": []}
    # 红绿灯: 文本(1) → 删, 让 reconcile 以单选(3)重建带颜色。已是单选(3)则不动。
    if schema.COL_LIGHT in fields_meta:
        fid, ftype = fields_meta[schema.COL_LIGHT]
        if ftype == FT_TEXT:
            d = _feishu_call("DELETE", "/bitable/v1/apps/%s/tables/%s/fields/%s" % (app_token, tid, fid), token=token)
            if d.get("code") == 0:
                ops["deleted"].append("%s(旧文本→重建单选)" % schema.COL_LIGHT)
            time.sleep(0.1)
    # 命中主题(英文 key 文本列): 存在即删(中文 主题标签 多选替位)。
    if "命中主题" in fields_meta:
        fid, _ = fields_meta["命中主题"]
        d = _feishu_call("DELETE", "/bitable/v1/apps/%s/tables/%s/fields/%s" % (app_token, tid, fid), token=token)
        if d.get("code") == 0:
            ops["deleted"].append("命中主题(英文列废弃→中文主题标签替位)")
        time.sleep(0.1)
    # W17 收尾: 老改名迁移(总分->对路分 / 行动分级->红绿灯)因目标列已是不同类型(数字/单选)导致 RENAME
    # 反复 field validation failed, 留下**空的**旧名孤儿列。先清后写表数据已进新列, 孤儿列可安全删。
    # 只在'旧名与新名同表并存'时删旧名(避免误删真正承载数据的列)。
    for old_name, new_name in FIELD_RENAMES.items():
        if old_name in fields_meta and new_name in fields_meta:
            fid, _ = fields_meta[old_name]
            d = _feishu_call("DELETE", "/bitable/v1/apps/%s/tables/%s/fields/%s" % (app_token, tid, fid), token=token)
            if d.get("code") == 0:
                ops["deleted"].append("%s(空孤儿列, 数据已在 %s)" % (old_name, new_name))
            time.sleep(0.1)
    return ops


def _ensure_table(token, app_token, table_key, table_name, fields, tables_ledger, cfg):
    """确保表存在且字段齐全, 返回 (table_id, created)。table_key = 账本里的键。
    首次不存在则建表; 已存在则**幂等补齐缺失字段**(老表加新列: 先查字段存在再建)。
    表 id 落账本(repo 外)。若账本里有 id 但飞书侧已被删(GET fields 报错), 重建。"""
    tid = tables_ledger.get(table_key)
    if tid:
        chk = _feishu_call("GET", "/bitable/v1/apps/%s/tables/%s/fields?page_size=1" % (app_token, tid), token=token)
        if chk.get("code") == 0:
            # 表在: 顺序要紧 ——
            #   1) 改名迁移(老列名 总分/行动分级 -> 对路分/红绿灯, 保历史数据; 否则 reconcile 当缺列重建=重复)。
            #   2) W17 类型迁移(先清后写表): 老文本 红绿灯 → 删, 废弃英文 命中主题 → 删(下一步以单选/多选重建)。
            #   3) 幂等补齐 schema 里但表上缺的字段(在涨分/订阅/主题标签/证据摘要 + 重建的红绿灯单选)。
            _migrate_field_names(token, app_token, tid, FIELD_RENAMES)
            _migrate_select_fields(token, app_token, tid)
            _reconcile_fields(token, app_token, tid, fields)
            return tid, False
        # 账本里的 id 已失效, 落空重建
        tid = None

    # 建表(飞书要求建表时至少一个字段; 用第一个字段作主字段)
    first = fields[0]
    body = {"table": {"name": table_name, "default_view_name": "全部",
                      "fields": [{"field_name": first[0], "type": first[1]}]}}
    r = _feishu_call("POST", "/bitable/v1/apps/%s/tables" % app_token, token=token, body=body)
    if r.get("code") != 0:
        return None, False
    tid = r.get("data", {}).get("table_id")
    # 补齐其余字段
    _reconcile_fields(token, app_token, tid, fields[1:])
    tables_ledger[table_key] = tid
    _save_tables(cfg, tables_ledger)
    return tid, True


def _clear_table(token, app_token, tid):
    """清空表全部记录(先清后写的清)。反复取第一页(<=500)删掉, 直到取空。
    已删记录不再返回, 无需处理分页 token 漂移, 逻辑天然收敛。"""
    deleted = 0
    for _ in range(500):  # 上限保护(<=25 万行)
        r = _feishu_call("POST", "/bitable/v1/apps/%s/tables/%s/records/search" % (app_token, tid),
                         token=token, body={"page_size": 500})
        if r.get("code") != 0:
            break
        ids = [it.get("record_id") for it in r.get("data", {}).get("items", []) if it.get("record_id")]
        if not ids:
            break
        d = _feishu_call("POST", "/bitable/v1/apps/%s/tables/%s/records/batch_delete" % (app_token, tid),
                         token=token, body={"records": ids})
        if d.get("code") != 0:
            break
        deleted += len(ids)
        time.sleep(0.2)
    return deleted


def _date_field(iso_day):
    """入池日期做文本列(避免时区跨日), 直接存 YYYY-MM-DD 字符串。"""
    return iso_day or ""


subs_dual = schema.subs_dual  # 单一来源 = schema.subs_dual(三表 + CSV 共用)。


# 对路分档位口径(证据摘要 & AI 指南共用): 分数 -> 人话档。阈值取自 cards 推荐门槛惯例。
def _fit_band(score):
    if score is None:
        return "对路未知"
    if score >= 0.60:
        return "高度对路"
    if score >= 0.45:
        return "对路"
    if score >= 0.30:
        return "弱对路"
    return "疑似跑偏"


def evidence_summary(s, theme_labels):
    """W17 证据摘要: 一行拼好给飞书 AI 字段当原料(只用行内已验证数据, 不引外部事实)。
    组成 = 对路分档 · 中文主题标签 · 在涨证据一句(有则) · 身份标签。缺项跳过, 用「·」连。"""
    parts = ["对路: " + _fit_band(s.get("score"))]
    if theme_labels:
        parts.append("主题: " + "/".join(theme_labels))
    ev = s.get("rising_evidence")
    if isinstance(ev, str) and ev.strip():
        parts.append("在涨: " + ev.strip()[:60])
    elif s.get("rising") is not None and s.get("rising") > 0:
        parts.append("在涨分 %.2f" % s["rising"])
    flags = s.get("identity_flags")
    if flags:
        try:
            import identity_filter
            zh = identity_filter.flags_zh(flags)
        except Exception:
            zh = "、".join(flags)
        if zh:
            parts.append("身份: " + zh)
    return " · ".join(parts)


def _flags_zh(flags):
    """身份标签 -> 中文短标签串(复用 identity_filter 的映射, 导入失败则原样拼)。"""
    try:
        import identity_filter
        return identity_filter.flags_zh(flags)
    except Exception:
        return "、".join(flags or [])


def build_full_ranking_records(ranked, pool_by_url, dossier_links, top_dossier_n=50):
    """把排序产物 + 池子元数据 + 档案链接拼成飞书记录列表(fields dict)。
    档案列: 仅 rank<=top_dossier_n 且映射里有链接的行才写。
    列结构单一来源 = schema.FULL_RANKING_COLUMNS。三词化(2026-07-07): 展示名用
      对路分(原总分) / 红绿灯(原行动分级) / 在涨分(新增合成) / 潜力分, 起势/浪/破圈=证据列。
    绝不物理删数据: 🔴 行照样进表, 带清楚的拦截原因列供人工终审。"""
    records = []
    for s in ranked:
        url = s.get("channel_url", "")
        p = pool_by_url.get(url, {})
        cid = url.rstrip("/").rsplit("/", 1)[-1]
        theme_labels = schema.theme_tags_zh(s.get("themes_hit") or [])  # 中文多选标签数组
        grade = s.get("action_grade") or ""
        # 红绿灯单选选项名: 🏢 行的存量选项名是「🏢官方联动」(见 _LIGHT_OPTIONS), 其余=原 emoji。
        light_opt = "🏢官方联动" if grade == "🏢" else grade
        f = {
            "排名": s.get("rank"),
            "频道名": s.get("channel_name", ""),
            "频道链接": {"link": url, "text": url},
            schema.COL_LIGHT: light_opt,                          # 红绿灯(单选, 写选项名字符串)
            "身份标签": _flags_zh(s.get("identity_flags")),
            schema.COL_THEME_TAGS: theme_labels,                  # 主题标签(多选, 写选项名数组)
            schema.COL_EVIDENCE: evidence_summary(s, theme_labels),  # 证据摘要(给 AI 当原料)
            "垂类": p.get("vertical") or "",
            "语言": p.get("lang") or p.get("country") or "",
            "入池日期": _date_field(p.get("first_seen") or p.get("last_refreshed")),
        }
        # 拦截原因: 🔴/🟡/🏢 行都写首条原因(🏢=官方联动原因, 供人工看; 🟢 留空)
        if grade in ("🔴", "🟡", "🏢") and s.get("grade_reasons"):
            f["拦截原因"] = s["grade_reasons"][0]
        if s.get("subscribers") is not None:
            f["订阅数"] = s["subscribers"]
            f[schema.COL_SUBS_TEXT] = subs_dual(s["subscribers"])  # 订阅(双格式文本)
        if s.get("score") is not None:
            f[schema.COL_FIT] = round(s["score"], 4)          # 对路分(原总分)
        if s.get("rising") is not None:
            f[schema.COL_RISING] = round(s["rising"], 4)      # 在涨分(合成)
        if s.get("potential") is not None:
            f[schema.COL_POTENTIAL] = round(s["potential"], 5)
        if s.get("momentum") is not None:
            f["起势分"] = round(s["momentum"], 4)
        if s.get("trend") is not None:
            f["浪层分"] = round(s["trend"], 4)
        if s.get("breakout") is not None:
            f["破圈比"] = round(s["breakout"], 3)
        if s.get("rank") and s["rank"] <= top_dossier_n:
            dl = dossier_links.get(cid)
            if dl:
                f["档案"] = {"link": dl, "text": "📄 档案"}
        records.append({"fields": f})
    return records


def sync_ranking_table(cfg, records, table_key, table_name, fields):
    """通用: 确保表存在 -> 先清后写(batch_create 每批<=500)。返回结果 dict。"""
    cred_path = os.path.expanduser(cfg.get("bitable", {}).get("credentials_path", ""))
    if not cred_path or not os.path.exists(cred_path):
        return {"ok": False, "error": "NotConfigured: 凭证文件不存在 %s" % (cred_path or "(未配置)")}
    try:
        cred = json.load(open(cred_path))
        token, traw = _get_token(cred)
        if not token:
            return {"ok": False, "error": "token failed: code=%s" % traw.get("code")}
        app_token = cred.get("app_token")
        if not app_token:
            return {"ok": False, "error": "凭证缺 app_token"}
        ledger = _load_tables(cfg)
        tid, created = _ensure_table(token, app_token, table_key, table_name, fields, ledger, cfg)
        if not tid:
            return {"ok": False, "error": "建表/取表失败"}
        cleared = 0 if created else _clear_table(token, app_token, tid)
        written = 0
        for i in range(0, len(records), 500):
            batch = records[i:i + 500]
            r = _feishu_call("POST",
                             "/bitable/v1/apps/%s/tables/%s/records/batch_create" % (app_token, tid),
                             token=token, body={"records": batch})
            if r.get("code") == 0:
                written += len(batch)
            else:
                return {"ok": False, "table_id": tid, "created": created, "cleared": cleared,
                        "written": written, "error": "batch_create failed at %d: code=%s %s"
                        % (i, r.get("code"), str(r.get("msg"))[:80])}
            time.sleep(0.3)
        base = cred.get("base_url") or ("https://feishu.cn/base/%s" % app_token)
        link = "%s?table=%s" % (base, tid) if "?" not in base else base
        return {"ok": written == len(records), "table_id": tid, "created": created,
                "cleared": cleared, "written": written, "total": len(records), "link": link}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def ensure_green_view(cfg, view_name="🟢 外联终审队列"):
    """在「全池榜单」表建/复用一个过滤视图, 只显示 行动分级=🟢 的行(Max 说的"价值密度更好的最终名单")。
    幂等: 同名视图已存在则复用其 id, 只重设过滤条件。视图 API 不顺就优雅放弃, 返回带 manual 提示的 dict。
    绝不物理删数据: 全表仍是全量 2422 行, 这只是一个 UI 过滤视图。"""
    cred_path = os.path.expanduser(cfg.get("bitable", {}).get("credentials_path", ""))
    if not cred_path or not os.path.exists(cred_path):
        return {"ok": False, "error": "凭证缺失"}
    try:
        cred = json.load(open(cred_path))
        token, traw = _get_token(cred)
        if not token:
            return {"ok": False, "error": "token failed"}
        app_token = cred.get("app_token")
        tid = _load_tables(cfg).get("full_ranking_table_id")
        if not tid:
            return {"ok": False, "error": "全池榜单表 id 未知(先跑一次同步建表)"}
        manual = ("在飞书全池榜单表手动两步筛: 新建视图 -> 添加筛选 [%s] 等于 🟢。" % schema.COL_LIGHT)

        # 找「红绿灯」字段 id(过滤条件要用字段 id 而非名字; 兼容尚未迁移的老表旧名「行动分级」)
        fld = _feishu_call("GET", "/bitable/v1/apps/%s/tables/%s/fields?page_size=100" % (app_token, tid), token=token)
        grade_fid = None
        for it in (fld.get("data", {}) or {}).get("items", []):
            if it.get("field_name") in (schema.COL_LIGHT, "行动分级"):
                grade_fid = it.get("field_id")
                break
        if not grade_fid:
            return {"ok": False, "error": "找不到 %s 字段" % schema.COL_LIGHT, "manual": manual}

        # 复用同名视图或新建
        views = _feishu_call("GET", "/bitable/v1/apps/%s/tables/%s/views?page_size=100" % (app_token, tid), token=token)
        vid = None
        for v in (views.get("data", {}) or {}).get("items", []):
            if v.get("view_name") == view_name:
                vid = v.get("view_id")
                break
        created = False
        if not vid:
            r = _feishu_call("POST", "/bitable/v1/apps/%s/tables/%s/views" % (app_token, tid),
                             token=token, body={"view_name": view_name, "view_type": "grid"})
            if r.get("code") != 0:
                return {"ok": False, "error": "建视图失败 code=%s %s" % (r.get("code"), str(r.get("msg"))[:80]),
                        "manual": manual}
            vid = r.get("data", {}).get("view", {}).get("view_id")
            created = True

        # 设过滤条件: 行动分级 is 🟢 (PATCH view.property.filter_info)
        body = {"property": {"filter_info": {"conjunction": "and", "conditions": [
            {"field_id": grade_fid, "operator": "is", "value": ["🟢"]}]}}}
        pr = _feishu_call("PATCH", "/bitable/v1/apps/%s/tables/%s/views/%s" % (app_token, tid, vid),
                          token=token, body=body)
        if pr.get("code") != 0:
            return {"ok": False, "view_id": vid, "created": created,
                    "error": "设过滤失败 code=%s %s" % (pr.get("code"), str(pr.get("msg"))[:80]),
                    "manual": manual}
        return {"ok": True, "view_id": vid, "view_name": view_name, "created": created}
    except Exception as e:
        return {"ok": False, "error": str(e),
                "manual": "在飞书全池榜单表手动两步筛: 新建视图 -> 添加筛选 [行动分级] 等于 🟢。"}


def _load_dossier_links(cfg):
    """从 feishu_docs_map.json 读 dossier/<cid> -> url。"""
    node = cfg.get("feishu_docs", {})
    mp = os.path.expanduser(node.get("map_path", "~/.config/creator-radar/feishu_docs_map.json"))
    if not os.path.exists(mp):
        return {}
    try:
        d = json.load(open(mp))
    except Exception:
        return {}
    return {k[len("dossier/"):]: v.get("url") for k, v in d.items()
            if k.startswith("dossier/") and isinstance(v, dict) and v.get("url")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--ranked", required=True, help="ranked.json (score.py/run_radar 产物)")
    ap.add_argument("--pool", help="池子 jsonl (取 vertical/lang/入池日期). 默认 creator_pool.jsonl")
    ap.add_argument("--table", choices=["full", "bili"], default="full")
    ap.add_argument("--make-green-view", action="store_true",
                    help="同步后在全池榜单表建/刷新「🟢 外联终审队列」过滤视图")
    args = ap.parse_args()

    cfg = json.load(open(args.config))
    ranked = json.load(open(args.ranked))
    pool_path = args.pool or os.path.join(ROOT, "data/pool/creator_pool.jsonl")
    pool = [json.loads(l) for l in open(pool_path) if l.strip()]
    pool_by_url = {r["channel_url"]: r for r in pool}

    if args.table == "full":
        dossier_links = _load_dossier_links(cfg)
        records = build_full_ranking_records(ranked, pool_by_url, dossier_links)
        res = sync_ranking_table(cfg, records, "full_ranking_table_id",
                                 FULL_RANKING_TABLE_NAME, FULL_RANKING_FIELDS)
    else:
        # B站榜单: 无档案列
        records = build_full_ranking_records(ranked, pool_by_url, {}, top_dossier_n=0)
        for rec in records:
            rec["fields"].pop("档案", None)
        res = sync_ranking_table(cfg, records, "bili_ranking_table_id",
                                 BILI_RANKING_TABLE_NAME, BILI_RANKING_FIELDS)

    print(json.dumps(res, ensure_ascii=False, indent=1))

    if args.make_green_view and args.table == "full":
        gv = ensure_green_view(cfg)
        print("green_view:", json.dumps(gv, ensure_ascii=False))


if __name__ == "__main__":
    main()
