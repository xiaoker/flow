# R0-D 只读生产证据读取面能力矩阵

日期：2026-09-07
阶段：PR B（仅实现读取面；不运行正式盘点）
基线：`41bb71da3199485ca45f6253916456a40e3d4405`

## 结论

现有管理接口可以分别读取单日历史快照、单个归一化 artifact、单日 source readiness、单个 pipeline window、单个 release artifact 和 recovery 状态，但不能在一个固定预算内给 R0-D 返回“日期 × 数据域 × 阶段”的闭合证据矩阵。特别是：

- 单一 `history-snapshot` 接口会读取 R2 对象 body，不能用于盘点器的默认路径；
- `history-artifact` 在没有 pointer 时会使用 `bucket.list()`，不能用于受限证据盘点；
- release artifact 接口会下载 body，且一次只接受一个 release/artifact；
- 现有状态接口不能同时标记 raw、normalized、model、两个 release slot 和 date-visible，也不能返回统一的 D1/R2 计量；
- 现有接口没有一个有界、稳定的日期 keyset 分页合约。

因此本阶段新增一个窄范围的 `GET /api/v1/admin/recovery-inventory` metadata endpoint。它复用现有 `INGEST_API_KEY` 鉴权，只查询已有表，只对已由 D1 pointer 或已通过 manifest 前缀校验的精确 R2 key 执行 `HEAD`，不调用 `get()` 或 `list()`。

## 能力矩阵

| 需求 | 现有能力 | 缺口/风险 | PR B 决定 |
| --- | --- | --- | --- |
| 单日历史 dashboard snapshot | `/api/v1/admin/history-snapshot` | 下载 R2 body；无法给多个域和 slot 做统一盘点 | 只读 D1 metadata，不复用 body 路径 |
| normalized artifact | `/api/v1/admin/history-artifact` | fallback 会 `bucket.list()` 并下载 CSV | 读取 `history_artifact_pointer` 的 key/hash/rowCount；不列桶、不下载 |
| release metadata | `/api/v1/admin/releases/artifact` | 单 artifact、下载 body；没有跨 slot/日期分页 | bounded `release_manifest` metadata，独立 dashboard / etf_penetration |
| source readiness | `/api/v1/admin/source-readiness` | 单日接口，未与 artifact、model、release 证据关联 | 按日期分页读取，并保留 provenance/unknown |
| pipeline status | `/api/v1/admin/pipeline-window/status` | 单 runKey，不能给日期矩阵 | 在日期候选查询中只读窗口日期；不改变租约 |
| model row presence | 页面/发布接口间接可见 | 没有只读的跨日期、跨市场行数摘要 | 对 `etf_daily`/`margin_daily`/`connect_daily` 做日期范围聚合并设硬上限 |
| date-visible | dashboard/release 读取路径 | 可见不能推断 penetration 完整 | 单独输出 `date_visible`，不提升 penetration 状态 |
| R2 key/hash | release/history metadata 已有 key | list/get 会放大资源和泄露范围 | 只用 D1 pointer/manifest 精确 key + `head()` |
| 分页 | 现有接口没有统一 cursor | OFFSET 会随历史增长变贵且不稳定 | opaque date keyset cursor，硬 limit 100 |
| D1 budget telemetry | 各接口未统一返回 | 无法证明 rows_written=0 | 汇总每次 D1 `meta.rows_read/rows_written`；缺失即 INCOMPLETE |
| R2 failure semantics | 各接口返回普通 503 | 盘点器可能把半页当完整 | 中途失败返回 `status=INCOMPLETE`、原因和 cursor，绝不 READY |

## 读取与预算边界

- 起止日期必填，最长自然日跨度 1,100 天；日期候选只来自低基数的控制面表。
- 默认每页 50 个日期，硬上限 100；使用 `data_date > cursor` 的 keyset 分页，没有 OFFSET。
- 单请求最多 12 条 D1 读查询、500,000 `rows_read`、64 次 R2 `HEAD`；`rows_written` 必须为 0。
- release artifact 超出 R2 HEAD 上限时标记 `INCOMPLETE` 并保留未核验项，不截断成 READY。
- R2 Class A 固定为 0，Class B 计为精确 `HEAD` 次数；不调用 `list()` 和默认 body `get()`。
- 任何 D1/R2 错误、403、429、超时、缺少 rows telemetry、key/hash 不合法均保留 `INCOMPLETE` 或明确的 hash 状态。

### 查询计划证据（本地 schema fixture）

在与仓库迁移一致的 SQLite fixture 上执行 `EXPLAIN QUERY PLAN`，日期范围查询均使用现有索引：

| 查询 | 计划证据 |
| --- | --- |
| 日期候选 CTE | `historical_snapshot` 使用主键 `as_of`；`release_manifest` 使用 `release_manifest_as_of_idx`；`dashboard_snapshot` 使用 `dashboard_snapshot_as_of_idx`；`market_history_daily` 使用主键 `trade_date`；`source_readiness` 使用 `source_readiness_target_idx`；pointer 分支使用 `history_artifact_pointer_date_idx (dataset, trade_date, status)` |
| `history_artifact_pointer` | `history_artifact_pointer_date_idx (dataset=? AND trade_date>? AND trade_date<?)`；只查询受控 `etf_scale`/`etf_pcf` dataset |
| ETF/margin/connect model 摘要 | 分别以 `trade_date` 开头的 `*_daily_lookup_idx` 做范围搜索 |
| pipeline window | 精确 `run_key IN (...)` 使用主键索引 |
| active release | 精确 slot 使用主键索引 |

日期候选集合的去重和最终排序会使用有界临时 B-tree；不会使用无界 OFFSET 或全表 R2 列举。正式导出器仍需在后续 PR C 中逐页记录真实 D1 `meta` 和账户预算遥测。

## 预期副作用

本 PR 不新增 migration、不写 D1/R2、不 dispatch workflow、不切换 flag、不修改 active release 或生产读取路径。由于会修改 `web/**`，合并到 `main` 可能触发仓库已有的 Worker deploy workflow；实际触发条件和最坏预算必须在 PR 前写入独立 `merge-side-effects.json`，并在用户明确授权后才可合并。
