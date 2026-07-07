#!/usr/bin/env python3
"""飞书文档层: 把日报 / 达人档案 / 关键报告写成飞书原生 docx 文档，
让飞书成为团队总部(所有人看的产物自动出现在飞书里，且互相跳转)。

实现路线(2026-07-07 docx 权限补齐后升级为原生文档):
  - 每个 key 首次出现建档一次，拿稳定 document_id，链接从此永久。
  - 之后每天原地更新: 清旧块写新块，URL 不变，互链网络不再需要防轮换。
  - markdown -> blocks 手工构建(不依赖未验证的 ccm 导入接口，控制力也更强):
    标题 1-3 / 正文 / 无序有序列表 / 表格 / callout 高亮块 / divider / 代码块,
    转不了的降级为纯文本块，宁可样式糙不可内容丢。
  - 历史注: 权限补齐前曾走 files/upload_all 传 .md 云文件(URL 每次覆盖会轮换)，
    迁移后旧文件已清理，映射条目从 file_token 换成 document_id。

设计原则(照抄现有 run_radar 的飞书调用风格):
  - 标准库 urllib，复用 run_radar._feishu_call 的调用形态(失败返回 {code:-1} 不抛)。
  - 一切失败温和降级: 只返回错误串 / 记 log，绝不中断主链。
  - 敏感标识符(app 凭证/document_id/folder_token 映射)全在 repo 外(~/.config/creator-radar/)。
  - 对外文字(文档标题/文件夹名)不用破折号。
"""
import json, os, re, sys, time, urllib.request, urllib.error
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


# ----- markdown -> docx blocks 转换器 -----

_INLINE_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_INLINE_BOLD = re.compile(r"\*\*(.+?)\*\*")
_INLINE_CODE = re.compile(r"`([^`]+)`")
_TABLE_SEP = re.compile(r"^\|[\s:|-]+\|$")
_ORDERED = re.compile(r"^(\d+)[.、]\s+(.*)$")


def _plain_run(s, italic=False):
    r = {"text_run": {"content": s}}
    if italic:
        r["text_run"]["text_element_style"] = {"italic": True}
    return r


def _code_runs(seg, italic=False):
    """无链接无加粗的文本段按 `行内代码` 切 runs。"""
    runs, pos = [], 0
    for m in _INLINE_CODE.finditer(seg):
        if m.start() > pos:
            runs.append(_plain_run(seg[pos:m.start()], italic))
        st = {"inline_code": True}
        if italic:
            st["italic"] = True
        runs.append({"text_run": {"content": m.group(1), "text_element_style": st}})
        pos = m.end()
    if pos < len(seg):
        runs.append(_plain_run(seg[pos:], italic))
    return [r for r in runs if r["text_run"]["content"]]


def _styled_runs(seg, italic=False):
    """无链接文本段按 **加粗** 切 runs，剩余再过行内代码。"""
    runs, pos = [], 0
    for m in _INLINE_BOLD.finditer(seg):
        if m.start() > pos:
            runs += _code_runs(seg[pos:m.start()], italic)
        st = {"bold": True}
        if italic:
            st["italic"] = True
        runs.append({"text_run": {"content": m.group(1), "text_element_style": st}})
        pos = m.end()
    if pos < len(seg):
        runs += _code_runs(seg[pos:], italic)
    return runs


def _text_elements(s):
    """行内 markdown -> docx text elements: [链接](url) / **加粗** / `代码` / 整行 _斜体_。
    只把首尾成对的下划线当斜体(行内下划线如 file_name 保持原样)。样式糙点没关系，内容一个字不丢。"""
    t = s
    italic = False
    if len(t) > 2 and t.startswith("_") and t.endswith("_") and not t.startswith("__"):
        italic = True
        t = t[1:-1]
    elements, pos = [], 0
    for m in _INLINE_LINK.finditer(t):
        if m.start() > pos:
            elements += _styled_runs(t[pos:m.start()], italic)
        st = {"link": {"url": m.group(2)}}
        if italic:
            st["italic"] = True
        elements.append({"text_run": {"content": m.group(1), "text_element_style": st}})
        pos = m.end()
    if pos < len(t):
        elements += _styled_runs(t[pos:], italic)
    return elements or [{"text_run": {"content": " "}}]


def _split_row(line):
    """markdown 表格行 -> 单元格文本列表。"""
    return [c.strip() for c in line.strip().strip("|").split("|")]


# blockquote -> 高亮块配色语义: 看首行 emoji / 关键词自动上色(内容一个字不改，只决定块的颜色)。
# 设计取经 sspai/96170「用色即语义」。摘要/结论=蓝，风险/警告=橙，亮点/结论要点=绿，其余=蓝(中性提示)。
_WARN_HINT = ("⚠️", "风险", "警告", "注意", "坑", "失败")
_KEY_HINT = ("✅", "🎯", "值得", "结论", "亮点", "证明", "关键")
_SUMMARY_HINT = ("📡", "📊", "🛰️", "摘要", "今日", "先看", "一句话", "概览")


def _infer_callout_style(body_lines):
    """从 blockquote 正文推断配色语义(见 CALLOUT_STYLE 键)。首行权重最高。"""
    head = body_lines[0] if body_lines else ""
    joined = " ".join(body_lines)
    if any(h in head for h in _WARN_HINT) or any(h in head for h in ("⚠️",)):
        return "warn"
    if any(h in head for h in _SUMMARY_HINT):
        return "info"
    if any(h in joined for h in _KEY_HINT):
        return "key"
    return "info"


