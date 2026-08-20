# Flow 发布可靠性、历史续接与 Worker 资源治理实施方案

> 交接对象：GPT-5.6 Luna（或后续实现者）
> 编写日期：2026-08-12
> 仓库：`xiaoker/flow`
> 生产地址：`https://flow.xiaoker.com`
> 状态：PR 1A/1B/2 已完成；PR 3 已实施并完成本地契约测试，待生产部署与 5 个交易日观测
> 优先级：P0 发布正确性，P1 请求资源，P1 采集调度

## 0. 执行指令

本方案解决四类反复出现的问题：

1. 同一交易日的旧任务可能晚于新任务结束，当前激活条件只比较 `asOf`，存在同日旧版本覆盖新版本的窗口。
2. 个股压强历史依赖“请求日期列表里的前一天”，找不到对应发布时会合法地返回空历史，导致一周、一月等窗口退化成只有最近一日。
3. `bootstrap.json` 和 `radar.json` 已接近当前项目 5 MB 单工件上限；页面进入默认视图或 Radar 时会重复读取大型对象，增大加载时间和 Cloudflare Worker Error 1102 的风险。
4. 单次 GitHub Actions 会在深交所数据尚未就绪时内部轮询最多约 36 分钟。多个调度窗口与 `cancel-in-progress: true` 叠加后，容易出现前一个任务被后一个任务取消、证据已采集但完整发布没有完成的现象。

必须按以下顺序实施，不要合并成一个“大 PR”：

```text
PR 1A  Worker 与 D1 先具备向后兼容能力
  ↓ 生产部署并验证 capabilities
PR 1B  离线发布器升级为 manifest v3，并修复历史续接与同日激活顺序
  ↓ 连续验证发布合同
PR 2   减小 bootstrap / radar / national 载荷，并按视图懒加载
  ↓ 记录 5 个交易日的大小与 Worker 错误
PR 3   移除任务内部的来源就绪长轮询，每个窗口只采集一次
```

任何一步没有通过本文件的验收条件，都不得继续下一步。

## 1. 开始前的仓库基线

实施前必须运行：

```bash
git status --short --branch
git fetch origin
git log -8 --oneline --decorate
```

本文编写时观测到：

```text
current branch: codex/fix-pressure-read-model
current HEAD:   ed1cb4b fix: keep ETF pressure index within read-model limits
origin/main:    fb33263 feat: add ETF pressure workbench (#9)
```

执行门禁：开始 PR 1A 前，先确认 `origin/main` 已包含 `ed1cb4b` 的等价修复。如果没有，先单独合并该修复，或在明确记录原因后把该提交带入 PR 1A。不能让新的可靠性改造建立在一个仍会生成超限 `pressure.json` 的基线上。

本文编写时工作树还有用户已有、与本方案无关的改动：

```text
M  .gitignore
M  reports/phase0/latest-manifest.json
M  reports/phase0/latest-quality-summary.json
M  reports/phase0/latest-release-qa.json
?? memory/
```

这些文件不属于本任务。不要覆盖、还原、暂存或提交。禁止使用 `git add -A`、`git add .` 或破坏性清理命令。

## 2. 必须先阅读的现有合同

实施者必须先阅读：

- `docs/flow-runtime-architecture.md`
- `docs/data-quality-contract.md`
- `docs/data-lineage-manual-checklist.md`
- `docs/handoff/etf-beta-pressure-workbench.md`
- `src/trading_desk/read_models.py`
- `src/trading_desk/stock_pressure.py`
- `src/trading_desk/dashboard.py`
- `scripts/build_stock_pressure_state.py`
- `scripts/publish_read_models.py`
- `web/app/lib/release-store.ts`
- `web/app/api/v1/admin/import/route.ts`
- `web/app/api/v1/admin/read-model-import/route.ts`
- `web/app/dashboard-bootstrap.tsx`
- `.github/workflows/daily-data.yml`
- `web/worker/index.ts`
- `web/wrangler.jsonc`

不得破坏以下主架构：

```text
官方来源
  │
  ▼
GitHub Actions：采集、QA、历史续接、离线计算
  │
  ├── R2：不可变原始证据与不可变 read model
  └── D1：release_manifest + active_release
            │
            ▼
Cloudflare Worker：读取小指针并流式返回 R2 对象
            │
            ▼
浏览器：按当前视图懒加载，不在 Worker 现场重算
```

## 3. 这次复用什么，不重复建设什么

| 现有能力 | 位置 | 本方案处理方式 |
| --- | --- | --- |
| 原始数据采集、规范化与 QA | `src/trading_desk/phase0.py`、`release_qa.py` | 继续复用，不建立第二套采集器或 QA 服务 |
| 确定性 read model 与哈希 | `src/trading_desk/read_models.py` | 扩展 manifest 合同，不重写发布系统 |
| 不可变 R2 对象 | 两个 admin import route | 继续保持不可覆盖语义 |
| D1 发布清单与活跃指针 | `release_manifest`、`active_release` | 增加同日顺序字段，不增加新的数据库 |
| 8 个个股详情分片 | `stock_0` 至 `stock_7` | 本轮保持 8，不扩成 32 或 64 |
| Worker 流式返回 | `release-store.ts` 和 API v2 routes | 保持流式返回，禁止 Worker 解析并聚合大 JSON |
| Cloudflare Cron 主触发 + GitHub schedule 兜底 | `web/worker/index.ts`、`daily-data.yml` | 保留双触发，移除单任务内的长轮询 |
| ETF 分类与计算版本 | 当前快照与 manifest | 原样保留，不改分类标准和计算口径 |

## 4. 明确不在本轮范围

