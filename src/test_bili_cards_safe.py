#!/usr/bin/env python3
"""离线失败安全 + 选卡/AI闸门逻辑单测(W22 B站进推荐卡 + W22.1 AI 复核闸门)。零真实网络。
证明:
  1) 选卡只取 🟡, 绝不 🔴/🏢/无灯; 按 rank 取前 N; 账本(carded/rejected)与 exclude 都被排除;
     v1 旧账本(纯日期串)自动归一为 carded。
  2) B站卡完整档案/档案/在涨分/潜力分 结构性留空(不硬凑); 诚实活性提示在值得签1。
  3) 【W22.1 核心】AI 判退 → 不出卡 → 顺位补下一个 🟡; bscored 已带 ai_review_flagged 的直接判退(零模型调用)。
  4) 【W22.1 宁缺不错】ollama 不可用 → 整批跳过(picked=0, note=复核不可用)。
  5) 写表异常 → {ok:False} 不抛; 账本零 carded 污染(判退记录允许落盘=设计如此)。
  6) dry-run 只选卡过闸门, 不写表不记账本。
  7) top-up 配额: 今天已有 K 张只补 quota-K 张; K>=quota 整批跳过。
"""
import json, os, sys, tempfile
sys.path.insert(0, "src")
import bili_cards, schema
import ai_review as _ai

# ---- 全局钩子: 绝不写真实 data/ai_review/ 缓存, 绝不真跑模型 ----
_cache_writes = []
_ai._append_cache = lambda today, rec: _cache_writes.append(rec)
_ai._load_cache = lambda today: {}
_ai.load_scoring_tags = lambda: {}
bili_cards._ollama_up = lambda rcfg, timeout=4: True

# 假复核员: 按频道名给 verdict。黄灯甲=搬运(conf0.9 该拒), 其余=干净真人。
def _fake_review(row, pool_row, tag, rcfg):
    name = row.get("channel_name", "")
    if name == "黄灯甲":
        return {"real_person": False, "repost_or_compilation": True, "org_or_brand": False,
                "off_topic": False, "confidence": 0.9, "reason": "标题挂满他人ID, 疑似搬运"}
    return {"real_person": True, "repost_or_compilation": False, "org_or_brand": False,
            "off_topic": False, "confidence": 0.85, "reason": "真人原创"}
_ai.review_candidate = _fake_review

# ---- 造一批 B站 ranked(混红黄蓝 + 行标判退), 覆盖各分支 ----
BSCORED = [
    {"rank": 1, "channel_name": "黄灯甲", "channel_url": "https://space.bilibili.com/111", "subscribers": 83307,
     "score": 0.60, "action_grade": "🟡", "themes_hit": ["pov_native"], "identity_flags": ["no_upload_date"], "pct": 1, "sem": 0.6, "sweet": 0.5, "pov": 0.4},
    {"rank": 2, "channel_name": "红灯乙", "channel_url": "https://space.bilibili.com/222", "subscribers": 154319,
     "score": 0.55, "action_grade": "🔴", "themes_hit": [], "identity_flags": ["reposter", "no_upload_date"], "pct": 1},
    {"rank": 3, "channel_name": "黄灯丙", "channel_url": "https://space.bilibili.com/333", "subscribers": 9277,
     "score": 0.52, "action_grade": "🟡", "themes_hit": ["authentic_vlog"], "identity_flags": ["no_upload_date"], "pct": 2},
    {"rank": 4, "channel_name": "官方丁", "channel_url": "https://space.bilibili.com/444", "subscribers": 500000,
     "score": 0.51, "action_grade": "🏢", "themes_hit": [], "identity_flags": ["already_partner"], "pct": 2},
    {"rank": 5, "channel_name": "黄灯戊", "channel_url": "https://space.bilibili.com/555", "subscribers": 58252,
     "score": 0.50, "action_grade": "🟡", "themes_hit": ["gear_native"], "identity_flags": ["no_upload_date"], "pct": 3},
    {"rank": 6, "channel_name": "黄灯己带行标", "channel_url": "https://space.bilibili.com/666", "subscribers": 30000,
     "score": 0.49, "action_grade": "🟡", "themes_hit": [], "pct": 3,
     "identity_flags": ["no_upload_date", "ai_review_flagged"],
     "grade_reasons": ["🤖AI复核疑似内容离题(conf 0.80): 主链已判"]},
    {"rank": 7, "channel_name": "黄灯庚", "channel_url": "https://space.bilibili.com/777", "subscribers": 21000,
     "score": 0.48, "action_grade": "🟡", "themes_hit": [], "identity_flags": ["no_upload_date"], "pct": 4},
]

