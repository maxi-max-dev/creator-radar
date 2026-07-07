#!/usr/bin/env python3
"""字幕记录层(内容层打标的地基语料 + 原始数据全记录的一部分)。
对当日排序产物 top_channels 频道 × 每频道最近 videos_per_channel 视频，用 yt-dlp 拉字幕，
VTT 清洗成干净纯文本落盘。打标是下一棒的事，本工具纯机械提取+清洗，不碰 LLM。

字幕语言选择(优先级):
  1) 人工字幕(subtitles): 命中频道主语言或英语则用，最可靠。
  2) 自动字幕(automatic_captions): 优先 *-orig 原声轨(YouTube 标为 "X (Original)"，是视频真实源语言，
     其余 150+ 条都是机翻，绝不误取)；再退回英语。
  语言不写死在池子里(池子无 language 字段/country 全空)，靠视频元数据里的原声轨自动判定。
预算红线(硬性):
  每天总处理视频 ≤ budget_videos_per_day(config)，单视频失败跳过不中断整批，节流 throttle_seconds。
去重账本 data/transcripts/fetched_index.json:
  video 级去重(抓过永不重抓)；没字幕的视频记 {no_subs:true} 占位，防下次重试。
存储:
  data/transcripts/<channel_id>/<video_id>.json，字段见 _record()。
  ⚠️ data/transcripts/ 必须保持 gitignore(整段字幕=第三方版权内容，绝不进将来要公开的仓库)。
禁止下载视频/音频本体(--skip-download 恒开，只 --write-subs/--write-auto-subs)。
"""
import argparse, json, os, re, subprocess, sys, tempfile, time
from datetime import date, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from radar_lib import load_config

POOL = os.path.join(ROOT, "data", "pool", "creator_pool.jsonl")
DAILY = os.path.join(ROOT, "data", "runs", "daily")
TRANSCRIPTS = os.path.join(ROOT, "data", "transcripts")
INDEX = os.path.join(TRANSCRIPTS, "fetched_index.json")

# transcripts config 节缺省(insta360.json 由他人维护，本工具不写它；缺节时用这些值也能跑)
DEFAULTS = {
    "top_channels": 50,
    "videos_per_channel": 3,
    "budget_videos_per_day": 150,
    "langs": ["en"],            # 兜底语言序列(人工字幕优先命中这些；自动字幕优先原声轨)
    "throttle_seconds": 2.5,    # 缺省与 collect.throttle_seconds 一致
}


# ---------- 账本(video 级去重) ----------

def load_index():
    if os.path.exists(INDEX):
        try:
            return json.load(open(INDEX))
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def save_index(idx):
    os.makedirs(TRANSCRIPTS, exist_ok=True)
    tmp = INDEX + ".tmp"
    with open(tmp, "w") as f:
        json.dump(idx, f, ensure_ascii=False, indent=1)
    os.replace(tmp, INDEX)


def already_fetched(video_id, idx):
    """video_id 抓过(成功或已确认无字幕)则 True，永不重抓。"""
    return video_id in idx


# ---------- VTT 清洗 ----------