- **不把个股分片从 8 改成 32 或 64。** 当前首要瓶颈是 `bootstrap` 和 `radar`，增加分片只会扩大 manifest、R2 HEAD 检查和 API 合同的改动面。
- **不做自动回滚。** 自动回滚可能把已经修正的数据退回旧版本。本轮使用发布前门禁、单调激活和发布后验证；失败后保留旧对象并人工决定纠正发布或回指。
- **不购买 Cloudflare 付费能力。** 不引入 Queues、Durable Objects、Workers Paid 或第三方定时服务。
- **不删除 R2 原始证据，不修改生命周期策略。** 数据保留属于独立的容量治理任务。
- **不更换交易所数据源，不改变 ETF 份额、PCF、融资融券或互联互通公式。**
- **不改变 ETF 分类标准。** 数据总览、板块轮动与 Radar 的分类统一属于另一个数据治理合同。
- **不做实时或盘中更新。** 当前仍是交易日级官方数据发布。
- **不合并账号认证体系，不做 UI 全面改版。** PR 2 只处理取数边界、加载状态和必要的交互适配。

## 5. 核心数据合同

### 5.1 发布日期与来源日期

不要把所有来源强制解释成同一个日期字段。manifest v3 必须区分：

```json
{
  "asOf": "2026-08-10",
  "observedAt": "2026-08-11T00:03:41Z",
  "sourceDates": {
    "sseEtfAsOf": "2026-08-10",
    "szseEtfAsOf": "2026-08-10",
    "marginAsOf": "2026-08-10",
    "connectAsOf": "2026-08-10",
    "pcfAsOf": "2026-08-11",
    "pcfEffectiveAsOf": "2026-08-10"
  }
}
```

定义：

- `asOf`：这个 release 代表的决策交易日，也是日期选择器和活跃指针使用的日期。
- `observedAt`：本轮完整官方证据采集完成的 UTC ISO 时间，用于同一 `asOf` 的发布顺序。
- `sourceDates`：每个来源自己的官方业务日或有效日。
- `pcfAsOf` 与 `pcfEffectiveAsOf` 必须分开，因为次日发布的 PCF 可以对应前一交易日。

完整性的判断不是“所有字符串必须相等”，而是：

```text
sourceDate(domain) == expectedTradingDate(domain, asOf, releaseWindow)
```

当前沪深 ETF、沪深两融和互联互通在共同交易日通常仍应落在同一个业务日。遇到不同市场假期时，可以有不同 `sourceDates`，但不得把日期不兼容的字段放进同一个公式。页面必须能展示每个来源日期，缺失时显示待发布，不能回退到最近值后伪装成当日数据。

### 5.2 manifest v3

生产者升级后生成：

```json
{
  "schemaVersion": 3,
  "releaseId": "release:2026-08-10:0123456789abcdef",
  "asOf": "2026-08-10",
  "observedAt": "2026-08-11T00:03:41Z",
  "sourceDates": {},
  "pressureSchemaVersion": 2,
  "stockShardCount": 8,
  "requiredArtifacts": [
    "bootstrap", "overview", "radar", "radar_flow", "rotation", "national",
    "connect", "margin", "pressure", "stock_search",
    "stock_0", "stock_1", "stock_2", "stock_3",
    "stock_4", "stock_5", "stock_6", "stock_7"
  ],
  "artifacts": {},
  "quality": {},
  "publication": {}
}
```

规则：

1. manifest v3 的 `requiredArtifacts` 必须与 `artifacts` 键集合完全一致，顺序使用生产者的固定合同顺序。
2. 每个 artifact 继续校验 key 前缀、SHA-256、解压后字节数和 5 MB 硬上限。
3. `observedAt` 必须是带时区的 ISO 时间；禁止用 Worker 收到请求的 `activatedAt` 代替。
4. `sourceDates`、`pressureSchemaVersion`、`stockShardCount` 均参与 manifest 哈希和 release ID 生成。
5. Server 在过渡期同时接受 manifest v2 和 v3；只有 v3 执行新字段的强校验。
6. v2 兼容至少保留 7 个交易日。确认所有自动任务都生成 v3 后，再开单独 PR 移除 v2，不在本轮顺手删除。

### 5.3 同一交易日的单调激活

当前只按 `asOf` 比较。目标改为按以下元组排序：

```text
(asOf, observedAt)
```

状态转换：

```text
上传工件
  │
  ├── 缺少 required artifact ──► waiting_artifacts，不切指针
  │
  ├── asOf 更旧 ───────────────► staged_older_than_active
  │
  ├── asOf 相同、observedAt 更旧 ► staged_stale_revision
  │
  └── 元组更新或同 release 重试 ─► activated / already_activated
```

D1 迁移采用加列，不删除或重建现有表：

```sql
ALTER TABLE release_manifest ADD COLUMN observed_at text;
ALTER TABLE active_release ADD COLUMN observed_at text;
```

旧行允许为空。读取旧行时只用于兼容和历史查询；一旦 active release 是 v3，其后 v3 激活必须使用非空 `observed_at`。SQL 的指针更新条件必须在同一个条件更新中完成，不能先查再写。

Drizzle schema 中这两个新增列在迁移期必须保持 nullable，因为已有行没有值。v3 的 route 校验负责保证新写入的发布非空；不要为了声明 `NOT NULL` 重建整张生产表。对于 v2 上传：如果 manifest 自带可验证的 `observedAt`，可以使用它；如果缺失，则使用一个只会排在同日有效观测之前的保守顺序值，不能用当前请求时间把旧 release 伪装成最新修订。

推荐把 `active_release` 作为唯一真值，并在同一个 D1 batch 中按以下顺序执行：

1. 把新 release 以 `STAGED` 写入 `release_manifest`。
2. 使用 `(as_of, observed_at)` 条件 UPSERT `active_release`。
3. 让当前 pointer 指向的 release 变为 `ACTIVE`，把此前的 `ACTIVE` 变为 `SUPERSEDED`。

不要先在应用层读取 active 再决定是否更新，这会留下两个并发任务同时通过预检的竞态窗口。状态更新 SQL 应再次通过 `active_release` 子查询确认最终 pointer，不能根据请求到达顺序猜测。