def _md_to_block_items(md_text, drop_title=None):
    """markdown -> 块序列 [(kind, payload)]。kind: flat(单块)/callout(引用行组)/table(行组)。
    认真转: 标题 1-3/正文/无序有序列表/表格/callout(blockquote)/divider/代码块; frontmatter 跳过
    (其内容在正文均有呈现); 首个 H1 与文档标题重复时丢弃。其余一律纯文本兜底，内容不丢。"""
    lines = md_text.splitlines()
    i = 0
    if lines and lines[0].strip() == "---":  # frontmatter
        j = 1
        while j < len(lines) and lines[j].strip() != "---":
            j += 1
        if j < len(lines):
            i = j + 1
    items, h1_dropped = [], False
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        if s.startswith("```"):
            j = i + 1
            buf = []
            while j < len(lines) and not lines[j].strip().startswith("```"):
                buf.append(lines[j])
                j += 1
            items.append(("flat", {"block_type": 14, "code": {
                "style": {"language": 1}, "elements": [{"text_run": {"content": "\n".join(buf) or " "}}]}}))
            i = j + 1
            continue
        if s == "---":
            items.append(("flat", {"block_type": 22, "divider": {}}))
            i += 1
            continue
        if s.startswith("# "):
            if not h1_dropped and drop_title:
                h1_dropped = True
                i += 1
                continue
            items.append(("flat", {"block_type": 3, "heading1": {"elements": _text_elements(s[2:])}}))
            i += 1
            continue
        if s.startswith("## "):
            items.append(("flat", {"block_type": 4, "heading2": {"elements": _text_elements(s[3:])}}))
            i += 1
            continue
        if s.startswith("### "):
            items.append(("flat", {"block_type": 5, "heading3": {"elements": _text_elements(s[4:])}}))
            i += 1
            continue
        if s.startswith(">"):
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip()[1:].strip())
                i += 1
            body = [b for b in buf if b] or [" "]
            items.append(("callout", (body, _infer_callout_style(body))))
            continue
        if s.startswith("|") and s.endswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1].strip()):
            rows = [_split_row(lines[i])]
            j = i + 2
            while j < len(lines) and lines[j].strip().startswith("|") and lines[j].strip().endswith("|"):
                rows.append(_split_row(lines[j]))
                j += 1
            items.append(("table", rows))
            i = j
            continue
        if s.startswith("- ") or s.startswith("* "):
            items.append(("flat", {"block_type": 12, "bullet": {"elements": _text_elements(s[2:])}}))
            i += 1
            continue
        m = _ORDERED.match(s)
        if m:
            items.append(("flat", {"block_type": 13, "ordered": {"elements": _text_elements(m.group(2))}}))
            i += 1
            continue
        items.append(("flat", {"block_type": 2, "text": {"elements": _text_elements(s)}}))
        i += 1
    return items


# ----- 卡片块构造(供 grid 列内使用: 大数字卡 / 导航卡) -----

def _t(text, bold=False, color=None, size=None):
    """构造一个 text block payload(可加粗/着色)。size 未用(飞书 docx 不支持任意字号，靠加粗/标题分级)。"""
    st = {}
    if bold:
        st["bold"] = True
    if color:
        st["text_color"] = color
    run = {"text_run": {"content": text}}
    if st:
        run["text_run"]["text_element_style"] = st
    return {"block_type": 2, "text": {"elements": [run]}}


def _big_number_card(number, label, emoji=""):
    """大数字卡(grid 列内): 一行超粗大数字 + 一行小标签。设计取经 sspai/96170「大数字+小标签」。
    数字用 H2 块放大(飞书唯一能显著放大字号的手段就是标题级)，标签用灰色小字。"""
    head = "%s %s" % (emoji, number) if emoji else str(number)
    return [
        {"block_type": 4, "heading2": {"elements": [{"text_run": {"content": head}}]}},
        _t(label, color=7),  # 灰色小标签
    ]


def _nav_card(emoji, title, desc, link_text, link_url):
    """导航卡(grid 列内): emoji+粗标题 / 一句话说明 / 一个直达链接。主页『往哪点』的原子单位。"""
    blocks = [
        {"block_type": 4, "heading2": {"elements": [{"text_run": {"content": "%s %s" % (emoji, title)}}]}},
        _t(desc, color=7),
    ]
    if link_url:
        blocks.append({"block_type": 2, "text": {"elements": [
            {"text_run": {"content": link_text, "text_element_style": {"link": {"url": link_url}, "bold": True}}}]}})
    else:
        blocks.append(_t(link_text, color=7))
    return blocks


# ----- docx 写入(建块/嵌套块/清空) -----

