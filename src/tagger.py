#!/usr/bin/env python3
"""打标层 v1: 对已有字幕的频道产结构化内容标签(分析层"把每一个汇总完的数据打上标签")。

对每个有字幕的频道，把该频道字幕(多条拼接截断)+ 简介 + 近期标题喂给本地 ollama qwen3:8b，
产出 content_themes/vertical/content_forms/pov_style/language/brand_traces/audience_notes。

脱敏双视图(防泄漏的根基，Max 点名的设计):
  data/tags/tags_raw.jsonl         完整标签，含 brand_traces，供档案/身份路径(already_partner 检测)使用
  data/tags/tags_for_scoring.jsonl 删掉 brand_traces 整段，且 content_themes/content_forms 里混进的品牌名
                                    (含变体串)用 config/insta360.json 的 leak_tokens_pattern 正则剔除，
                                    供打分路径使用，永不能看到品牌痕迹。

本模块只新建文件，不改任何既有文件；配置全走下面 DEFAULTS 字典(学 src/transcripts.py 的做法)，
只读 config/insta360.json 的 leak_tokens_pattern 一个字段做脱敏，不读写该文件其余内容。
纯标准库 + 本地 ollama /api/chat。逐频道失败跳过计数不中断，按 channel_id 覆盖幂等可重跑。
"""
import argparse, json, os, re, sys, time, urllib.error, urllib.request
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TRANSCRIPTS = os.path.join(ROOT, "data", "transcripts")
POOL = os.path.join(ROOT, "data", "pool", "creator_pool.jsonl")
COMMENTS = os.path.join(ROOT, "data", "comments")
TAGS_DIR = os.path.join(ROOT, "data", "tags")
CONFIG_PATH = os.path.join(ROOT, "config", "insta360.json")

# 本模块自带缺省(不写 config/insta360.json，只读它的 leak_tokens_pattern 一个字段)
DEFAULTS = {
    "model": "qwen3:8b",
    "endpoint": "http://127.0.0.1:11434/api/chat",
    "temperature": 0.2,
    "timeout_seconds": 180,
    "transcript_chars": 8000,      # 单频道字幕拼接后截断长度
    "description_chars": 600,      # 与 doc_fields.description_chars 同惯例，不读取只是数值上保持一致
    "titles_chars": 600,
    "comments_sample": 15,         # audience_notes 语料最多取多少条评论文本
    "comment_text_chars": 200,     # 单条评论截断，防超长评论把语料撑爆
}

VERTICALS = ["moto", "cycling", "surf", "ski", "climb", "dive", "fpv", "hike", "run", "skate", "other"]

SYS_PROMPT = (
    "你是内容打标分析师，服务对象是运动影像相机品牌的选人分析工作。"
    "给你一个 YouTube 户外/骑行类创作者的真实字幕语料、频道简介、近期视频标题(可能还有观众评论样本)，"
    "输出一张结构化 JSON 标签卡，用于内容主题分类和后续选人分析。\n\n"
    "字段语义(必须严格按这里的含义):\n"
    "- content_themes: 内容主题标签数组，3-6 个，中文，具体到可操作的分类粒度"
    "(如 长途摩旅/机车测评/滑雪教学/装备安装，不要写宽泛词如'户外''运动')\n"
    f"- vertical: 垂类单选，只能是这些之一: {'/'.join(VERTICALS)}。fpv 专指无人机穿越机内容，"
    "不确定或跨多个垂类时选 other\n"
    "- content_forms: 内容形态数组(如 教程/vlog/竞速/长途游记/装备评测/剪辑集/纪录片)\n"
    "- pov_style: 拍摄视角，只能是: pov 为主 / 第三人称为主 / 混合\n"
    "- brand_traces: 只关注运动相机/摄影器材品牌，不是骑行载具品牌(摩托车/自行车品牌名不算，"
    "如 KTM/雅马哈/闪电 这类不要放进 competitors)。"
    "{\"insta360\": 字幕/简介/标题里是否提到 insta360 或影石(bool), "
    "\"competitors\": [提到的运动相机/运动摄影器材竞品品牌名，如 gopro/dji/大疆/akaso/sjcam，"
    "仅限相机厂商，不含骑行载具品牌], "
    "\"quotes\": [最多2条包含相机品牌提及的原句摘录，每条不超过20字]}\n"
    "- audience_notes: 如果给了评论样本，写一句话受众印象(中文)；没给评论样本就写 null，不要编\n\n"
    "铁律: 只能从我给的真实语料里提取和归纳，不确定的内容主题不要硬凑够6个，"
    "没提到品牌就如实填 false/空数组，不许因为品牌语境而编造品牌提及。/no_think"
)