允许同日官方纠正：同一个 `asOf` 且新的 `observedAt` 更晚时可以替换。较早任务即使更晚结束，也不能覆盖已经激活的较新观测版本。

### 5.4 历史续接状态

不要再从本轮请求的三个日期里猜 `PREVIOUS_DATE`。发布器必须查询“小于当前 `asOf` 的最近一个兼容发布”：

```text
当前 asOf
  │
  ▼
GET /api/v1/admin/releases/previous-candidates
    ?before=2026-08-10
    &requires=pressure
    &limit=10
  │
  ├── 返回最近候选 ─► 按 releaseId 精确读取 pressure 小对象并确认 schemaVersion=2
  │                    选中后再下载 stock_search 和 8 个 stock shard
  │                    校验全部 X-Flow-Release-Id 相同后再合并
  │
  └── 从未存在兼容 release ─► 合法 bootstrap，当前节点作为历史起点
```

这里不能只查询 manifest 的 `pressureSchemaVersion`。现有 manifest v2 没有这个字段，但其中可能已经包含 schema v2 的 `pressure.json`。迁移期的正确做法是：

1. Server 只根据日期、状态和工件名称返回最多 10 个小型候选元数据。
2. 对 manifest v3，离线端优先使用已声明的 `pressureSchemaVersion`。
3. 对 manifest v2，离线端读取候选的 `pressure` 视图并校验 `data.pressure.schemaVersion`。
4. 明确为 schema v1 的候选可以跳过；声明或验证为 schema v2 的候选如果缺对象、header 不一致或内容损坏，必须失败，不能悄悄跳到更早版本。旧 manifest 未声明 schema、且 pressure 对象缺失或损坏时同样失败，因为系统无法证明它只是“不兼容”而不是“发布损坏”。
5. 所有候选都属于不兼容 schema 时，允许把当前 schema v2 视为一次有记录的 `bootstrapped`；诊断中必须列出被跳过的 release。

这样可以承接当前分支已经生成、但 manifest 仍为 v2 的 pressure 历史，避免第一次 manifest v3 发布把现有历史清零。

不能继续使用公开 API 的 `?date=<asOf>` 下载候选工件。同一日期可能存在多个 release，而当前历史查询会按日期选最新版本，不保证就是候选列表返回的 `releaseId`。PR 1A 必须同时提供受保护的精确工件读取端点：

```text
GET /api/v1/admin/releases/artifact
    ?releaseId=release:2026-08-07:...
    &name=pressure
```

该 route 通过 `release_manifest` 精确选择 release，校验固定工件白名单和 R2 key 前缀，然后直接流式返回对象。它不得解析大 JSON。响应必须包含 `X-Flow-Release-Id`、`X-Flow-As-Of`、`X-Flow-Artifact` 和 `ETag`。离线续接检查 pressure 后，再通过同一个端点下载 `stock_search` 和 8 个 stock shard，彻底避免“候选是 A、实际下载成同日 B”的错配。

新 catalog 必须写入：

```json
{
  "historyStatus": "continued",
  "historyContinuity": {
    "previousReleaseId": "release:...",
    "previousAsOf": "2026-08-07",
    "previousStockCount": 6198,
    "currentStockCount": 6204,
    "matchedStockCount": 6189,
    "matchRate": 99.85
  }
}
```

`historyStatus` 只能是：

- `continued`：找到兼容发布且历史成功续接。
- `bootstrapped`：系统中确实不存在任何更早的兼容发布。
- `partial`：只允许写入未激活的诊断产物，不能进入 active release。

门禁：

- 找到兼容发布，但 search、pressure、任一分片缺失或 release header 不一致：失败，不发布。
- 找到兼容发布，但 `previousItems == 0`：失败，不得伪装为首次启动。
- 没找到兼容发布：允许 `bootstrapped`，每只当前个股只有一个历史节点。
- 已匹配的旧个股：新历史覆盖必须是 `min(previousCoverage + 1, 21)`；不得无原因下降。
- 新上市或新进入 PCF 覆盖的个股：允许从一个节点开始。
- 已退出当前 PCF 覆盖的个股：不带入当前 catalog。
- `matchRate >= 95%`：通过。
- `80% <= matchRate < 95%`：警告，但允许发布，并在 QA artifact 中列出数量变化。
- `matchRate < 80%`：阻止发布。确有大规模样本口径变化时，必须先人工解释并修改经过评审的合同，不能通过环境变量绕过。

## 6. PR 1A：先升级服务端兼容能力

### 6.1 目标

先让生产 Worker 能识别旧 manifest v2 和新 manifest v3。此时离线生产者仍继续发 v2，因此部署失败不会中断现有数据发布。

PR 1A 的代码必须同时兼容迁移前和迁移后的 D1 schema。GitHub 合并后 Cloudflare 自动部署与人工 D1 migration 的先后顺序不能被假定：新代码在 `observed_at` 列尚不存在时仍要继续服务当前 v2 release；capabilities 只有在实际探测到两个新列后，才可以把 manifest v3 和 `sameDateObservedAtOrder` 宣布为可用。迁移完成前返回 v2-only capability，不要返回 500，也不要提前让生产者发送 v3。

### 6.2 修改范围

预计涉及：

- `web/db/schema.ts`
- 新增 `web/drizzle/0006_release_observed_at.sql`
- `web/drizzle/meta/_journal.json` 及由 Drizzle 正常生成的 snapshot 文件
- `web/app/api/v1/admin/import/route.ts`
- `web/app/api/v1/admin/read-model-import/route.ts`
- 新增 `web/app/api/v1/admin/capabilities/route.ts`
- 新增 `web/app/api/v1/admin/releases/previous-candidates/route.ts`
- 新增 `web/app/api/v1/admin/releases/artifact/route.ts`
- `web/app/lib/release-store.ts`
- `web/app/lib/dashboard-store.ts`
- `web/app/api/v2/history/dates/route.ts`
- `web/tests/rendered-html.test.mjs`
- `docs/flow-runtime-architecture.md`

