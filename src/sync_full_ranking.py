#!/usr/bin/env python3
"""达人雷达 · 飞书「全池榜单」表同步。

把当日全池排序整表写进飞书多维表格的第二张表「全池榜单」(与「达人推荐」表分开)。
列: 排名 / 频道名 / 频道链接 / 订阅数 / 总分 / 命中主题 / 垂类 / 语言 / 档案(top50) / 入池日期。

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

# 飞书字段类型: 1=多行文本 2=数字 5=日期 15=超链接
FT_TEXT, FT_NUMBER, FT_DATE, FT_URL = 1, 2, 5, 15

FULL_RANKING_TABLE_NAME = "全池榜单"
BILI_RANKING_TABLE_NAME = "B站榜单"

# 全池榜单字段 schema (顺序即建表顺序). 档案列只有 top50 有值。
# 2026-07-07 盲投审计后加 4 列: 行动分级/身份标签(身份过滤器) + 起势分/潜力分(momentum)。
# 建字段幂等: _ensure_table 会先查已存在字段, 只补缺的(老表也能加新列)。
FULL_RANKING_FIELDS = [
    ("排名", FT_NUMBER),
    ("频道名", FT_TEXT),
    ("频道链接", FT_URL),
    ("订阅数", FT_NUMBER),
    ("总分", FT_NUMBER),
    ("起势分", FT_NUMBER),
    ("潜力分", FT_NUMBER),
    ("浪层分", FT_NUMBER),
    ("破圈比", FT_NUMBER),
    ("行动分级", FT_TEXT),
    ("身份标签", FT_TEXT),
    ("拦截原因", FT_TEXT),
    ("命中主题", FT_TEXT),
    ("垂类", FT_TEXT),
    ("语言", FT_TEXT),
    ("档案", FT_URL),
    ("入池日期", FT_TEXT),
]

# B站榜单 = 全池榜单去掉档案列 + 去掉起势/潜力/浪层/破圈(B站无 RSS 视频级数据, 免留永久空列)。
# 保留 行动分级/身份标签/拦截原因(B站池正是身份过滤器价值最大的地方: 理财/玄学/带货/搬运)。
_BILI_DROP = {"档案", "起势分", "潜力分", "浪层分", "破圈比"}
BILI_RANKING_FIELDS = [(n, t) for (n, t) in FULL_RANKING_FIELDS if n not in _BILI_DROP]


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


def _reconcile_fields(token, app_token, tid, fields):
    """幂等补字段: 查现有字段名, 只建缺的。返回本次新建的字段名列表。"""
    existing = _list_field_names(token, app_token, tid)
    added = []
    for name, ftype in fields:
        if name in existing:
            continue
        c = _feishu_call("POST", "/bitable/v1/apps/%s/tables/%s/fields" % (app_token, tid),
                         token=token, body={"field_name": name, "type": ftype})
        if c.get("code") == 0:
            added.append(name)
        time.sleep(0.1)
    return added


def _ensure_table(token, app_token, table_key, table_name, fields, tables_ledger, cfg):
    """确保表存在且字段齐全, 返回 (table_id, created)。table_key = 账本里的键。
    首次不存在则建表; 已存在则**幂等补齐缺失字段**(老表加新列: 先查字段存在再建)。
    表 id 落账本(repo 外)。若账本里有 id 但飞书侧已被删(GET fields 报错), 重建。"""
    tid = tables_ledger.get(table_key)
    if tid:
        chk = _feishu_call("GET", "/bitable/v1/apps/%s/tables/%s/fields?page_size=1" % (app_token, tid), token=token)
        if chk.get("code") == 0:
            # 表在: 幂等补齐 schema 里但表上缺的字段(4 新列首跑在此补上)
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
    2026-07-07 起加 4 列: 行动分级/身份标签/起势分/潜力分 + 拦截原因(🔴 才写)。
    绝不物理删数据: 🔴 行照样进表, 带清楚的拦截原因列供人工终审。"""
    records = []
    for s in ranked:
        url = s.get("channel_url", "")
        p = pool_by_url.get(url, {})
        cid = url.rstrip("/").rsplit("/", 1)[-1]
        f = {
            "排名": s.get("rank"),
            "频道名": s.get("channel_name", ""),
            "频道链接": {"link": url, "text": url},
            "行动分级": s.get("action_grade") or "",
            "身份标签": _flags_zh(s.get("identity_flags")),
            "命中主题": "、".join(s.get("themes_hit") or []),
            "垂类": p.get("vertical") or "",
            "语言": p.get("lang") or p.get("country") or "",
            "入池日期": _date_field(p.get("first_seen") or p.get("last_refreshed")),
        }
        # 拦截原因: 只有 🔴 行写首条原因(🟡 的核验原因也顺带写, 供人工看; 🟢 留空)
        if s.get("action_grade") in ("🔴", "🟡") and s.get("grade_reasons"):
            f["拦截原因"] = s["grade_reasons"][0]
        if s.get("subscribers") is not None:
            f["订阅数"] = s["subscribers"]
        if s.get("score") is not None:
            f["总分"] = round(s["score"], 4)
        if s.get("momentum") is not None:
            f["起势分"] = round(s["momentum"], 4)
        if s.get("potential") is not None:
            f["潜力分"] = round(s["potential"], 5)
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
        manual = ("在飞书全池榜单表手动两步筛: 新建视图 -> 添加筛选 [行动分级] 等于 🟢。")

        # 找「行动分级」字段 id(过滤条件要用字段 id 而非名字)
        fld = _feishu_call("GET", "/bitable/v1/apps/%s/tables/%s/fields?page_size=100" % (app_token, tid), token=token)
        grade_fid = None
        for it in (fld.get("data", {}) or {}).get("items", []):
            if it.get("field_name") == "行动分级":
                grade_fid = it.get("field_id")
                break
        if not grade_fid:
            return {"ok": False, "error": "找不到 行动分级 字段", "manual": manual}

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
