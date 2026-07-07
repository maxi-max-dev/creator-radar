# 第四层「对的浪」验证报告 (trends-v1)

生成日期: 2026-07-07 | 快照: `data/rss/2026-07-07.jsonl` + `data/trends/2026-07-07.jsonl`

**一句话判决**: 浪层可以用, 但当前是「单日横截面近似」, 决策价值集中在 `pool_wave` 一路,
外部三路是佐证不是精调。`trend_score` 在真正上浪的 30 个频道里有干净区分度 (0.04 到 0.95),
`breakout` 揪出了 36 个「视频观看数超过粉丝总盘」的算法投票案例。**每日跑会越来越准**,
因为浪的本质是时间序列, 单日只能拍一张照。

设计铁律 (Max 已认可): 趋势是放大器不是保票。
`浪层信号 = 浪在涨 × 进得早 × 他的浪上内容跑赢他自己的基线`。晚进场蹭热点 = 负信号 = `trend_chaser`。

---

## 1. 今天各源采集战果

| 源 | scope | 条数 | 状态 | 失败原因 |
|---|---|---|---|---|
| **pool_wave** (池内浪, 零新增网络) | 8 垂类 | 179 升温词 | ✅ 主力 | 无 |
| **google_trends** (大盘) | US / GB / JP | 30 | ✅ | CN 返回 HTTP 400 (Google 对 geo=CN 不开放, 预期内) |
| **bilibili_rank** (国内) | rid 234 运动 / 223 汽车 | 120 | ✅ | rid 250 出行 命中一次性 HTTP 503 (RSSHub 上游抖动, 下次重试即可) |
| **reddit_hot** (海外社区) | r/motorcycles / r/MTB | 50 | ⚠️部分 | 另 4 个 sub (fpv/skiing/surfing/climbing): JSON 端 403 Blocked, .rss 端退回时撞 429 限流 |

**总计 379 条**, 落 `data/trends/2026-07-07.jsonl` (append 友好, 每天可重复跑攒历史)。

采集口径与网络纪律:
- **pool_wave**: 复用已有 RSS 视频级 (views + 发布时间) + pool 的 `vertical` 字段, 无新增网络请求。
  打标层 `data/tags/` 本次为空, 已按设计退回**视频标题关键词分词** (unigram + bigram)。
- **google**: 标准库 `xml.etree` 解析 RSS, 取 `title` + `ht:approx_traffic`。0.5s 礼貌间隔。
- **bilibili**: 走本地 RSSHub `:1200`, 请求间隔**硬底 3.5s**。⚠️RSSHub 的 `/bilibili/ranking/<rid>`
  路由**不带播放量**, 因此 metric 用**榜位** (1 = 最热) 代理热度。
- **reddit**: 带 `User-Agent` 头 + **>=1s 节流**。先试官方 `hot.json` (含 upvote score),
  被 403 挡时自动退回 `hot.rss` (Atom, 无 score, 只拿 title + 日期)。任何一路失败只记原因不中断。

**Reddit 诚实说明**: `www.reddit.com` 对无鉴权爬虫的封锁很硬 (JSON 返回 bot-challenge HTML/403),
`.rss` 端能出数但在快速连续请求下触发 429。本次拿到 2 个 sub 的数据是**尽力而为的真实结果**,
不是全量。Reddit 对本项目决策价值本就最低 (海外社区大盘), 留在管道里等它偶尔放行即可,
若要稳定拿 Reddit 建议后续接官方 OAuth app (免费, 60 req/min) 或加大 sub 间隔到 3s+。

---

## 2. 每垂类升温词表 (top by heat) + 证据

热度倍数 = 「近期命中该词的视频 views/天 中位数」÷「该垂类历史 views/天 中位数」。
`✓` = 该词同时出现在外部源标题里 (corroborated 佐证徽章)。

