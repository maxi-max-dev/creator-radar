#!/usr/bin/env python3
"""飞书文档层: 把日报 / 达人档案 / 关键报告的 markdown 传上飞书云空间，
让飞书成为团队总部(所有人看的产物自动出现在飞书里)。

为什么是「上传 md 文件」而不是「建 docx 原生文档」:
  本 bot 当前授予的权限里没有 docx:document / docx:document:create,
  也没有 ccm_import_open 媒体上传权限，原生 docx 建档与 markdown 导入两条路都被 403 挡死
  (deprecated 的老 doc/v2 也已全局停用)。零开发的 Max 无法轻易在管理后台补授权，
  故走「不需要任何新权限」的稳妥路: 用 drive 文件夹组织 + files/upload_all 把 .md 传成云文件,
  飞书点开有 markdown 预览。Max 作为每个文件夹的 full_access 协作者能看到全部产物分门别类。
  升级到原生 docx 只需补授权后替换 _put_doc 一个函数(其余编排/幂等/降级不变)。

设计原则(照抄现有 run_radar 的飞书调用风格):
  - 标准库 urllib，复用 run_radar._feishu_call 的调用形态(失败返回 {code:-1} 不抛)。
  - 一切失败温和降级: 只返回错误串 / 记 log，绝不中断主链。
  - 敏感标识符(app 凭证/folder_token/file_token 映射)全在 repo 外(~/.config/creator-radar/)。
  - 对外文字(文件名/文件夹名)不用破折号。
"""
import json, os, sys, time, urllib.request, urllib.error, uuid
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

OPEN_BASE = "https://open.feishu.cn/open-apis"
# Max 在本 app 下的 open_id 不写死在 repo(红线: 一切标识符不进仓库)，从凭证文件的 max_open_id 字段读。
# 所有新建云资源都把他加成 full_access(他浏览器常挂游客号，不能靠链接分享)。
# 顶层文件夹与三个子文件夹(对外文字，不用破折号)
TOP_FOLDER = "达人雷达"
SUBFOLDERS = ["日报", "达人档案", "报告"]
# 幂等映射与文件夹缓存(repo 外，跟凭证同目录)
DEFAULT_MAP_PATH = "~/.config/creator-radar/feishu_docs_map.json"


# ----- 底层调用(与 run_radar._feishu_call 同形) -----

def _feishu_call(method, path, token=None, body=None):
    """飞书开放平台最小调用。失败返回 {code:-1,...} 不抛(照抄 run_radar 风格)。"""
    url = OPEN_BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"code": -1, "_http": e.code, "msg": e.read().decode(errors="replace")[:300]}
    except Exception as e:
        return {"code": -1, "msg": str(e)}


def _upload_file(token, folder_token, file_name, data_bytes):
    """multipart 上传一个文件到指定文件夹(drive/v1/files/upload_all, parent_type=explorer)。
    返回 {code, data:{file_token,url}} 形态。失败返回 {code:-1}。"""
    boundary = "----cr" + uuid.uuid4().hex
    parts = []
    def field(k, v):
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                      % (boundary, k, v)).encode())
    field("file_name", file_name)
    field("parent_type", "explorer")
    field("parent_node", folder_token)
    field("size", str(len(data_bytes)))
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
                  "Content-Type: application/octet-stream\r\n\r\n" % (boundary, file_name)).encode())
    parts.append(data_bytes)
    parts.append(("\r\n--%s--\r\n" % boundary).encode())
    payload = b"".join(parts)
    req = urllib.request.Request(OPEN_BASE + "/drive/v1/files/upload_all", data=payload, method="POST")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"code": -1, "_http": e.code, "msg": e.read().decode(errors="replace")[:300]}
    except Exception as e:
        return {"code": -1, "msg": str(e)}


def _get_token(cred):
    t = _feishu_call("POST", "/auth/v3/tenant_access_token/internal",
                     body={"app_id": cred["app_id"], "app_secret": cred["app_secret"]})
    return t.get("tenant_access_token"), t