### 6.3 capabilities 端点

新增一个很小的受保护 JSON 端点：

```json
{
  "deploymentCommit": "<build commit>",
  "readModelManifestSchemas": [2, 3],
  "pressureSchemas": [2],
  "stockShardCounts": [8],
  "maxArtifacts": 20,
  "maxArtifactBytes": 5000000,
  "maxUploadBytes": 1500000,
  "features": [
    "requiredArtifacts",
    "sameDateObservedAtOrder",
    "previousCompatibleRelease"
  ]
}
```

安全要求：

- Cloudflare Access 继续在边缘校验 service token。
- route 自身还必须校验 `Authorization: Bearer FLOW_INGEST_API_KEY`。
- 不返回 secret、R2 key 全列表、数据库 ID 或 GitHub token。
- 该端点只描述兼容能力，不改变任何状态。

### 6.4 previous-candidates 端点

响应只返回续接所需的发布元数据，不返回大对象：

```json
{
  "status": "found",
  "candidates": [
    {
      "releaseId": "release:...",
      "asOf": "2026-08-07",
      "manifestSchemaVersion": 2,
      "pressureSchemaVersion": null,
      "stockShardNames": ["stock_0", "...", "stock_7"]
    }
  ]
}
```

没有兼容发布时返回 HTTP 200：

```json
{ "status": "not_found" }
```

查询条件：

- `as_of < before`
- status 是 `ACTIVE` 或 `SUPERSEDED`
- manifest 包含 `pressure`、`stock_search` 和声明的全部 stock shard
- 按 `as_of DESC, observed_at DESC, created_at DESC` 选择最近兼容项
- 最多返回 10 个候选；`pressureSchemaVersion` 在旧 manifest 未声明时返回 `null`

为了控制 Worker CPU，D1 最多取最近 30 个候选 manifest 后在 TypeScript 中做小对象校验，返回最多 10 个。禁止扫描整个历史库，也禁止在该 Worker route 中读取并解析 `pressure.json`；schema 迁移判断由离线任务完成。

精确 artifact route 与 capabilities 使用同一套双重认证。非法 release ID、非法工件名和 key 前缀不匹配必须拒绝；对象不存在返回 404；对象 metadata 与 manifest 不一致返回 503。成功响应直接流式转发 R2 body，不在 Worker 中计算 hash。

### 6.5 日期选择器的发布合同

`/api/v2/history/dates` 必须只返回存在完整 read-model manifest、状态为 `ACTIVE` 或 `SUPERSEDED` 的日期。不要把只有原始归档或 `historical_snapshot`、但没有完整视图工件的日期放进主日期选择器。

如需继续保留“原始归档日期”，应另设诊断字段或独立接口，不得与用户可查看的完整 release 日期混在同一个 `dates` 数组。这个规则直接防止“日期选择器能选中某天，但 Radar、国家队或 pressure 没有该日数据”的旧问题。

### 6.6 PR 1A 验收

- 本地和远端 D1 迁移成功，旧数据仍可读取。
- 新 Worker 在远端迁移尚未应用时继续读取和发布 v2，不产生 500。
- capabilities 在列未就绪时只声明 v2，迁移完成后才声明 v3。
- 线上 capabilities 返回同时支持 v2 和 v3。
- 当前 v2 自动发布仍能激活。
- v3 缺 `observedAt`、`sourceDates` 或 `requiredArtifacts` 时返回 422。
- 迁移期 v2 缺 `observedAt` 时不会以请求时间获得同日最高发布优先级。
- v3 缺任一工件时 finalize 返回 `waiting_artifacts`，active pointer 不变。
- previous-candidates 能跳过不含 pressure 的旧发布，并返回有上限的候选列表。
- 精确 artifact route 在同日多个 release 时只返回请求的 `releaseId`，不按日期回退。
- 日期选择器不显示只有原始归档、没有完整 read model 的日期。
- Worker 请求路径没有增加全量 R2 JSON 解析。

## 7. PR 1B：升级离线发布、历史续接和发布后验证

### 7.1 目标

生产端已经支持 v3 后，再让 GitHub Actions 生成 v3。这个 PR 完成发布正确性的闭环。

### 7.2 修改范围

预计涉及：

- `src/trading_desk/read_models.py`
- `src/trading_desk/stock_pressure.py`
- `scripts/build_stock_pressure_state.py`
- `scripts/publish_read_models.py`
- 新增 `scripts/check_release_capabilities.py`
- 新增 `scripts/verify_published_release.py`
- `.github/workflows/daily-data.yml`
- `tests/unit/test_read_models.py`
- `tests/unit/test_stock_pressure.py`
- `tests/unit/test_workflow_contract.py`
- 对应的新脚本单元测试

### 7.3 workflow 顺序

必须保持“原始证据优先归档”：

```text
采集官方来源
  │
  ▼
规范化 + QA
  │
  ├── 无论 complete / pending，都先尝试归档原始证据与质量报告
  │
  ▼
读取线上 capabilities
  │
  ├── 不兼容或不可达：停止 read-model 发布，任务明确失败，旧 active 不变
  │
  ▼
解析 previous-candidates，并离线确认 pressure schema
  │
  ▼
离线续接历史 + 构建 manifest v3
  │
  ▼
逐工件上传并 finalize
  │
  ▼
发布后验证脚本
```

capability 预检不能放在最前面，否则 Worker 暂时不可用时会连交易所原始证据也不采集。远端 history import 成功时证据进入 R2；Worker 或网络不可达时，`history-bundle.json`、质量报告和必要的原始文件必须至少进入 `actions/upload-artifact`，随后任务标红。不能把“远端归档失败”写成“已经归档成功”。

### 7.4 发布后验证脚本

