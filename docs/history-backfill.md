# FLOW 历史回补与按日查看

## 目标范围

- 请求范围：`2024-08-01` 至 `2026-07-21`。
- 交易日来自上交所融资融券官方日表，不用自然日或本地节假日表猜测。
- 首个目标日前额外读取最多 5 个交易日，只用于计算 `1d/3d` 基准，不把这些上下文日冒充为请求范围内的归档。
- GitHub Actions 工作流：`.github/workflows/backfill-history.yml`。任务可重复执行；已经成功写入 `historical_snapshot` 的交易日会跳过，失败后可续跑。

## 官方覆盖边界

| 数据域 | 2024-08-01 起逐日回补 | 原始证据 | 页面历史日表现 |
|---|---:|---|---|
| 上交所 ETF 份额 | 是 | `commonQuery.do` 的逐日 JSON | 计算单只/分类/全市场份额变化 |
| 深交所 ETF 份额 | 否 | 当前列表 JSON + XLSX 仅能从启用日向前积累 | 历史日明确显示“官方日列表不支持历史回填” |
| 上交所两融 | 是 | `queryMargin.do` 的逐日 JSON | 与深交所合并展示余额、买入额、融券余额 |
| 深交所两融 | 是 | `ShowReport/data` 的逐日 JSON | 与上交所合并展示 |
| 港交所互联互通 | 部分 | 中文历史页的 `data_tab_daily_YYYYMMDDc.js` | 有文件时展示；过期 404 时显示“休市或无披露”，不填造数值 |

港交所中文页面的隐藏字段 `CSMDailyStatDropDownMonths=7`，说明页面只提供最近 7 个月每日文件。`2024-08-01` 的中文文件实测返回 HTTP 404。因此，早期港股通数据无法从用户指定的当前官方页面真实回填；系统会保存该 404 响应、请求日期、URL、观测时间和 SHA-256，作为“官方已不保留”的可审计证据。

## 存储结构

```text
R2 flow
└── market-data/
    ├── raw/{source}/{dataset}/{requestedDate}/{sha256}.{ext}
    ├── normalized/{source}/{dataset}/{requestedDate}/{rawSha}/{part}.csv
    ├── history/{tradeDate}/{snapshotSha}.json
    ├── history-manifests/{runId}/{tradeDate}.json
    ├── history-pending-manifests/{runId}/{tradeDate}.json
    ├── collection/{runId}/{asOf}/{releaseId}.json
    └── derived/etf-flow-state/{classificationVersion}/...

D1 flow
├── historical_snapshot
│   └── 每个交易日一行：日期、快照哈希、R2 key、质量计数、来源数
└── market_history_daily
    └── 每个交易日一行：沪深两融汇总、南北互通汇总、可用性标记、来源快照哈希
```

数十万 ETF 明细不重复塞入 D1。原始文件、规范化 CSV 和页面历史快照放在 R2；D1 只保存按日期定位所需的小索引。`market-data/` 不设置自动过期，其中 `market-data/raw/` 还启用无限期 Bucket Lock，防止官方原件被覆盖或误删。

PCF 成分表体量较大：只在上传和导入链路中 gzip，R2 以 `.csv.gz` 形式保留规范化证据，人工复核时解压即可；哈希始终针对解压后的 CSV 原文计算。

两融与互通走势图使用 `market_history_daily` 紧凑索引，避免每次打开页面读取数百个 R2 快照。索引值逐日从已通过质量门禁的不可变历史快照提取，可用 `scripts/rebuild_remote_market_history.py --apply` 从 R2 原件重建；日常 09:15 任务在归档当日快照时自动追加。港交所已下架的早期文件保持 `connect_available=0`，不使用相邻日或第三方数据填补。

历史导入按单个原始工件提交：`scripts/publish_history_artifacts.py` 先逐个写入 raw/normalized 对象和 `collection` 状态，最后用 `finalize=true` 请求核对预期工件集合。只有所有状态均为 `VERIFIED`/`PUBLISHED` 且快照质量门禁通过时，才写入 `historical_snapshot`；空响应会标记为 `PENDING`，不会被当作成功数据。重复请求使用相同哈希键，可从任意失败工件继续。

ETF 长周期使用单独的紧凑派生状态：从通过质量门禁且哈希一致的历史快照提取单只 ETF 每日份额变化，保留最近 260 个交易日。1 日、3 日、1 周、2 周、1 月、3 月、6 月和 12 月均为每日变化逐日累计；今年以来为当年每日变化累计。派生状态使用内容哈希路径保存，并由带 SHA-256 的指针定位，加载时再次复验。原始文件始终是事实来源，派生状态可以随时重建。

按当前文件大小估算，约 500 个交易日的回补和约 1.3 MB 的滚动 ETF 状态远低于 R2 免费层 10 GB 存储量。

## 按日查看

- 页面顶部使用日期输入框或前后交易日按钮。
- 直接地址：`https://flow.xiaoker.com/?date=2025-01-02`。
- 可用日期 API：`GET /api/v1/history/dates`。
- 选择非交易日时，页面显示此前最近的已归档交易日，并明确提示日期发生了回退。
- 只有 `quality_fail=0` 且 R2 快照通过结构校验的日期会进入可选列表。

## 自动 QA

回补日必须通过以下稳定规则后才写入：

1. 上交所 ETF、沪深两融的 `trade_date` 必须等于请求日；
2. ETF 代码为 6 位、业务主键唯一、份额非负；
3. 两融总额与融资/融券分项在官方舍入容差内一致；
4. 每个原始响应和规范化 CSV 均保存 SHA-256，Worker 收到后重新计算；
5. 历史快照日期必须与包内 `asOf` 一致，质量失败的快照拒绝入库；
6. 同一日期重复回补使用内容哈希键，D1 日期索引原子更新，可安全续跑。
7. ETF 派生状态拒绝重复日期、重复代码、非有限数值和分类版本不一致；
8. 官方原始对象按内容哈希命名，写入前先检查对象；Bucket Lock 启用后重试不会覆盖已有原件。
