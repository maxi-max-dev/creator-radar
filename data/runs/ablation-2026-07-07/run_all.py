#!/usr/bin/env python3
"""串行跑完剩余 9 个变体(fresh-baseline 已单独跑过)，每个调 backtest.py。
ollama 本地 embedding，绝不并发。跑完把每变体的 metrics + 正例明细的原始数
汇总进 _summary.json，供 ABLATION.md 生成用。"""
import json, os, subprocess, sys, time

ROOT = "/Users/max/Documents/creator-radar"
ADIR = os.path.join(ROOT, "data/runs/ablation-2026-07-07")
CFG = os.path.join(ADIR, "configs")
POOL = os.path.join(ROOT, "data/pool/creator_pool.jsonl")

# fresh-baseline 已跑；这里跑其余 9 个（顺序固定，便于复现）
VARIANTS = [
    "no-sweet", "no-markers", "semantic-only",
    "theme-drop-pov_native", "theme-drop-adventure_bold", "theme-drop-authentic_vlog",
    "theme-drop-journey_narrative", "theme-drop-gear_native", "theme-drop-vertical_craft",
]


def run_one(name):
    out = os.path.join(ADIR, name)
    cfgp = os.path.join(CFG, name + ".json")
    err = os.path.join(ADIR, name + ".stderr.log")
    t0 = time.time()
    with open(err, "w") as ef:
        rc = subprocess.call(
            ["python3", os.path.join(ROOT, "src/backtest.py"),
             "--config", cfgp, "--pool", POOL, "--out", out + "/"],
            stdout=subprocess.DEVNULL, stderr=ef,
        )
    dt = time.time() - t0
    print(f"[{name}] rc={rc} {dt:.0f}s", flush=True)
    return rc


for i, v in enumerate(VARIANTS, 1):
    print(f"=== ({i}/{len(VARIANTS)}) {v} ===", flush=True)
    rc = run_one(v)
    if rc != 0:
        print(f"!!! {v} FAILED rc={rc}, see {v}.stderr.log", flush=True)

print("ALL_DONE", flush=True)