# 高亮块配色语义(飞书 docx callout 浅色档 1-7，经读回验证映射)。设计取经 sspai/96170:
# 用色即语义，别花哨。这里只用四种，覆盖「定位/提示/关键/警告」四类语义，其余场景一律走灰。
CALLOUT_STYLE = {
    "cover": {"bg": 6, "border": 6, "emoji": "satellite"},        # 蓝：封面/定位区
    "info": {"bg": 6, "border": 6, "emoji": "information_source"},  # 蓝：中性提示
    "key": {"bg": 5, "border": 5, "emoji": "dart"},               # 绿：关键结论/亮点
    "metric": {"bg": 4, "border": 4, "emoji": "bar_chart"},       # 黄：数据卡
    "warn": {"bg": 3, "border": 3, "emoji": "warning"},           # 橙：提醒/风险
    "quiet": {"bg": 1, "border": 1, "emoji": "memo"},             # 灰：旁注(不与上面抢眼)
}


def _docx_add_callout(token, doc_id, index, text_lines, style="key"):
    """callout 高亮块(嵌套块 API 一次带出子文本块)。style 决定配色语义(见 CALLOUT_STYLE)。
    text_lines: 纯文本行列表; 若某行以 __EMOJI:name__ 打头则该行不注 emoji_id(留给块级 emoji)。返回 True/False。"""
    st = CALLOUT_STYLE.get(style, CALLOUT_STYLE["key"])
    kids = ["co%d" % k for k in range(len(text_lines))]
    callout = {"background_color": st["bg"], "border_color": st["border"]}
    if st.get("emoji"):
        callout["emoji_id"] = st["emoji"]
    desc = [{"block_id": "co_root", "block_type": 19, "callout": callout, "children": kids}]
    for k, q in zip(kids, text_lines):
        desc.append({"block_id": k, "block_type": 2, "text": {"elements": _text_elements(q)}, "children": []})
    r = _feishu_call("POST", "/docx/v1/documents/%s/blocks/%s/descendant" % (doc_id, doc_id),
                     token=token, body={"children_id": ["co_root"], "index": index, "descendants": desc})
    return r.get("code") == 0


def _docx_add_grid(token, doc_id, index, columns):
    """分栏(Grid, block 24) + N 个分栏列(GridColumn, block 25)，每列装一小叠块。
    columns: [ [block_dict, ...], ... ] 每个子列表是一列内的块序列(已是最终 docx block payload，不含 block_id/children)。
    列数范围 [2,4](飞书 grid column_size 上限保守到 4，超了上层降级)。失败返回 False 让上层降级为平铺。
    设计取经 sspai/96170「分栏做导航卡/三栏并排」: 这是主页『往哪点』一眼可扫的关键。"""
    C = len(columns)
    if C < 2 or C > 4:
        return False
    ratio = max(1, round(99 / C))
    root_id = "grid"
    col_ids = ["gc%d" % i for i in range(C)]
    desc = [{"block_id": root_id, "block_type": 24, "grid": {"column_size": C}, "children": col_ids}]
    total = 1
    for i, (cid, blocks) in enumerate(zip(col_ids, columns)):
        kid_ids = []
        for j, blk in enumerate(blocks):
            kid = "g%d_%d" % (i, j)
            kid_ids.append(kid)
            desc.append(dict(blk, block_id=kid, children=[]))
            total += 1
        desc.append({"block_id": cid, "block_type": 25,
                     "grid_column": {"width_ratio": ratio}, "children": kid_ids})
        total += 1
    if total > 220:  # 嵌套块单次上限保护
        return False
    r = _feishu_call("POST", "/docx/v1/documents/%s/blocks/%s/descendant" % (doc_id, doc_id),
                     token=token, body={"children_id": [root_id], "index": index, "descendants": desc})
    return r.get("code") == 0


def _docx_add_table(token, doc_id, index, rows):
    """表格块(嵌套块 API 一次带出全部单元格与文本)。超大表返回 False 让上层降级为文本。"""
    R = len(rows)
    C = max(len(r) for r in rows)
    if R * C * 2 + 1 > 220:  # 嵌套块单次请求上限保护
        return False
    cells, desc = [], []
    for ri in range(R):
        for ci in range(C):
            cid, tid = "c%d_%d" % (ri, ci), "t%d_%d" % (ri, ci)
            cells.append(cid)
            content = rows[ri][ci] if ci < len(rows[ri]) else ""
            desc.append({"block_id": cid, "block_type": 32, "table_cell": {}, "children": [tid]})
            desc.append({"block_id": tid, "block_type": 2,
                         "text": {"elements": _text_elements(content)}, "children": []})
    root = {"block_id": "tbl", "block_type": 31,
            "table": {"property": {"row_size": R, "column_size": C, "header_row": True}},
            "children": cells}
    r = _feishu_call("POST", "/docx/v1/documents/%s/blocks/%s/descendant" % (doc_id, doc_id),
                     token=token, body={"children_id": ["tbl"], "index": index, "descendants": [root] + desc})
    return r.get("code") == 0