### surfing (40 词)
| heat | 词 | 命中 | 早分位 | 证据视频 (views) |
|---|---|---|---|---|
| 287.85x | `biggest` | 2 | 0.87 | SURFING THE BIGGEST WAVES OF MY LIFE?! (RAW POV) — 90,974 |
| 281.22x | **`raw pov`** | 2 | 0.62 | 同上 |
| 100+x | `big` ✓ / `raw` ✓ | | | 外部 (Reddit/Google) 也在谈 |

### ski_snowboard (21 词)
| heat | 词 | 命中 | 证据 (views) |
|---|---|---|---|
| 463.88x | **`kitesurf` / `kitesurfing` / `glisse kite`** | 2 | VÉNÈRE 🤬 #glisse #kite #kitesurf — 186,968 |

→ **风筝冲浪 (kitesurf) 是 ski_snowboard 垂类里最清晰的一股上升子题材**, bigram 全线命中同一条爆款。

### moto (29 词)
| heat | 词 | 命中 | 证据 (views) |
|---|---|---|---|
| 628.95x | **`streettriple`** | 2 | What to do if you do end up miscalculating... — 92,778 |
| 180.09x | `riders` ✓ | 2 | 5 things that separate riders who improve — 30,546 |

### climbing_bouldering (15 词) / diving_freediving (34 词) / trail_running (40 词)
这三个垂类的 top 词命中了**污染项** (见第 6 节局限): 例如 `disney world`、`chevrolet`、`test drive`
分别浮到 diving / trail_running 头部, 是因为 pool 里少数频道的 `vertical` 标签打错了。
真正干净的信号在这些垂类里靠后一点: 如 climbing 的 `pitch` (68.91x, "POV: You Look Up At The Next Pitch")、
trail_running 的 `binaural audio` (31x, 12 条命中 = 一股「沉浸式双耳录音」子题材)。

**升温词表亮点 3 条 (人工挑, 去掉污染与噪声后)**:
1. **surfing / `raw pov` (281x)** — 「未剪辑第一人称大浪」是当前 surfing 最跑赢基线的内容形态, 且多个频道在做。
2. **ski_snowboard / `kitesurf` (464x)** — 风筝冲浪整簇 bigram 同时升温, 单一爆款 18.7 万播放拉动。
3. **moto / `streettriple` (629x)** — Triumph Street Triple 车型内容爆量, 车型词是最不容易误判的浪。

---

## 3. trend_score 全池分布 (证明区分度)

`trend_score = match × early × onwave` (三因子相乘)。

| 桶 | n | 十分位 [min→p90, max] |
|---|---|---|
| ok-coverage (数据足够算) | 105 | 0,0,0,0,0,0,0,0,**0.343,0.52**, max 0.95 |
| 非零 (真正在某浪上) | 30 | min **0.039**, median **0.489**, max **0.95** |

**读法**: 105 个数据充分的频道里, 只有 **30 个**的近期视频真正命中了升温词 (match>0)。
这是**设计使然, 不是 bug**: 没在任何已知浪上 = `trend_score=0` (不是负分, 他只是没上浪不代表差)。
在这 30 个真正上浪的频道里, 分数从 0.04 铺到 0.95, **区分度干净**。
其余 74 个数据足够但近期没上浪的频道稳稳落在 0, 1963 个无 RSS 的池内频道给中性 0.3 (none), 绝不冤杀。

`trend_chaser` 本次 **0 个**触发 — 见第 6 节: 单日快照下 `early`(进场分位) 与 `onwave`(浪上表现)
很难同时低到阈值以下, 蹭热点的判定需要多日累积才有意义, 目前只靠「进场时间代理」是弱判定。

---

## 4. 浪层 top15 频道证据表 (一眼看懂为什么在浪上)

