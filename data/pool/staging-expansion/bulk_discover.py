#!/usr/bin/env python3
"""Phase 1 池子扩容: 批量发现新运动户外创作者 → 暂存区(untracked)。

严格纪律:
  - 只读 tracked 文件(config/insta360.json, src/collect.py, src/radar_lib.py,
    data/pool/creator_pool.jsonl)。复用 collect.py 的函数(import,不改)。
  - 一切产出只进本目录 data/pool/staging-expansion/:
      discovered-2026-07-07.jsonl  (发现的频道,行格式对齐主池 + vertical 字段)
      processed_channels.json      (去重账本,断点续跑)
  - 不合并进主池,不改 config,不 commit,不下载任何视频/音频。

预算: 默认墙钟 4h 或 本次新增 3000 频道,先到为准(--wall-seconds/--max-new 可覆盖)。单垂类入池上限 400(跨波次累计)。
波次: --lang default=第一波(词库中无 lang 标记的英西日葡法词) / --lang zh=第二波中文词 / --lang all=全部。
      中文第二波(Max 2026-07-07 追加)跑法: python3 bulk_discover.py --lang zh --wall-seconds 2700 --max-new 500
去重: 对 主池 + 暂存区已有 + 已处理账本 三重去重,账本跨波次共享。
韧性: 每频道处理完即刷盘账本与 jsonl;单频道失败跳过不中断;每 200 频道打一行进度。
"""
import argparse, json, os, sys, time, signal
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))  # .../creator-radar
SRC = os.path.join(REPO, "src")
sys.path.insert(0, SRC)

# 复用现有采集函数(import,绝不修改 collect.py)
from collect import _run_ytdlp, fetch_channel, make_snapshot  # noqa: E402
from radar_lib import load_config, load_pool, leak_pattern     # noqa: E402

CONFIG = os.path.join(REPO, "config", "insta360.json")
MAIN_POOL = os.path.join(REPO, "data", "pool", "creator_pool.jsonl")
TERMS_FILE = os.path.join(HERE, "discover_terms.json")
OUT_JSONL = os.path.join(HERE, f"discovered-{date.today().isoformat()}.jsonl")
LEDGER = os.path.join(HERE, "processed_channels.json")

# ---- 预算旋钮 ----
WALL_CLOCK_SECONDS = 4 * 60 * 60      # 4 小时
MAX_NEW_CHANNELS = 3000               # 新增频道上限
PER_VERTICAL_CAP = 400                # 单垂类入池上限
RESULTS_PER_TERM = 50                 # 每词取前 50 结果
PROGRESS_EVERY = 200                  # 每 N 频道打一行进度


def load_ledger():
    """去重账本: 已处理(尝试过)的频道 id/url 集合。重启不重复劳动。"""
    if os.path.exists(LEDGER):
        try:
            with open(LEDGER) as f:
                d = json.load(f)
            return set(d.get("processed", []))
        except (json.JSONDecodeError, ValueError):
            print("  ledger corrupt, starting fresh", file=sys.stderr)
    return set()


def save_ledger(processed):
    """原子写账本(临时文件 + rename)。"""
    tmp = LEDGER + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"processed": sorted(x for x in processed if x)}, f, ensure_ascii=False)
    os.replace(tmp, LEDGER)


def existing_have_set():
    """主池 + 已有暂存 jsonl 里的所有 channel_id/url,发现阶段的去重基线。"""
    have = set()
    for r in load_pool(MAIN_POOL):
        have.add(r.get("channel_url")); have.add(r.get("channel_id"))
    if os.path.exists(OUT_JSONL):
        with open(OUT_JSONL) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                have.add(r.get("channel_url")); have.add(r.get("channel_id"))
    have.discard(None)
    return have


def load_existing_vertical_counts():
    """重启时从已有暂存 jsonl 恢复各垂类已入池计数(尊重单垂类上限)。"""
    counts = {}
    if os.path.exists(OUT_JSONL):
        with open(OUT_JSONL) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                v = r.get("vertical")
                if v:
                    counts[v] = counts.get(v, 0) + 1
    return counts


