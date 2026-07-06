# 达人雷达 Creator Radar（占位名）

给品牌方的达人主动发现引擎：导入候选池，零样本排序，可解释推荐，人工终审。

第一个品牌配置是影石 Insta360（飞书 AI 先锋未来人才大赛·影石命题「AI 达人发现与内容裂变」参赛方案）。引擎本体品牌无关，换品牌只需换一份 JSON 配置。

## 它证明过什么

在 1,085 个全球摩托/骑行 YouTube 频道的真实池子上，引擎在看不到任何品牌合作痕迹的前提下（打分前剔除全部品牌 token），把影石历史上真实合作过的达人以 **4.6 倍于随机基线**的密度排进前 5%。全程零样本：打分维度来自影石官方文章的逆向工程，未用任何正例标签调参。

| 指标 | 全部正例(26) | 中腰部子集(20) | 随机基线 |
|---|---|---|---|
| 前 5% 召回 | 23% | 20% | 5% |
| 前 10% 召回 | 27% | 25% | 10% |
| 正例中位百分位 | 32.4% | 28.5% | 50% |

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

前置：本机 [ollama](https://ollama.com) 并拉取 embedding 模型：

    ollama pull bge-m3

跑盲测回测（全池 1,085 频道约 1-4 分钟，纯本地零 API 成本）：

    python3 src/backtest.py --config config/insta360.json --pool data/pool/creator_pool.jsonl --out data/runs/my-run/

产出：全池排序 `backtest_scores.json` + 指标 `backtest_metrics.json`。基线产物存 `data/runs/2026-07-06-pilot/`。给任意新池子排序（不需要正例标注）用 `src/score.py`，参数相同。

复现性说明：ollama 的 embedding 在不同服务实例间存在极微小浮点漂移，两次全量运行的正例中位百分位为 32.4% 与 32.3%（一个频道在 350/399 名间摆动），召回指标不受影响。所有指标应读作区间信号而非精确值。

## 目录

    config/    品牌配置（主题查询、权重、甜点参数、泄漏词表）
    src/       radar_lib.py 公共库 / score.py 产品路径 / backtest.py 验证路径
               （python 标准库 + 本地 ollama，无第三方依赖）
    data/      池子、正例标注、每次运行的产物
    docs/      底层推荐逻辑设计书
    report/    对外报告

## 作者

Max