SCHEMA_HINT = (
    '严格输出这个 JSON 结构，不要多余文字:\n'
    '{"content_themes":["主题1","主题2","主题3"],'
    f'"vertical":"{"|".join(VERTICALS)} 选一个",'
    '"content_forms":["形态1","形态2"],'
    '"pov_style":"pov 为主|第三人称为主|混合",'
    '"brand_traces":{"insta360":false,"competitors":[],"quotes":[]},'
    '"audience_notes":"一句话或 null"}'
)


# ---------- 输入装配 ----------

def load_leak_pattern():
    """只读 config/insta360.json 的 leak_tokens_pattern 一个字段(不读写其余内容，不改此文件)。"""
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    return re.compile(cfg["leak_tokens_pattern"], re.I)


def load_pool_by_channel_id():
    by_id = {}
    if not os.path.exists(POOL):
        return by_id
    for line in open(POOL):
        try:
            r = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        cid = r.get("channel_id")
        if cid:
            by_id[cid] = r
    return by_id


def list_transcript_channels():
    """有字幕目录的 channel_id 列表(排除账本文件)。"""
    if not os.path.isdir(TRANSCRIPTS):
        return []
    out = []
    for name in sorted(os.listdir(TRANSCRIPTS)):
        p = os.path.join(TRANSCRIPTS, name)
        if os.path.isdir(p):
            out.append(name)
    return out


def load_channel_transcripts(channel_id, max_chars):
    """拼接该频道所有字幕文件的 text 字段(截断到 max_chars)，并取多数语言当该频道语言。
    返回 (text, lang) 或 (None, None) 无可用字幕。"""
    ch_dir = os.path.join(TRANSCRIPTS, channel_id)
    if not os.path.isdir(ch_dir):
        return None, None
    texts, langs = [], []
    for fn in sorted(os.listdir(ch_dir)):
        if not fn.endswith(".json"):
            continue
        try:
            rec = json.load(open(os.path.join(ch_dir, fn)))
        except (json.JSONDecodeError, ValueError, OSError):
            continue
        t = rec.get("text")
        if t:
            texts.append(t)
        if rec.get("lang"):
            langs.append(rec["lang"])
    if not texts:
        return None, None
    joined = " ".join(texts)[:max_chars]
    # 语言直接从文件元数据拿(不问模型): 该频道字幕文件里出现次数最多的 lang
    lang = max(set(langs), key=langs.count) if langs else None
    return joined, lang


def load_channel_comments(channel_id, sample_n, text_chars):
    """扫 data/comments/*/<channel_id>.jsonl 各日期目录，取样评论文本(截断)。没有则返回 None。"""
    if not os.path.isdir(COMMENTS):
        return None
    texts = []
    for day in sorted(os.listdir(COMMENTS)):
        day_dir = os.path.join(COMMENTS, day)
        if not os.path.isdir(day_dir):
            continue
        path = os.path.join(day_dir, f"{channel_id}.jsonl")
        if not os.path.exists(path):
            continue
        for line in open(path):
            try:
                r = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            t = r.get("text")
            if t:
                texts.append(t[:text_chars])
            if len(texts) >= sample_n:
                break
        if len(texts) >= sample_n:
            break
    return texts or None