| trend | 垂类 | 频道 | match | early | onwave | 代表视频 (views, 相对自身爆发) |
|---|---|---|---|---|---|---|
| **0.950** | ski | World of Ozz | 1.00 | 0.95 | 1.00 | They Don't Let Just Anyone Ride... (1,031v, 8.7x) |
| **0.858** | surf | Chris Rogers | 1.00 | 0.86 | 1.00 | I Risked my Drone to get this Shot (13,918v, 9.9x) |
| **0.821** | surf | Surfing With Noz | 1.00 | 0.82 | 1.00 | SURFING THE BIGGEST WAVES OF MY LIFE (RAW POV) (90,974v, **12.1x**) |
| 0.795 | surf | Aidan Brown | 1.00 | 0.80 | 1.00 | Surfing an XL Swell in Waikiki RAW POV (10,174v, 10.1x) |
| 0.767 | moto | Ahhyeah | 1.00 | 0.77 | 1.00 | Driving Around at Good Guys Car Show (373v, 8.2x) |
| 0.759 | moto | 765POV | 1.00 | 0.83 | 0.91 | ...miscalculating & overshooting (92,778v, 6.6x) |
| 0.643 | climb | Alexis Righetti | 1.00 | 0.64 | 1.00 | VTT extrême, haute montagne (16,964v, 10.7x) |
| 0.595 | surf | James Ferrell | 1.00 | 0.74 | 0.80 | The waves every 4th of July are nuts (12,534v, 5.3x) |
| 0.553 | trail | Mountain Bike Rider | 1.00 | 0.55 | 1.00 | E-CVTs are here! (19,823v, 9.3x) |
| 0.537 | surf | Frewbru | 1.00 | 0.54 | 1.00 | Best Surf of Winter So Far? (1,222v, 10.2x) |
| 0.520 | ski | ドローン男子 7sky | 1.00 | 0.52 | 1.00 | DJI OSMO POCKET 4 (1,401v, **49x**) |
| 0.518 | diving | RON ON THE GO | 1.00 | 0.64 | 0.81 | Live Magic Kingdom (51,660v, 5.3x) |
| 0.502 | ski | Bastien LAB | 1.00 | 0.50 | 1.00 | VÉNÈRE 🤬 kitesurf (186,968v, **147x**) |
| 0.497 | climb | Brad Burns | 1.00 | 0.98 | 0.51 | First Person Rock Climbing MRC Direct (75v, 2.9x) |
| 0.489 | trail | The Loam Wolf | 1.00 | 0.94 | 0.52 | Your Voice Matters, Shape MTB (17,555v, 3.0x) |

**读法示例 (Surfing With Noz, 0.821)**: match=1 (视频命中 `biggest`/`raw pov`/`surfing`/`waves` 多个 surf 升温词),
early=0.82 (这些词他进场较早), onwave=1 (这条视频 9 万播放 = 他自己历史基线的 **12 倍**)。
三条腿都硬 → 教科书级「乘着正在升温的浪, 且他的浪上内容碾压自己基线」。

---

## 5. breakout 破圈比分布 + top10

破圈比 = 近期视频 `views ÷ 订阅数` 的最大值, log 压缩到 [0,1]。>1 = 观看数超过粉丝总盘 = 算法推给陌生人。

| 桶 | n | 十分位 |
|---|---|---|
| ok-coverage | 276 | 0 (到 p80), p90 **0.332**, max 1.0 |
| 破圈 (ratio>1) | **36** | — |

十分位再次显示: 破圈是稀有事件, 信号集中在 p90 以上的尾部 (符合预期, 大多数视频不会外溢出粉丝盘)。

**top10 破圈案例**:
| 破圈比 | 订阅 | 频道 | 视频 |
|---|---|---|---|
| **23.49x** | 3,950 | 765POV | ...miscalculating & overshooting (9.2 万播放 vs 4 千粉) |
| 11.99x | 1,280 | Ride with Sully | We Found a GREAT WHITE SHARK On Our Boat |
| 7.80x | 206,000 | RomahaCBR | МОТОБАТ очень ЗОЛ |
| 5.51x | 298,000 | WOLFPACK ADVENTURES | I Raced 100 Riders Down This INSANE Mountain! |
| 5.20x | 12,200 | LoMoto | I thought the water was shallow #enduro |
| 4.73x | 2,380 | BIZER | It seemed easy from the inside |
| 4.67x | 142,000 | Yamaha Racing | FOAM! How factory MXGP mechanics... |
| 4.06x | 125,000 | Ridge Lenny | Catching this fish was an absolute battle |
| 3.62x | 60,200 | Moi Moi TV | Absolute carnage spectating Canadian... |
| 3.49x | 8,910 | Avinash HS | The ONLY KERALA MONSOON Motovlog |

