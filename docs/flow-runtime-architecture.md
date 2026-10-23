# Flow 运行时架构

## 目标

交易所原始文件只在采集任务中获取和校验；用户请求只读取已经通过质量门禁的发布对象。Worker 不在请求期间加载 ETF 全量状态、不做板块聚合、不计算长周期。

## Actions 月度预算准入（platform-limit-v2）

`config/resource-budget.json` 的 `resourceAccounting.actionsAdmissionPolicy`
显式选择新语义：Actions 的账户硬边界来自 `limits.githubActionsMinutesPerMonth`；
`targets` / `hardLimits` 中的内部月阈值仅用于告警，不再因为超过 1500/1850
就停止必要任务。这个配置不是平台余额证明，也不会更改 GitHub 套餐、spending limit
或付费授权。缺少/过期用量、超出平台边界、单任务预算超额仍拒绝。

同一配置由离线总预算、恢复计划、正式准备入口及 Worker 账本消费。
没有这个版本字段的旧配置仍采用原来的严格阈值；D1、R2、请求限额不变。
新账户采用新策略；旧账本不会在部署或普通遥测刷新时被悄悄提额。
通过既有管理员 `account_reconcile` 操作显式传
`policyUpdate=platform-limit-v2`，可将已知旧 Actions 上限 1500/1850
更新为当前配置的平台上限。需要新鲜完整证据、expectedRevision 和 CAS；
先 dry-run，再以同一 operationId 提交/核对回执。自定义较低上限不能被该更新覆盖。
其他维度、历史消耗、pending、UNKNOWN 占用均保留；版本化业务身份、租约和恢复审批不变。

告警不阻止 `allow/warn` 任务继续，但不是无限执行许可。重复任务必须复用
业务身份/operationId；确定性失败不重复采集；任务累计准入、有限重试、检查点、
超时、步骤超额熔断继续生效。某任务 UNKNOWN 的保留量纳入账户剩余量，不能清零；
它本身不禁止另一个有充分剩余量且无冲突的任务。实际出现 OVER_BUDGET 时仍须停止
并核实异常责任，不能因去掉月度人为阈值而忽略无法界定的超额消耗。

这次收敛不授权生产预算写入、候选、历史登记、最新激活或补采。
代码合并/部署后，回到总路线图 r6 的 080-DATA 产品恢复动作包，优先复用归档，
分别验收短周期、长周期和真实候选；不是重新从 051 开始，也不把上线当作页面已修复。

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

### Formal archived-source hydration

The existing manual candidate workflow can explicitly supply `archiveReplaySource`
in its request. Omission preserves the legacy single GitHub snapshot path.
Contract `flow-archive-replay-source-v1` pins `baseSnapshotSha256`,
`calendarGzipBase64`, `calendarSha256` (decompressed canonical calendar bytes),
and `partitions` containing only sourceId/tradeDate/objectKey/sha256. The recipe
hash and request count must match `budgetManifest.archiveReplaySource` in the
immutable approved recovery plan. It is not approval by itself.

Before any source request, the preparation confirmation must match the request
and plan and remain valid for at most ten minutes, within the plan deadline.
The helper makes exactly three bounded GitHub reads and at most sixteen
sequential indexed normalized-archive reads to the existing fixed admin endpoint.
It never lists R2, fetches official sites, dispatches or writes remotely. GitHub
and admin credentials remain isolated; redirects receive neither credential.
Each returned key/hash, actual CSV facts and the final materialized source hash
are checked before any local source file is saved. Calendar expansion is bounded
to 2.8 MB, compressed base64 to 51,200 characters; the existing 2 MB CSV / 4 MiB
source limits remain. This recent-batch limit is not long-history capacity.

The existing shared budget includes both attempts' archive-read upper estimate
in the initial hold; resume does not create a second allowance. Pure account
budget checks run before source reads and again against the actual built graph.
`sourceReadRequests` reports GitHub and archive reads separately from later
control requests. Estimates are not measured account capacity. The unchanged
job timeout, live resource admission, lease, approval and UNKNOWN rules remain
authoritative. An interrupted read does not save partial input or permit a
blind retry. No real execution is authorized by adding a recipe or merging code.