# 1) 候选排序: 只 🟡; 账本 v2 + v1 兼容 + exclude
cands = bili_cards.select_bili_cards(BSCORED, seen={}, n=3)
assert [c["channel_name"] for c in cands] == ["黄灯甲", "黄灯丙", "黄灯戊"], cands
seen_v2 = {"https://space.bilibili.com/111": {"date": "2026-07-07", "status": "rejected", "reason": "x"}}
cands2 = bili_cards.select_bili_cards(BSCORED, seen=seen_v2, n=2)
assert [c["channel_name"] for c in cands2] == ["黄灯丙", "黄灯戊"], "rejected 状态必须排除"
cands3 = bili_cards.select_bili_cards(BSCORED, seen={}, n=2, exclude=["https://space.bilibili.com/111/"])
assert [c["channel_name"] for c in cands3] == ["黄灯丙", "黄灯戊"], "exclude(今日已在表)必须排除"
# v1 账本自动归一
tmp_v1 = tempfile.mktemp(suffix=".json")
json.dump({"https://space.bilibili.com/333": "2026-07-07"}, open(tmp_v1, "w"))
norm = bili_cards.load_seen(tmp_v1)
assert norm["https://space.bilibili.com/333"] == {"date": "2026-07-07", "status": "carded"}, norm
os.unlink(tmp_v1)
print("PASS 1: 候选只 🟡 / 账本 v2 carded+rejected 都排除 / v1 归一 / exclude 生效")

# 2) 卡内容: 平台=B站, 红绿灯🟡, 结构性留空, 诚实提示; 过闸门的卡风险列带复核状态
picks_for_rows = [dict(BSCORED[0]), dict(BSCORED[2])]
picks_for_rows[0]["_gate"] = {"verdict": {"confidence": 0.85}}
rows = bili_cards.build_bili_card_rows(picks_for_rows, "2026-07-08")
r0, r1 = rows[0], rows[1]
assert r0[schema.COL_PLATFORM] == schema.PLATFORM_BILIBILI and r0[schema.COL_LIGHT] == "🟡"
assert r0[schema.COL_SUBS_TEXT] == schema.subs_dual(83307)
assert "完整档案" not in r0 and "档案" not in r0 and "在涨分" not in r0 and "潜力分" not in r0
assert "活性" in r0["值得签1"] and "上传日期" in r0["值得签1"]
assert "已过AI复核" in r0["风险"], r0["风险"]
assert "已过AI复核" not in r1["风险"]
print("PASS 2: 平台/🟡/双格式/结构性留空/诚实提示; 过闸门卡风险列注明复核状态")

# 3) 【核心】AI 判退 → 不出卡 → 顺位补; 行标判退零模型调用
g = bili_cards.gated_select(BSCORED, seen={}, need=3, pool_by_url={}, cfg={}, today="2026-07-08")
names = [s["channel_name"] for s in g["picks"]]
assert not g["unavailable"]
# 扫描窗=min(3+3,6)=6 个候选: 甲(判退搬运), 丙(过), 戊(过), 己(行标判退), 庚(过) -> 凑满 3 张=丙戊庚
assert names == ["黄灯丙", "黄灯戊", "黄灯庚"], names
rej = {s["channel_name"]: (reason, src) for s, reason, src in g["rejected"]}
assert "黄灯甲" in rej and rej["黄灯甲"][1] == "live" and "搬运" in rej["黄灯甲"][0]
assert "黄灯己带行标" in rej and rej["黄灯己带行标"][1] == "flag" and "AI复核" in rej["黄灯己带行标"][0]
assert all(s.get("_gate", {}).get("verdict") for s in g["picks"]), "通过的卡必须带复核通过记录"
print("PASS 3: AI 判退(live)+行标判退(flag) 都不出卡, 顺位补到 丙/戊/庚, 通过卡带 verdict 记录")

