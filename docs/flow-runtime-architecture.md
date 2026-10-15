# Flow 运行时架构

## 目标

交易所原始文件只在采集任务中获取和校验；用户请求只读取已经通过质量门禁的发布对象。Worker 不在请求期间加载 ETF 全量状态、不做板块聚合、不计算长周期。

## 数据路径

```text
Cloudflare Cron（主触发）
        │ dispatch
        ▼
GitHub Actions（官方采集 / QA / 离线计算）
        │
        ├─ R2 immutable raw + normalized evidence
        ├─ R2 read-model views
        └─ D1 release_manifest → active_release（最后一步切指针）
        │
        ▼
Cloudflare Worker
        ├─ 静态 SPA 壳
        ├─ D1 读取发布指针
        └─ R2 body 流式返回
```

## 发布约束

- `asOf` 是单一交易日；不把不同交易日的数据拼成一个页面。
- `quality.fail > 0` 或 `gate=blocked` 的快照不能替换 active release。
- ETF `historyCoverage` 必须在离线构建阶段生成；请求时不得回退到 R2 rolling state。
- 视图对象使用内容哈希和固定 key，重复运行不会覆盖原始对象。
- `active_release` 只保存当前指针；切换前先把所有对象和清单写完。
- 普通 pipeline window 只有在提交 `completionAsOf == targetDate` 且快照
  `publication.status == confirmed` 的完成凭据后才能进入 `COMPLETED`；较早的
  fallback 可以归档，但不能关闭当前交易日窗口。恢复任务继续使用 receipt-backed
  recovery 状态接口。
- manifest v2 与 v3 在迁移期并存；v3 使用 `observedAt` 对同一 `asOf` 的修订做单调排序，旧 v2 缺少该字段时按保守的较旧版本处理。schema v4 是新增的 v0.7 能力合同，只能作为候选版本 `stage_only` 写入，生产激活接口明确拒绝；v2/v3 已发布历史与默认生产读取合同保持不变。
- v3 manifest 额外声明 `sourceDates`、`pressureSchemaVersion=2`、8 个股票分片和固定 `requiredArtifacts`；发布日期 hash 同时覆盖这些控制字段。
- v4 在同一 manifest 中增加 `capabilityContract`：列出核心 ETF、雷达、轮动、国家队、互联互通、两融、各周期、ETF 穿透、个股搜索和详情的状态、真实来源日期、覆盖、原因码及精确 artifact key/hash/bytes。一个 slot 未绑定另一个 slot 的精确发布时，跨 slot 能力必须为 `UNKNOWN`，不能借同日或旧版本引用冒充 READY。v4 的 READY 状态会在 Worker 入口校验日期、依赖状态、覆盖计数和逐项不可变引用；损坏引用拒绝候选。
- schema v4 可由离线构建器显式启用 `--capability-contract-v07` 生成；候选发布必须使用 `scripts/publish_read_models.py --stage-only`。该开关默认关闭；当前生产工作流继续产生 schema v3，不在 070-A 中切换读取路径、激活 v4、补采或重建历史，也不新增 D1 migration。
- v3 当前固定发布 18 个工件：`bootstrap` 只含首页摘要、板块周期矩阵和 ETF 搜索索引；`radar` 含完整 ETF 行；`radar_flow` 只在打开 ETF 详情后按同一 `releaseId` 延迟读取；`national` 只含国家队代码子集；`stock_search` 与 8 个股票分片按个股详情读取。
- 默认首页不加载 Radar 全量行、ETF 穿透图、融资融券压力明细或股票分片；同一 `releaseId` 下的视图读取在浏览器内缓存，且每次读取校验 `X-Flow-Release-Id` 与 `asOf`，避免跨版本拼页。
- `/api/v2/history/dates` 只返回包含完整 read-model 工件集合的发布日期，不把只有原始归档的日期暴露给主日期选择器。

## 采集触发

Cloudflare Cron 仍是主调度器，按晚间、早间和 reconciliation 三个窗口 dispatch `market-data-collect.yml`。GitHub schedule 只运行 `market-data-watchdog.yml`，在 `pipeline_window` 缺失或可恢复时补发重任务。相同 GitHub run 重试由 `ingest_run` 幂等预检拦截；新一轮发布必须通过新的质量门禁。