def gather_candidates(terms, have, leak_re):
    """逐词搜索(每词前 RESULTS_PER_TERM 结果),收集不在 have 里的候选频道。
    返回 [(vertical, channel_id, channel_url), ...],保 vertical 标签供入池写字段。
    去重: 去主池/暂存已有 + 去本次候选内部重复 + 去品牌自营号。"""
    throttle = _throttle()
    seen_cid = set()
    candidates = []
    for i, t in enumerate(terms, 1):
        vertical, term = t["vertical"], t["term"]
        d = _run_ytdlp(f"ytsearch{RESULTS_PER_TERM}:{term}")
        n_before = len(candidates)
        for e in (d.get("entries") or []) if d else []:
            cid, curl = e.get("channel_id"), e.get("channel_url")
            name = e.get("channel") or ""
            if not curl or cid in seen_cid:
                continue
            if cid in have or curl in have:
                continue
            if leak_re.search(name):  # 品牌自营号不入池
                continue
            seen_cid.add(cid)
            candidates.append((vertical, cid, curl))
        print(f"  [{i}/{len(terms)}] term={term!r} vertical={vertical} "
              f"+{len(candidates) - n_before} cand (total {len(candidates)})", file=sys.stderr)
        time.sleep(throttle)
    return candidates


def _throttle():
    cfg = load_config(CONFIG)
    return cfg["collect"]["throttle_seconds"]