def _docx_write_blocks(token, doc_id, items):
    """把块序列写进文档(按序追加)。flat 连续段批量(<=40/call)，callout/table 走嵌套块 API。
    任一 callout/table 失败降级为纯文本块(内容不丢)。返回 (写入块数, 降级数, 首个错误或空串)。"""
    state = {"idx": 0, "written": 0, "err": ""}
    buf = []

    def flush():
        while buf:
            chunk = buf[:40]
            del buf[:40]
            r = _feishu_call("POST", "/docx/v1/documents/%s/blocks/%s/children" % (doc_id, doc_id),
                             token=token, body={"children": chunk, "index": state["idx"]})
            if r.get("code") != 0:
                state["err"] = state["err"] or "children code=%s %s" % (r.get("code"), str(r.get("msg"))[:80])
                return False
            state["idx"] += len(chunk)
            state["written"] += len(chunk)
            time.sleep(0.25)
        return True

    degraded = 0
    for kind, payload in items:
        if kind == "flat":
            buf.append(payload)
            continue
        if not flush():
            return state["written"], degraded, state["err"]
        if kind == "callout":
            # payload 兼容两形: 旧=纯行列表; 新=(行列表, style)
            if isinstance(payload, tuple):
                lines, style = payload
            else:
                lines, style = payload, "key"
            if _docx_add_callout(token, doc_id, state["idx"], lines, style=style):
                state["idx"] += 1
                state["written"] += 1
            else:
                degraded += 1
                for q in lines:
                    buf.append({"block_type": 2, "text": {"elements": _text_elements(q)}})
        elif kind == "grid":
            # payload = [ [block_dict,...], ... ] 每子列表一列。失败降级为逐列平铺(内容不丢)。
            if _docx_add_grid(token, doc_id, state["idx"], payload):
                state["idx"] += 1
                state["written"] += 1
            else:
                degraded += 1
                for col in payload:
                    for blk in col:
                        buf.append(blk)
        elif kind == "table":
            if _docx_add_table(token, doc_id, state["idx"], payload):
                state["idx"] += 1
                state["written"] += 1
            else:
                degraded += 1
                for row in payload:
                    buf.append({"block_type": 2, "text": {"elements": _text_elements(" | ".join(row))}})
        time.sleep(0.25)
    flush()
    return state["written"], degraded, state["err"]


def _docx_wipe(token, doc_id):
    """清空文档正文(根 children 分批删，删父块级联删子树)。空文档直接 True。"""
    for _ in range(80):
        r = _feishu_call("GET", "/docx/v1/documents/%s/blocks?page_size=500" % doc_id, token=token)
        if r.get("code") != 0:
            return False
        items = r.get("data", {}).get("items", [])
        page = next((b for b in items if b.get("block_type") == 1), None)
        n = len((page or {}).get("children") or [])
        if n == 0:
            return True
        d = _feishu_call("DELETE", "/docx/v1/documents/%s/blocks/%s/children/batch_delete" % (doc_id, doc_id),
                         token=token, body={"start_index": 0, "end_index": min(n, 40)})
        if d.get("code") != 0:
            return False
        time.sleep(0.25)
    return False


def _doc_url(doc_id):
    return "https://my.feishu.cn/docx/%s" % doc_id


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


def _put_doc(token, folder_token, file_name, md_text, key, doc_map, open_id="", items=None):
    """把一份内容写成飞书原生 docx 文档(幂等原地更新)。
    key 首次出现: 建档一次拿稳定 document_id(链接从此永久，建档即回写映射防孤儿)。
    已存在: 原地清旧块写新块，URL 不变。
    items 给定时直接用这组预构块(主页/日报/档案的分栏卡+彩色高亮块走这条，md 表达不了);
    不给时把 md_text 认真转 blocks(标题/正文/列表/表格/callout/divider)。
    callout/表格/分栏失败降级纯文本，内容不丢。每次都补一遍 Max 的 full_access(幂等，不赌文件夹继承)。
    返回 (ok:bool, url或错误串)。"""
    title = file_name[:-3] if file_name.endswith(".md") else file_name
    try:
        prev = doc_map.get(key) or {}
        doc_id = prev.get("document_id")
        if not doc_id:
            r = _feishu_call("POST", "/docx/v1/documents", token=token,
                             body={"title": title, "folder_token": folder_token})
            doc_id = r.get("data", {}).get("document", {}).get("document_id")
            if not doc_id:
                return False, "create doc failed: code=%s %s" % (r.get("code"), str(r.get("msg"))[:100])
            # 建档即入映射: 后续写块失败也不会下次重复建档(防孤儿文档)
            doc_map[key] = {"document_id": doc_id, "url": _doc_url(doc_id), "folder": folder_token,
                            "name": file_name, "updated": date.today().isoformat()}
        if not _docx_wipe(token, doc_id):
            return False, "wipe failed: %s" % doc_id
        if items is None:
            items = _md_to_block_items(md_text, drop_title=title)
        written, degraded, err = _docx_write_blocks(token, doc_id, items)
        if err:
            return False, "write blocks: %s" % err
        _add_collaborator(token, doc_id, "docx", open_id)
        doc_map[key] = {"document_id": doc_id, "url": _doc_url(doc_id), "folder": folder_token,
                        "name": file_name, "updated": date.today().isoformat()}
        if degraded:
            doc_map[key]["degraded_blocks"] = degraded
        return True, _doc_url(doc_id)
    except Exception as e:
        return False, "error: %s" % e


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
    doc_map = _load_map(node.get("map_path", DEFAULT_MAP_PATH))
    doc_map["_folders"] = {k: _folder_url(v) for k, v in folders.items() if not k.startswith("_")}
    ctx = {"cred": cred, "token": token, "oid": oid, "folders": folders,
           "map_path": node.get("map_path", DEFAULT_MAP_PATH),
           "doc_map": doc_map}
    return ctx, None


def _folder_url(tok):
    return "https://my.feishu.cn/drive/folder/%s" % tok if tok else ""