日任务的发布顺序固定为：先归档完整原始证据，再探测 Worker capabilities；只有 v3 合同可用时才按精确 `releaseId` 续接 pressure 历史、构建读模型并逐 artifact 发布。最后由受保护的 canary 校验当前指针、历史日期、全部核心视图、搜索分片和 ETag；任何门禁失败都保留上一份完整 active release。

## 主要接口

- `/api/v2/dashboard`：流式返回当前或指定历史发布的 bootstrap 首页对象；完整 ETF 行和穿透图通过下方视图接口按需读取。
- `/api/v2/views/{view}`：按视图读取已发布对象，允许 `bootstrap / overview / radar / radar_flow / rotation / national / connect / margin / pressure`。
- `/api/v2/releases/latest`：返回 D1 active release 清单。
- `/api/v2/history/dates`、`/api/v2/market-history`：只读有限历史索引，不读取 R2 ETF 状态。
- `/api/v1/admin/capabilities`：受保护的发布契约探测；只有 D1 `observed_at` 两列都存在时才声明 v3 兼容能力；同一部署可额外声明 v4 capability contract 候选支持，但该能力仅限 stage-only，不能激活。
- `/api/v1/admin/releases/previous-candidates`：返回有上限的完整历史 read-model 候选元数据。
- `/api/v1/admin/releases/artifact`：按 `releaseId + name` 精确流式读取历史工件，避免同日多版本按日期误选。

离线运维脚本：`scripts/check_release_capabilities.py`（v3 发布前预检）和 `scripts/verify_published_release.py`（发布后最多 15 次请求的 canary）。

## 本地验证

```bash
PYTHONPATH=. .venv/bin/pytest -q
cd web && npm test && npm run lint
python scripts/build_stock_pressure_state.py --catalog stock-flow-catalog.json --output stock-pressure-catalog.json --before-date "$(jq -r '.asOf' stock-flow-catalog.json)"
python scripts/build_read_models.py --snapshot dashboard-snapshot.json --output-dir read-models --stock-catalog stock-pressure-catalog.json --require-materialized
```
## 080-DATA: explicit candidate archive replay (r5)

An immutable, provenance-pinned dashboard source may opt into
`etfReplayInput.contractVersion=flow-etf-archive-replay-v0.8.0`. This is a
candidate-only input profile, not an automatic production reader or collector.
The existing `CoreReadModelBuildAdapter` and verify adapter consume it through
`build_market_read_models`; schema 4 / the v0.7 capability contract is mandatory.
Sources without the profile retain the legacy behavior.

The profile contains `calendar` (the dependency planner's shared official-date
contract) and `partitions`. Each partition pins sourceId `sse`/`szse`, tradeDate,
the immutable normalized ETF objectKey, SHA256 and exact base64 CSV bytes.
The enclosing approved/pinned artifact establishes provenance; a caller-created
hash does not prove official origin. No arbitrary URL is fetched by replay.
An SSE margin business-date response uses the truthful source ID
`sse_official_margin_business_dates`, with its actual response bytes/hash,
query range and complete single-page result. It is not renamed a calendar feed.

Actual normalized bytes are replayed through the existing exchange adapter and
daily formula. The rolling state is a rebuildable projection, not a source of
expected sessions. A missing official day stays null even when enough older
cache rows survive. Window evidence pins calendar/input identities, expected
daily-change dates, baseline and market gaps. Pending source publication cannot
be promoted by materialization. Daily and each period/metric remain separate.

Bounds remain explicit: at most 533 calendar dates / 1066 partitions, 5000 rows
per partition, 2 MB total decoded normalized CSVs; existing 4 MiB pinned input
and 32 MiB output limits still apply. These limits may reject a long-window
bundle. They are not raised implicitly and a one-week success does not prove
all long periods runnable. Future bounded replay planning must reuse verified
inputs/checkpoints under the existing runner budget, not fetch everything again.

`rebuild_dashboard_snapshot_from_state.py --official-calendar PATH` can apply
the same strict date contract to a verified offline projection. Without this
option its legacy behavior is unchanged. Strict outputs require the candidate
contract; this option is not permission to register, publish or activate them.

This change adds no Worker deployment path, migration, production flag or
schedule. A Python/docs-only PR does not match the current automatic Worker
deployment workflow's `web/**` filter. PR CI and later main CI still consume
Actions minutes. Real candidates, historical writes and latest activation each
retain their existing authorization and resource admission requirements.