# 4) 宁缺不错: ollama 不可用 → 整批跳过
bili_cards._ollama_up = lambda rcfg, timeout=4: False
g2 = bili_cards.gated_select(BSCORED, seen={}, need=2, pool_by_url={}, cfg={}, today="2026-07-08")
assert g2["unavailable"] and not g2["picks"]
tmp = tempfile.mktemp(suffix=".json")
res_un = bili_cards.push_bili_cards({"bilibili": {"cards_per_day": 2}}, BSCORED, "2026-07-08",
                                    dry_run=True, seen_path=tmp, pool_by_url={})
assert res_un["ok"] and res_un["picked"] == 0 and res_un["note"] == bili_cards.UNAVAILABLE_NOTE, res_un
assert not os.path.exists(tmp)
bili_cards._ollama_up = lambda rcfg, timeout=4: True   # 恢复
print("PASS 4: ollama 不可用 → 整批跳过(picked=0, 宁缺不错), 不写账本")

# 5) 写表失败两种形态 → {ok:False} 不抛; 账本零 carded 污染
# 5a) 最狠形态: _feishu_call 直接抛(真实实现从不抛, 这里证明外层 try 兜得住) -> 不抛, 零 carded
def boom_raise(method, path, token=None, body=None):
    if "tenant_access_token" in path:
        return {"tenant_access_token": "FAKE"}
    if "/fields" in path:
        return {"code": 0, "data": {"items": [{"field_name": schema.COL_PLATFORM}], "has_more": False}}
    if "records/search" in path:
        return {"code": 0, "data": {"items": [], "has_more": False}}
    if "batch_create" in path:
        raise RuntimeError("模拟飞书 500")
    return {"code": 0}
bili_cards._feishu_call = boom_raise
cred = os.path.expanduser("~/.config/creator-radar/feishu.json")
cfg2 = {"bilibili": {"cards_per_day": 2}, "bitable": {"credentials_path": cred, "avatar_enrich": False}}
tmp2 = tempfile.mktemp(suffix=".json")
res_boom = bili_cards.push_bili_cards(cfg2, BSCORED, "2026-07-08", dry_run=False, seen_path=tmp2, pool_by_url={})
assert res_boom["ok"] is False, res_boom
led = bili_cards.load_seen(tmp2)
assert not any(v.get("status") == "carded" for v in led.values()), "写表失败绝不能记 carded(否则明天漏推)"
# 5b) 现实形态: batch_create 回 code=-1(真实 _feishu_call 的失败形态) -> 优雅分支, 判退记录仍落盘
def boom_code(method, path, token=None, body=None):
    if "tenant_access_token" in path:
        return {"tenant_access_token": "FAKE"}
    if "/fields" in path:
        return {"code": 0, "data": {"items": [{"field_name": schema.COL_PLATFORM}], "has_more": False}}
    if "records/search" in path:
        return {"code": 0, "data": {"items": [], "has_more": False}}
    if "batch_create" in path:
        return {"code": -1, "msg": "模拟飞书 500"}
    return {"code": 0}
bili_cards._feishu_call = boom_code
tmp2b = tempfile.mktemp(suffix=".json")
res_boom2 = bili_cards.push_bili_cards(cfg2, BSCORED, "2026-07-08", dry_run=False, seen_path=tmp2b, pool_by_url={})
assert res_boom2["ok"] is False and "batch_create failed" in res_boom2["error"], res_boom2
led2 = bili_cards.load_seen(tmp2b)
assert not any(v.get("status") == "carded" for v in led2.values()), "零 carded 污染"
assert led2.get("https://space.bilibili.com/111", {}).get("status") == "rejected", "判退记录落盘(设计如此, 省明天复核)"
print("PASS 5: 写表抛异常/回错误码 两形态都 {ok:False} 不抛 / 零 carded 污染 / 判退记录保留")