def _h(level, text):
    """标题块 payload(level 1/2/3)。"""
    return {"block_type": {1: 3, 2: 4, 3: 5}[level], {1: "heading1", 2: "heading2", 3: "heading3"}[level]:
            {"elements": _text_elements(text)}}


def _p(md_line):
    """正文块 payload(走行内 md: 链接/加粗/代码)。"""
    return {"block_type": 2, "text": {"elements": _text_elements(md_line)}}


def _divider():
    return {"block_type": 22, "divider": {}}


def build_homepage_items(cfg, stats, ctx):
    """主页仪表盘: 直接产 docx 块序列(分栏卡/彩色高亮块 md 表达不了，故不走 md)。
    重设计目标(Max 反馈『找不到东西』=最大失败): 让第一次打开的人 10 秒内知道往哪点。
    结构: 封面区 → 『今天看什么』三步导航卡(grid) → 数据大数字卡(grid) → 完整目录区(分组) → 底部机制。
    设计取经 sspai/96170(封面定位/大数字卡/分栏导航/用色语义/分隔)。当日日报直链文档本体(永久 id)。"""
    f = ctx["folders"]
    doc_map = ctx["doc_map"]
    cred = ctx["cred"]
    bitable_url = cred.get("base_url") or ("https://my.feishu.cn/base/%s" % cred["app_token"])
    daily_url = doc_map.get("daily/%s" % date.today().isoformat(), {}).get("url", "")
    daily_link = daily_url or _folder_url(f.get("日报"))
    system_url = doc_map.get("system/overview", {}).get("url", "")
    now = datetime.now().isoformat(timespec="minutes")
    items = []

    # --- 封面区: 大标题 + 一句话定位(蓝色 callout) ---
    items.append(("flat", _h(1, "达人雷达 · 总部")))
    items.append(("callout", (["**给品牌方的达人主动发现引擎。**",
                               "所有人要看的产物都在这一页，按用途分好，每天 08:30 自动更新。第一次来？先看下面「今天看什么」三张卡。"],
                              "cover")))
    items.append(("flat", _divider()))

    # --- 今天看什么: 三步导航卡(grid 三栏)。这是『往哪点』的核心，放最显眼位置 ---
    items.append(("flat", _h(2, "今天看什么")))
    items.append(("flat", _p("_不知道从哪看起，就按这三张卡的顺序：先看今天推荐，再去表格里筛，想深挖点开档案。_")))
    items.append(("grid", [
        _nav_card("📰", "① 今日日报", "今天池子动了什么、推荐了谁、为什么。一页看完，5 分钟。",
                  "打开今日日报", daily_link),
        _nav_card("📊", "② 推荐表格", "所有推荐自动灌进这张多维表，在这里筛选、终审、流转评审、跟投放。",
                  "打开多维表格", bitable_url),
        _nav_card("🗃️", "③ 最新档案", "看中谁想深挖，点开他的档案：数据全貌 + 值得签/风险/合作建议。",
                  "打开档案文件夹", _folder_url(f.get("达人档案"))),
    ]))
    items.append(("flat", _divider()))

    # --- 数据大数字卡: grid 四栏「大数字+小标签」 ---
    items.append(("flat", _h(2, "核心数据")))
    items.append(("grid", [
        _big_number_card(stats.get("pool_size", "?"), "候选池频道数", "🗂️"),
        _big_number_card(stats.get("cards_today", "?"), "今日推荐达人", "⭐"),
        _big_number_card(stats.get("dossier_count", "?"), "已建达人档案", "🗃️"),
        _big_number_card("%s×" % stats.get("blind_test_multiple", "4.6"), "盲测密度/随机基线(冻结考试池1106)", "🎯"),
    ]))
    items.append(("callout", (["📊 **记分板**：%s" % stats.get("scoreboard_status",
                              "首份 picks 已存档，2026-08-04 结算")], "info")))
    items.append(("flat", _divider()))

    # --- 完整目录区(分组): API 无目录块，这就是人工目录。分三组，一眼看清全站有什么 ---
    items.append(("flat", _h(2, "全部内容")))

    items.append(("flat", _h(3, "🎯 每天产出")))
    items.append(("flat", _p("[今日日报](%s)　当天池子动态、推荐卡、记分板结算，一页读完。" % daily_link)))
    items.append(("flat", _p("[历史日报文件夹](%s)　往期每日快照，按日期归档。" % _folder_url(f.get("日报")))))

    items.append(("flat", _h(3, "🔨 运营主战场")))
    items.append(("flat", _p("[多维表格](%s)　推荐自动入表，做终审与评审流转；每行「档案」列直达该达人档案。" % bitable_url)))
    items.append(("flat", _p("[达人档案文件夹](%s)　榜单前 %s 名各一页：基本盘、订阅快照史、近期视频、评论区概况、跨平台矩阵；前 5 名附推荐卡。"
                             % (_folder_url(f.get("达人档案")), stats.get("dossier_count", "?")))))

    items.append(("flat", _h(3, "📁 深度材料")))
    items.append(("flat", _p("[报告文件夹](%s)　方法论与验证：行业方法对比、盲测验证报告、删除实验裁判。给评委和队友看。" % _folder_url(f.get("报告")))))
    if system_url:
        items.append(("flat", _p("[系统说明](%s)　是什么 / 怎么跑 / 三层雷达 / 技术栈，队友快速上手。" % system_url)))
    else:
        items.append(("flat", _p("系统说明（待生成）　是什么 / 怎么跑 / 三层雷达 / 技术栈。")))
    items.append(("flat", _p("全池榜单（扩容合并后上线）　全池 1000+ 频道完整排序，将以多维表格第二张表上线。")))
    items.append(("flat", _divider()))

    # --- 底部机制说明(灰色 quiet callout，不与上面抢眼) ---
    items.append(("flat", _h(2, "更新机制")))
    items.append(("callout", ([
        "达人雷达每天 **08:30** 自动跑：刷新池子 → 全池重排 → 生成推荐卡 → 原地更新本页与当日日报 → 同步档案。",
        "所有文档原地更新，链接永久不变，可放心收藏与互链。",
        "最后更新：%s" % now,
    ], "quiet")))
    return items


