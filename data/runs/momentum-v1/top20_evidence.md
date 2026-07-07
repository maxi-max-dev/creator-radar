# 起势层(momentum) top20 证据表 + 验证数据

> 自动生成 (src/momentum_validate.py)。数据源: 全池 1106 频道, 其中 fit 排名前 350 拉了 YouTube 官方 RSS。

覆盖度: `ok`(活跃有近期视频)=272 · `stale`(有历史但近期无上传, momentum=0)=78 · `none`(未拉 RSS, 给中性分 0.3)=756

## a. 区分度检验: 活跃频道 momentum 十分位

证明它不是全零/全一的摆设, 是一条平滑的判别梯度。

| 分位 | momentum |
|---|---|
| P0 | 0.000 |
| P10 | 0.204 |
| P20 | 0.355 |
| P30 | 0.444 |
| P40 | 0.535 |
| P50 | 0.609 |
| P60 | 0.686 |
| P70 | 0.744 |
| P80 | 0.802 |
| P90 | 0.874 |
| P100 | 0.933 |

活跃频道 n=272 · 均值 0.573 · 标准差 0.247 · 两位小数去重后有 82 个不同取值。

## b. 表面效度: momentum top20 证据

每行都能一眼看懂为什么它亮: 频道 · 最亮的近期视频标题 · 播放数 · 发布天数 · 超频道自身中位的倍数。

| # | 频道 | fit# | momentum | 爆款视频 | 播放数 | 发布天数 | 超自身中位 | 短视频 |
|---|---|---|---|---|---|---|---|---|
| 1 | Swifty | 248 | 0.933 | FABIO KRIEGT ÄRGER VON KLAUSA | 25,129 | 3.0d | 10.27x | 是 |
| 2 | Checkpoint Chaser | 159 | 0.933 | 30,000KM later..the FINAL ride to KUALA LUMP | 23,556 | 3.0d | 15.77x |  |
| 3 | Wheelie Good TV | 126 | 0.933 | ABR Festival 26 / A personal reflection / I  | 3,772 | 3.0d | 13.06x |  |
| 4 | Avinash HS | 331 | 0.931 | Weekday Morning Ride to PENUKONDA FORT / TWO | 5,582 | 3.1d | 10.01x |  |
| 5 | SMZ | 298 | 0.929 | VLOG/MOTOVLOG KHKCNB 21 : "China Bata Bike K | 15,428 | 3.2d | 11.63x |  |
| 6 | MOTOBLADE | 29 | 0.926 | Indian Chieftain Dark Horse 125th Anniversar | 2,804 | 3.3d | 22.09x |  |
| 7 | jmac86 motovlog | 347 | 0.925 | INSTALLING RCB BRAKE SYSTEM to JMAC AEROX /  | 7,489 | 3.4d | 31.89x |  |
| 8 | Bicycle Touring Pro | 24 | 0.920 | 600 MILES ACROSS MONTANA / Solo Bike Touring | 6,687 | 3.6d | 1375.95x |  |
| 9 | RocKers | 107 | 0.919 | Řeka Jihlava | 708 | 3.6d | 17.98x | 是 |
| 10 | Adventure Bike Rider | 266 | 0.918 | ABR Festival 2026 - After Movie | 3,350 | 3.7d | 132.25x |  |
| 11 | ソエジマックスのモトブログ | 69 | 0.916 | カワサキ ZX-6R 乗ってみた！【モトブログ】Kawasaki ZX-6R revie | 40,905 | 3.8d | 11.99x |  |
| 12 | 1000PScom - World of Motorcycles | 68 | 0.912 | Can you drift like that 🫪 #ducatihypermono69 | 41,134 | 4.0d | 13.99x | 是 |
| 13 | Kasklı Motorcu | 178 | 0.907 | BMW S1000RR BAHÇE SÜSÜ | 43,345 | 4.2d | 14.71x | 是 |
| 14 | Soy Blak | 236 | 0.901 | VIVIMOS LA PRIMERA CAÍDA del VIAJE... / +100 | 20,009 | 4.5d | 10.55x |  |
| 15 | Adam Riemann | 262 | 0.898 | Riding from Croatia to Montenegro unsupporte | 95,944 | 4.6d | 14.17x |  |
| 16 | Jesse Melamed | 184 | 0.897 | The Mullet Isn't For Going Fast. That's Why  | 14,367 | 4.7d | 27.23x |  |
| 17 | Sikandar Khan's MotoVerse | 245 | 0.896 | I Had 72 Hours to Get Back Home / Urdu MotoV | 2,009 | 4.8d | 15.75x |  |
| 18 | GoPro Bike | 13 | 0.894 | GoPro: BEST TRACK EVER! Jackson Goldstone 20 | 97,999 | 4.8d | 18.85x |  |
| 19 | Lavi & Ollie | 306 | 0.893 | Local fruits for lunch 😋 #india #travel #adv | 137,822 | 4.9d | 13.26x | 是 |
| 20 | Traffic Channel | 90 | 0.893 | HEM SUÇLU HEM GÜÇLÜ! Türkiye'de Yaşanan Moto | 35,040 | 4.7d | 7.93x |  |

## d. 阴性对照: fit-top 组 vs 随机组 momentum 均值

关键预期(反直觉): momentum 应该和 fit **正交**(它们量的是不同维度: fit=对的人, momentum=对的时机)。
若两组均值几乎相等, 说明 momentum 是一个**独立**信号, 能在任何 fit 分层内部再排序, 而不是 fit 的影子。

| 组 | momentum 均值 | n |
|---|---|---|
| fit-top 100 | 0.437 | 100 |
| 随机 100 (500 次重采样, 取自已拉 RSS 的池) | 0.444 | 100×500 |
| 差值 (fit_top − random) | -0.007 | — |

**读法**: 差值接近 0 = 正交性成立 = momentum 携带 fit 没有的新信息(这正是我们要的)。反例(若 momentum 强相关 fit)会是浪费一层。