def build_user_prompt(channel_name, description, titles, transcript_text, comment_texts, tcfg):
    parts = [SCHEMA_HINT, "", f"频道名: {channel_name}"]
    if description:
        parts.append(f"频道简介: {description[:tcfg['description_chars']]}")
    if titles:
        parts.append("近期视频标题: " + " ; ".join(titles)[:tcfg["titles_chars"]])
    parts.append(f"字幕语料(多条视频拼接): {transcript_text}")
    if comment_texts:
        parts.append("观众评论样本: " + " | ".join(comment_texts))
    else:
        parts.append("观众评论样本: (无)")
    return "\n\n".join(parts)


# ---------- ollama 调用 ----------

def call_ollama(user_prompt, tcfg):
    body = json.dumps({
        "model": tcfg["model"],
        "messages": [
            {"role": "system", "content": SYS_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False, "format": "json",
        "options": {"temperature": tcfg["temperature"]},
    }).encode()
    req = urllib.request.Request(tcfg["endpoint"], data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=tcfg["timeout_seconds"]) as r:
        content = json.loads(r.read())["message"]["content"]
    return json.loads(content)


def _norm_tag_array(v):
    """模型偶尔把数组字段吐成单个字符串，容错归一化成 list[str]。"""
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v if x]
    return [str(v)]


def validate_and_normalize(raw_tag, channel_id):
    """校验模型输出的关键字段，容错归一化(数组/枚举越界)，不合规的枚举值落 other。"""
    vertical = raw_tag.get("vertical")
    if vertical not in VERTICALS:
        vertical = "other"
    pov = raw_tag.get("pov_style")
    if pov not in ("pov 为主", "第三人称为主", "混合"):
        pov = "混合"
    bt = raw_tag.get("brand_traces") or {}
    if not isinstance(bt, dict):
        bt = {}
    brand_traces = {
        "insta360": bool(bt.get("insta360", False)),
        "competitors": _norm_tag_array(bt.get("competitors"))[:10],
        "quotes": _norm_tag_array(bt.get("quotes"))[:2],
    }
    audience_notes = raw_tag.get("audience_notes")
    if audience_notes in ("null", "None", ""):
        audience_notes = None
    return {
        "content_themes": _norm_tag_array(raw_tag.get("content_themes"))[:6],
        "vertical": vertical,
        "content_forms": _norm_tag_array(raw_tag.get("content_forms"))[:6],
        "pov_style": pov,
        "brand_traces": brand_traces,
        "audience_notes": audience_notes,
    }


# ---------- 打标主流程 ----------

def tag_channel(channel_id, pool_by_id, tcfg):
    """单频道打标。返回 tag dict 或 None(无字幕/失败)。异常向上抛给调用者按频道 catch。"""
    transcript_text, lang = load_channel_transcripts(channel_id, tcfg["transcript_chars"])
    if not transcript_text:
        return None

    row = pool_by_id.get(channel_id, {})
    channel_name = row.get("channel_name") or channel_id
    description = row.get("description") or ""
    titles = row.get("recent_video_titles") or []
    comment_texts = load_channel_comments(channel_id, tcfg["comments_sample"], tcfg["comment_text_chars"])

    user_prompt = build_user_prompt(channel_name, description, titles, transcript_text, comment_texts, tcfg)
    raw = call_ollama(user_prompt, tcfg)
    tag = validate_and_normalize(raw, channel_id)

    tag["channel_id"] = channel_id
    tag["channel_name"] = channel_name
    tag["language"] = lang  # 直接来自字幕文件元数据，不问模型
    tag["tagged_at"] = datetime.now().isoformat(timespec="seconds")
    tag["model"] = tcfg["model"]
    return tag


def strip_for_scoring(tag, leak_re):
    """产打分视图: 删掉 brand_traces 整段；content_themes/content_forms 里混进泄漏词的整条剔除。"""
    out = {k: v for k, v in tag.items() if k != "brand_traces"}
    out["content_themes"] = [t for t in tag["content_themes"] if not leak_re.search(t)]
    out["content_forms"] = [t for t in tag["content_forms"] if not leak_re.search(t)]
    if out.get("audience_notes") and leak_re.search(out["audience_notes"]):
        out["audience_notes"] = None
    return out


# ---------- jsonl 幂等落盘(按 channel_id 覆盖) ----------

def _load_existing_by_id(path):
    rows = {}
    if not os.path.exists(path):
        return rows
    for line in open(path):
        try:
            r = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        cid = r.get("channel_id")
        if cid:
            rows[cid] = r
    return rows


def upsert_jsonl(path, new_rows_by_id):
    """按 channel_id 覆盖已有行(幂等可重跑)，其余行保留原样，原子写。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    existing = _load_existing_by_id(path)
    existing.update(new_rows_by_id)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        for cid in existing:
            f.write(json.dumps(existing[cid], ensure_ascii=False) + "\n")
    os.replace(tmp, path)


# ---------- 主入口 ----------

def run(limit=None):
    tcfg = dict(DEFAULTS)
    leak_re = load_leak_pattern()
    pool_by_id = load_pool_by_channel_id()
    channel_ids = list_transcript_channels()
    if limit is not None:
        channel_ids = channel_ids[:limit]

    raw_new, scoring_new = {}, {}
    ok, skipped_no_transcript, failed = 0, 0, 0
    fail_log = []

    for i, cid in enumerate(channel_ids, 1):
        name_hint = pool_by_id.get(cid, {}).get("channel_name", cid)
        t0 = time.time()
        try:
            tag = tag_channel(cid, pool_by_id, tcfg)
            if tag is None:
                skipped_no_transcript += 1
                print(f"[{i}/{len(channel_ids)}] SKIP (no transcript): {name_hint}", file=sys.stderr)
                continue
            raw_new[cid] = tag
            scoring_new[cid] = strip_for_scoring(tag, leak_re)
            ok += 1
            dt = round(time.time() - t0, 1)
            print(f"[{i}/{len(channel_ids)}] ok ({dt}s): {name_hint} -> "
                  f"vertical={tag['vertical']} themes={tag['content_themes']}", file=sys.stderr)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, KeyError, ValueError) as e:
            failed += 1
            fail_log.append({"channel_id": cid, "channel_name": name_hint, "error": str(e)})
            print(f"[{i}/{len(channel_ids)}] FAIL: {name_hint}: {e}", file=sys.stderr)
        except Exception as e:  # 任何未预见异常都不许炸整批
            failed += 1
            fail_log.append({"channel_id": cid, "channel_name": name_hint, "error": f"unexpected: {e}"})
            print(f"[{i}/{len(channel_ids)}] FAIL (unexpected): {name_hint}: {e}", file=sys.stderr)

    raw_path = os.path.join(TAGS_DIR, "tags_raw.jsonl")
    scoring_path = os.path.join(TAGS_DIR, "tags_for_scoring.jsonl")
    upsert_jsonl(raw_path, raw_new)
    upsert_jsonl(scoring_path, scoring_new)

    if fail_log:
        with open(os.path.join(TAGS_DIR, "tag_failures.json"), "w") as f:
            json.dump(fail_log, f, ensure_ascii=False, indent=1)

    stats = {
        "channels_considered": len(channel_ids),
        "tagged_ok": ok,
        "skipped_no_transcript": skipped_no_transcript,
        "failed": failed,
        "raw_path": raw_path,
        "scoring_path": scoring_path,
    }
    return stats


def main():
    ap = argparse.ArgumentParser(description="打标层 v1: 频道字幕+简介+标题 -> 结构化内容标签(双视图落盘)")
    ap.add_argument("--limit", type=int, help="只跑前 N 个频道(冒烟用)")
    args = ap.parse_args()
    stats = run(limit=args.limit)
    print(f"打标完成: 考虑 {stats['channels_considered']} 频道，成功 {stats['tagged_ok']}，"
          f"无字幕跳过 {stats['skipped_no_transcript']}，失败 {stats['failed']}", file=sys.stderr)
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