def push_homepage(cfg, stats=None):
    """建/刷新主页仪表盘「达人雷达 · 总部」，放在顶层文件夹根部。幂等 key=home/index(每天覆盖刷新)。
    stats 不给时自动从今日运行产物推算(池子/推荐数)。温和降级: 出错只返回错误串 dict。"""
    ctx, err = _open_cred(cfg)
    if err:
        return err
    stats = dict(stats or {})
    stats.setdefault("pool_size", _guess_pool_size())
    stats.setdefault("cards_today", _guess_cards_today())
    stats.setdefault("dossier_count", _guess_dossier_count())
    stats.setdefault("blind_test_multiple", "4.6")
    stats.setdefault("scoreboard_status", "首份 picks 已存档，2026-08-04 结算")
    items = build_homepage_items(cfg, stats, ctx)
    ok, info = _put_doc(ctx["token"], ctx["folders"].get(TOP_FOLDER), "达人雷达 · 总部.md", "",
                        "home/index", ctx["doc_map"], open_id=ctx["oid"], items=items)
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


def _guess_dossier_count():
    """从 data/dossiers/*.md 数档案份数(主页大数字兜底)。"""
    try:
        d = os.path.join(ROOT, "data", "dossiers")
        return sum(1 for f in os.listdir(d) if f.endswith(".md"))
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
          "引擎在 1,106 频道的冻结考试池上，把影石历史真实合作过的达人以 **4.6 倍于随机基线**的密度排进前 5%（官方指标只在冻结考试池复现；生产池每日生长，当前 2,400+）。全程零样本，未用任何正例标签调参。", "",
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


# ----- 互链网络: 档案导航头 / 日报卡片档案链接 / 表格档案列回填 -----

def _dossier_nav(doc_map):
    """档案飞书副本的头部导航(互链)。上传时注入而不写进本地文件:
    data/dossiers 每日自动 commit 进公开仓库，红线要求 document_id/token 不进 repo，
    所以链接只存在于飞书副本，URL 运行时从映射读。原生 docx 后 document_id 永久，
    当日日报可以直链文档本体(当天日报文档还没建时退回「日报」文件夹)。"""
    home = doc_map.get("home/index", {}).get("url", "")
    daily = doc_map.get("daily/%s" % date.today().isoformat(), {}).get("url", "")
    rep = daily or (doc_map.get("_folders") or {}).get("日报", "")
    lines = []
    if home:
        lines.append("[🛰️ 返回总部主页](%s)  " % home)
    if rep:
        lines.append("[📰 %s](%s)  " % ("当日日报" if daily else "雷达日报", rep))
    return ("\n".join(lines) + "\n\n") if lines else ""


def patch_report_dossier_links(report_path, cfg=None, doc_map=None):
    """日报 md 每张推荐卡小节里补/刷新「达人档案」链接行(按频道名匹配映射)。
    原生 docx 后档案链接永久，本函数的作用收窄为: 补上当天首次出现的新频道档案链接
    (其文档在 push_dossiers 时才建档，write_report 时映射里还没有)。
    reports/ 已 gitignore，链接不进 repo。幂等: 旧链接行被替换不堆叠。返回补上的条数。"""
    if doc_map is None:
        node = (cfg or {}).get("feishu_docs", {})
        doc_map = _load_map(node.get("map_path", DEFAULT_MAP_PATH))
    if not os.path.exists(report_path):
        return 0
    by_name = {}
    for k, v in doc_map.items():
        if k.startswith("dossier/") and v.get("url") and str(v.get("name", "")).endswith(".md"):
            by_name[v["name"][:-3]] = v["url"]
    if not by_name:
        return 0
    lines = _read(report_path).splitlines()
    out, i, n = [], 0, 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = re.match(r"^###\s+(.+?)\s+·\s+#", line)
        if m and by_name.get(m.group(1).strip()):
            url = by_name[m.group(1).strip()]
            j = i + 1
            if j < len(lines) and not lines[j].strip():
                out.append(lines[j])
                j += 1
            if j < len(lines) and lines[j].startswith("[🗂️ 达人档案]("):
                j += 1  # 丢弃旧链接行(重写保新)
                if j < len(lines) and not lines[j].strip():
                    j += 1
            out.append("[🗂️ 达人档案](%s)" % url)
            out.append("")
            n += 1
            i = j
            continue
        i += 1
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    return n