**momentum vs breakout 备忘 (展示层两列并列, 互补不重复)**:
- **momentum (起势)** = 视频 views/天 相对**他自己的历史中位数** → 「他比自己更火了吗」。
- **breakout (破圈)** = 视频 views 相对**他自己的粉丝盘** → 「算法在把他推给圈外人吗」。
- 两者会背离: 765POV 同时高 momentum + 极高 breakout (小频道爆款) = 最强双确认;
  大频道 (RomahaCBR 20 万粉) 破圈 7.8x 说明即便粉丝基数大也外溢, 是很强的信号。

---

## 6. 局限诚实清单

1. **单日快照 ≈ 横截面近似, 不是真·浪**。真正的浪是时间序列 (一个词的热度连续多天上升)。
   本次所有「升温倍数」都是「近 14 天视频 vs 该垂类历史基线」的**单点比值**, 不含**趋势斜率**。
   `data/trends/<date>.jsonl` 已做成 **append 友好**, **每天跑一次, 攒够 7-14 天后**,
   trends.py 可升级成「看一个词的 heat 是否逐日爬升」, 那才是真浪。**现在越跑越准**。

2. **`trend_chaser` 判定目前弱**。它靠「进场时间分位 (early) 低 + 浪上表现 (onwave) 低」代理蹭热点,
   但单日快照下 early 只是「这个词在池内首次出现日期」的粗代理, 不是「大众进场曲线」。
   本次 0 个触发。多日累积 + 「词的热度曲线拐点后才进场」才能真正识别蹭热点者。

3. **升温词表被 pool `vertical` 标签污染**。`chevrolet`/`test drive` 浮到 trail_running,
   `disney world` 浮到 diving_freediving — 是 pool 里少数频道的 vertical 打错 (车评/主题乐园频道被误标)。
   浪层**忠实反映** vertical 输入, 垃圾进垃圾出。**修法在上游** (pool 打标质量), 不在本层。

4. **无 tagger 时靠标题分词, unigram 噪声大**。`insane`/`big`/`just`/`world` 这类泛词会挤上榜,
   bigram (`raw pov`/`kitesurf kitesurfing`/`test drive`) 才是干净信号。
   打标工兵的 `data/tags/` 一旦产出**子题材语义标签**, collect_trends 与 trends 会**自动切换**过去用它
   (代码已就位: `load_tags()` 命中即优先, 无需改任何东西), 噪声将大幅下降。

5. **heat 倍数数值本身噪声大 (可达 600x-1169x)**。因为垂类基线 vpd 低而单条爆款 views 高。
   **但这不污染 `trend_score`**: 频道侧的 `onwave` 因子有 **8x log 封顶归一**, 把病毒单条压平,
   所以最终分数稳在 0-0.95。heat 只用于**排升温词表**, 不直接进频道分数。这是刻意的抗噪设计。

6. **Google / Reddit / B站 是大盘不是垂类精调**。它们只做 corroborated 佐证徽章 (小幅加权),
   绝不主导评分。B站排行还因 RSSHub 不带播放量退化成榜位代理。Reddit 常被限流拿不全。
   这三路的价值是「外部世界是否也在谈这个词」的**旁证**, 不是决策主力。

7. **`onwave` 的证据视频与命中词是「同一条」耦合**。升温词从 RSS 提炼, 命中判定又回到同一批 RSS,
   存在轻度循环 (一条爆款既定义了浪词又证明了自己在浪上)。多日累积后浪词来自**历史**、命中判定看**当下**,
   循环自然解开。单日下这是已知的乐观偏差。

---

## 7. run_radar 集成说明 (怎么接, 不要自己改 run_radar)