class Stopper:
    """SIGINT/SIGTERM 优雅停机: 当前频道处理完就退,已刷盘不丢。"""
    stop = False
    def __init__(self):
        signal.signal(signal.SIGINT, self._h)
        signal.signal(signal.SIGTERM, self._h)
    def _h(self, *a):
        print("  signal received, finishing current channel then stopping", file=sys.stderr)
        self.stop = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", default="default",
                    help="default=只跑无 lang 标记的词(第一波) / zh=只跑 lang:zh 中文词(第二波) / all=全部")
    ap.add_argument("--wall-seconds", type=int, default=WALL_CLOCK_SECONDS, help="本次运行墙钟上限(秒)")
    ap.add_argument("--max-new", type=int, default=MAX_NEW_CHANNELS, help="本次运行新增频道上限(per-run)")
    args = ap.parse_args()

    cfg = load_config(CONFIG)
    throttle = cfg["collect"]["throttle_seconds"]
    n_vid = cfg["collect"]["videos_per_channel"]
    leak_re = leak_pattern(cfg)
    today = date.today().isoformat()

    all_terms = json.load(open(TERMS_FILE))["terms"]
    if args.lang == "all":
        terms = all_terms
    elif args.lang == "default":
        terms = [t for t in all_terms if not t.get("lang")]
    else:
        terms = [t for t in all_terms if t.get("lang") == args.lang]

    processed = load_ledger()          # 已尝试过的频道(断点续跑,跨波次共享)
    have = existing_have_set()         # 主池 + 暂存已有
    vert_counts = load_existing_vertical_counts()  # 跨波次累计,垂类上限共享
    staging_total = sum(vert_counts.values())      # 暂存区已有总数
    added_wave = 0                                  # 本次运行(本波次)新增

    print(f"=== bulk_discover start {today} lang={args.lang} ===", file=sys.stderr)
    print(f"terms={len(terms)}/{len(all_terms)} main_pool+staging have={len(have)} "
          f"ledger_processed={len(processed)} already_in_staging={staging_total}", file=sys.stderr)
    print(f"budget: wall={args.wall_seconds}s new_cap={args.max_new} "
          f"per_vertical_cap={PER_VERTICAL_CAP} results/term={RESULTS_PER_TERM} throttle={throttle}s",
          file=sys.stderr)

    stopper = Stopper()
    t_start = time.time()

    # ---- 阶段一: 搜集全部候选(逐词,轻量) ----
    candidates = gather_candidates(terms, have, leak_re)
    print(f"=== candidates gathered: {len(candidates)} unique channels to try ===", file=sys.stderr)

    # ---- 阶段二: 逐频道拉元数据入暂存 ----
    out = open(OUT_JSONL, "a")
    n_seen = 0            # 本轮尝试过的候选数(含跳过)
    n_fetched = 0         # 成功拉到元数据数
    n_fail = 0            # fetch 失败/无名数
    n_skip_dup = 0        # 因已处理/已有跳过数
    n_skip_cap = 0        # 因垂类满跳过数

    try:
        for vertical, cid, curl in candidates:
            # 预算:墙钟 / 总量 到顶即停
            if stopper.stop:
                print("  stop flag set, breaking", file=sys.stderr); break
            if time.time() - t_start >= args.wall_seconds:
                print("  wall-clock budget reached, stopping", file=sys.stderr); break
            if added_wave >= args.max_new:
                print(f"  new-channel cap {args.max_new} reached, stopping", file=sys.stderr); break

            key = cid or curl
            if key in processed or curl in have or cid in have:
                n_skip_dup += 1
                continue
            if vert_counts.get(vertical, 0) >= PER_VERTICAL_CAP:
                n_skip_cap += 1
                processed.add(key)  # 记账避免续跑反复评估
                continue

            n_seen += 1
            fresh = None
            try:
                fresh = fetch_channel(curl, n_vid)  # 复用 collect.fetch_channel(--skip-download)
            except Exception as ex:  # 单频道任何异常都跳过,不中断整轮
                print(f"  fetch error {curl}: {ex}", file=sys.stderr)
            time.sleep(throttle)

            # 处理完(不论成败)记账并刷盘,断点续跑不重复
            processed.add(key)

            if not fresh or not fresh.get("channel_name"):
                n_fail += 1
            else:
                f_url, f_cid = fresh.get("channel_url"), fresh.get("channel_id")
                if f_url in have or f_cid in have:
                    n_skip_dup += 1  # 元数据阶段才暴露的重复(id 归一)
                elif leak_re.search(fresh.get("channel_name") or ""):
                    n_fail += 1      # 品牌自营号
                else:
                    fresh.pop("_snapshot", None)  # 视频层快照不入暂存(先存后洗留给主流程,本阶段只发现)
                    fresh.update({
                        "source": "auto-discover", "is_positive": False, "positive_source": None,
                        "first_seen": today, "last_refreshed": today,
                        "vertical": vertical,   # 额外字段:打分器忽略,Phase 2 合并用
                    })
                    out.write(json.dumps(fresh, ensure_ascii=False) + "\n")
                    out.flush(); os.fsync(out.fileno())
                    have.add(f_url); have.add(f_cid)
                    vert_counts[vertical] = vert_counts.get(vertical, 0) + 1
                    staging_total += 1
                    added_wave += 1

            if n_seen % PROGRESS_EVERY == 0:
                elapsed = int(time.time() - t_start)
                print(f"  progress: seen={n_seen} added_wave={added_wave} staging_total={staging_total} "
                      f"fail={n_fail} dup={n_skip_dup} cap={n_skip_cap} elapsed={elapsed}s "
                      f"verticals={dict(sorted(vert_counts.items()))}", file=sys.stderr)
                save_ledger(processed)  # 周期性刷账本(每频道也可,这里降 IO 频率)
    finally:
        out.close()
        save_ledger(processed)

    elapsed = int(time.time() - t_start)
    report = {
        "date": today,
        "lang_wave": args.lang,
        "elapsed_seconds": elapsed,
        "candidates_gathered": len(candidates),
        "channels_tried": n_seen,
        "added_this_wave": added_wave,
        "staging_total_after": staging_total,
        "fetch_failed_or_leak": n_fail,
        "skipped_duplicate": n_skip_dup,
        "skipped_vertical_cap": n_skip_cap,
        "fail_rate_pct": round(n_fail / n_seen * 100, 1) if n_seen else 0.0,
        "vertical_distribution_cumulative": dict(sorted(vert_counts.items())),
        "out_jsonl": OUT_JSONL,
        "ledger": LEDGER,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    main()
