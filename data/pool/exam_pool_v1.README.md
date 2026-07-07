# exam_pool_v1.jsonl 冻结考试池（永不改动）

这是达人雷达盲测回测的**冻结考试池**，2026-07-07 从合并前的 `creator_pool.jsonl` 原样拷贝而来。

- 规模：1106 频道，26 个隐藏正例（20 中腰部 + 6 巨星）。
- md5：`11618f1666f09cb6c2984f53f503d68d`（拷贝时与源池逐字节一致）。
- **纪律**：本文件永不改动。所有对外官方指标（召回率、中位百分位）只在这份池子上复现，保证评委 clone 下来跑出的数字与材料一致。
- 生产池 `creator_pool.jsonl` 每天生长（采集器 refresh + discover + 扩容合并），与考试池**解耦**。两池分工：考试池量指标，生产池找达人。

复现命令：

    python3 src/backtest.py --config config/insta360.json --pool data/pool/exam_pool_v1.jsonl --out data/runs/my-run/

官方 baseline 产物：`data/runs/2026-07-07-official-v1.2/`。
