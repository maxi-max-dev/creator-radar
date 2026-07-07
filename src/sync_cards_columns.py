#!/usr/bin/env python3
"""达人雷达 · 「当日推荐卡」表(cards 表)幂等加列 + 更新今日已存在行。

盲投审计后, 推荐卡表要加 4 列: 起势分 / 潜力分 / 行动分级 / 身份标签。
本脚本**不 append 新行**(那会重复), 而是:
  1) 幂等建字段(先查字段存在再建)。
  2) search 找到今天(日期=today)的行 -> batch_update 填新列。

cards 表 id = credentials 文件(~/.config/creator-radar/feishu.json)的 table_id。
数值来自当日 ranked.json(run_radar 产物, 已含 momentum/potential/action_grade/identity_flags)。
按 频道链接 匹配 ranked 行(表里存的是 URL)。

用法:
  python3 src/sync_cards_columns.py --config config/insta360.json \
      --ranked data/runs/daily/<today>/ranked.json [--date YYYY-MM-DD]
"""
import argparse, json, os, sys, time
import urllib.request, urllib.error
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import identity_filter

FT_TEXT, FT_NUMBER = 1, 2
NEW_FIELDS = [("起势分", FT_NUMBER), ("潜力分", FT_NUMBER), ("行动分级", FT_TEXT), ("身份标签", FT_TEXT)]


def _call(method, path, token=None, body=None):
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


def _field_names(token, app_token, tid):
    names, pt = set(), None
    for _ in range(50):
        p = "/bitable/v1/apps/%s/tables/%s/fields?page_size=100" % (app_token, tid)
        if pt:
            p += "&page_token=" + pt
        r = _call("GET", p, token=token)
        if r.get("code") != 0:
            break
        d = r.get("data", {})
        for it in d.get("items", []):
            if it.get("field_name"):
                names.add(it["field_name"])
        pt = d.get("page_token")
        if not d.get("has_more"):
            break
    return names


def _ensure_fields(token, app_token, tid):
    existing = _field_names(token, app_token, tid)
    added = []
    for name, ftype in NEW_FIELDS:
        if name in existing:
            continue
        r = _call("POST", "/bitable/v1/apps/%s/tables/%s/fields" % (app_token, tid),
                  token=token, body={"field_name": name, "type": ftype})
        if r.get("code") == 0:
            added.append(name)
        time.sleep(0.1)
    return added


def _search_all(token, app_token, tid):
    """取全表 records(record_id + fields)。分页。"""
    out, pt = [], None
    for _ in range(200):
        body = {"page_size": 500}
        if pt:
            body["page_token"] = pt
        r = _call("POST", "/bitable/v1/apps/%s/tables/%s/records/search" % (app_token, tid),
                  token=token, body=body)
        if r.get("code") != 0:
            break
        d = r.get("data", {})
        out.extend(d.get("items", []))
        pt = d.get("page_token")
        if not d.get("has_more"):
            break
        time.sleep(0.2)
    return out


def _rec_url(rec):
    """从记录 fields 里取频道链接文本(超链接字段是 dict/list)。"""
    v = (rec.get("fields") or {}).get("频道链接")
    if isinstance(v, dict):
        return v.get("link") or v.get("text") or ""
    if isinstance(v, list) and v:
        x = v[0]
        return (x.get("link") or x.get("text") or "") if isinstance(x, dict) else str(x)
    return str(v or "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--ranked", required=True)
    ap.add_argument("--date", default=date.today().isoformat())
    args = ap.parse_args()

    cfg = json.load(open(args.config))
    cred_path = os.path.expanduser(cfg.get("bitable", {}).get("credentials_path", ""))
    if not cred_path or not os.path.exists(cred_path):
        print(json.dumps({"ok": False, "error": "凭证缺失"}, ensure_ascii=False)); return
    cred = json.load(open(cred_path))
    app_token, tid = cred.get("app_token"), cred.get("table_id")

    t = _call("POST", "/auth/v3/tenant_access_token/internal",
              body={"app_id": cred["app_id"], "app_secret": cred["app_secret"]})
    token = t.get("tenant_access_token")
    if not token:
        print(json.dumps({"ok": False, "error": "token failed", "raw": t}, ensure_ascii=False)); return

    added = _ensure_fields(token, app_token, tid)

    scored = json.load(open(args.ranked))
    by_url = {s.get("channel_url", "").rstrip("/"): s for s in scored}

    recs = _search_all(token, app_token, tid)
    # 只更新「今天」的行(cards 表按日 append, 历史行不动)。日期字段是毫秒时间戳, 比对日期部分。
    from datetime import datetime, timezone
    updates, matched, skipped_other_day = [], 0, 0
    for rec in recs:
        fields = rec.get("fields") or {}
        dv = fields.get("日期")
        day = None
        if isinstance(dv, (int, float)):
            day = datetime.fromtimestamp(dv / 1000, tz=timezone.utc).astimezone().date().isoformat()
        if day and day != args.date:
            skipped_other_day += 1
            continue
        url = _rec_url(rec).rstrip("/")
        s = by_url.get(url)
        if not s:
            continue
        matched += 1
        f = {
            "行动分级": s.get("action_grade") or "",
            "身份标签": identity_filter.flags_zh(s.get("identity_flags")),
        }
        if s.get("momentum") is not None:
            f["起势分"] = round(s["momentum"], 4)
        if s.get("potential") is not None:
            f["潜力分"] = round(s["potential"], 5)
        updates.append({"record_id": rec["record_id"], "fields": f})

    written = 0
    for i in range(0, len(updates), 500):
        batch = updates[i:i + 500]
        r = _call("POST", "/bitable/v1/apps/%s/tables/%s/records/batch_update" % (app_token, tid),
                  token=token, body={"records": batch})
        if r.get("code") == 0:
            written += len(batch)
        else:
            print(json.dumps({"ok": False, "fields_added": added, "matched": matched,
                              "error": "batch_update failed code=%s %s" % (r.get("code"), str(r.get("msg"))[:100])},
                             ensure_ascii=False))
            return
        time.sleep(0.3)

    print(json.dumps({"ok": True, "fields_added": added, "records_total": len(recs),
                      "today_matched": matched, "updated": written,
                      "skipped_other_day": skipped_other_day, "date": args.date}, ensure_ascii=False))


if __name__ == "__main__":
    main()