def backfill_bitable_dossier_links(cfg):
    """多维表格「档案」列回填(互链: 表格行 -> 档案文档)。字段不存在自动建(超链接类型)。
    匹配优先级: 频道链接里含 channel_id > 频道名等于档案标题。原生 docx 后档案链接永久，
    全表扫描的语义收窄为: 给当天新增行补链接(重写老行同值幂等无害)。温和降级: 出错只返回错误 dict。"""
    node = cfg.get("feishu_docs", {})
    cred_path = os.path.expanduser(node.get("credentials_path", ""))
    if not cred_path or not os.path.exists(cred_path):
        return {"ok": False, "error": "NotConfigured: 凭证文件不存在"}
    try:
        cred = json.load(open(cred_path))
        token, traw = _get_token(cred)
        if not token:
            return {"ok": False, "error": "token failed: code=%s" % traw.get("code")}
        A, T = cred.get("app_token"), cred.get("table_id")
        if not A or not T:
            return {"ok": False, "error": "凭证缺 app_token/table_id"}
        doc_map = _load_map(node.get("map_path", DEFAULT_MAP_PATH))
        dossiers = {k[len("dossier/"):]: v for k, v in doc_map.items()
                    if k.startswith("dossier/") and v.get("url")}
        if not dossiers:
            return {"ok": False, "error": "映射里无档案条目"}
        # 确保「档案」字段存在(type 15 = 超链接)
        r = _feishu_call("GET", "/bitable/v1/apps/%s/tables/%s/fields?page_size=100" % (A, T), token=token)
        names = [f.get("field_name") for f in r.get("data", {}).get("items", [])]
        if "档案" not in names:
            c = _feishu_call("POST", "/bitable/v1/apps/%s/tables/%s/fields" % (A, T), token=token,
                             body={"field_name": "档案", "type": 15})
            if c.get("code") != 0:
                return {"ok": False, "error": "建档案字段失败: code=%s %s" % (c.get("code"), str(c.get("msg"))[:80])}
        # 全表扫描匹配
        updates, scanned, page = [], 0, ""
        for _ in range(20):
            body = {"page_size": 100}
            if page:
                body["page_token"] = page
            r = _feishu_call("POST", "/bitable/v1/apps/%s/tables/%s/records/search" % (A, T),
                             token=token, body=body)
            if r.get("code") != 0:
                return {"ok": False, "error": "records search failed: code=%s" % r.get("code")}
            for it in r.get("data", {}).get("items", []):
                scanned += 1
                fields = it.get("fields", {})
                link = fields.get("频道链接")
                url = link.get("link", "") if isinstance(link, dict) else ""
                hit = next((d for cid, d in dossiers.items() if cid and cid in url), None)
                if not hit:
                    nm = fields.get("频道名")
                    if isinstance(nm, list):
                        nm = "".join(seg.get("text", "") for seg in nm if isinstance(seg, dict))
                    nm = (nm or "").strip()
                    hit = next((d for d in dossiers.values()
                                if str(d.get("name", "")).endswith(".md") and d["name"][:-3] == nm), None)
                if hit:
                    label = hit.get("name", "档案")
                    label = label[:-3] if label.endswith(".md") else label
                    updates.append({"record_id": it.get("record_id"),
                                    "fields": {"档案": {"link": hit["url"], "text": "📄 " + label}}})
            if not r.get("data", {}).get("has_more"):
                break
            page = r.get("data", {}).get("page_token", "")
        done = 0
        for i in range(0, len(updates), 100):
            u = _feishu_call("POST", "/bitable/v1/apps/%s/tables/%s/records/batch_update" % (A, T),
                             token=token, body={"records": updates[i:i + 100]})
            if u.get("code") == 0:
                done += len(updates[i:i + 100])
        return {"ok": done == len(updates) and done > 0, "scanned": scanned,
                "matched": len(updates), "updated": done}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _strip_frontmatter(md):
    """去掉开头的 YAML frontmatter(其字段在正文均有呈现)。原生文档里 yaml 是噪音。"""
    lines = md.splitlines()
    if lines and lines[0].strip() == "---":
        j = 1
        while j < len(lines) and lines[j].strip() != "---":
            j += 1
        if j < len(lines):
            return "\n".join(lines[j + 1:]).lstrip("\n")
    return md


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


# ----- 档案模板: 头部信息卡 + 分节视觉分隔(只作用于飞书副本，本地 md 格式不变) -----

# 档案节名 -> emoji 锚点(sspai/96170: emoji 作视觉锚点，节节可扫)。
_DOSSIER_SECTION_EMOJI = {
    "频道基本盘": "📇", "订阅快照史": "📈", "近期视频": "🎬",
    "评论区概况": "💬", "跨平台矩阵": "🌐", "当日推荐卡": "⭐",
}


