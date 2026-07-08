#!/usr/bin/env python3
"""离线失败安全 + 选卡逻辑单测(W22 B站进推荐卡)。零真实网络。
证明:
  1) 选卡只取 🟡, 绝不 🔴/🏢/无灯; 按 rank 取前 N; 已出过卡(seen 账本)的频道被跨日去重排除。
  2) B站卡完整档案/档案/在涨分/潜力分 结构性留空(不硬凑)。
  3) push_bili_cards 全链任一步炸了都返回 {ok:False}, 绝不抛(失败安全, 不影响 YouTube 卡/主链)。
  4) dry-run 只选卡不写表不记账本。
"""
import json, os, sys, tempfile
sys.path.insert(0, "src")
import bili_cards, schema

# ---- 造一批 B站 ranked(混红黄蓝 + 无灯), 覆盖各分支 ----
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
]

# 1) 选卡: 只 🟡, 取前 2, seen 排除
picks = bili_cards.select_bili_cards(BSCORED, seen={}, cards_per_day=2)
assert [p["channel_name"] for p in picks] == ["黄灯甲", "黄灯丙"], picks
# seen 排除 黄灯甲 -> 应顺延到 黄灯丙 + 黄灯戊
picks2 = bili_cards.select_bili_cards(BSCORED, seen={"https://space.bilibili.com/111": "2026-07-07"}, cards_per_day=2)
assert [p["channel_name"] for p in picks2] == ["黄灯丙", "黄灯戊"], picks2
# 全部 🟡 都出过 -> 0 张
allseen = {s["channel_url"]: "2026-07-07" for s in BSCORED if s["action_grade"] == "🟡"}
assert bili_cards.select_bili_cards(BSCORED, seen=allseen, cards_per_day=2) == []
print("PASS 1: 选卡只 🟡 / 按 rank 取前 N / 跨日去重排除已出卡频道")

# 2) 卡内容: 平台=B站, 红绿灯🟡, 完整档案/档案/在涨分/潜力分 结构性缺失(不带 key)
rows = bili_cards.build_bili_card_rows(picks, "2026-07-08")
r0 = rows[0]
assert r0[schema.COL_PLATFORM] == schema.PLATFORM_BILIBILI
assert r0[schema.COL_LIGHT] == "🟡"
assert r0[schema.COL_SUBS_TEXT] == schema.subs_dual(83307)   # 订阅双格式
assert "完整档案" not in r0 and "档案" not in r0, "B站卡完整档案/档案必须留空(不硬凑)"
assert "在涨分" not in r0 and "潜力分" not in r0 and "起势分" not in r0, "B站结构性无这些信号, 不带"
assert "活性" in r0["值得签1"] and "上传日期" in r0["值得签1"], "第一条证据必须诚实提示活性待人工核"
print("PASS 2: 平台=B站 / 红绿灯🟡 / 订阅双格式 / 完整档案+档案+在涨分 留空 / 诚实活性提示")

# 3) dry-run: 只选卡不写表不记账本
tmp = tempfile.mktemp(suffix=".json")
cfg = {"bilibili": {"cards_per_day": 2}, "bitable": {"credentials_path": "/nonexistent"}}
res_dry = bili_cards.push_bili_cards(cfg, BSCORED, "2026-07-08", dry_run=True, seen_path=tmp)
assert res_dry["ok"] and res_dry["dry_run"] and res_dry["picked"] == 2, res_dry
assert not os.path.exists(tmp), "dry-run 绝不能写账本"
print("PASS 3: dry-run 只选卡, 不写表不记账本")

# 4) 失败安全: batch_create 炸了 -> {ok:False}, 不抛; 账本不被污染
def boom_call(method, path, token=None, body=None):
    if "tenant_access_token" in path:
        return {"tenant_access_token": "FAKE"}
    if "/fields" in path:
        return {"code": 0, "data": {"items": [], "has_more": False}}
    if "records/search" in path:
        return {"code": 0, "data": {"items": [], "has_more": False}}
    if "batch_create" in path:
        raise RuntimeError("模拟飞书 500")   # 最狠: 直接抛, 看外层 try 是否兜住
    return {"code": 0}
bili_cards._feishu_call = boom_call
# 造一份能读到凭证的假 cfg(指向真实凭证文件, 只为过 os.path.exists; 网络已被 monkeypatch 全拦)
cred = os.path.expanduser("~/.config/creator-radar/feishu.json")
cfg2 = {"bilibili": {"cards_per_day": 2}, "bitable": {"credentials_path": cred, "avatar_enrich": False}}
tmp2 = tempfile.mktemp(suffix=".json")
res_boom = bili_cards.push_bili_cards(cfg2, BSCORED, "2026-07-08", dry_run=False, seen_path=tmp2)
assert res_boom["ok"] is False, res_boom
assert not os.path.exists(tmp2), "写表失败时账本绝不能被记(否则明天漏推)"
print("PASS 4: 写表异常 -> {ok:False} 不抛 / 账本不污染 (YouTube 卡与主链不受影响)")

print("\nALL PASS: B站进推荐卡 失败安全 + 选卡口径 成立")