`scripts/verify_published_release.py` 最多发 15 个请求，建议固定检查：

1. `/api/v2/releases/latest`
2. `/api/v2/dashboard`
3. `/api/v2/dashboard?date=<asOf>`
4. `/api/v2/history/dates`
5. `/api/v2/views/overview`
6. `/api/v2/views/radar`
7. `/api/v2/views/rotation`
8. `/api/v2/views/national`
9. `/api/v2/views/connect`
10. `/api/v2/views/margin`
11. `/api/v2/views/pressure`
12. `/api/v2/stock-search`
13. 从 search 选一个确定性样本后请求其 `/api/v2/stocks/<shard>`
14. 对一个小工件发送 `If-None-Match`，确认 304

所有 200 响应必须满足：

- `X-Flow-Release-Id` 与刚发布的 manifest 一致。
- `X-Flow-As-Of` 与 `asOf` 一致。
- JSON 顶层 schema、日期和关键字段存在。
- latest manifest 的 `requiredArtifacts`、schema 和 hash 与本地产物一致。
- 日期列表包含 `asOf`。
- stock search 中的样本在声明分片能找到。

验证脚本必须带 Cloudflare Access service headers。被 Access 重定向到登录页是失败，不是“站点正常”。

发布后验证失败时：

- GitHub job 标红。
- 不自动回滚。
- 保留本轮 R2 不可变对象、manifest、QA 和验证输出。
- 如果已经激活，优先修复后发布更晚 `observedAt` 的同日纠正版本。
- 只有确认新版本会持续误导用户时，才按人工 runbook 把 active pointer 回指到已知正确 release。

### 7.5 PR 1B 验收

- manifest v3 在本地对相同输入完全确定。
- raw evidence 在 capability 失败时仍已归档。
- history import 不可达时，Actions artifact 仍保存可重放证据，且任务明确失败。
- 同日较新 `observedAt` 先激活后，较旧版本晚到只能得到 `staged_stale_revision`。
- 同 release 重试是幂等的，即重复执行结果相同且不产生第二份可变状态。
- 能从 manifest v2 候选的 `pressure.json` 确认 schema v2，并续接已有历史。
- 找到兼容前序 release 时，已匹配个股的历史覆盖按一天增长，最长 21 个节点。
- 前序 release 缺任一分片时发布失败，旧 active 继续服务。
- 系统从未存在兼容 pressure release 时，允许且明确标记 `bootstrapped`。
- 发布后验证覆盖当前日期与显式历史日期。

## 8. PR 2：治理 bootstrap、Radar 和国家队载荷

### 8.1 先测量，不凭感觉切分

在修改结构前新增一个离线大小报告，至少输出：

- 每个 artifact 的 raw bytes、gzip bytes、占 5 MB 硬上限比例。
- `bootstrap` 中每个顶层字段的 canonical JSON bytes。
- `radar.etfTerminal` 与 `radar.etfStockFlow` 各自大小。
- ETF instrument、dailySeries、groupPeriods、contributors 等主要数组大小。

此前一次成功运行的审计值仅作为线索，Luna 必须用当前全量数据重测：

| 工件 | 之前观测的 raw bytes |
| --- | ---: |
| bootstrap | 约 4,836,209 |
| radar | 约 4,831,925 |
| pressure | 约 2,727,240 |
| national | 约 1,172,424 |
| stock_search | 约 798,514 |
| 每个 stock shard | 约 1.16 至 1.32 MB |

这说明当前应先拆默认页和 Radar，而不是增加个股分片。

### 8.2 目标取数边界

```text
首次打开
  ├── bootstrap：页面壳、release 元数据、日期、状态、默认概览必需字段
  ├── history/dates
  └── market-history

切换 Radar
  └── radar：ETF 列表、周期指标、分类与 Radar 画面必需字段

打开某只 ETF 详情
  └── radar_flow：ETF→个股贡献兼容图，首次打开时加载并按 releaseId 缓存

切换国家队
  └── national：只含国家队子集与该页面所需历史，不复制全量 etfTerminal

打开个股
  ├── stock_search
  └── 一个 stock shard
```

实现要求：

1. `bootstrap` 不再等于完整 `snapshot`。先逐字段追踪默认页真实使用字段，再生成显式白名单对象。
2. `radar` 移除 `etfStockFlow`。新增一个 `radar_flow` 工件，只有打开 ETF 详情时才读取。
3. `national` 从全量 `etfTerminal` 改为国家队 ETF 子集。
4. `dashboard-bootstrap.tsx` 不再用“重新读取整个 dashboard”来实现普通视图切换。按 `releaseId + view` 缓存已经取得的视图。
5. 同一页面合并不同对象前继续校验 `X-Flow-Release-Id` 和 `asOf`；不一致时保留当前完整版本并延后切换。
6. 后台检查新版本只读取 `/api/v2/releases/latest`。release 未变化时不得重新下载 bootstrap 或当前视图。
7. 懒加载失败时只在对应卡片或抽屉显示错误，不把整个交易台替换成全屏 loading。
8. Worker 继续直接流式返回 R2 对象，不允许为裁剪字段而在请求路径 `JSON.parse` 大对象。

PR 2 仍必须遵守“服务端先兼容、生产者后发新工件”：先让 import allowlist、`/api/v2/views/[view]` 和 capabilities 声明支持 `radar_flow`，生产部署验证后，离线生产者才把它加入 `requiredArtifacts`。如果代码和生产者同在一个 PR，capability 预检必须在旧 Worker 尚未部署时阻止新合同上传。

`radar_flow` 会把当前工件总数从 17 增加到 18，仍低于现有 20 上限。本 PR 不增加 ETF 详情分片。如果移除重复字段后 `radar_flow` 仍超过 3 MB，先停下并提交字段大小报告与单独的分片设计，不要在同一 PR 临时加入 8 或 16 个新工件。

### 8.3 软预算与硬门禁

