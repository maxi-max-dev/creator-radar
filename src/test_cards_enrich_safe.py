#!/usr/bin/env python3
"""Offline failure-safety test for cards_enrich.enrich_records (no real network to feishu writes needed for the safety logic).
Proves: avatar fetch failure -> field left empty (no 频道头像 key); name miss -> no 完整档案 key; the row still gets built/skipped safely."""
import sys, os
sys.path.insert(0, "src")
import cards_enrich

# Monkeypatch the network-touching pieces to simulate failures.
calls = {"batch_update": []}

def fake_get_token(cred): return "FAKE"
def fake_feishu_call(method, path, token=None, body=None):
    if "records/batch_update" in path:
        calls["batch_update"].append(body)
        return {"code": 0}
    return {"code": 0, "data": {"items": [], "has_more": False}}
def fake_name_index(token, at, tid):
    return {"MatchMe": "recFULL123"}   # only this name matches full-ranking
def fake_fetch_avatar(token, at, url, use_ytdlp_fallback=True, deadline=None):
    return "ftGOOD" if "good" in url else None   # avatar succeeds only for 'good' urls

cards_enrich._get_token = fake_get_token
cards_enrich._feishu_call = fake_feishu_call
cards_enrich.build_fullranking_name_index = fake_name_index
cards_enrich.fetch_avatar_token = fake_fetch_avatar

cfg = {"bitable": {"credentials_path": os.path.expanduser("~/.config/creator-radar/feishu.json")}}
targets = [
  {"record_id": "r1", "name": "MatchMe",  "url": "https://youtube.com/channel/good1"},  # avatar OK + link OK
  {"record_id": "r2", "name": "NoMatch",  "url": "https://youtube.com/channel/bad1"},   # avatar FAIL + link MISS -> no fields, safely skipped
  {"record_id": "r3", "name": "MatchMe2", "url": "https://youtube.com/channel/good2"},  # avatar OK, link MISS
]
res = cards_enrich.enrich_records(cfg, targets, item_timeout_s=1)
print("result:", res)

# Assertions
assert res["ok"], "should be ok"
assert res["avatar_ok"] == 2 and res["avatar_fail"] == 1, res
assert res["link_ok"] == 1 and res["link_miss"] == 2, res
# Inspect what got written
sent = calls["batch_update"][0]["records"]
byid = {u["record_id"]: u["fields"] for u in sent}
assert byid["r1"].get("频道头像") == [{"file_token": "ftGOOD"}], byid["r1"]
assert byid["r1"].get("完整档案") == ["recFULL123"], byid["r1"]
assert "r2" not in byid, "r2 had no successful field -> must NOT be in update batch (safely skipped)"
assert byid["r3"].get("频道头像") == [{"file_token": "ftGOOD"}] and "完整档案" not in byid["r3"], byid["r3"]
print("PASS: 头像失败留空 / 名字不匹配留空 / 全失败行安全跳过 / 成功项照写 — 失败安全成立")
