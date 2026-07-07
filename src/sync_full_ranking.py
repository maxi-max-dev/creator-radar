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
FULL_RANKING_FIELDS = [
    ("排名", FT_NUMBER),
    ("频道名", FT_TEXT),
    ("频道链接", FT_URL),
    ("订阅数", FT_NUMBER),
    ("总分", FT_NUMBER),
    ("命中主题", FT_TEXT),
    ("垂类", FT_TEXT),
    ("语言", FT_TEXT),
    ("档案", FT_URL),
    ("入池日期", FT_TEXT),
]

# B站榜单 = 全池榜单去掉档案列
BILI_RANKING_FIELDS = [(n, t) for (n, t) in FULL_RANKING_FIELDS if n != "档案"]


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


def _ensure_table(token, app_token, table_key, table_name, fields, tables_ledger, cfg):
    """确保表存在, 返回 (table_id, created)。table_key = 账本里的键。
    首次不存在则建表(带首字段), 再补齐其余字段。表 id 落账本(repo 外)。
    若账本里有 id 但飞书侧已被删(GET fields 报错), 重建。"""
    tid = tables_ledger.get(table_key)
    if tid:
        chk = _feishu_call("GET", "/bitable/v1/apps/%s/tables/%s/fields?page_size=1" % (app_token, tid), token=token)
        if chk.get("code") == 0:
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
    for name, ftype in fields[1:]:
        c = _feishu_call("POST", "/bitable/v1/apps/%s/tables/%s/fields" % (app_token, tid),
                         token=token, body={"field_name": name, "type": ftype})
        # 已存在或建失败都不致命, 继续
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


def build_full_ranking_records(ranked, pool_by_url, dossier_links, top_dossier_n=50):
    """把排序产物 + 池子元数据 + 档案链接拼成飞书记录列表(fields dict)。
    档案列: 仅 rank<=top_dossier_n 且映射里有链接的行才写。"""
    records = []
    for s in ranked:
        url = s.get("channel_url", "")
        p = pool_by_url.get(url, {})
        cid = url.rstrip("/").rsplit("/", 1)[-1]
        f = {
            "排名": s.get("rank"),
            "频道名": s.get("channel_name", ""),
            "频道链接": {"link": url, "text": url},
            "命中主题": "、".join(s.get("themes_hit") or []),
            "垂类": p.get("vertical") or "",
            "语言": p.get("lang") or p.get("country") or "",
            "入池日期": _date_field(p.get("first_seen") or p.get("last_refreshed")),
        }
        if s.get("subscribers") is not None:
            f["订阅数"] = s["subscribers"]
        if s.get("score") is not None:
            f["总分"] = round(s["score"], 4)
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


if __name__ == "__main__":
    main()