**现状**: run_radar 已有起势层集成范式可完全照抄 (`src/run_radar.py:171` `run_rss_momentum` + `:820` `fuse_momentum`)。
浪层按同一范式挂载, 以下是给总控工兵的接线图 (**本工兵不改 run_radar**):

### 步骤 1 — 采集 (放在 momentum 的 RSS 采集之后, 复用同一份当日 RSS)
```
python3 src/collect_trends.py --date <today>
# → 追加 data/trends/<today>.jsonl (四路独立 try, 任何失败只记原因不中断)
# 零新增视频下载; B站硬底 3.5s / Reddit >=1s / Google 0.5s
```

### 步骤 2 — 打分 (纯本地, 紧跟采集)
```
python3 src/trends.py \
  --trends data/trends/<today>.jsonl \
  --pool   data/pool/creator_pool.jsonl \
  --rss    data/rss/<today>.jsonl \
  --out-scores data/runs/trends-v1/trend_scores.json \
  --out-terms  data/runs/trends-v1/rising_terms.json

python3 src/breakout.py \
  --pool data/pool/creator_pool.jsonl \
  --rss  data/rss/<today>.jsonl \
  --out  data/runs/trends-v1/breakout_scores.json
```
两个打分器都按 `channel_url` / `channel_id` 输出, 与 momentum 同构, 便于 join。

### 步骤 3 — 融合进展示层 (建议, 供总控决定)
`trend_scores.json` / `breakout_scores.json` 都是 `channel_url` 可 join 的行。
建议在 `fuse_momentum` 之后追加一步 (或写一个平行的 `fuse_trends`), 把两列并进 `scored` 行:
- `s["trend"]  = trend_by_url[url]["trend_score"]`
- `s["trend_chaser"] = ...["trend_chaser"]`
- `s["breakout"] = breakout_by_url[url]["breakout_score"]`

**放大器口径 (与 momentum 铁律一致)**: 浪层是**放大器不是独立加分**。若要进 `potential`,
建议乘性温和放大且**只放大不惩罚未上浪者** (未上浪 trend=0 时**不要**压 fit, 因为「没上浪」≠「差」,
这与 momentum 的 `momentum=neutral → 不动 fit` 是同一个保护逻辑)。例如:
`potential *= (1 + trend_gain × trend_score)`, trend_score=0 时不动。`trend_chaser=True` 的可在展示层加⚠️徽章。

### 步骤 4 — 展示层加列 (`CSV_COLUMNS` @ `run_radar.py:210`, `build_table_rows` @ `:223`)
在「潜力分」后建议加 3 列:
- **浪层分** ← `s.get("trend")`
- **破圈比** ← `s.get("breakout")`
- **蹭热点** ← `"⚠️" if s.get("trend_chaser") else ""`

同步在 `build_table_rows` 的 dict 里加对应键, bitable schema (第 19 列区) 相应扩列。

### 失败降级 (照抄 momentum 的优雅降级)
- 采集任一路失败 → 该路跳过记原因, `data/trends/<today>.jsonl` 仍产出其余路。
- 打分器读不到 trends 文件 → 建议 fuse 层把 `trend` 全填 0 (潜力分不动, 排名不变), 与 momentum 缺失时全中性同理。
- **绝不让浪层的缺失影响 fit / potential 主排名** —— 它永远只是可选的放大器与展示列。

---

## 文件清单
- `src/collect_trends.py` — 四路采集 (pool_wave / google / bilibili / reddit)
- `src/trends.py` — 升温词表提炼 + trend_score (match×early×onwave) + trend_chaser
- `src/breakout.py` — 破圈比 (views ÷ subs, log 归一)
- `data/trends/2026-07-07.jsonl` — 当日四路原始快照 (379 条, append 友好)
- `data/runs/trends-v1/trend_scores.json` — 全池 2422 行浪层分
- `data/runs/trends-v1/rising_terms.json` — 6 垂类升温词表 (含证据/佐证徽章/进场分位)
- `data/runs/trends-v1/breakout_scores.json` — 459 频道破圈比
- `data/runs/trends-v1/TRENDS.md` — 本报告