_TS_LINE = re.compile(r"^\d\d:\d\d:\d\d\.\d\d\d\s*-->")           # 时间戳行
_INLINE_TS = re.compile(r"<\d\d:\d\d:\d\d\.\d\d\d>")               # 行内词级时间戳 <00:00:00.000>
_TAG = re.compile(r"</?c[^>]*>|</?[iub]>|<v[^>]*>|</v>")           # <c>/<c.color>/<i>/<b>/<v Speaker> 等
_ENTITY = {"&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">", "&#39;": "'", "&quot;": '"'}


def _strip_line(line):
    """去掉一行里的 VTT 标记/词级时间戳/HTML 实体，返回归一化后的纯文本(可能为空串)。"""
    line = _INLINE_TS.sub("", line)
    line = _TAG.sub("", line)
    for k, v in _ENTITY.items():
        line = line.replace(k, v)
    return re.sub(r"\s+", " ", line).strip()


def clean_vtt(raw):
    """VTT -> 干净纯文本: 去时间戳/cue 设置/标记，去自动字幕滚动重复行，合并成段。
    滚动去重逻辑: 自动字幕每个 cue 会把上一 cue 的尾行带到本 cue 顶部滚动展示，
    只保留"相对已输出内容是新出现"的行，连续完全重复的行折叠为一次。"""
    out = []
    for block in re.split(r"\r?\n\r?\n", raw):
        block = block.strip("\r\n")
        if not block or block.startswith(("WEBVTT", "Kind:", "Language:", "NOTE", "STYLE")):
            continue
        for line in block.splitlines():
            if _TS_LINE.match(line) or "-->" in line:   # 时间戳/cue 行
                continue
            txt = _strip_line(line)
            if not txt:
                continue
            # 滚动去重: 若与最近已输出的一行完全相同(自动字幕把上句滚到下个 cue)，跳过
            if out and out[-1] == txt:
                continue
            out.append(txt)

    # 二次滚动折叠: 处理 A / A B / B 这类逐词滚动(相邻行互为前后缀时保留更长者)
    merged = []
    for txt in out:
        if merged:
            prev = merged[-1]
            if txt.startswith(prev) and len(txt) > len(prev):   # 本行是上一行的延展 -> 用本行替换
                merged[-1] = txt
                continue
            if prev.endswith(txt):                              # 本行是上一行的尾部重复 -> 跳过
                continue
        merged.append(txt)

    text = " ".join(merged)
    return re.sub(r"\s+", " ", text).strip()


# ---------- yt-dlp 拉取 ----------

def probe_video(video_id, throttle):
    """探测单个视频的字幕可用性与标题(1 次请求，不下载本体)。
    返回 (title, subtitles_dict, autocaps_dict) 或 (None, None, None)。"""
    cmd = ["yt-dlp", f"https://www.youtube.com/watch?v={video_id}",
           "-J", "--skip-download", "--no-warnings", "--no-update"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if p.returncode != 0 or not p.stdout.strip():
            return None, None, None
        d = json.loads(p.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        return None, None, None
    finally:
        time.sleep(throttle)
    return d.get("title"), (d.get("subtitles") or {}), (d.get("automatic_captions") or {})


_NON_CAPTION = {"live_chat"}  # yt-dlp 把直播聊天回放也塞进 subtitles，不是字幕，排除


def pick_lang(subtitles, autocaps, langs):
    """选字幕语言。返回 (sub_lang_arg, resolved_lang, is_auto) 或 (None, None, None)=无可用字幕。
    优先级: 人工字幕命中 langs > 人工字幕任意真语言 > 自动字幕原声轨(*-orig) > 自动字幕英语。"""
    # 直播聊天回放(live_chat)伪装成 subtitles，会拉不到 VTT，先剔除避免误判无字幕
    manual = [k for k in subtitles if k not in _NON_CAPTION]

    # 1) 人工字幕: 先按 langs 顺序命中，否则取第一条真人工字幕
    if manual:
        for want in langs:
            if want in manual:
                return want, want, False
        return manual[0], manual[0], False

    # 2) 自动字幕原声轨: YouTube 用 <lang>-orig 标记视频真实源语言(如 en-orig=English (Original))
    orig = [k for k in autocaps if k.endswith("-orig")]
    if orig:
        arg = orig[0]
        resolved = arg[:-5] or arg   # 去掉 -orig 后缀得到语言码
        return arg, resolved, True

    # 3) 兜底: 自动字幕里的 langs 序列(通常 en)
    for want in langs:
        if want in autocaps:
            return want, want, True

    return None, None, None


def fetch_subtitle_text(video_id, sub_lang_arg, is_auto, throttle):
    """拉指定语言字幕的 VTT 并清洗成纯文本(禁下载本体，1 次请求)。返回清洗后文本或 None。"""
    flag = "--write-auto-subs" if is_auto else "--write-subs"
    with tempfile.TemporaryDirectory() as td:
        cmd = ["yt-dlp", f"https://www.youtube.com/watch?v={video_id}",
               "--skip-download", flag, "--sub-langs", sub_lang_arg, "--sub-format", "vtt",
               "--no-warnings", "--no-update", "-o", os.path.join(td, "%(id)s.%(ext)s")]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            time.sleep(throttle)
            return None
        time.sleep(throttle)
        if p.returncode != 0:
            return None
        # 找到落下的 .vtt(文件名可能是 <id>.<lang>.vtt)
        vtts = [f for f in os.listdir(td) if f.endswith(".vtt")]
        if not vtts:
            return None
        try:
            raw = open(os.path.join(td, vtts[0]), encoding="utf-8", errors="replace").read()
        except OSError:
            return None
    text = clean_vtt(raw)
    return text or None


# ---------- 频道来源(复用 comments.py 同款: ranked.json 无 channel_id，按 url 从池子补) ----------

def fetch_recent_video_ids(channel_url, n_videos, throttle):
    """列频道最近 n_videos 个视频 id(flat，不下载)。1 次请求。失败返回 []。"""
    cmd = ["yt-dlp", channel_url.rstrip("/") + "/videos", "-J", "--flat-playlist",
           "--skip-download", "--no-warnings", "--no-update", "--playlist-items", f"1-{n_videos}"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if p.returncode != 0 or not p.stdout.strip():
            return []
        d = json.loads(p.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        return []
    finally:
        time.sleep(throttle)
    out = []
    for e in (d.get("entries") or []):
        vid, title = e.get("id"), e.get("title")
        if vid:
            out.append({"video_id": vid, "title": title})
    return out


def load_ranked_channels(today, top_n):
    """从当日 daily/<today>/ranked.json 取前 top_n 频道(带 channel_id，来自池子按 url 回填)。"""
    ranked_path = os.path.join(DAILY, today, "ranked.json")
    if not os.path.exists(ranked_path):
        return []
    scored = json.load(open(ranked_path))
    pool_by_url = {}
    for l in open(POOL):
        r = json.loads(l)
        pool_by_url[r.get("channel_url")] = r
    out = []
    for s in scored[:top_n]:
        p = pool_by_url.get(s["channel_url"], {})
        cid = p.get("channel_id")
        if not cid:
            continue
        out.append({"channel_id": cid, "channel_url": s["channel_url"], "channel_name": s["channel_name"]})
    return out


# ---------- 落盘 ----------

def _record(ch, video_id, title, lang, is_auto, text):
    return {
        "channel_id": ch["channel_id"],
        "channel_name": ch["channel_name"],
        "video_id": video_id,
        "title": title,
        "lang": lang,
        "is_auto": is_auto,
        "text": text,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }


def save_transcript(rec):
    """落盘 data/transcripts/<channel_id>/<video_id>.json(幂等覆盖，原子写)。"""
    ch_dir = os.path.join(TRANSCRIPTS, rec["channel_id"])
    os.makedirs(ch_dir, exist_ok=True)
    path = os.path.join(ch_dir, rec["video_id"] + ".json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ---------- 主流程 ----------

def _cfg(cfg):
    """合并 transcripts 节与缺省(insta360.json 不含此节时也能跑)。throttle 缺省取 collect 的。"""
    tcfg = dict(DEFAULTS)
    tcfg["throttle_seconds"] = cfg.get("collect", {}).get("throttle_seconds", DEFAULTS["throttle_seconds"])
    tcfg.update(cfg.get("transcripts", {}) or {})
    return tcfg


def run(cfg, today, top_n=None, vids_override=None):
    """top_n 频道 × 每频道最近 videos_per_channel 视频 × 拉字幕清洗落盘。
    受预算(budget_videos_per_day)与 video 级账本约束；单视频失败跳过不中断。"""
    tcfg = _cfg(cfg)
    top_n = top_n if top_n is not None else tcfg["top_channels"]
    n_vid = vids_override if vids_override is not None else tcfg["videos_per_channel"]
    budget = tcfg["budget_videos_per_day"]
    langs = tcfg["langs"]
    throttle = tcfg["throttle_seconds"]

    idx = load_index()
    channels = load_ranked_channels(today, top_n)

    videos_used = 0
    from collections import Counter
    lang_dist = Counter()
    stats = {"channels_considered": len(channels), "channels_attempted": 0,
             "videos_ok": 0, "videos_no_subs": 0, "videos_failed": 0,
             "videos_skipped_dedup": 0, "videos_budget": budget, "videos_processed": 0}

    for ch in channels:
        if videos_used >= budget:
            print(f"transcripts: 预算 {budget} 视频用尽，停止", file=sys.stderr)
            break
        stats["channels_attempted"] += 1
        try:
            vids = fetch_recent_video_ids(ch["channel_url"], n_vid, throttle)
            if not vids:
                print(f"  transcripts miss (no videos): {ch['channel_name']}", file=sys.stderr)
                continue

            for v in vids[:n_vid]:
                vid = v["video_id"]
                if already_fetched(vid, idx):
                    stats["videos_skipped_dedup"] += 1
                    continue
                if videos_used >= budget:
                    break
                videos_used += 1  # 每尝试一个视频计一次预算(含 probe)

                title, subs, autos = probe_video(vid, throttle)
                if title is None and subs is None and autos is None:
                    stats["videos_failed"] += 1
                    print(f"  transcripts miss (probe fail {vid}): {ch['channel_name']}", file=sys.stderr)
                    continue
                title = title or v.get("title")

                sub_arg, lang, is_auto = pick_lang(subs, autos, langs)
                if sub_arg is None:
                    # 确认无字幕: 记占位账本，永不重试
                    idx[vid] = {"no_subs": True, "channel_id": ch["channel_id"], "date": today}
                    stats["videos_no_subs"] += 1
                    print(f"  transcripts no-subs: {ch['channel_name']} / {vid}", file=sys.stderr)
                    continue

                text = fetch_subtitle_text(vid, sub_arg, is_auto, throttle)
                if not text:
                    # 探测到轨但取/洗后为空，也按无字幕记占位防重试
                    idx[vid] = {"no_subs": True, "channel_id": ch["channel_id"], "date": today}
                    stats["videos_no_subs"] += 1
                    print(f"  transcripts empty-after-clean: {ch['channel_name']} / {vid}", file=sys.stderr)
                    continue

                rec = _record(ch, vid, title, lang, is_auto, text)
                save_transcript(rec)
                idx[vid] = {"no_subs": False, "channel_id": ch["channel_id"],
                            "lang": lang, "is_auto": is_auto, "date": today}
                lang_dist[lang] += 1
                stats["videos_ok"] += 1
                print(f"  transcripts ok: {ch['channel_name']} / {vid} [{lang}"
                      f"{'/auto' if is_auto else '/manual'}] {len(text)} chars "
                      f"(budget {videos_used}/{budget})", file=sys.stderr)
        except Exception as e:  # 单频道任何异常都不中断整批
            print(f"  transcripts SKIP {ch['channel_name']}: {e}", file=sys.stderr)

    stats["videos_processed"] = videos_used
    stats["lang_distribution"] = dict(lang_dist)
    save_index(idx)
    return stats


def main():
    ap = argparse.ArgumentParser(description="字幕记录层(video 级去重+预算+禁下载本体)")
    ap.add_argument("--config", required=True)
    ap.add_argument("--date", help="目标日期(默认今天)")
    ap.add_argument("--top-n", type=int, help="覆盖 transcripts.top_channels(冒烟用小值)")
    ap.add_argument("--videos-per-channel", type=int, help="覆盖 transcripts.videos_per_channel")
    args = ap.parse_args()
    cfg = load_config(args.config)
    today = args.date or date.today().isoformat()
    stats = run(cfg, today, top_n=args.top_n, vids_override=args.videos_per_channel)
    print(f"字幕记录完成: 尝试 {stats['channels_attempted']} 频道，"
          f"成功 {stats['videos_ok']}，无字幕 {stats['videos_no_subs']}，"
          f"失败 {stats['videos_failed']}，去重跳过 {stats['videos_skipped_dedup']}，"
          f"处理 {stats['videos_processed']}/{stats['videos_budget']}，"
          f"语言分布 {stats['lang_distribution']}", file=sys.stderr)
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