第一阶段只警告，不立即阻止发布：

| 工件 | 软目标 | 硬上限 |
| --- | ---: | ---: |
| bootstrap | 1,000,000 bytes | 5,000,000 bytes |
| overview | 250,000 bytes | 5,000,000 bytes |
| radar | 2,000,000 bytes | 5,000,000 bytes |
| radar_flow | 3,000,000 bytes | 5,000,000 bytes |
| pressure | 3,000,000 bytes | 5,000,000 bytes |
| national | 500,000 bytes | 5,000,000 bytes |
| stock_search | 1,000,000 bytes | 5,000,000 bytes |
| 单个 stock shard | 2,000,000 bytes | 5,000,000 bytes |

构建输出 `artifact-size-report.json`。超过软目标时 GitHub Summary 明确警告，超过 5 MB 时继续硬失败。连续 5 个交易日都在软目标内后，再单独决定是否把 `bootstrap`、`radar` 和 `radar_flow` 软目标升级为硬门禁。

### 8.4 PR 2 验收

- 默认页面不请求 `radar`、`radar_flow`、`pressure` 或 stock shard。
- 进入 Radar 时不读取 `radar_flow`，只有打开 ETF 详情才读取一次并缓存。
- 国家队页不下载全量 ETF 终端对象。
- 同一 release 内来回切换视图不会重复下载相同工件。
- 当前页加载失败不会清空已经显示的完整数据。
- 全量生产规模样本下所有工件小于 5 MB，软预算结果写入 Actions artifact。
- Cloudflare Worker 路径没有全量 JSON 聚合、排序或哈希。

## 9. PR 3：把采集重试改成窗口级状态机

### 9.1 目标

去掉单个 Actions job 内部最多 18 次、每次间隔 120 秒的来源就绪轮询。调度窗口负责再次触发，单次运行负责采一次、归档一次、判断一次。

### 9.2 目标状态机

```text
一个调度窗口开始
  │
  ▼
采集一次
  │
  ├── ConnectionError / HTTPError
  │       └── 最多 3 次短退避重试，仍失败则归档诊断并 job 红
  │
  ├── 官方来源尚未发布目标日期
  │       ├── evening：归档 pending，正常结束，不激活
  │       └── morning / reconcile：归档 pending，明确失败，不激活
  │
  └── complete + QA pass
          └── 构建、发布、激活、发布后验证
```

保留现有触发结构：

| 触发器 | 北京时间 | 作用 |
| --- | --- | --- |
| Cloudflare Cron | 18:30 | 晚间主采集 |
| GitHub schedule | 18:45 | 晚间兜底 |
| Cloudflare Cron | 07:45 | 深交所早间主采集 |
| GitHub schedule | 08:00 | 早间兜底 |
| Cloudflare Cron | 08:45 | PCF 与完整发布核对 |
| GitHub schedule | 08:55 | 最终兜底 |

规则：

- 来源返回成功但日期尚未推进属于“来源未就绪”，不是网络错误，不在同一个 job 里 sleep。
- 网络退避只处理明确的 ConnectionError、HTTPError、408、429 和 5xx，不重试日期不完整。
- 所有窗口都必须先归档已取得的原始证据。
- 只有完整 release 才能激活，页面始终显示上一个完整版本。
- 初始仍保留 `cancel-in-progress: true`。PR 1 的单调激活和 PR 3 的短任务稳定运行 5 个交易日后，再根据实际重叠时长单独评审是否改为 false。
- Cloudflare Cron dispatch 失败必须继续写结构化日志；GitHub schedule 仍是独立兜底。
- 统计最近 10 至 20 次完整 one-shot 运行的 p95 时长；样本不足时使用已观测最大时长。GitHub 兜底与 Cloudflare 主触发的间隔必须大于该值加 5 分钟；若现有 10 或 15 分钟不足，优先把兜底向后移动，而不是恢复长轮询。
- 当前 daily job 会运行完整 Python 测试。只有在新增并启用独立的 PR/main CI 后，才可以把定时采集中的完整代码测试替换为小型 collector contract smoke；没有独立 CI 时保持现状。

### 9.3 PR 3 验收

- workflow 中不存在 `SOURCE_READINESS_ATTEMPTS=18` 或 `sleep 120`。
- 来源未就绪的一次运行不超过正常采集和归档所需时间。
- evening 的预期深交所日期缺口不会错误激活，也不会制造虚假 0。
- morning / reconcile 缺数据时 job 红、证据存在、旧 active 不变。
- 网络错误最多重试 3 次，Actions Summary 能区分网络失败与来源日期未就绪。
- Cloudflare 与 GitHub 两个相邻任务重叠时，激活指针不会回退。

### 9.4 当前实现记录（2026-08-20）

- `daily-data.yml` 已改为窗口级单次采集；来源日期未就绪时归档 pending，不在同一 job 内执行 18 次长轮询。
- 网络错误仍保留最多 3 次、30 秒/60 秒短退避；schema、认证和数据质量错误不重复采集。
- Cloudflare Worker 调度 GitHub Actions 增加 0/1/3 秒短退避，并记录 `scheduleLagMs`；超过 5 分钟会写入延迟告警日志。
- 生产部署后仍需观察 5 个交易日，确认 Cloudflare 主触发、GitHub 兜底、pending 归档和 active 指针连续无缺口。

## 10. 测试设计

### 10.1 测试路径图

```text
官方响应
  │
  ├── transport error ──► bounded retry ──► archive error ──► no activation
  │
  ├── stale source date ─► pending archive ────────────────► no activation
  │
  └── complete rows
        │
        ▼
      QA gate
        │
        ├── fail ────────► evidence only
        │
        └── pass
              │
              ▼
        capability contract
              │
              ├── mismatch ─► fail before read-model upload
              └── compatible
                    │
                    ▼
          previous release resolver
              │
              ├── none ever ─► bootstrap history
              ├── compatible ─► verify 10 objects and merge
              └── broken ─────► fail closed
                    │
                    ▼
          manifest v3 + immutable upload
                    │
              all required objects?
              ├── no ─► waiting_artifacts
              └── yes
                    │
                    ▼
             monotonic D1 activation
              ├── stale ─► staged only
              └── current ► active
                    │
                    ▼
             authenticated canary
```

