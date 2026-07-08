#!/usr/bin/env python3
"""红绿灯拦截三数（76% / 8% / 86%）的独立复算脚本 · W15 复核冻结输入。

这是一个**自足**脚本：只读本目录里的三份冻结文件，不依赖生产池、不依赖引擎源码、
不联网，纯标准库。任何人 clone 仓库后 `python3 recompute_redlight.py` 即可复现台账
里那三个拦截数字，验证它们不是手搓出来的。

输入（同目录）：
  audit80_verdicts_raw.csv   80 个盲投候选的人工判级（✅ 可直接联系 / ⚠️ 需核验 / ❌ 盲投尴尬）
  ranked_youtube_daily.json  当日 YouTube 全池排序（含 action_grade = 引擎红绿灯，规则 v1.1）
  ranked_bilibili.json       当日 B站全池排序（含 action_grade）

口径（与 README「红绿灯拦得住吗」段落一致）：
  ① 拦截率 = 人工判 ❌ 的候选里，被引擎硬拦（🔴 或 🏢）的比例        → 25/33 ≈ 76%
  ② 误拦率 = 人工判 ✅ 的候选里，被引擎硬拦（🔴 或 🏢）的比例        → 2/25 = 8%
  ③ 🟢密度 = 引擎判 🟢 的候选里，人工也判 ✅ 的比例                 → 18/21 ≈ 86%

名字对齐：审计 CSV 里已知正例带 " 🟡正例" 装饰后缀，复算前剥掉，与 ranked 的
channel_name 精确匹配（80/80 全命中，无模糊匹配）。
"""
import csv, json, os, re

HERE = os.path.dirname(os.path.abspath(__file__))
SYM = {"✅": "green", "⚠️": "yellow", "❌": "red"}
BLOCK = {"🔴", "🏢"}  # 硬拦两色（别碰 + 官方联动都不发达人邀请）


def clean_name(nm):
    """剥掉审计 CSV 的正例装饰后缀，还原真实频道名。"""
    return re.sub(r"\s*🟡正例\s*$", "", nm).strip()


def load_human_verdicts(path):
    out = []  # (idx, name, human_grade)
    for r in list(csv.reader(open(path)))[1:]:
        if len(r) < 4 or not r[0].strip().isdigit():
            continue  # 跳过表头/分节行
        h = SYM.get(r[3].strip())
        if h:
            out.append((r[0].strip(), clean_name(r[1]), h))
    return out


def load_engine_grades(paths):
    eng = {}
    for p in paths:
        for s in json.load(open(p)):
            eng[(s.get("channel_name") or "").strip()] = s.get("action_grade")
    return eng


def main():
    human = load_human_verdicts(os.path.join(HERE, "audit80_verdicts_raw.csv"))
    eng = load_engine_grades([
        os.path.join(HERE, "ranked_youtube_daily.json"),
        os.path.join(HERE, "ranked_bilibili.json"),
    ])

    joined, missing = [], []
    for idx, name, h in human:
        g = eng.get(name)
        (joined if g is not None else missing).append((idx, name, h, g))

    print(f"候选总数 {len(human)}，join 命中 {len(joined)}，未命中 {len(missing)}")
    for idx, name, h, _ in missing:
        print(f"  ⚠️ 未 join: #{idx} {name!r} (人工={h})")

    red = [r for r in joined if r[2] == "red"]
    grn = [r for r in joined if r[2] == "green"]
    egreen = [r for r in joined if r[3] == "🟢"]

    i1 = sum(1 for r in red if r[3] in BLOCK)
    i2 = sum(1 for r in grn if r[3] in BLOCK)
    i3 = sum(1 for r in egreen if r[2] == "green")

    print()
    print(f"① 拦截率 = {i1}/{len(red)} = {i1/len(red)*100:.1f}%   (README: 76%)")
    print(f"② 误拦率 = {i2}/{len(grn)} = {i2/len(grn)*100:.1f}%   (README: 8%)")
    print(f"③ 🟢密度 = {i3}/{len(egreen)} = {i3/len(egreen)*100:.1f}%  (README: 86%)")


if __name__ == "__main__":
    main()