def _parse_dossier_basics(md_text):
    """从档案「频道基本盘」小节抽头部信息卡要素: 排名/订阅/总分/前百分比/频道链接。
    解析失败返回 {}(上层回退纯 md，内容不丢)。纯正则，不依赖行序。"""
    out = {}
    for line in md_text.splitlines():
        s = line.strip()
        if s.startswith("- 频道链接："):
            out["url"] = s.split("：", 1)[1].strip()
        elif s.startswith("- 订阅数："):
            out["subs"] = s.split("：", 1)[1].strip()
        elif s.startswith("- 雷达排名："):
            v = s.split("：", 1)[1].strip()
            m = re.search(r"第\s*(\d+)\s*名", v)
            out["rank"] = m.group(1) if m else v
            mp = re.search(r"前\s*([\d.]+%)", v)
            if mp:
                out["pct"] = mp.group(1)
        elif s.startswith("- 对路分：") or s.startswith("- 总分："):  # 三词化后是「对路分」, 兼容旧档「总分」
            v = s.split("：", 1)[1].strip()
            m = re.match(r"([\d.]+)", v)
            out["score"] = m.group(1) if m else v
        elif s.startswith("- 命中主题："):
            out["themes"] = s.split("：", 1)[1].strip()
    return out


def build_dossier_items(md_body, doc_map):
    """把一篇档案 md(已去 frontmatter)渲染成飞书块序列: 顶部导航 + 头部信息卡(排名/订阅/总分一眼见)
    + 分节(每节 emoji 锚点 + divider)。解析不出基本盘就整篇走普通 md 转换(内容不丢)。
    md_body 里若已含注入的导航行(以 [🛰️ 返回)开头)，原样保留在最前。设计取经 sspai/96170。"""
    basics = _parse_dossier_basics(md_body)
    if not basics.get("rank") and not basics.get("subs") and not basics.get("score"):
        return _md_to_block_items(md_body)  # 解析失败: 老路兜底

    items = []
    lines = md_body.splitlines()

    # 提取顶部已注入的导航行(返回总部/日报)，放最前作 info callout
    nav = [ln.strip() for ln in lines[:4] if ln.strip().startswith("[🛰️ 返回") or ln.strip().startswith("[📰")]

    # 标题(第一个 # )
    title = next((ln.strip()[2:].strip() for ln in lines if ln.strip().startswith("# ")), "达人档案")
    items.append(("flat", _h(1, title)))
    if nav:
        items.append(("callout", (["　".join(n.strip() for n in nav)], "info")))

    # 头部信息卡: 三/四栏大数字(排名/订阅/总分/前百分比)
    cards = []
    if basics.get("rank"):
        cards.append(_big_number_card("#%s" % basics["rank"], "雷达排名", "🏅"))
    if basics.get("subs"):
        cards.append(_big_number_card(basics["subs"], "订阅数", "👥"))
    if basics.get("score"):
        cards.append(_big_number_card(basics["score"], "对路分", "🎯"))
    if basics.get("pct"):
        cards.append(_big_number_card(basics["pct"], "全池百分位", "📊"))
    if len(cards) >= 2:
        items.append(("grid", cards[:4]))
    # 频道链接 + 命中主题 放一条 info callout(基本盘要点，一眼带走)
    meta = []
    if basics.get("url"):
        meta.append("🔗 频道链接：%s" % basics["url"])
    if basics.get("themes"):
        meta.append("🏷️ 命中主题：%s" % basics["themes"])
    if meta:
        items.append(("callout", (meta, "info")))
    items.append(("flat", _divider()))

    # 正文分节: 跳过已在信息卡呈现的「频道基本盘」小节，其余节加 emoji 锚点 + 节前 divider
    i = 0
    first_section = True
    while i < len(lines):
        s = lines[i].rstrip()
        st = s.strip()
        if st.startswith("# "):  # 标题已单独处理
            i += 1
            continue
        if st.startswith("## "):
            name = st[3:].strip()
            if name == "频道基本盘":
                # 跳过整节(其内容已进信息卡)
                i += 1
                while i < len(lines) and not lines[i].strip().startswith("## "):
                    i += 1
                continue
            if not first_section:
                items.append(("flat", _divider()))
            first_section = False
            emoji = _DOSSIER_SECTION_EMOJI.get(name, "")
            items.append(("flat", _h(2, ("%s %s" % (emoji, name)).strip())))
            i += 1
            continue
        # 非标题行: 收集到下一个 ## 或文件尾，交给 md 转换器(表格/列表/正文都在这条链路)
        buf = []
        while i < len(lines) and not lines[i].strip().startswith("## ") and not lines[i].strip().startswith("# "):
            buf.append(lines[i])
            i += 1
        if any(b.strip() for b in buf):
            items += _md_to_block_items("\n".join(buf))
    return items


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
    # 文件夹 URL 落映射(repo 外): 档案导航等互链场景运行时从这里读，token 不进仓库
    doc_map["_folders"] = {k: _folder_url(v) for k, v in folders.items() if not k.startswith("_")}
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
        nav = _dossier_nav(doc_map)  # 互链导航头，只注入飞书副本(本地文件保持无 token)
        for f in files:
            cid = f[:-3]
            md = _read(os.path.join(ddir, f))
            title = _dossier_title(md, cid)
            fname = "%s.md" % _safe(title)
            body = nav + _strip_frontmatter(md)  # frontmatter 字段正文均有，原生文档里不留 yaml 噪音
            d_items = build_dossier_items(body, doc_map)  # 头部信息卡 + 分节锚点(飞书副本专属美化)
            ok, info = _put_doc(token, target, fname, body, "dossier/%s" % cid, doc_map,
                                open_id=oid, items=d_items)
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
    elif which == "backfill":
        print(json.dumps(backfill_bitable_dossier_links(cfg), ensure_ascii=False, indent=1))
