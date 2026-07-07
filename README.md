# 达人雷达 Creator Radar（占位名）

给品牌方的达人主动发现引擎：导入候选池，零样本排序，可解释推荐，人工终审。

第一个品牌配置是影石 Insta360（飞书 AI 先锋未来人才大赛·影石命题「AI 达人发现与内容裂变」参赛方案）。引擎本体品牌无关，换品牌只需换一份 JSON 配置。

## 它证明过什么

在 **1,106 个**全球摩托/骑行 YouTube 频道的冻结考试池上，引擎在看不到任何品牌合作痕迹的前提下（打分前剔除全部品牌 token），把影石历史上真实合作过的达人以 **4.6 倍于随机基线**的密度排进前 5%。全程零样本：打分维度来自影石官方文章的逆向工程，未用任何正例标签调参。

下面是现役 **v1.2** 配置（五主题，`config/insta360.json`）在考试池 `data/pool/exam_pool_v1.jsonl` 上的官方指标：

| 指标 | 全部正例(26) | 中腰部子集(20) | 随机基线 |
|---|---|---|---|
| 前 5% 召回 | 23%（4.6 倍） | 20% | 5% |
| 前 10% 召回 | 27% | 25% | 10% |
| 前 20% 召回 | 42% | 45% | 20% |
| 正例中位百分位 | 28.4% | 27.1% | 50% |

**考试池 / 生产池双池制**：`exam_pool_v1.jsonl`（1,106 频道，26 隐藏正例）是**冻结**的盲测考试池，永不改动，所有官方指标只在它上面复现，保证评委 clone 下来跑出的数字与材料一致。生产池 `creator_pool.jsonl` 每天生长（采集刷新 + 自动发现 + 垂类扩容），**当前约 2,400+ 频道**，与考试池解耦：考试池量指标，生产池找达人。

方法、局限声明与迭代记录见 [docs/architecture.md](docs/architecture.md)。

## 架构一图流

    config/<brand>.json（策略全外置）
        → 采集层（YouTube / B站 适配器，IG·TikTok v2）
        → 特征层（文本 / 规模 / 平台标记）
        → 打分层（bge-m3 本地 embedding 语义匹配 + 甜点函数 + 标记）
        → 验证层（盲测回测，泄漏防护）
        → 解释层（可解释推荐卡，v1.5）
        → 工作流层（飞书多维表格 + 飞书 AI 能力）
        → 回流（投放结果 → 配置调优）

## 快速开始