# 6) dry-run: 不写表不记账本(闸门照过)
tmp3 = tempfile.mktemp(suffix=".json")
res_dry = bili_cards.push_bili_cards({"bilibili": {"cards_per_day": 2}}, BSCORED, "2026-07-08",
                                     dry_run=True, seen_path=tmp3, pool_by_url={})
assert res_dry["ok"] and res_dry["dry_run"] and res_dry["picked"] == 2, res_dry
assert res_dry["names"] == ["黄灯丙", "黄灯戊"]
assert any(x["name"] == "黄灯甲" for x in res_dry["gate"]["rejected"]), "dry-run 也要报判退名单"
assert not os.path.exists(tmp3), "dry-run 绝不能写账本"
print("PASS 6: dry-run 闸门照过+报判退, 不写表不记账本")

# 7) top-up 配额: 今天已有 K 张只补 quota-K; K>=quota 整批跳过
created_batches = []
def topup_call(method, path, token=None, body=None):
    if "tenant_access_token" in path:
        return {"tenant_access_token": "FAKE"}
    if "/fields" in path:
        return {"code": 0, "data": {"items": [{"field_name": schema.COL_PLATFORM}], "has_more": False}}
    if "records/search" in path:
        # 今天已有 1 张 B站卡(黄灯甲) -> remaining = 2-1 = 1
        return {"code": 0, "data": {"items": [
            {"record_id": "rX", "fields": {"日期": bili_cards._date_ms("2026-07-08"),
                                           "频道链接": {"link": "https://space.bilibili.com/111"}}}],
            "has_more": False}}
    if "batch_create" in path:
        created_batches.append(body["records"])
        return {"code": 0, "data": {"records": [{"record_id": "rNew%d" % i} for i in range(len(body["records"]))]}}
    return {"code": 0}
bili_cards._feishu_call = topup_call
bili_cards._enrich_bili_avatars = lambda cfg, rows, res: {"skipped": "test"}
tmp4 = tempfile.mktemp(suffix=".json")
res_top = bili_cards.push_bili_cards(cfg2, BSCORED, "2026-07-08", dry_run=False, seen_path=tmp4, pool_by_url={})
assert res_top["ok"] and res_top["picked"] == 1, res_top
assert res_top["names"] == ["黄灯丙"], "已有甲占 1 配额, 只补 1 张=顺位第一个过闸门的丙"
led4 = bili_cards.load_seen(tmp4)
assert led4["https://space.bilibili.com/333"]["status"] == "carded"
# K>=quota: search 返回 2 张已有 -> 整批跳过
def full_call(method, path, token=None, body=None):
    if "tenant_access_token" in path:
        return {"tenant_access_token": "FAKE"}
    if "/fields" in path:
        return {"code": 0, "data": {"items": [{"field_name": schema.COL_PLATFORM}], "has_more": False}}
    if "records/search" in path:
        return {"code": 0, "data": {"items": [
            {"record_id": "r1", "fields": {"日期": bili_cards._date_ms("2026-07-08"), "频道链接": {"link": "https://space.bilibili.com/111"}}},
            {"record_id": "r2", "fields": {"日期": bili_cards._date_ms("2026-07-08"), "频道链接": {"link": "https://space.bilibili.com/333"}}}],
            "has_more": False}}
    return {"code": 0}
bili_cards._feishu_call = full_call
res_full = bili_cards.push_bili_cards(cfg2, BSCORED, "2026-07-08", dry_run=False, seen_path=tempfile.mktemp(), pool_by_url={})
assert res_full["ok"] and res_full["picked"] == 0 and "配额" in res_full["note"], res_full
print("PASS 7: top-up 只补 quota-K 张 / K>=quota 整批跳过")

print("\nALL PASS: B站进推荐卡 失败安全 + 选卡口径 + W22.1 AI 闸门(判退不出卡/顺位补/宁缺不错) 成立")