def _add_collaborator(token, obj_token, obj_type, open_id):
    """把 Max 加成 full_access 协作者(幂等: 已是协作者飞书返回成功或已存在，都当成功)。"""
    if not open_id:
        return False
    r = _feishu_call("POST",
                     "/drive/v1/permissions/%s/members?type=%s&need_notification=false" % (obj_token, obj_type),
                     token=token,
                     body={"member_type": "openid", "member_id": open_id, "perm": "full_access"})
    # code 0 = 新增成功; 1064230/已存在类也算 ok(不同租户回码不一，宽松判定)
    return r.get("code") == 0 or "exist" in str(r.get("msg", "")).lower()


# ----- 文件夹树(幂等) -----

def _list_children_folders(token, parent_token):
    """列 parent 下的子文件夹，返回 {name: token}。只取 type==folder。"""
    out = {}
    page = ""
    for _ in range(10):  # 最多翻 10 页足够
        path = "/drive/v1/files?page_size=50&folder_token=" + parent_token
        if page:
            path += "&page_token=" + page
        r = _feishu_call("GET", path, token=token)
        if r.get("code") != 0:
            break
        for f in r.get("data", {}).get("files", []):
            if f.get("type") == "folder":
                out[f.get("name")] = f.get("token")
        if not r.get("data", {}).get("has_more"):
            break
        page = r.get("data", {}).get("page_token", "")
        if not page:
            break
    return out


def _ensure_folder(token, parent_token, name):
    """在 parent 下确保有名为 name 的文件夹(有就复用，没有就建)。返回 folder_token 或 None。"""
    existing = _list_children_folders(token, parent_token)
    if name in existing:
        return existing[name]
    r = _feishu_call("POST", "/drive/v1/files/create_folder", token=token,
                     body={"name": name, "folder_token": parent_token})
    if r.get("code") == 0:
        return r.get("data", {}).get("token")
    return None


def ensure_folder_tree(token, open_id=""):
    """确保 达人雷达/{日报,达人档案,报告} 全在(幂等)，并把 Max 加成每个文件夹的 full_access。
    返回 {"达人雷达": tok, "日报": tok, "达人档案": tok, "报告": tok} 或缺失键。"""
    result = {}
    # 顶层 folder 的父 = bot 云空间根
    root_meta = _feishu_call("GET", "/drive/explorer/v2/root_folder/meta", token=token)
    root_token = root_meta.get("data", {}).get("token")
    if not root_token:
        return {"_error": "no root folder: %s" % str(root_meta.get("msg"))[:80]}
    top = _ensure_folder(token, root_token, TOP_FOLDER)
    if not top:
        return {"_error": "create top folder failed"}
    result[TOP_FOLDER] = top
    _add_collaborator(token, top, "folder", open_id)
    for name in SUBFOLDERS:
        sub = _ensure_folder(token, top, name)
        if sub:
            result[name] = sub
            _add_collaborator(token, sub, "folder", open_id)
    return result


# ----- 幂等映射(本地 json，repo 外) -----

def _load_map(map_path):
    p = os.path.expanduser(map_path)
    if os.path.exists(p):
        try:
            return json.load(open(p))
        except Exception:
            return {}
    return {}