前置：本机 [ollama](https://ollama.com) 并拉取 embedding 模型与 chat 模型：

    ollama pull bge-m3
    ollama pull qwen3:8b

跑盲测回测（**在冻结考试池上复现官方指标**，1,106 频道约 1-4 分钟，纯本地零 API 成本）：

    python3 src/backtest.py --config config/insta360.json --pool data/pool/exam_pool_v1.jsonl --out data/runs/my-run/

产出：排序 `backtest_scores.json` + 指标 `backtest_metrics.json`，应与 `data/runs/2026-07-07-official-v1.2/` 一致（前 5% 召回 23%/20%、前 10% 27%/25%、中位 28.4%/27.1%）。

**官方指标固定在考试池上复现**：`exam_pool_v1.jsonl` 是冻结的盲测池（永不改动），生产池 `creator_pool.jsonl` 每天生长、规模每天不同，故复现只认考试池。给生产全池或任意新池子排序（不需要正例标注）用 `src/score.py --pool data/pool/creator_pool.jsonl`，参数形态相同。

复现性说明：ollama 的 embedding 在不同服务实例间存在极微小浮点漂移。召回类指标（前 5%/前 10%/前 20%）稳定；百分位类指标（正例中位百分位）可能有 ±1pp 量级的抖动（个别频道在相邻名次间摆动）。所有指标应读作区间信号而非精确值。

## 常驻运行（每天自动跑）

⚠️ **`run_radar.py` 默认会向 iMessage、飞书真实推送**（含飞书多维表格灌数据）。首次试跑请加 `--dry-run`，确认行为符合预期后再去掉。

「跑一次」之外，雷达可以作为常驻系统每天自动运行：定时采集 → 重排 → 生成推荐卡 → 推送。

一条命令跑全链（冒烟用小参数）：

    python3 src/run_radar.py --budget 40 --discover-terms 2 --top-n 5

安全试跑（不推送、不提交，只产日报）：

    python3 src/run_radar.py --budget 40 --discover-terms 2 --top-n 5 --dry-run

流程（YouTube 主线）：`collect.py`（yt-dlp 刷新池内频道元数据 + 多语言搜索词发现新频道入池）→ 生产全池重排 → 与上次运行排名 diff（新进前 100 / 窜升 ≥200 位）→ `explain.py`（本地 ollama chat 模型对候选精读，产可解释推荐卡）→ 日报 `reports/YYYY-MM-DD-radar.md` → 推送（iMessage + 飞书多维表格 + 飞书文档）→ 追加 `logs/radar.log`。主线跑完**串行跑 B站线**（小预算刷新 + 打分 + B站榜单 + 日报「B站雷达」一节，见下方「全球与中文双线」）。可用 `--skip-bili` 只跑主线。

本命令还会真实执行评论采样与字幕采集（均受各自预算封顶，只读公开数据不下载视频/音频本体），并按 `outputs` 推送到对应出口；无凭证的出口自动 skip，不中断整条主链。

飞书三张多维表格（`sync_full_ranking.py` + `run_radar` 接线，同一份 base 凭证）：①**达人推荐**（当日推荐卡，append）②**全池榜单**（生产全池当日排序整表，每日先清后写重刷；列含排名/频道/订阅/总分/命中主题/垂类/语言/档案(top50)/入池日期）③**B站榜单**（B站线 top 榜，列同全池榜单去掉档案列）。表 id 存 credentials 同目录的 `feishu_tables.json`（不进仓库）。「全池榜单」采用**先清后写**（fetch record_id → batch_delete → batch_create ≤500/批）而非删表重建，让表 id 稳定、飞书侧引用与视图不被打断。

数据积累（先存后洗）：每个被刷新/新发现的频道当天落一条完整原始快照到 `data/history/`（append-only jsonl，含视频层 video_id/时长/is_short），每日跑完自动 `git commit` 数据目录（history/pool/scoreboard）并 push，私有仓库即异地备份。预测记分板：每周一把当日推荐存 `data/scoreboard/picks-*.json`（带订阅基线），28 天后自动结算订阅增长对照全池中位增速，跑赢/跑平/跑输写进日报「记分板」一节。

策略全在 `config/insta360.json` 的 `collect` / `explain` / `scoreboard` / `outputs` 四节：搜索词、每次预算、节流、推荐卡模型、结算窗口、分发出口。飞书多维表格出口已实装（`outputs` 含 `"bitable"`、`"feishu_docs"`，凭证在 repo 外 `~/.config/creator-radar/feishu.json`）。

定时器：`launchd/com.max.creator-radar.plist`，每天 08:30 跑默认参数。安装：

    cp launchd/com.max.creator-radar.plist ~/Library/LaunchAgents/
    launchctl load ~/Library/LaunchAgents/com.max.creator-radar.plist
    launchctl list | grep creator-radar   # 验证在册

前置：本机装 `yt-dlp` 与 `ollama`（含 bge-m3 embedding 与一个 chat 模型，默认 qwen3:8b）。一切产出（reports/logs/data）都留仓库目录内，不写 iCloud 路径（launchd 下 TCC 权限会静默失败）。

**部署前提**：iMessage 通知脚本路径（`src/run_radar.py` 里的 `NOTIFY`）、飞书凭证路径（`~/.config/creator-radar/feishu.json`）、本地 RSSHub 地址均为作者本机配置，换机部署需按实际环境修改。

## 迭代记录

| 版本 | 池子 / 主题 | 官方指标（前5%/前10% 召回，中位百分位） | 关键动作 |
|---|---|---|---|
| v1.0 | 1,085 池 · 6 主题 | 前5% 23% / 前10% 27% / 中位 32.4% | 首轮盲测，前 5% 达随机基线 **4.6 倍**（`data/runs/2026-07-06-pilot/`） |
| v1.2 | 1,106 考试池 · 5 主题 | 前5% 23% / 前10% 27% / 中位 **28.4%** | 删除实验驱动：盲测裁判判定 `adventure_bold` 为白重量（召回六格逐格不变、中位略好），删除后六主题变五主题；各主题 variants 扩容新垂类（冲浪/滑雪/攀岩/潜水/越野跑/FPV 等）。前 5%/前 10% 召回持平，前 20% 与中位改善（`data/runs/2026-07-07-official-v1.2/`） |

v1.0→v1.2 的池子从 1,085 长到 1,106（考试池冻结在 1,106），再合并垂类扩容到生产池约 2,400+。「用盲测裁判删掉一个主题」是删除哲学的活演示：删除实验完整记录（9 种删法 7 种承重、1 种白重量、1 种存疑）见 `data/runs/ablation-2026-07-07/ABLATION.md`。

## 全球与中文双线

- **主战场是全球**（YouTube）：生产池由 55+ 多语言搜索词构建，回测正例全部为英文侧达人。影石营收大头在海外，达人发现主战场跟着营收走。
- **中文生态是第二战场**（B站）：`config/insta360_bilibili.json` 是 B站品牌配置（同构，措辞换中文创作者语境、甜点峰值上调到 B站中腰部规模）。B站线怎么跑：`run_radar` 按 `config.platform` 分发采集器，YouTube 主线跑完串行跑 B站线，即 `collect_bilibili.py`（本机 RSSHub vsearch 种子发现 + `api.bilibili.com` card 端点补粉丝/签名，只拉公开元数据，绝不下视频）→ B站池打分 → B站榜单进飞书 → 日报「B站雷达」一节。手动跑：

      python3 src/collect_bilibili.py --config config/insta360_bilibili.json
      python3 src/score.py --config config/insta360_bilibili.json --pool data/pool/bilibili_pool.jsonl --out data/runs/my-bili-run/

- **正例 sanity**：B站已核实正例「菜腿小崔」（15.4 万粉）在 319 UP 主的零样本排序里**自然被发现排 #3**，未做任何针对性调参，验证同一套打分逻辑吃 B站元数据也成立。

## 目录

    config/    品牌配置（insta360.json = YouTube 线 v1.2；insta360_bilibili.json = B站线）
    src/       radar_lib.py 公共库 / score.py 产品路径 / backtest.py 验证路径
               collect.py + collect_bilibili.py 采集 / run_radar.py 总调度
               sync_full_ranking.py 飞书全池·B站榜单同步
               （python 标准库 + 本地 ollama，无第三方依赖）
    data/pool/ exam_pool_v1.jsonl（冻结考试池 1,106，永不改动）
               creator_pool.jsonl（生产池，每日生长，当前约 2,400+）
               bilibili_pool.jsonl（B站池）
    data/runs/ 每次运行产物（2026-07-07-official-v1.2 = 现役官方指标）
    docs/      底层推荐逻辑设计书
    report/    对外报告

## 作者

Max
