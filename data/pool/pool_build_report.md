# YouTube 摩托垂类候选池构建报告

调研日期：2026-07-06
方法：yt-dlp 真实抓取（`--flat-playlist --playlist-items 1-5 -J` 拉频道元数据 + `youtubetab:approximate_date` 拉近似上传时间；`ytsearch30:<query>` 扩池搜索）

---

## 1. 池子总量

- **总频道数：1085**
  - 正例（is_positive=true）：26
  - 种子频道（source=seed）：152
  - 扩池搜索发现（source=search:*）：907
- 请求预算使用：1159 / 2500

---

## 2. 订阅数分布

| 区间 | 频道数 | 占比 |
|---|---|---|
| <1万 | 381 | 35.1% |
| 1-10万 | 322 | 29.7% |
| 10-100万 | 301 | 27.7% |
| >100万 | 80 | 7.4% |
| null (拿不到) | 1 | 0.1% |

---

## 3. 正例覆盖情况

### 3.1 中腰部正例（insta360_midtail_positives.md）
- 输入文件中标记的中腰部正例（含 Chaseontwowheels 超区间参考项）：20 个
- 成功拉取到元数据：**20 / 20**

### 3.2 巨星正例（insta360_partners.md）
- 输入文件中列出的巨星/运动员正例：28 个
- 其中有个人 YouTube 频道并成功拉取元数据：**6 / 6**
- 无个人 YouTube 频道（已跳过，非引擎可发现对象）：**22 个**

跳过明细（无个人频道原因）：

| 姓名 | 跳过原因 |
|---|---|
| Marc Márquez | 官方车手账号为 Instagram，无个人 YouTube 频道 |
| Jonathan Rea | 官方渠道为 X/Twitter，无个人 YouTube 频道 |
| Alex Lowes | 仅列 Kawasaki Racing Team 车手身份，无个人 YouTube 频道 |
| Robbie Maddison | 证据链接是 Insta360 官方频道视频(watch?v=h10_xNrE-B8)，非其本人频道 |
| MotoAmerica | 赛事级合作非个人创作者，跳过 |
| Shaun White | 官方社媒多平台但未列 YouTube 频道链接 |
| Qi Guangpu | 中国运动员，未列个人 YouTube 频道 |
| Okamoto Keiji | 残奥运动员，未列个人 YouTube 频道 |
| Walker Shredz Woodring | 未列个人 YouTube 频道链接 |
| Zhang Jiahao | 未列个人 YouTube 频道链接（证据为 Insta360 blog tag 页） |
| Zhang Shupeng | 未列个人 YouTube 频道链接 |
| Brad Simms | BMX 职业车手，未列个人 YouTube 频道 |
| Valentin Delluc | 出现在摩托部分之外的骑行段落引用中(Wings vs Wheels联动)，未列个人频道，只有联动视频链接 |
| Sportive Cyclist | 链接为 sportivecyclist.com 官网测评文章，未给出对应 YouTube 频道 URL，无法确认是否为频道形式 |
| Noble Z | 潜水摄影师，未列个人 YouTube 频道 |
| Stephen Friedman | 制片人/前职业冲浪手，证据链接是 Insta360 官方频道视频(watch?v=pkaz5yabtlQ)，非其本人频道 |
| Cache Bunny | 个人官网 cachebunny.com + Instagram，未列 YouTube 频道 |
| Will Smith | 演员/音乐人，无相关个人 YouTube 频道信息 |
| Seemonkey360 | 渠道为 Facebook/Instagram，未列 YouTube 频道 |
| Pat Aldinger | 商业摄影指导，渠道为 Instagram/IMDb，未列 YouTube 频道 |
| David Franz | 音乐人，渠道为个人官网/Instagram，未列 YouTube 频道 |
| Karen X Cheng | 渠道为 TikTok，未列 YouTube 频道 |

---

## 4. 限流遭遇情况

- **全程零失败**：未遇到 429/bot 检测导致的永久跳过；所有尝试的频道均成功拉取或以 404/账号不可用 记录跳过。

---

## 5. 搜索词贡献明细

### 5.1 种子文件自带的搜索词（priority 2 输入，非本次执行，仅统计现状）

| 搜索词 | 种子池中的频道数 |
|---|---|
| motovlog | 26 |
| motorcycle touring | 26 |
| cycling vlog | 24 |
| adventure motorcycle riding | 18 |
| mtb pov | 17 |
| sportbike vlog | 16 |
| road cycling vlog | 9 |
| mountain biking | 7 |
| downhill mtb | 7 |
| enduro mtb pov | 7 |

### 5.2 本次扩池新增搜索词（priority 3，本次执行）

| 搜索词 | 新增去重后频道数 |
|---|---|
| motovlog españa | 21 |
| motovlog brasil | 25 |
| モトブログ | 25 |
| motovlog indonesia | 13 |
| motorrad vlog | 18 |
| moto vlog france | 21 |
| adv riding | 5 |
| supermoto | 19 |
| motorcycle camping | 15 |
| bike review pov | 17 |
| motovlog italia | 28 |
| motovlog deutschland | 20 |
| motorcycle vlog philippines | 19 |
| motovlog malaysia | 15 |
| moto vlog india | 16 |
| motorcycle diaries | 27 |
| cafe racer vlog | 19 |
| dirt bike vlog | 16 |
| motorcycle roadtrip | 15 |
| scooter vlog | 19 |
| motorcycle life vlog | 23 |
| biker vlog | 8 |
| motorrad tour | 20 |
| moto viaje | 15 |
| onboard motorcycle camera | 17 |
| motorcycle daily vlog | 17 |
| solo motorcycle travel | 12 |
| motovlog turkiye | 14 |
| motovlog vietnam | 19 |
| motorcycle helmet cam | 17 |
| moto vlog | 3 |
| motovlogger | 6 |
| royal enfield vlog | 23 |
| harley davidson vlog | 14 |
| motorcycle touring europe | 17 |
| motosiklet vlog | 16 |
| мотовлог | 21 |
| motovlog polska | 9 |
| moto vlog mexico | 19 |
| motovlog argentina | 11 |
| motovlog colombia | 26 |
| 바이크 브이로그 | 19 |
| 機車vlog | 27 |
| motorcycle gear review | 7 |
| ducati vlog | 17 |
| bmw gs adventure | 14 |
| ktm duke vlog | 16 |
| motorcycle commute vlog | 19 |
| motovlog thailand | 23 |
| moto camping | 5 |
| adventure bike touring | 8 |
| motorcycle travel documentary | 12 |
| motovlog pakistan | 17 |
| motovlog nepal | 12 |
| africa twin adventure | 11 |

---

## 6. 数据质量说明

- `subscribers` 为 null 的频道数：1 / 1085
- `description` 为 null 的频道数：80 / 1085
- `country` 为 null 的频道数：1085 / 1085（yt-dlp `--flat-playlist` 模式不返回频道国家字段，需要完整 about 页抓取才能获得，本次未对全量频道做该项重量级抓取，故绝大多数为 null，如实记录不猜测）
- `last_upload_date` 为 null 的频道数：158 / 1085（`youtubetab:approximate_date` 偶发不返回时间戳，已对positive优先做过重试，seed/search层保留真实null）

所有字段均来自 yt-dlp 对 YouTube 的真实请求返回，解析不出的字段一律写 null，未做任何猜测或编造。正例标记严格按输入文件 `insta360_midtail_positives.md`（positive_source=midtail）与 `insta360_partners.md`（positive_source=star）执行，未新增任何正例。
