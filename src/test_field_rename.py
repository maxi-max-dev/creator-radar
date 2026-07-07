#!/usr/bin/env python3
"""离线单测: 飞书全池榜单「列改名迁移」请求构造正确性(零网络)。

只验证 _migrate_field_names 发出的请求形状对不对(HTTP 方法 / 路径 / body),
绝不发真请求: 全程 monkeypatch _feishu_call, 用假的字段清单驱动。
覆盖幂等三态: 旧名在→改名 / 新名已在→跳过 / 旧名新名并存→保守不动 / 全迁完→零请求。

跑: python3 src/test_field_rename.py   (退出码 0 = 全过)
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sync_full_ranking as sfr

APP, TID = "appXXX", "tblYYY"


def _fake_call_factory(fields_map):
    """造一个假 _feishu_call: GET fields 返回给定字段清单; PUT/POST 记账不发网络。
    fields_map: {field_name: field_id}。返回 (fake_call, calls)。calls 记录每次 (method, path, body)。"""
    calls = []

    def fake(method, path, token=None, body=None):
        calls.append((method, path, body))
        if method == "GET" and "/fields" in path:
            items = [{"field_name": n, "field_id": i} for n, i in fields_map.items()]
            return {"code": 0, "data": {"items": items, "has_more": False}}
        if method == "PUT" and "/fields/" in path:
            # 模拟改名成功: 就地更新假清单(旧名->新名), 保 id 不变
            new_name = body.get("field_name")
            fid = path.rsplit("/", 1)[-1]
            old = next((n for n, i in fields_map.items() if i == fid), None)
            if old:
                fields_map[new_name] = fields_map.pop(old)
            return {"code": 0}
        return {"code": 0}

    return fake, calls


def _run(fields_map):
    fake, calls = _fake_call_factory(fields_map)
    sfr._feishu_call = fake
    migrated = sfr._migrate_field_names("tok", APP, TID, sfr.FIELD_RENAMES)
    puts = [c for c in calls if c[0] == "PUT"]
    return migrated, puts, calls


def test_rename_old_to_new():
    """老表两列都是旧名 → 两个 PUT 改名请求, 路径 + body 正确。"""
    fmap = {"总分": "fld_score", "行动分级": "fld_grade", "订阅数": "fld_subs"}
    migrated, puts, _ = _run(dict(fmap))
    assert len(puts) == 2, f"应发 2 个改名 PUT, 实发 {len(puts)}"
    for method, path, body in puts:
        assert method == "PUT"
        assert path.startswith(f"/bitable/v1/apps/{APP}/tables/{TID}/fields/"), f"路径错: {path}"
        assert set(body.keys()) == {"field_name"}, f"body 只应含 field_name: {body}"
    # 断言改名映射正确: 总分->对路分, 行动分级->红绿灯
    got = {o: n for o, n in migrated}
    assert got == {"总分": sfr.schema.COL_FIT, "行动分级": sfr.schema.COL_LIGHT}, got
    # 断言用对了 field_id(总分那条 PUT 打到 fld_score)
    body_by_fid = {p.rsplit("/", 1)[-1]: b["field_name"] for _, p, b in puts}
    assert body_by_fid["fld_score"] == sfr.schema.COL_FIT
    assert body_by_fid["fld_grade"] == sfr.schema.COL_LIGHT
    print("PASS test_rename_old_to_new: 2 PUT, 路径/body/field_id 全对")


def test_idempotent_already_migrated():
    """新名已在(旧名不在) = 已迁过 → 零 PUT。"""
    fmap = {sfr.schema.COL_FIT: "fld_score", sfr.schema.COL_LIGHT: "fld_grade"}
    migrated, puts, _ = _run(dict(fmap))
    assert puts == [], f"已迁过应零改名请求, 实发 {len(puts)}"
    assert migrated == []
    print("PASS test_idempotent_already_migrated: 已迁过零 PUT")


def test_both_present_conservative():
    """旧名新名并存(别人手建了新列) → 保守不动, 零 PUT。"""
    fmap = {"总分": "fld_a", sfr.schema.COL_FIT: "fld_b",
            "行动分级": "fld_c", sfr.schema.COL_LIGHT: "fld_d"}
    migrated, puts, _ = _run(dict(fmap))
    assert puts == [], f"并存应保守不动, 实发 {len(puts)}"
    assert migrated == []
    print("PASS test_both_present_conservative: 新旧并存零 PUT(保守不动)")


def test_partial_only_one_old():
    """只有一列还是旧名(另一列已迁) → 只发 1 个 PUT。"""
    fmap = {"总分": "fld_score", sfr.schema.COL_LIGHT: "fld_grade"}
    migrated, puts, _ = _run(dict(fmap))
    assert len(puts) == 1, f"应发 1 个 PUT, 实发 {len(puts)}"
    assert migrated == [("总分", sfr.schema.COL_FIT)]
    print("PASS test_partial_only_one_old: 半迁只发 1 PUT")


if __name__ == "__main__":
    test_rename_old_to_new()
    test_idempotent_already_migrated()
    test_both_present_conservative()
    test_partial_only_one_old()
    print("\nALL PASS: 改名请求构造正确(零网络, 幂等四态全覆盖)")