def _save_map(map_path, m):
    p = os.path.expanduser(map_path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(m, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)  # 原子写


def _put_doc(token, folder_token, file_name, md_text, key, doc_map, open_id=""):
    """把一份 markdown 以「文件」形式放进 folder(幂等替换)。
    幂等策略: doc_map[key] 存在就先删旧 file_token 再传新的(净效果=覆盖更新内容)，
    key 不存在就新建。回写 doc_map[key]={file_token,url,folder}。
    返回 (ok:bool, info:str)。
    升级到原生 docx 只需改本函数内部(改成 docx 建档/更新 blocks)，编排层不动。"""
    data_bytes = md_text.encode("utf-8")
    prev = doc_map.get(key)
    if prev and prev.get("file_token"):
        # 删旧(失败不致命，最坏留一份旧的，下面照常传新)
        _feishu_call("DELETE", "/drive/v1/files/%s?type=file" % prev["file_token"], token=token)
    r = _upload_file(token, folder_token, file_name, data_bytes)
    if r.get("code") != 0:
        return False, "upload failed: code=%s %s" % (r.get("code"), str(r.get("msg"))[:120])
    ftok = r.get("data", {}).get("file_token")
    url = r.get("data", {}).get("url", "")
    _add_collaborator(token, ftok, "file", open_id)
    doc_map[key] = {"file_token": ftok, "url": url, "folder": folder_token, "name": file_name,
                    "updated": date.today().isoformat()}
    return True, url


# ----- 三个出口: 日报 / 档案 / 报告 -----

def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def push_daily_report(cfg, today=None, report_path=None):
    """把当日日报 markdown 传上飞书(日报/ 文件夹)。文件名「达人雷达日报 YYYY-MM-DD」。
    幂等 key = daily/<date>(重跑同一天覆盖)。温和降级: 出错只返回错误串。"""
    return _run_uploads(cfg, kind="daily", today=today, explicit_paths=[report_path] if report_path else None)


def push_dossiers(cfg):
    """把 data/dossiers/*.md 各传一篇进「达人档案/」。文件名=频道名(读 frontmatter)。
    幂等 key = dossier/<channel_id>(已存在覆盖更新)。返回结果 dict。"""
    return _run_uploads(cfg, kind="dossiers")


def push_reports(cfg, report_files):
    """把给定的一批 vault 报告 md 各传一篇进「报告/」。文件名=报告标题(去 .md)。
    幂等 key = report/<basename>。report_files: [(abs_path, display_name or None), ...] 或 [abs_path,...]。"""
    return _run_uploads(cfg, kind="reports", explicit_paths=report_files)


# ----- 根部两篇: 主页仪表盘 + 系统说明 -----

def _open_cred(cfg):
    """加载凭证 + token + open_id + 文件夹树 + 映射，四件套一次备齐。失败返回 (None, err_dict)。"""
    node = cfg.get("feishu_docs", {})
    cred_path = os.path.expanduser(node.get("credentials_path", ""))
    if not cred_path or not os.path.exists(cred_path):
        return None, {"ok": False, "error": "NotConfigured: 凭证文件不存在 %s" % (cred_path or "(未配置)")}
    cred = json.load(open(cred_path))
    token, traw = _get_token(cred)
    if not token:
        return None, {"ok": False, "error": "token failed: code=%s" % traw.get("code")}
    oid = cred.get("max_open_id", "")
    folders = ensure_folder_tree(token, oid)
    if folders.get("_error"):
        return None, {"ok": False, "error": "folder tree: %s" % folders["_error"]}
    ctx = {"cred": cred, "token": token, "oid": oid, "folders": folders,
           "map_path": node.get("map_path", DEFAULT_MAP_PATH),
           "doc_map": _load_map(node.get("map_path", DEFAULT_MAP_PATH))}
    return ctx, None


def _folder_url(tok):
    return "https://my.feishu.cn/drive/folder/%s" % tok if tok else ""


def build_homepage_md(cfg, stats, ctx):
    """主页仪表盘 markdown(团队唯一入口)。顶部 callout 放四个大数字，分区导航配说明，底部更新机制+时间。
    stats: {pool_size, cards_today, blind_test_multiple, scoreboard_status, daily_report_url}。"""
    f = ctx["folders"]
    doc_map = ctx["doc_map"]
    cred = ctx["cred"]
    bitable_url = cred.get("base_url") or ("https://my.feishu.cn/base/%s" % cred["app_token"])
    daily_url = stats.get("daily_report_url") or doc_map.get("daily/%s" % date.today().isoformat(), {}).get("url", "")
    now = datetime.now().isoformat(timespec="minutes")
    L = ["# 达人雷达 · 总部", ""]
    # 顶部 callout 大数字块(blockquote 渲染成高亮卡)
    L += ["> 🛰️ **达人雷达 · 团队总部**  ",
          "> 给品牌方的达人主动发现引擎。所有人看的产物都在这里，按类分好，每天自动更新。",
          "", "---", ""]
    L += ["## 核心数字", "",
          "| 指标 | 当前 |",
          "|---|---|",
          f"| 🗂️ 候选池规模 | **{stats.get('pool_size', '?')}** 频道 |",
          f"| ⭐ 今日推荐 | **{stats.get('cards_today', '?')}** 位达人 |",
          f"| 🎯 盲测密度 | **{stats.get('blind_test_multiple', '4.6')} 倍**于随机基线(前 5%) |",
          f"| 📊 记分板 | {stats.get('scoreboard_status', '首份 picks 已存档，2026-08-04 结算')} |",
          "", "---", ""]
    # 分区导航
    L += ["## 分区导航", ""]
    L += ["### 📊 多维表格（运营主战场）",
          f"{bitable_url}",
          "_每日推荐自动灌进这张表，运营在这里做终审、流转评审状态、跟踪投放。_", ""]
    L += ["### 📰 今日日报",
          f"{daily_url or '(今日尚未生成)'}",
          "_每天 08:30 的池子动态、推荐卡、记分板结算，一页看完。历史日报见「日报」文件夹。_", ""]
    L += ["### 🗃️ 达人档案区",
          f"{_folder_url(f.get('达人档案'))}",
          "_每位当日推荐达人一页：基本盘、订阅快照史、近期视频、评论区概况、跨平台矩阵、推荐理由。_", ""]
    L += ["### 📁 报告区",
          f"{_folder_url(f.get('报告'))}",
          "_方法论与验证：行业方法对比、盲测验证报告、删除实验裁判。给评委和队友看的深度材料。_", ""]
    L += ["### 📈 全池榜单",
          "（扩容合并后上线）",
          "_全池 1000+ 频道的完整排序，将以多维表格第二张表形式上线。_", "", "---", ""]
    L += ["## 数据更新机制", "",
          "达人雷达每天 **08:30** 自动运行：刷新池子 → 全池重排 → 生成推荐卡 → 更新本页大数字与今日日报链接 → 同步档案。",
          "重复文档自动覆盖更新，不堆重复。",
          "",
          f"_最后更新：{now}_"]
    return "\n".join(L) + "\n"


def push_homepage(cfg, stats=None):
    """建/刷新主页仪表盘「达人雷达 · 总部」，放在顶层文件夹根部。幂等 key=home/index(每天覆盖刷新)。
    stats 不给时自动从今日运行产物推算(池子/推荐数)。温和降级: 出错只返回错误串 dict。"""
    ctx, err = _open_cred(cfg)
    if err:
        return err
    stats = dict(stats or {})
    stats.setdefault("pool_size", _guess_pool_size())
    stats.setdefault("cards_today", _guess_cards_today())
    stats.setdefault("blind_test_multiple", "4.6")
    stats.setdefault("scoreboard_status", "首份 picks 已存档，2026-08-04 结算")
    md = build_homepage_md(cfg, stats, ctx)
    ok, info = _put_doc(ctx["token"], ctx["folders"].get(TOP_FOLDER), "达人雷达 · 总部.md", md,
                        "home/index", ctx["doc_map"], open_id=ctx["oid"])
    _save_map(ctx["map_path"], ctx["doc_map"])
    return {"ok": ok, "kind": "homepage", "url_or_err": info,
            "folder": _folder_url(ctx["folders"].get(TOP_FOLDER))}


def push_system_doc(cfg):
    """建/刷新「系统说明」(README + architecture 核心合并转写，给队友看)，放顶层文件夹根部。
    幂等 key=system/overview。温和降级: 出错只返回错误串 dict。"""
    ctx, err = _open_cred(cfg)
    if err:
        return err
    md = build_system_md()
    ok, info = _put_doc(ctx["token"], ctx["folders"].get(TOP_FOLDER), "达人雷达 · 系统说明.md", md,
                        "system/overview", ctx["doc_map"], open_id=ctx["oid"])
    _save_map(ctx["map_path"], ctx["doc_map"])
    return {"ok": ok, "kind": "system_doc", "url_or_err": info}


def _guess_pool_size():
    """从池子文件数行推算规模(主页大数字兜底)。"""
    try:
        p = os.path.join(ROOT, "data", "pool", "creator_pool.jsonl")
        with open(p, encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except Exception:
        return "?"


def _guess_cards_today():
    """从今日 daily run 的 cards.json 推算推荐数(主页大数字兜底)。"""
    try:
        p = os.path.join(ROOT, "data", "runs", "daily", date.today().isoformat(), "cards.json")
        return len(json.load(open(p)))
    except Exception:
        return "?"


def build_system_md():
    """系统说明: README + architecture 核心内容合并转写成一篇给队友的文档(是什么/怎么跑/三层雷达/技术栈)。
    统一设计: callout 摘要 + 表格 + divider。"""
    L = ["# 达人雷达 · 系统说明", ""]
    L += ["> 📖 **一句话**  ",
          "> 给品牌方的达人主动发现引擎：导入候选池，零样本排序，可解释推荐，人工终审。",
          "> 引擎本体品牌无关，换品牌只需换一份 JSON 配置。影石 Insta360 是第一个品牌配置。",
          "", "---", ""]
    L += ["## 一、它解决什么", "",
          "品牌达人营销漏斗里最贵的一环是「发现」：在几百万创作者里人肉找人、靠感觉判断像不像。",
          "现有工具解决的是「查一个人的数据」，没解决「主动找到下一个还没红的对的人」。",
          "影石的公开选人史正是后者：签大牌之前先签一批几万粉的 POV 骑行博主，「早期下注」是明说的哲学。",
          "达人雷达把这种选人直觉变成**可配置、可验证、可解释**的排序系统。", "", "---", ""]
    L += ["## 二、怎么跑", "",
          "一条命令跑全链（本地零 API 成本，embedding 与推荐卡都走本机 ollama）：", "",
          "```",
          "python3 src/run_radar.py --budget 40 --discover-terms 2 --top-n 5",
          "```", "",
          "流程：采集(yt-dlp 刷新池内频道 + 多语言搜索词发现新面孔) → 全池重排 → 与上次排名 diff →",
          "本地 LLM 精读候选出推荐卡 → 日报 → 推送(iMessage + 飞书多维表格 + 飞书文档) → 数据快照入库。", "",
          "定时器 launchd 每天 08:30 自动跑。策略全在 `config/<brand>.json`，换品牌换配置不换代码。",
          "", "---", ""]
    L += ["## 三、三层雷达（分层架构）", "",
          "| 层 | 做什么 | 现状 |",
          "|---|---|---|",
          "| 配置层 | 品牌策略全外置(主题查询/权重/甜点参数/泄漏词表) | 已实装 |",
          "| 采集层 | YouTube(yt-dlp) + B站(guest cookie)，IG/TikTok 规划 | YouTube/B站已实装 |",
          "| 特征层 | 文本 + 规模 + 平台标记(POV/onboard/helmet cam) | 已实装 |",
          "| 打分层 | bge-m3 语义匹配 + 甜点函数 + 标记，三路加权 | 已实装 |",
          "| 验证层 | 盲测回测(泄漏防护 + recall@top k%) | 已实装 |",
          "| 解释层 | 本地 LLM 对前 100 名精读出可解释推荐卡 | 已实装 |",
          "| 工作流层 | 飞书多维表格 + 飞书文档(本总部) + 飞书 AI 槽位 | 已实装 |",
          "| 回流 | 投放结果回写 → 下一轮配置调整 | 记分板 v0 已实装 |",
          "", "---", ""]
    L += ["## 四、打分公式（现役 v1.2）", "",
          "```",
          "score = 0.70 × 语义 + 0.18 × 粉丝甜点 + 0.12 × 平台标记",
          "```", "",
          "- **语义**：6 个主题查询(措辞来自影石官方文章逆向)，每主题写摩托/骑行两个垂类变体取最大值，多语种 embedding 天然吃日语西语频道。",
          "- **粉丝甜点**：订阅数对数钟形函数，峰值约 8 万粉，编码「早期下注」。峰值与宽度是运营可调的策略旋钮。",
          "- **平台标记**：正则命中 POV / onboard / helmet cam 等原生拍摄信号。",
          "", "---", ""]
    L += ["## 五、证明过什么", "",
          "在 1,085 个全球摩托/骑行 YouTube 频道的真实池子上，打分前剔除全部品牌 token(看不到任何合作痕迹)，",
          "引擎把影石历史真实合作过的达人以 **4.6 倍于随机基线**的密度排进前 5%。全程零样本，未用任何正例标签调参。", "",
          "| 指标 | 全部正例(26) | 中腰部子集(20) | 随机基线 |",
          "|---|---|---|---|",
          "| 前 5% 召回 | 23% | 20% | 5% |",
          "| 前 10% 召回 | 27% | 25% | 10% |",
          "| 正例中位百分位 | 32.4% | 28.5% | 50% |",
          "", "---", ""]
    L += ["## 六、技术栈", "",
          "- **语言/依赖**：Python 标准库 + 本地 ollama，无第三方 pip 依赖。",
          "- **embedding**：bge-m3（本地，语义匹配）。",
          "- **推荐卡 LLM**：qwen3:8b（本地，可切飞书 AI 云端升级位，只改 config 不改代码）。",
          "- **采集**：yt-dlp（YouTube）、guest-cookie 四端点（B站）。",
          "- **工作流**：飞书多维表格（候选/推荐/评审流转）+ 飞书文档总部（本工作台）+ iMessage 推送。",
          "- **调度**：launchd 每天 08:30。数据快照每日自动 git commit 异地备份。",
          "",
          "_本文由 README 与设计书核心内容合并转写，给队友快速上手。完整设计书见 repo docs/architecture.md。_"]
    return "\n".join(L) + "\n"


def _dossier_title(md_text, fallback):
    """从 dossier frontmatter 取 channel_name 当文件名; 取不到用 fallback。"""
    for line in md_text.splitlines():
        s = line.strip()
        if s.startswith("channel_name:"):
            v = s.split(":", 1)[1].strip().strip('"').strip("'")
            if v:
                return v
    return fallback


def _report_title(md_text, fallback):
    """从报告首个 # 标题取文件名; 取不到用 fallback(文件名去 .md)。"""
    for line in md_text.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return fallback


def _run_uploads(cfg, kind, today=None, explicit_paths=None):
    """统一编排: 建/复用文件夹树 -> 逐份 _put_doc(幂等) -> 回写映射。
    kind ∈ {daily, dossiers, reports}. 返回结果 dict(可 JSON 序列化，进 run 汇总/log)。"""
    node = cfg.get("feishu_docs", {})
    cred_path = os.path.expanduser(node.get("credentials_path", ""))
    if not cred_path or not os.path.exists(cred_path):
        return {"ok": False, "error": "NotConfigured: 凭证文件不存在 %s" % (cred_path or "(未配置)")}
    map_path = node.get("map_path", DEFAULT_MAP_PATH)
    try:
        cred = json.load(open(cred_path))
    except Exception as e:
        return {"ok": False, "error": "cred load: %s" % e}
    token, traw = _get_token(cred)
    if not token:
        return {"ok": False, "error": "token failed: code=%s %s" % (traw.get("code"), str(traw.get("msg"))[:80])}
    oid = cred.get("max_open_id", "")  # Max 的 open_id 从凭证文件读，不写死在 repo

    folders = ensure_folder_tree(token, oid)
    if folders.get("_error"):
        return {"ok": False, "error": "folder tree: %s" % folders["_error"], "folders": folders}

    doc_map = _load_map(map_path)
    uploaded, failed = [], []

    if kind == "daily":
        today = today or date.today().isoformat()
        path = (explicit_paths or [None])[0] or os.path.join(ROOT, "reports", "%s-radar.md" % today)
        target = folders.get("日报")
        if not os.path.exists(path):
            return {"ok": False, "error": "日报文件不存在 %s" % path}
        md = _read(path)
        fname = "达人雷达日报 %s.md" % today
        ok, info = _put_doc(token, target, fname, md, "daily/%s" % today, doc_map, open_id=oid)
        (uploaded if ok else failed).append({"name": fname, "key": "daily/%s" % today, "url_or_err": info})

    elif kind == "dossiers":
        target = folders.get("达人档案")
        ddir = os.path.join(ROOT, "data", "dossiers")
        files = sorted(f for f in os.listdir(ddir) if f.endswith(".md")) if os.path.isdir(ddir) else []
        for f in files:
            cid = f[:-3]
            md = _read(os.path.join(ddir, f))
            title = _dossier_title(md, cid)
            fname = "%s.md" % _safe(title)
            ok, info = _put_doc(token, target, fname, md, "dossier/%s" % cid, doc_map, open_id=oid)
            (uploaded if ok else failed).append({"name": fname, "key": "dossier/%s" % cid, "url_or_err": info})

    elif kind == "reports":
        target = folders.get("报告")
        for item in (explicit_paths or []):
            path, disp = (item if isinstance(item, (list, tuple)) else (item, None))
            if not os.path.exists(path):
                failed.append({"name": os.path.basename(path), "key": "report/%s" % os.path.basename(path),
                               "url_or_err": "文件不存在(跳过)"})
                continue
            md = _read(path)
            base = os.path.splitext(os.path.basename(path))[0]
            title = disp or _report_title(md, base)
            fname = "%s.md" % _safe(title)
            ok, info = _put_doc(token, target, fname, md, "report/%s" % base, doc_map, open_id=oid)
            (uploaded if ok else failed).append({"name": fname, "key": "report/%s" % base, "url_or_err": info})

    _save_map(map_path, doc_map)
    return {"ok": len(failed) == 0, "kind": kind, "folders": {k: v for k, v in folders.items() if not k.startswith("_")},
            "uploaded": uploaded, "failed": failed,
            "counts": {"ok": len(uploaded), "failed": len(failed)}}


def _safe(name):
    """文件名清洗: 去掉飞书/文件系统忌讳字符，避免上传失败。不用破折号替换(保留原字符或用空格)。"""
    bad = '/\\:*?"<>|\n\r\t'
    out = "".join(c if c not in bad else " " for c in name).strip()
    return (out or "未命名")[:180]


if __name__ == "__main__":
    # 手动跑: python3 src/feishu_docs.py [daily|dossiers|reports <path>...|homepage|system]
    from radar_lib import load_config
    cfg = load_config(os.path.join(ROOT, "config", "insta360.json"))
    which = sys.argv[1] if len(sys.argv) > 1 else "daily"
    if which == "daily":
        print(json.dumps(push_daily_report(cfg), ensure_ascii=False, indent=1))
    elif which == "dossiers":
        print(json.dumps(push_dossiers(cfg), ensure_ascii=False, indent=1))
    elif which == "reports":
        print(json.dumps(push_reports(cfg, sys.argv[2:]), ensure_ascii=False, indent=1))
    elif which == "homepage":
        print(json.dumps(push_homepage(cfg), ensure_ascii=False, indent=1))
    elif which == "system":
        print(json.dumps(push_system_doc(cfg), ensure_ascii=False, indent=1))
