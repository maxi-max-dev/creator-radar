# 起势层 momentum v1: 方法 · 验证 · 融合 · 局限

> 三层雷达的第二层。潜力 = 对的人(fit, 已验证 recall@top5% = 23%, 4.6 倍随机) × 对的时机(momentum, 本层) × 敢被验证(预测记分板, 已上线)。
> 一句话判决: **momentum 可以用。** 它在活跃频道上呈平滑的判别梯度(标准差 0.247, 82 个不同取值), 表面效度经得起逐条肉眼核对, 且与 fit 正交(携带独立信息), 融合后能在 fit 分层内部把'此刻在爆'的创作者顶上来。
> 数据源: 2026-07-07 一次快照, 全池 1106 频道, 其中按 fit 排名前 350 拉了 YouTube 官方 RSS。

---

## 一、核心思路(一句话)

某频道最近视频的**播放速度(views/天)** 显著超过**它自己所有已知视频的 views/天 中位数** = 起势信号。

只比频道对自己历史的相对爆发, **绝不跨频道横向比绝对播放量**(大频道天然高, 那是 fit 层甜点函数管的维度)。一个 5 万粉的频道突然某条片子跑出自身 15 倍速度, 比一个 500 万粉频道的日常百万播放更是'起势'。

## 二、方法(打分管道)

数据来源是免费无 key 的 YouTube 官方频道 RSS(`youtube.com/feeds/videos.xml?channel_id=UCxxx`), 每频道返回最近约 15 条视频的标题 / 发布时间 / `media:statistics` 播放数。本次 350 频道 100% 拿到播放数与发布日期, 中位 15 条/频道。

`src/momentum.py` 的每频道管道:

1. **每条视频算 `views_per_day = view_count / max(天龄, 地板天数)`**。地板天数(`min_days_since_upload`=2.0)防刚发视频分母虚小把速度顶穿。
2. **频道历史基线 = 所有已知视频 views/天 的中位数**(shorts 降权后)。中位数抗单条病毒视频。
3. **起势窗口 = [成熟度门槛, 近期窗口] = [3.0 天, 45 天]**。窗口内每条算 `爆发比 = views/天 ÷ 基线中位`。两端外的视频只进基线不当突破点。
4. **log 压缩(底 2) + 封顶(8x) + 归一化到 [0,1]**。防单条现象级视频把分数打爆, 8x 已是'现象级起势'再高不必区分。
5. **时间衰减**: 突破分按发布距今天数指数衰减(半衰期 30 天), 昨天爆的比 40 天前爆的更值钱。
6. **频道 momentum = 窗口内加权突破分的最大值**(取最亮那条爆款代表起势)。

### 两个关键设计决策(都经实测定夺)

- **成熟度门槛 `min_maturity_days` = 3.0 天**(关键旋钮)。第一版只有分母地板, 结果 top20 被'发布 0.3 天、播放几千'的新帖霸榜——因为分母极小 views/天 虚高, 且这些视频还没经足够时间证明真实观众拉力。做了 floor∈{1.5,3,5,7} + maturity∈{2,3,4} 的敏感度扫描后定为: **只有天龄 ≥ 3 天的视频才有资格当爆发信号**(仍进历史基线)。3 天=新到能代表当下、又老到 views/天 可信的平衡点。定了之后 top20 变成'3-5 天龄、播放数真正跑赢自身'的可信爆款。
- **shorts 单独降权 0.5**。短视频播放量体系和长视频不同(推荐流打法, 动辄百万但不代表内容势能), 直接混进长视频历史会污染中位。降权而非剔除, 因为纯 shorts 创作者的爆发也是真信号。RSS 不带时长, 用标题/链接含 `#shorts`/`/shorts/` 的文本启发式判定(本次 37.8% 视频命中, 与守零下载红线兼容)。

### 数据不足绝不冤杀

覆盖度分三档, 缺数据一律给中性分而非 0 分:

| coverage | 含义 | momentum |
|---|---|---|
| `ok` | 有历史 + 近期窗口内有可评视频(272 个) | 真实计算值 |
| `stale` | 有历史但近期窗口内无上传=频道沉寂(78 个) | 0.0(它确实没在起势) |
| `none` | 未拉 RSS(fit 排名 350 名开外, 756 个) | 中性 0.3(未知不是坏, 但也拿不出起势证据) |

`stale` 归零是刻意的: 一个 3 个月没更新的频道, 无论多大牌, 当前都不是'正在起势'。`none` 给中性分则保证 fit 排名靠后、没拉 RSS 的频道不会因缺数据被融合公式压下去。