### 10.2 必须新增或修改的测试

#### Python 单元测试

- manifest v3 包含完整 `requiredArtifacts`、`sourceDates`、`observedAt`、pressure schema 和 8 个分片声明。
- 相同输入生成相同 manifest 和 release ID。
- previous-candidates 返回有效前序时，历史覆盖增长并限制在 21。
- manifest v2 未声明 pressure schema 时，离线端从 pressure payload 正确识别 v2；schema v1 候选被记录并跳过。
- 同日存在两个候选 release 时，所有前序工件都按选定 `releaseId` 精确下载。
- 合法 bootstrap、前序存在但缺分片、release header 不一致、前序 items 为空分别走正确分支。
- 新个股从一个节点开始，退出样本不继续携带。
- match rate 的通过、警告、失败边界分别为 95% 和 80%。
- artifact 大小报告使用 canonical bytes，超过 5 MB 仍硬失败。
- capability 不兼容时，发布器不会上传第一个 read model。
- canary 对 release mismatch、日期 mismatch、Access 302、404、缺 key、错误分片和 ETag 失效均失败。

#### Worker 与 D1 测试

- v2 与 v3 manifest 在过渡期都能通过各自合同。
- v3 缺 required artifact、字段类型错、key 越界、hash 或 bytes 错时拒绝。
- 同日较新观测先激活、旧观测后到时保持 staged。
- 较旧日期不能覆盖 active。
- 同一 release 重试保持 active，不把旧 ACTIVE 行错误改成 SUPERSEDED。
- previous-candidates 跳过无 pressure 版本并有候选上限。
- protected artifact route 验证 bearer、release ID、工件白名单、R2 前缀和 metadata。
- capabilities 需要 bearer，且只暴露允许字段。
- 显式历史日期读取正确 release，不回退到最新版本。
- history/dates 只返回可读取完整 read model 的日期。

#### 前端行为测试

- 默认页不请求非默认视图。
- Radar 列表与 ETF 详情流分开请求。
- ETF 详情首次打开加载 `radar_flow`，再次打开从同 release 缓存读取。
- releaseId 不一致时不合并数据。
- 懒加载失败保留当前页面，只显示局部错误。
- 历史日期不自动跳回最新 release。

#### workflow 合同测试

- raw archive 位于 capability check 之前。
- capability check 位于 read-model build 与 upload 之前。
- canary 位于激活之后。
- PR 3 后不存在长轮询，只保留短网络重试。
- `cancel-in-progress: true` 在本轮仍保留。