## 三、四项验证

### a. 区分度检验(证明不是全零/全一的摆设)

活跃频道(coverage=ok, n=272) momentum 十分位:

| 分位 | P0 | P10 | P20 | P30 | P40 | P50 | P60 | P70 | P80 | P90 | P100 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| momentum | 0.000 | 0.204 | 0.355 | 0.444 | 0.534 | 0.609 | 0.686 | 0.744 | 0.802 | 0.874 | 0.933 |

均值 0.573, 标准差 0.247, 两位小数去重后 **82 个不同取值**。这是一条平滑的判别梯度, 不是三个桶。底部(P0-P10=0)是窗口内有片但都没跑赢自身的真平频道, 顶部(P90+>0.87)是有真爆款的。完整表见 `top20_evidence.md`。

### b. 表面效度(top20 证据, 人看一眼就懂)

`top20_evidence.md` 给出 momentum top20 的逐条证据: 频道 · 最亮爆款视频标题 · 播放数 · 发布天数 · 超自身中位倍数。三个代表例:

| 频道 | fit# | momentum | 爆款视频 | 播放数 | 天龄 | 超自身中位 |
|---|---|---|---|---|---|---|
| Checkpoint Chaser | 159 | 0.933 | 30,000KM later..the FINAL ride to KUALA LUMPUR | 23,556 | 3.0d | 15.7x |
| ソエジマックスのモトブログ | 69 | 0.916 | カワサキ ZX-6R 乗ってみた！【モトブログ】 | 40,905 | 3.8d | 12.0x |
| Adventure Bike Rider | 266 | 0.918 | ABR Festival 2026 - After Movie | 3,350 | 3.7d | 132x |

每一行都能一眼讲清'为什么它亮': 一条明显跑赢自己日常水平的近期视频。**这是本层最强的说服点**——不是黑箱分数, 是可核对的爆款证据。

### c. 时间回滚 mini-demo(诚实版)

读了 vault `AI先锋大赛-影石/AI先锋大赛-名册时间线与JD情报.md`(2026-07-06 Wayback 台账, 含影石官方名册的精确签约时间线)。挑了三位名册大使做检验, 原始数据存 `roster_rollback_demo.json`:

| 大使 | 订阅 | fit# | momentum | 结论 |
|---|---|---|---|---|
| **Adam Riemann** | 982k | 262 | **0.898** (ok) | 点亮: "Croatia to Montenegro"探险长片 9.6 万播放=自身 14 倍, 4.6 天龄 |
| Fabio Wibmer | 8.04M | 1003 | 0.0 (stale) | 沉寂: 最近上传 122 天前, 当前无起势 |
| Anne Gumiran | 1540 | 971 | 0.0 (stale) | 同名小号(疑非本人), 278 天没更新, 数据不足 |

**诚实声明(重要)**: 这**不是真正的时光机**。我只有 2026-07 当前快照的播放量, 没有历史逐日播放速度, 无法还原'上榜前那一刻 momentum 是否已点亮'。这里能做的只是'对已知登上官方名册的创作者, 当前 momentum 是否点亮'的表面效度检验。

即便如此, 结果本身有信息量: 三位名册大使里只有 Adam Riemann 当前在起势, 两位老牌顶流当前沉寂。这恰好印证三层叙事的分工——**fit 找对的人**(名册大使多是已签顶流, 被甜点函数刻意压到中段, 所以 Wibmer 才 fit#1003), **momentum 只管'此刻谁在爆'**, 两层正交各管一维。真正的时光机要靠每日 RSS 快照攒历史(见下文明日积累计划)。

### d. 阴性对照(随机组 vs fit-top 组)

对照 fit-top 100 与随机 100(500 次重采样, 取自已拉 RSS 的池)的 momentum 均值:

| 组 | momentum 均值 |
|---|---|
| fit-top 100 | 0.437 |
| 随机 100 (500 次重采样) | 0.444 |
| **差值 (fit_top − random)** | **−0.007** |

**读法(反直觉但正是要的)**: 差值≈0 说明 momentum 和 fit **正交**。这不是坏事, 是设计目标: 若 momentum 强相关 fit, 它就是 fit 的影子, 白搭一层。差值接近零证明 momentum 携带 fit 没有的**独立信息**——它能在任何 fit 分层内部再排序。融合验证(见下)也印证: 同为 fit-top100 的频道, momentum 高的(Traffic Channel 0.89)升 71 位, momentum=0 的沉寂频道(Two Wheel Cruise)跌 569 位。

## 四、融合公式(potential = fit × 起势)

选**乘性放大器**而非加性:

```
potential = fit_score × (1 + gain × (momentum − pivot))
gain = 0.6,  pivot = 0.3 (=neutral)
```

- **为什么乘性**: '对的时机'是对'对的人'的**放大器**, 不是独立加分项。一个 fit 极低的人再火, 也不该被这层单独捞进推荐(乘性下 fit≈0 → potential≈0)。加性会让一个纯蹭热点、跟品牌毫不相干的爆款账号靠 momentum 硬挤进来。
- **为什么减 pivot**: momentum 高于 pivot(=中性 0.3)才正向放大 fit, 低于则轻微压制。保证**'数据不足=中性=不动 fit 排名'** 这条铁律: coverage=none 的频道 momentum=0.3=pivot, 放大量恰为 0, potential==fit, 排名纹丝不动。
- **gain=0.6 是运营策略旋钮**: momentum 满分时最多把 fit 分放大 (1+0.6×0.7)≈1.42 倍。调大=更激进押注时机, 调小=更看重长期契合。

融合效果(全池 potential 排名 vs fit 排名, |排名差| 中位 76):
- **升**: 有真爆款的在题创作者顶上来。Traffic Channel fit#90→pot#19, Nacho(正例)fit#232→pot#48。这正是'对的人 AND 对的时机'的交集。
- **降**: 沉寂频道(stale, momentum=0)沉底。Two Wheel Cruise fit#98→pot#667。在题但当前不更新, 对'现在就合作'的目的正确降权。
- **正例校验**: 26 个池内正例里 **15 个在 potential 排名中上升**, 且上升的都是当前有爆发的(Moto Feelz #48→#23, RideWithRea #32→#13); 当前 momentum 弱的正例(Lali 0.23)下降。这说明融合在'已知对的人'里进一步筛出了'此刻最热'的。

## 五、与 backtest 官方指标的隔离(铁律)

**起势层绝不进冻结考试池的官方指标。** 实现上由结构保证:
- `backtest.py` 直接调 `score_pool()`, 从不 import `fuse_momentum`, 全文件零 momentum 引用。
- 融合逻辑 `radar_lib.fuse_momentum()` 是独立函数, 只被产品路径(`score.py --momentum` / 将来 `run_radar`)调用。
- 实测: 本 worktree 池(=exam_pool_v1, 1106 频道)跑 backtest, recall@top5%/@top10% 与官方 v1.2 **逐字节一致**(6/26=23% · 7/26=27%)。recall@top20% 数字不同(27% vs 官方 42%)是因为本 worktree 基于合并前代码, 配置还是六主题旧版(v1.1), 而官方 v1.2 是删除实验后的五主题新配置; 两者都跑在同一个 1106 冻结考试池上(勘误 2026-07-07: 本行原写「官方 v1.2 跑在 2422 大池」是错误归因, 由挑刺官 Round 1 刺#1 抓出, ablation fresh-baseline 六主题在 1106 上 top20%=27% 与本 worktree 一致, 证实差异来自配置版本而非池子)。与 momentum 无关——momentum 一个字都没碰 backtest。

## 六、run_radar 集成说明

已在 worktree 写好, 待合并:
- `config/insta360.json` 新增 `momentum` 节, 每个旋钮带 `_notes` 说明。
- `radar_lib.fuse_momentum(scored, momentum_path, cfg)`: 就地给每个 scored 行加 `momentum` / `momentum_cov` / `potential` 字段 + `potential_rank` / `potential_pct`。momentum 文件不存在时优雅降级(全中性, potential==fit, 排名不变)。
- `score.py` 新增 `--momentum` 与 `--rank-by {fit,potential}` 参数, 向后兼容(不给 --momentum 时行为完全不变)。

**接进 run_radar.py 每日主链的建议**(三步, 不改 backtest):
1. 每日采集后, 拉 top-N RSS: `python3 src/collect_rss.py --config <cfg> --pool <POOL> --ranked <当日 ranked.json> --out data/rss`。
2. 算 momentum: `python3 src/momentum.py --config <cfg> --pool <POOL> --rss data/rss/<今日>.jsonl --out data/runs/daily/<今日>/momentum_scores.json`。
3. 在 `run_radar.main()` 里 `scored = score_pool(...)` 之后, 插一行 `fuse_momentum(scored, momentum_path, cfg)`。然后:
   - **展示层**: CSV/bitable schema 新增两列「起势分」(momentum, 独立列)+「潜力分」(potential, 融合列), 从 `build_table_rows` 取 `s["momentum"]` / `s["potential"]`。日报/推荐卡按 potential 排, 但保留 fit 分独立可见(叙事需要三层各自透明)。
   - **记分板**: picks 快照同时记 momentum, 到期结算时可回看'当初 momentum 亮的是否真的兑现'(闭环)。
   - **fit 的 rank/pct 保持不动**(仍是 score_pool 原值), 只是多了 potential_rank 供产品排序。

## 七、局限(诚实清单)

1. **单快照播放量的偏差**: 只有一次抓取的累计播放数, 没有真实逐日增速。views/天 = 累计播放 ÷ 天龄, 是**平均**速度, 会把'首日爆、之后平'和'匀速涨'混为一谈。真增速要靠时间序列。
2. **老视频均值化问题**: 历史基线用频道全部已知视频(RSS 只给最近约 15 条)的中位。若频道最近整体在涨(水位抬升), 基线也水涨船高, 会**低估**真实起势; 反之若频道在衰退, 基线偏低会**高估**。基线是滑动的近期中位, 非频道生涯真基准。
3. **成熟度门槛的双刃**: 3 天门槛把'刚发就爆'的真·病毒视频挡在窗外 3 天(它先进基线)。代价是最新鲜的爆发要晚 3 天才被 momentum 捕捉。这是为压制新帖分母虚高付的必要代价, 但确实牺牲了一点时效。
4. **tiny-baseline 爆发比失真**: 极少更新的频道历史中位 views/天 接近零, 任何视频都显示天文数字爆发比(实测 Bicycle Touring Pro 1373x, 7 个频道 >100x)。`burst_cap=8x` 的 log 压缩已把它们都收进合理分带(1373x→0.92 而非顶到 0.99, 因衰减也参与), **cap 正常工作**, 但这类频道的绝对爆发比数字不可直接采信, 只看归一化后的 momentum。
5. **shorts 启发式不精确**: 无时长数据, 靠标题/链接文本判 shorts。会漏判没打 #shorts 标签的短视频, 也可能误判标题带 short 的长视频。降权 0.5 而非硬切降低了误判代价。
6. **RSS 只回最近约 15 条**: 基线样本量小(中位 15), 中位数在小样本上有波动。更长的历史要靠每日快照累积。
7. **回滚 demo 不是真时光机**: 见验证 c——无历史增速, 无法证明'上榜前 momentum 已点亮', 只能做当前快照的表面效度。

## 八、明日积累计划(前瞻: momentum 会越来越准)

本层最大的杠杆是**时间**。今天是单快照, 从今天起每日一次 RSS 快照(append 到 `data/rss/<date>.jsonl`, 采集器已写成 append 友好)会逐步解锁:

- **真·逐日增速**(治局限 1): 同一视频跨天的播放量差 = 真实日增速, 取代'累计÷天龄'的平均近似。一周后就能算'某视频这两天日增速突然翻倍'。
- **加速度信号**(momentum 的导数): 不只看'跑得快', 还能看'越跑越快'——增速本身在上升的频道是更早期的起势信号。
- **真时光机回滚**(治局限 7): 快照攒够后, 可对未来新登名册的创作者做真正的'上榜前 momentum 时间线'验证, 而非本次的当前快照代用。
- **stale 更精准**: 多日快照能区分'刚好这几天没更'和'真的弃坑', 让 stale 归零更可信。
- **基线稳定化**(治局限 6): 累积历史让频道基线不再受 RSS 15 条窗口限制。

一句话: momentum v1 今天已能用(判别梯度 + 可核证据 + 正交性 + 融合有效), 而它是三层里**唯一会随每日快照自动变强**的一层。

---

## 附: 文件清单

- `src/collect_rss.py` — YouTube 官方 RSS 采集器(纯标准库, 零下载, 节流 0.5s, append 友好)
- `src/momentum.py` — 起势打分器(全部旋钮读 config.momentum)
- `src/momentum_validate.py` — 四项验证数据 + top20 证据表生成器
- `radar_lib.fuse_momentum()` / `score.py --momentum` — 融合进产品路径
- `data/rss/2026-07-07.jsonl` — 本次 350 频道 RSS 原始快照(先存后洗的'存')
- `data/runs/momentum-v1/momentum_scores.json` — 全池 momentum 分 + 证据字段
- `data/runs/momentum-v1/top20_evidence.md` — 十分位 + top20 + 阴性对照(可复现)
- `data/runs/momentum-v1/roster_rollback_demo.json` — 回滚 demo 原始数据
- `config/insta360.json` `momentum` 节 — 全部策略旋钮 + notes