### 10.3 本地验证命令

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q
(cd web && npm run lint)
(cd web && npm test)
```

涉及 D1 迁移时：

```bash
(cd web && npm run db:migrate:local)
(cd web && npm test)
```

`npm test` 已包含 build。不要为了省时间只跑局部测试后直接发布。可以先跑局部测试加快反馈，但 PR 完成前必须跑完整命令。

## 11. 生产失败模式与处理

| 失败模式 | 自动测试 | 运行时处理 | 用户看到什么 |
| --- | --- | --- | --- |
| capabilities 不可达或版本不兼容 | 是 | 原始证据已归档，停止 read model 发布 | 继续看到上一份完整数据 |
| 远端原始归档接口不可达 | 是 | 保存 Actions 可重放证据并失败 | 继续看到上一份完整数据 |
| 前序兼容 release 缺分片 | 是 | 当前发布失败，不重置历史 | 继续看到上一份完整数据 |
| 同日多个前序 release | 是 | 按选定 releaseId 精确读取，不按日期回退 | 历史不会混入另一修订版 |
| 同日旧任务晚到 | 是 | `staged_stale_revision`，不切 active | 无版本回退 |
| R2 缺 required artifact | 是 | finalize 409，active 不变 | 继续看到上一份完整数据 |
| manifest 与工件 hash 不符 | 是 | 422/500 并记录明确错误 | 继续看到上一份完整数据 |
| 发布后 API 返回不同 release | 是 | canary 标红，保留证据，不自动回滚 | 可能短暂看到新版本，需要人工判断 |
| Radar 详情懒加载失败 | 是 | 局部错误和重试，页面主体不清空 | 当前页面仍可使用 |
| 来源日期未推进 | 是 | pending archive，不激活 | 顶部仍显示上一完整日期 |
| 日期只有原始归档、没有 read model | 是 | 不进入主日期选择器 | 用户不会选中一个无法展示的日期 |
| Worker Error 1102 | 压力与大小测试覆盖主要原因 | 大对象拆分、流式返回、请求无重算 | 请求应快速失败或成功，不长时间白屏 |

本方案完成后不应存在“无测试、无错误处理、用户也看不到提示”的静默失败路径。

## 12. 发布与回滚顺序

### 12.1 PR 1A

1. 合并代码。
2. 等待 Cloudflare GitHub 集成完成向后兼容的 Worker 部署；确认当前 v2 页面和发布仍可用。
3. 应用 additive D1 migration。
4. 用 Access service token 验证 capabilities 已从 v2-only 变为同时支持 v2/v3。
5. 手动运行一次现有 v2 `Daily market data`，再次确认兼容与单调激活。

### 12.2 PR 1B

1. 合并生产者 v3。
2. 手动运行 `collection_window=reconcile`。
3. 检查 manifest v3、历史 continuity、激活结果和 canary。
4. 再让定时任务接管。

### 12.3 PR 2

1. 先在 Actions artifact 保存修改前后的大小报告。
2. 部署前端与 read-model 新工件合同。
3. 手动发布一个完整 release。
4. 验证默认、Radar、ETF 详情、国家队、pressure、个股页和历史日期。
5. 观察 5 个交易日的 1102、请求延迟和工件大小。

### 12.4 PR 3

1. 合并 workflow 状态机。
2. 分别手动运行 evening、morning、reconcile，使用可控 fixture 或显式历史日期验证状态。
3. 观察一个完整交易日的 Cloudflare 主触发与 GitHub 兜底。
4. 不在同一个 PR 改 `cancel-in-progress`。

### 12.5 人工回退原则

- 代码部署失败：回退对应 PR，manifest v2 兼容期保证旧发布仍可读。
- 新数据未激活：无需回退，active 本来就没变。
- 新数据已激活但 canary 失败：优先发布同日更晚 `observedAt` 的纠正版本。
- 必须回指时，只允许使用受控 D1 命令或经认证的专用运维脚本，明确记录目标 `releaseId`、`asOf` 和 manifest hash。不要实现自动回滚按钮。

## 13. 免费额度约束

本方案不新增付费资源，控制原则如下：

- 每个发布仍只有 18 个左右的 read-model 对象，不改成几十个或几百个小对象。
- finalize 最多检查 manifest 声明的 20 个对象，当前目标 18 个。
- post-publish canary 不超过 15 次 API 请求。
- Worker 只做 D1 小查询、R2 get/head 和流式响应，不做全量 JSON 计算。
- GitHub Actions 只在日级窗口运行，取消同一 job 内 36 分钟的 readiness polling 后，分钟消耗会下降。
- R2 继续保存不可变对象；本轮不复制整桶、不启用额外生命周期副本。
- 不使用付费 Queue、Durable Object 或外部定时平台。

免费额度是否仍充足必须用实际用量验证。每个 PR 验收时记录：Actions 时长、R2 对象数和字节数、D1 读写次数、Worker 1102/5xx 数量、默认页与 Radar 的响应字节数。禁止只凭配置推断“肯定免费”。

## 14. 实施依赖与并行策略

| 步骤 | 主要模块 | 依赖 |
| --- | --- | --- |
| PR 1A 服务端兼容 | `web/db`、`web/app/api/v1`、`web/app/lib` | 当前 pressure 超限修复先进入基线 |
| PR 1B 离线发布正确性 | `src/trading_desk`、`scripts`、`.github/workflows` | PR 1A 已部署且 capabilities 验证通过 |
| PR 2 载荷治理 | `src/trading_desk/read_models.py`、`web/app`、API view 合同 | manifest v3 稳定 |
| PR 3 调度状态机 | `.github/workflows`、workflow tests | 单调激活与 canary 已上线 |

推荐顺序实施，不使用并行 worktree。PR 1A 和 1B 修改同一发布合同，PR 2 又依赖该合同，PR 3 依赖单调激活。并行开发会把兼容性风险转化成合并时的猜测，收益不高。

## 15. Definition of Done

全部完成必须同时满足：

- [ ] `origin/main` 包含当前 pressure 工件限额修复。
- [ ] D1 additive migration 已在本地和远端应用。
- [ ] 生产 capabilities 同时声明 v2/v3，后续生产者已经生成 v3。
- [ ] 同日旧任务不能覆盖新观测版本。
- [ ] 个股 pressure 历史从最近兼容发布续接，不再依赖请求的三个日期。
- [ ] 现有 manifest v2 + pressure schema v2 的历史可以被 v3 首次发布承接。
- [ ] 历史续接的所有工件都按选定 `releaseId` 精确读取，不按日期模糊选择。
- [ ] `previousItems=0` 只有真实首次 bootstrap 才合法。
- [ ] required artifacts 缺一个就不能激活。
- [ ] 发布后 canary 校验 current、historical、全部核心视图和一个 stock shard。
- [ ] 默认页、Radar、ETF 详情和国家队按需加载，不重复读取大对象。
- [ ] 每个 read model 小于 5 MB，并产生大小报告。
- [ ] workflow 不再内部轮询 36 分钟。
- [ ] 部分来源只归档，不进入 active release。
- [ ] 日期选择器只列出完整可读的 read-model release。
- [ ] Python、Worker、前端、workflow 合同和全量 build 测试全部通过。
- [ ] 生产连续 5 个交易日没有新的 1102，日期没有遗漏，长周期覆盖没有无故下降。
- [ ] 没有提交 `.gitignore`、`reports/phase0/latest-*` 或 `memory/` 的用户改动。

## 16. Luna 的交付格式

每个 PR 完成后，Luna 必须报告：

1. 修改了哪些合同，为什么。
2. 数据库迁移是否 additive，如何验证。
3. 新旧 manifest 是否兼容。
4. 跑了哪些测试，准确结果是什么。
5. 当前工件大小表。
6. 手动运行的 Actions URL、release ID、`asOf`、`observedAt` 和激活状态。
7. 生产 canary 结果。
8. 未解决风险和下一 PR 的前置条件。

如果实施过程中需要超出“NOT in scope”中的任一边界，先停止并把新的数据、成本和备选方案交给用户确认，不得自行扩张范围。

## 17. 工程评审结论

- Scope Challenge：已缩小范围。先解决发布顺序、历史续接和大载荷，不做 32/64 分片与自动回滚。
- Architecture Review：4 个主要冲突已消除，包括同日覆盖、版本部署竞态、日期语义和长任务取消。
- Code Quality Review：复用现有 read model、R2、D1 和 QA；未引入第二套发布系统。
- Test Review：已定义从官方响应到 canary 的完整路径，并覆盖失败分支。
- Performance Review：首要对象是 4.8 MB 的 bootstrap/radar，而不是 8 个 stock shard。
- Failure Modes：没有允许静默失败的新增路径。
- Parallelization：4 个顺序阶段，无推荐并行 lane。
- 状态：DONE，方案可交给实施者执行。
