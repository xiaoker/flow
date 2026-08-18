# Flow 数据正确性、历史连续性与发布可靠性总修复交接

> 交接对象：GPT-5.6 Luna（或后续实现者）
> 编写日期：2026-08-16
> 仓库：`xiaoker/flow`
> 生产地址：`https://flow.xiaoker.com`
> 优先级：P0 数据正确性，P0 发布一致性，P1 历史连续性，P1 Worker 稳定性
> 状态：待实施
> 依据：2026-08-14 生产 QA、D1/R2 只读核验、当前仓库代码审查

## 0. 给 Luna 的执行结论

这不是一组互不相关的页面 Bug。当前问题来自三条相互放大的链路：

```text
同一交易日存在多套派生口径
        │
        ├─ 首页只统计沪市，Radar/板块统计沪深
        └─ QA 只校验单个对象，不校验对象之间的恒等式
        │
        ▼
历史数据虽然保存在原始层，但没有完整物化到新 read model
        │
        ├─ 两融/互通从数百日退化为 3 日
        └─ ETF→个股历史只有 2 日，20 日指标不可计算
        │
        ▼
生产读取路径、发布指针、前端缓存与部署版本不一致
        │
        ├─ D1/R2 已有新 release，页面仍显示旧日期
        ├─ 搜索、国家队、个股详情读取到不同版本或旧包
        └─ 高频刷新把读取失败误报成采集失败
```

最终目标不是“让页面看到最新日期”，而是：

```text
最新可用日期
+ 同一 release 全页面数值可对账
+ 长周期不比上一活动版本退化
+ 缺失值不补造
+ 发布失败可回退
+ Worker 请求路径保持轻量
= 才允许切换 active release
```

### 0.1 必须遵守的修复顺序

```text
1. 冻结错误版本继续扩散，建立当前生产基线
2. 修复统一 ETF 事实层与跨工件 QA
3. 恢复两融、互通、ETF 与 ETF→个股历史物化
4. 补齐 NAV、北交所两融及可验证的个股增强字段
5. 生成候选 release，并用显式 releaseId 做 canary
6. 修复生产读取路径、部署版本与缓存
7. 修复页面状态、搜索、国家队表格和自动刷新提示
8. 原子切换 active pointer，连续观察 5 个交易日
```

禁止先修复“页面读取最新 active release”。2026-08-14 审计时的 active release 本身存在 ETF 合计不一致和历史退化。先把它直接暴露给页面，会把一个“旧但相对完整”的问题变成“新但错误且历史残缺”的问题。

---

## 1. 本文的优先级与覆盖关系

如果本文与以下旧交接文档冲突，以本文为准；无冲突部分继续复用：

- `docs/handoff/release-reliability-and-worker-budget.md`
- `docs/handoff/etf-beta-pressure-workbench.md`
- `docs/handoff/stock-detail-search.md`
- `docs/flow-runtime-architecture.md`
- `docs/data-quality-contract.md`
- `docs/history-backfill.md`

本文不要求重写整个系统。必须复用现有采集器、R2 原始层、D1 索引、read-model 发布器和 Worker 流式读取架构。

---

## 2. 开始实施前的仓库与生产基线

### 2.1 当前本地工作树

本文编写时：

```text
branch: codex/deploy-worker-workflow-v2
HEAD:   e86158f
origin/main: bef0f0e
local main: 7e9a7cf
```

当前工作树包含用户已有、尚未提交的改动：

```text
M  .github/workflows/backfill-history.yml
M  .github/workflows/daily-data.yml
M  .github/workflows/data-qa.yml
M  .github/workflows/deploy-worker.yml
M  .gitignore
M  reports/phase0/latest-manifest.json
M  reports/phase0/latest-quality-summary.json
M  reports/phase0/latest-release-qa.json
M  tests/unit/test_workflow_contract.py
?? memory/
```

这些改动不属于本文。不得覆盖、还原、暂存或提交。禁止：

```text
git reset --hard
git checkout -- <file>
git add .
git add -A
git clean -fd
```

推荐在新的 worktree 或干净克隆中，从最新 `origin/main` 创建 `codex/flow-data-integrity-remediation`，并只按 PR 顺序引入经过确认的修改。不要假设当前本地文件已经部署。

### 2.2 实施前必须保存的只读证据

在任何迁移、重新发布或 pointer 切换前，保存以下输出到一个新的 `reports/remediation-baseline/<timestamp>/`，不要覆盖 `reports/phase0/latest-*`：

```bash
git status --short --branch
git log -8 --oneline --decorate

# 只读查询，记录 active release、历史范围和发布状态。
(cd web && npx wrangler d1 execute flow --remote --command \
  "SELECT * FROM active_release; SELECT release_id, as_of, status, observed_at, created_at FROM release_manifest ORDER BY created_at DESC LIMIT 10;")

(cd web && npx wrangler d1 execute flow --remote --command \
  "SELECT COUNT(*) AS dates, MIN(as_of) AS from_date, MAX(as_of) AS to_date, SUM(quality_fail) AS failed FROM historical_snapshot;")

(cd web && npx wrangler d1 execute flow --remote --command \
  "SELECT COUNT(*) AS dates, MIN(trade_date) AS from_date, MAX(trade_date) AS to_date FROM market_history_daily;")
```

同时下载但不修改当前 active manifest 与 18 个工件，记录 SHA-256、字节数、`asOf`、历史点数和核心汇总。严禁在证据保存前重建或覆盖任何原始对象。

---

## 3. 可信事实基线

以下数字来自 2026-08-14 的生产 QA。它们是定位基线，不是永久常量。Luna 开始时必须重跑同样的只读核验，并在 PR 描述中列出差异。

| 项目 | 2026-08-14 观测值 |
| --- | --- |
| D1 ACTIVE release | `release:2026-08-13:ff4b307e291c4534` |
| 页面实际展示日期 | `2026-08-10` |
| `historical_snapshot` | 488 个交易日，2024-08-01 至 2026-08-13 |
| active overview ETF 1d | `-66.40 亿份` |
| active radar 1604 只 ETF 1d 求和 | `-111.93 亿份` |
| active rotation 14 个分组 1d 求和 | `-111.93 亿份` |
| radar 沪市 1d | `-66.33 亿份` |
| radar 深市 1d | `-45.60 亿份` |
| active margin 历史点 | 3 |
| active connect 历史点 | 3 |
| 旧生产 margin 历史点 | 约 485 |
| 旧生产 connect 历史点 | 约 139 |
| pressure availableDates | 2 个：2026-08-12、2026-08-13 |
| pressure 20 日覆盖 | `history20Coverage=0` |
| pressure 个股数 | 6078 |
| PCF 匹配 ETF | 497 / 1604 |
| 个股价格覆盖 | 0 |
| 个股两融覆盖 | 0 |
| ETF 金额估算覆盖 | 0 / 1604 |
| 国家队 R2 数据 | 12 只 |
| 国家队生产表格 | `0 / 12`，无表格行 |

完整 QA 报告：`.gstack/qa-reports/qa-report-flow-xiaoker-com-2026-08-14.md`。

---

## 4. 完整问题清单

### P0-01：生产读取路径与 D1 active release 分裂

**表现**

- D1/R2 已有 2026-08-13 完整 18 工件 release。
- 页面仍显示 2026-08-10。
- active `stock_search` 中存在“中天科技 / 600522”，生产搜索却找不到。
- active `pressure` 已存在，生产页却显示“尚未进入当前发布版本”。

**风险**

用户无法判断自己看到的是哪一个 release；日期、搜索、视图和个股分片可能跨版本拼接。

**修复要求**

1. 证明生产 Worker 的实际 Git SHA、manifest schema、active release ID 与仓库部署提交。
2. `/api/v2/releases/latest`、`/api/v2/dashboard`、每个 view 和 stock shard 都返回一致的：
   - `X-Flow-Release-Id`
   - `X-Flow-As-Of`
   - `X-Flow-Build-Sha`
   - `ETag`
3. 前端只允许合并同一 `releaseId + asOf` 的对象。
4. 历史日期不得回退到最新 release，最新 release 也不得回退到旧静态快照后伪装成当前数据。

**验收**

同一个浏览器会话中，首页、Radar、国家队、互通、两融、pressure、搜索和个股页的 release header 完全一致；`中天科技` 与 `600522` 都能搜索并进入同一日期的个股页。

### P0-02：ETF 首页与 Radar/板块轮动合计不一致

**已确认代码原因之一**

`src/trading_desk/dashboard.py:660` 当前调用：

```python
etf_flows, etf_summary = _latest_etf_flows(sse_etf, dates)
```

而 `etfTerminal`、ETF→个股模型使用的是 `sse_etf + szse_etf`。首页指标文案也仍写“上交所 ETF 份额变化”。这会形成两套事实路径。

**修复要求**

1. 建立唯一的 canonical ETF daily fact，业务键固定为：

```text
trade_date + market + instrument_code
```

2. 首页、ETF flows、Radar、板块轮动、国家队子集全部从同一个 `etfTerminal` 或同一个 canonical fact 派生。
3. 不再让 `_latest_etf_flows()` 单独读取只含沪市的 DataFrame；最好直接从 `etfTerminal.instruments[].periods.1d` 派生首页汇总和 Top 5。
4. 对账使用原始“份”或整数最小单位，在舍入前精确相等；显示层允许的误差最多 `0.01 亿份`。

**强制恒等式**

```text
overview ETF 1d raw total
= sum(radar instrument 1d raw values)
= sum(rotation group 1d raw values)
= sum(canonical ETF daily delta)

radar SSE total + radar SZSE total = radar all-market total

classified total + unclassified total = all eligible ETF total
```

任何恒等式失败，release 必须停留在 candidate/staged，不能切 active。

### P0-03：新 read model 截断两融和互通长期历史

**表现**

- 原始历史有约 488 个交易日。
- active `margin.json` 和 `connect.json` 只有 3 个点。
- 旧页面仍能展示约 485 个两融点、139 个互通点。

**原因方向**

`build_view_payloads()` 只把当前 `snapshot.marginSeries` / `connectSeries` 复制进 read model；日任务只采最近 3 个日期。现有 `market_history_daily` 与 `web/app/lib/market-history.ts` 能恢复长序列，但新 immutable read model 构建没有把它作为正式输入。

**修复要求**

1. 复用现有 `market_history_daily`，不新建第二套长历史数据库。
2. 在 GitHub Actions 离线构建 read model 前，获取截至 `asOf` 的 compact market history，最多 750 日。
3. 把这份历史作为 `build_read_models.py` 的显式输入，并纳入 release hash。
4. Worker 请求时不得读取数百个 R2 快照，不得现场拼接历史。
5. 新 release 的历史长度默认不得小于上一 active release。官方修订或明确的 source removal 例外必须带原因字段并经过人工确认。

**验收**

- margin 历史恢复到所有已确认官方点。
- connect 历史恢复到官方仍可追溯的点，不用相邻值填补官方 404。
- 选择任一历史日时，曲线只截止该日，不包含未来数据。

### P0-04：ETF→个股穿透历史不是独立事实，只靠上一 release 续接

**表现**

- 原始市场快照有 488 日。
- pressure 只有 2 个发布节点；页面从 `1/21` 变为 `2/21`，仍无法计算 20 日。
- 当前逻辑从上一 active release 的 8 个 stock shard 携带历史。发布链中断、schema 变更、旧版本不兼容或分片读取失败时，历史会重新开始。

**修复方向**

不要复制 21 份完整 release，也不要继续把“上一活动版本”当历史数据库。新增独立、不可变、可重建的日事实分区：

```text
R2
└── market-data/derived/stock-flow-daily/v2/
    └── YYYY-MM-DD/
        ├── manifest.json
        ├── stock_0.json.gz
        ├── ...
        └── stock_7.json.gz
```

每日分区只保存紧凑历史字段，不重复保存完整 contributor 列表：

```text
stockKey
listingMarket
stockCode
tradeDate
netShares
grossInflowShares
grossOutflowShares
penetratedShares
matchedEtfCount
alignedPcfEtfCount
lineageSha256[] 或 lineageManifestKey
calculationVersion
```

当前 release 构建时读取最近 21 至 63 个真实、日期兼容的 daily partition，离线计算 `rolling5/rolling20/historyCoverage`，再写入现有 `stock_0..7`。

**禁止**

- 用当前 PCF 回填过去日期。
- 复制当前节点 21 次。
- 把自然日当交易日。
- 用缺失值 0 补齐曲线。
- 因某天无发布而跨缺口计算 20 日累计。

**验收**

1. 至少 21 个真实、同口径发布日后，`history20Coverage > 0`。
2. 每个历史节点能追溯到对应 ETF 份额原始对象和有效 PCF 原始对象。
3. 同日重跑是幂等替换，不重复追加。
4. 任一分区损坏时，构建失败且上一 active release 保持不变。

### P1-05：历史 PCF 覆盖不足，不能宣称全市场 ETF→个股

**表现**

active 只匹配约 497 / 1604 只 ETF；跨境 ETF 还出现 PCF 有效日不匹配警告。

**修复要求**

先做 PCF inventory，而不是直接开始回填：

```text
每个交易日
  × 每个 ETF 市场
  × 官方 PCF 是否存在
  × published_date
  × PRE_TRADING_DAY / effective_date
  × 原始 SHA-256
  × 是否可用于 share_date
```

输出 `reports/remediation/pcf-inventory.json` 与 Markdown 摘要。只有官方原始 PCF 存在且日期可验证的交易日才能生成 stock-flow daily partition。

如果深市或某类 ETF 暂无稳定官方 PCF：

- 页面显示实际 `matchedEtfCount / eligibleEtfCount`。
- 文案明确“当前穿透覆盖沪市/已匹配 ETF”，不能写“全市场”。
- 未匹配 ETF 的贡献保持未知，不是 0。
- 是否引入第三方 PCF 必须另行取得用户授权。

### P1-06：个股价格与个股两融没有真正打通

**表现**

active pressure 的 `priceCount=0`、`marginCount=0`；个股详情金额、融资余额、20 日价格相关指标为空。

**修复要求**

1. 先接入官方个股两融明细，建立 `margin_security_daily` 规范数据集。
2. 市场必须分别保存 `SSE / SZSE / BSE`，同日同市场同代码唯一。
3. 价格和流通盘只有在公开、免费、稳定、可重放的官方批量来源得到验证后才接入。
4. 没有可信官方来源时保留 `null`，页面显示 `—` 与覆盖原因，不得静默使用东方财富、腾讯等第三方。
5. 个股金额估算需要同日官方收盘价；不使用最新价回填历史日。

### P1-07：ETF 金额估算全部不可用，但 UI 仍保留可用入口

**表现**

active `cashFlowEligible=0`；1604 只 ETF 的最近 1 日金额估算均不可用。

**修复要求**

1. 补采并保存官方 NAV 原始数据与 `nav_date`。
2. 每日金额只能按：

```text
当日份额变化 × 对应同日官方 NAV
```

3. 长周期金额是逐日金额求和，不能用区间份额差乘当前 NAV。
4. `nav_date != share_date` 时该日金额为 `null`。
5. read model 增加 capability：

```json
{
  "etfCash": {
    "enabled": false,
    "eligible": 0,
    "total": 1604,
    "reason": "official_nav_coverage_below_threshold"
  }
}
```

6. capability 不满足时，前端禁用或隐藏金额模式，并解释原因，不能展示空表或全 0。

### P1-08：北交所融资融券尚未并入

**表现**

当前只展示 SSE + SZSE；用户已明确要求采集 BSE 原始两融并合并。

**修复要求**

1. 在 `src/trading_desk/adapters/official.py` 增加北交所官方 adapter。
2. 原始响应与规范化 CSV 按现有 R2 规则不可变保存。
3. 规范层市场值为 `BSE`，不得混入 `SZSE`。
4. 复用现有 `margin_daily` 市场维度。
5. 对 `market_history_daily` 做 additive migration，至少增加：

```text
bse_balance
margin_scope_version
```

6. 页面从“沪深合计”改为“沪深北合计”，同时保留三市场拆分；旧 release 仍按旧 scope 明示，不静默改变历史指标含义。

**强制恒等式**

```text
cn_financing_balance = sse_balance + szse_balance + bse_balance
```

缺任一市场时总额为 `null/pending`，不能把两市场和伪装成三市场合计。

### P1-09：国家队观察池数据存在，但表格为空

**表现**

- R2 `national` 有 12 只 instruments。
- 页面摘要显示 12/12。
- 生产 DOM 曾显示 `0 / 12`，tbody 无行。

当前源码 `web/app/national-team-view.tsx` 已按 12 个代码构建 rows，因此必须先区分：旧 Worker/旧 bundle、全局搜索 query 污染、视图加载合并失败，还是渲染逻辑回归。

**修复要求**

1. 国家队继续是 ETF Radar 的代码子集，不新增采集源。
2. 名称使用该 12 只基金的基金简称；份额、周期和日期来自同 release 的 Radar 行。
3. 无搜索词时必须渲染 12 行；有搜索词时显示真实过滤结果。
4. `matchedCount`、`filteredRows.length`、表格行数和 `national.etfTerminal.instruments.length` 建立测试合同。
5. “持有人披露待接入”继续保持为空，不用截图或第三方估算国家队持仓。

### P1-10：QA 和 canary 只验证“对象存在”，没有验证语义一致

**表现**

当前 release QA 为 pass，但 P0-02 的 45.53 亿份差异仍被激活；canary 只检查视图可读。

**修复要求**

将发布门禁分成四层：

```text
L1 原始 → 规范：字段、日期、单位、代码、SHA
L2 规范 → canonical fact：唯一键、市场覆盖、相邻交易日
L3 canonical fact → read models：跨工件恒等式、历史覆盖、capability
L4 发布后：releaseId、asOf、ETag、搜索样本、渲染样本、性能
```

新增 `crossArtifactReconciliation`，至少包含：

```text
overview_vs_radar_etf_1d
radar_vs_rotation_etf_1d
radar_market_split
national_subset_of_radar
pressure_vs_stock_shards
stock_search_vs_stock_shards
margin_market_sum
connect_component_sum
artifact_release_identity
```

任何 critical invariant 失败都阻断 activation。

### P1-11：修复最新读取后可能暴露历史退化，缺少“不得倒退”门禁

新 candidate 与上一 active release 比较：

```text
margin history points
connect history points
ETF terminal trading days
pressure available dates
instrument count by market
cash eligible count
PCF matched ETF count
stock search count
national matched count
```

默认规则：关键覆盖不得下降。允许下降的情况必须在 manifest 中声明：

```json
{
  "coverageRegression": {
    "allowed": true,
    "reason": "official_revision_or_contract_change",
    "approvedBy": "manual"
  }
}
```

自动任务不得自行设置 `allowed=true`。

### P1-12：Worker Error 1102 与请求期间大对象处理风险

系统已经采用 read model 分片，但生产曾多次出现 `Worker exceeded resource limits`。

**目标请求路径**

```text
request
  → D1 读取一个小 pointer/manifest
  → R2 Get 一个目标工件
  → 不 JSON.parse、不聚合、不排序
  → 流式返回 body
```

**禁止**

- Worker 请求期间重算 ETF 状态。
- Worker 同时读取多个 1-5 MB 工件再合并。
- Worker 读取 488 个历史快照。
- 为过滤字段在 Worker 中解析整个 Radar/pressure。

继续执行现有大小预算：

| 工件 | 软目标 | 硬上限 |
| --- | ---: | ---: |
| bootstrap | 1,000,000 bytes | 5,000,000 |
| overview | 250,000 | 5,000,000 |
| radar | 2,000,000 | 5,000,000 |
| radar_flow | 3,000,000 | 5,000,000 |
| pressure | 3,000,000 | 5,000,000 |
| national | 500,000 | 5,000,000 |
| stock_search | 1,000,000 | 5,000,000 |
| 单个 stock shard | 2,000,000 | 5,000,000 |

### P2-13：自动更新提示把读取失败误报为采集失败

当前 UI 的“自动更新连续失败”不能区分失败层。

新增四层状态：

```text
collectionStatus   官方来源采集
buildStatus        离线计算与 QA
activationStatus   release 是否激活
clientReadStatus   浏览器读取 active release
```

页面必须显示具体层、最后成功时间、active release ID 和 `asOf`。例如：

```text
官方采集已完成 · 2026-08-13
候选版本 QA 阻断 · overview 与 Radar 差 45.53 亿份
当前仍展示 release:2026-08-12:... 的完整数据
```

不能把客户端超时写成“采集失败”。

### P2-14：自动刷新频率、加载状态和缓存策略仍需生产验证

当前源码已改为每 30 分钟只检查 `/api/v2/releases/latest`，release 未变化时不下载完整数据。但生产曾仍出现高频“正在读取最新发布版本”和错误条，说明部署版本或状态合并可能不一致。

**修复要求**

1. 最新页：首次打开、手动刷新、页面重新获得焦点且距上次检查超过 30 分钟时检查 pointer。
2. 历史页不自动刷新日期。
3. pointer 不变时不下载 bootstrap/view。
4. 已有完整页面时，后台检查不得显示全屏 loading。
5. pointer/manifest 使用 `no-store` 或强制 revalidate；按 release ID 的 immutable 工件使用 ETag 和长期私有缓存。
6. refresh error 只显示局部、可恢复提示；三次连续失败后也不能清空当前完整数据。

### P2-15：日期选择器与“原始有数据”概念混淆

主日期选择器只能列出具备完整、可读、已通过 QA 的 read-model release。原始文件已经归档但页面尚未完成构建的日期，只能出现在诊断状态，不得进入主选择器。

```text
raw archived date ≠ complete published date
```

选择历史日期时，所有视图、搜索和个股分片必须按该日期的 releaseId 读取；缺工件时返回明确 unavailable，不回退到最新日。

### P2-16：部署分支、生产提交和本地状态不透明

`origin/main` 已有自动部署 workflow，但本地分支、local main、origin/main 和生产 Worker 可能不同步。

**修复要求**

1. 只允许 `main` 的受保护 push 或手动 workflow 部署生产。
2. workflow 写入 `FLOW_BUILD_SHA=${GITHUB_SHA}`。
3. `/api/v2/releases/latest` 或独立小型诊断接口返回 build SHA、部署时间和 schema versions，不返回 secret。
4. canary 必须断言线上 `X-Flow-Build-Sha == GITHUB_SHA`。
5. PR 合并、deploy workflow 成功、生产 canary 成功三者缺一不能回复“已上线”。
6. GitHub Actions 继续使用 commit SHA 固定依赖，并保持 Node 24 兼容；不得退回曾触发 Node 20 deprecated 警告的 action 版本。`tests/unit/test_workflow_contract.py` 必须锁定这一合同。

### P2-17：原始数据保留与 R2 生命周期缺少可验证恢复合同

历史数据曾出现“页面看不到，误以为原始数据丢失”的情况。必须把“原始存在”“规范数据存在”“派生 read model 可读”分开监控。

**修复要求**

1. `market-data/raw/` 继续不可变保存并保持 Bucket Lock；不得配置自动过期。
2. normalized、daily fact 和 release objects 都必须有 manifest、SHA-256 和 lineage。
3. 只有能从 raw + code version 确定性重建的派生对象才允许设置生命周期；设置前必须先做对象 inventory 和恢复演练。
4. 如果未来要在生命周期到期前复制，先生成 source/destination manifest，云端复制后逐对象核对 SHA，再切换引用；不能只复制文件名后假设成功。
5. 在免费额度下不默认维护整桶双份副本。优先长期保留 raw，派生对象按可重建性治理。
6. 增加每日存储健康摘要：最早/最新 raw date、每数据域日期数、missing dates、orphan manifests、无法读取对象数。

**验收**

- 随机选择一个历史日，能从 raw 重建 normalized、market history 和 ETF state。
- 删除本地缓存后仍能从 R2 原始对象恢复。
- 页面缺历史时，诊断能明确是“未物化”而不是“原始丢失”。

---

## 5. 目标架构

```text
┌────────────────────────── 官方来源 ──────────────────────────┐
│ SSE ETF/NAV/PCF   SZSE ETF/PCF   HKEX 中文历史页            │
│ SSE/SZSE/BSE 两融汇总与明细   可验证的官方个股行情          │
└──────────────────────────────┬───────────────────────────────┘
                               │
                               ▼
                    GitHub Actions 采集窗口
                               │
              ┌────────────────┴────────────────┐
              ▼                                 ▼
       R2 immutable raw                  normalized facts
       URL/params/body/SHA              日期/单位/市场/业务键
              │                                 │
              └────────────────┬────────────────┘
                               ▼
                     canonical daily fact layer
             ┌─────────────────┼──────────────────┐
             ▼                 ▼                  ▼
       ETF daily fact    market_history_daily  PCF daily fact
             │                 │                  │
             │                 │                  ▼
             │                 │          stock_flow_daily partitions
             └─────────────────┴──────────────────┘
                               │
                               ▼
                  offline read-model candidate build
                               │
                cross-artifact + coverage regression QA
                               │
                    ┌──────────┴──────────┐
                    │ fail                │ pass
                    ▼                     ▼
             evidence only         R2 immutable release objects
                                          │
                                  explicit release canary
                                          │
                                          ▼
                              D1 atomic active pointer switch
                                          │
                                          ▼
                             Worker pointer lookup + R2 stream
                                          │
                                          ▼
                              browser lazy view by releaseId
```

### 5.1 什么继续复用

| 能力 | 现有位置 | 处理方式 |
| --- | --- | --- |
| 官方采集与规范化 | `src/trading_desk/adapters/official.py`、`phase0.py` | 扩展，不重建 |
| 原始证据与历史 bundle | `storage.py`、`history_bundle.py`、`history_publish.py` | 保持不可变 |
| ETF 滚动状态 | `etf_flow_state.py` | 作为 canonical ETF 派生基础 |
| ETF→个股纯计算 | `etf_stock_flow.py` | 复用公式，增加 daily partition 输出 |
| pressure 计算 | `stock_pressure.py` | 改为消费 daily partitions，不依赖上一 active release |
| read model/manifest | `read_models.py` | 扩展合同与 QA，不重写 |
| 发布器 | `publish_read_models.py` | 增加 stage-only/candidate 流程 |
| 发布后 canary | `verify_published_release.py` | 增加语义与 build SHA 校验 |
| D1 compact history | `market_history_daily` | 继续作为两融/互通长历史索引 |
| Worker 流式读取 | `release-store.ts`、API v2 routes | 保持轻量 |
| 国家队观察池 | `NATIONAL_TEAM_CODES` + Radar | 继续作为子集，不新增源 |

---

## 6. 统一数据合同

### 6.1 日期合同

每个 release 必须分开记录：

```text
asOf                  用户决策日/页面日期
observedAt            完整证据被观察到的 UTC 时间
sourceDates.*         各官方来源业务日
publishedDate         官方文件发布日期
effectiveTradeDate    文件实际对应的交易日
dateBasis             日期解释规则版本
```

已确定的产品规则：

- 上交所 ETF：按官方业务日保存。
- 深交所 ETF：列表页显示一个发布日期，Excel/列表数据按已验证规则归属于前一交易日；必须保存页面日期与 effective trade date，不能只保留一个模糊日期。
- 港交所互联互通：使用简体中文历史每日页；北向只展示官方成交活跃度，不伪造净流入。
- PCF：保存 published date、`PRE_TRADING_DAY` 与 effective date；次日 PCF 只有在官方字段明确指向前一交易日时才能用于该日前一日份额计算。
- 两融：按各交易所官方 trade date；BSE 与 SSE/SZSE 分开。

### 6.2 ETF canonical fact

复用现有 `etf_daily` 的规范字段，唯一键：

```text
release_id + trade_date + market + instrument_code
```

派生计算使用不含 `release_id` 的业务身份：

```text
trade_date + market + instrument_code
```

最小字段：

```text
trade_date
market                    SSE | SZSE
instrument_code
instrument_name           官方基金简称
shares_raw
shares_unit
nav
nav_date
category
classification_version
date_basis
source_id
source_url
observed_at
raw_sha256
```

任何名称映射都不能代替市场+代码。新上市 ETF 从首次官方出现日起存在；退市 ETF 保留历史并在证券维表标记 inactive，不删除历史。

### 6.3 ETF 分类合同

Overview、Radar、板块轮动和国家队必须使用同一 `classificationVersion`。当前版本为 `2026-07-v3` 时，所有 read model 必须携带同一值。

每只 eligible ETF 必须恰好进入一个组：

```text
行业/主题/宽基/债券/商品/货币/港股海外/红利价值/其他/未归类
```

无法可靠分类的保留在“未归类”，不能静默丢弃。

### 6.4 market history 合同

`market_history_daily` 是两融和互通曲线的 compact fact，不是页面缓存。新增 BSE 后至少包含：

```text
trade_date
sse_balance
szse_balance
bse_balance
margin_balance
margin_scope_version
southbound_net
northbound_turnover
availability flags
source_snapshot_sha256
observed_at
```

每行能由对应 historical snapshot 重建；`scripts/rebuild_remote_market_history.py` 必须继续可重放。

### 6.5 stock-flow daily 合同

每个日分区必须携带：

```json
{
  "schemaVersion": 2,
  "tradeDate": "2026-08-13",
  "sharePreviousDate": "2026-08-12",
  "pcfPublishedDate": "2026-08-14",
  "pcfEffectiveDate": "2026-08-13",
  "calculationVersion": "etf-stock-pressure-v2",
  "coverage": {},
  "lineage": {},
  "artifacts": {}
}
```

当前 stock detail 的 contributors 只保留当日 Top N；历史 daily partition 不重复保存 contributors，避免 R2 膨胀。

### 6.6 release manifest 合同

优先在现有 v3 上做 additive 扩展，不为本任务主动制造一次 v4 大迁移。只有现有 validator 无法安全区分新旧激活门禁时，才在单独服务端兼容 PR 中引入 v4。

推荐的 additive 控制字段：

```json
{
  "schemaVersion": 3,
  "releaseId": "release:YYYY-MM-DD:<hash>",
  "asOf": "YYYY-MM-DD",
  "observedAt": "...Z",
  "producerBuildSha": "data workflow git sha",
  "sourceDates": {},
  "classificationVersion": "2026-07-v3",
  "calculationVersions": {},
  "capabilities": {},
  "coverage": {},
  "reconciliation": {},
  "previousActiveReleaseId": "...",
  "requiredArtifacts": [],
  "artifacts": {}
}
```

Worker 部署 SHA 与数据 producer SHA 是两个概念：前者通过 `X-Flow-Build-Sha` 暴露，后者写入 manifest。新字段先由 Worker/import route 宽容读取；当 capabilities 声明 `semanticQaV1=true` 后，生产者才把这些字段升级为激活必需项。如果最终必须升 v4，仍遵守“服务端先兼容、生产者后切换”。

---

## 7. 实施计划与 PR 顺序

### PR 0：只读基线、失败测试与候选发布开关

**目标**：先把错误变成可重复的测试，不改变生产读取结果。

**工作**

1. 保存第 2.2 节基线。
2. 为 P0-02 写失败回归测试，证明首页只统计 SSE。
3. 为 P0-03 写历史长度回归测试。
4. 为 P0-04 写“上一 release 不兼容时不能静默从 1 日开始”的测试。
5. 给发布器增加 `--stage-only` 或等价 candidate 模式；candidate 写入 immutable 对象和 manifest，但不切 active pointer。
6. 增加按显式 `releaseId` 读取 candidate artifact 的 canary 支持。

**验收**

- 新测试在修复前稳定失败。
- candidate 可完整上传、验证、删除指针影响为零。
- 现有 active pointer 不变。

### PR 1：统一 ETF fact 与跨工件 QA

**主要模块**

```text
src/trading_desk/dashboard.py
src/trading_desk/etf_flow_state.py
src/trading_desk/read_models.py
src/trading_desk/release_qa.py
tests/unit/test_dashboard.py
tests/unit/test_etf_flow_state.py
tests/unit/test_read_models.py
tests/unit/test_release_qa.py
```

**工作**

1. 抽出一个显式的沪深 canonical ETF frame/terminal 构建入口。
2. 删除首页独立的 SSE-only 汇总路径；首页、Top 5、Radar、rotation、national 从同一 terminal 派生。
3. 加入第 4 节所有 ETF 恒等式。
4. 让 release QA 输出按市场、分类、周期和 coverage 的对账矩阵。
5. 未归类 ETF 保留；重复市场+代码+日期阻断发布。

**验收**

- fixture 和生产规模样本中，overview/radar/rotation 的 1d 原始值精确一致。
- SSE + SZSE = all-market。
- 国家队 12 个代码是 Radar 子集。
- 同一输入生成相同 release hash。

### PR 2：恢复两融/互通/ETF 长历史物化

**主要模块**

```text
web/app/lib/market-history.ts
web/app/api/v2/market-history/route.ts
src/trading_desk/read_models.py
scripts/build_read_models.py
scripts/rebuild_remote_market_history.py
.github/workflows/daily-data.yml
tests/unit/test_market_history_rebuild.py
tests/unit/test_read_models.py
```

**工作**

1. 给 `build_read_models.py` 增加显式 `--market-history` 输入。
2. 日任务在 read-model build 前获取/生成截至 `asOf` 的 compact history。
3. 构建 `margin`、`connect`、必要的 `overview` 历史时只使用 `trade_date <= asOf`。
4. 把历史输入的 hash 写入 manifest。
5. 加入与 previous active 的历史长度回归门禁。
6. 继续使用 `etf_flow_state` 的真实相邻交易日序列生成 ETF 周期，不把最近 3 日输入当全部历史。

**验收**

- 两融历史不低于上一 active 的已确认点数。
- 互通历史不低于上一 active 的已确认点数。
- ETF 1d/3d/1w/1m 等周期严格满足 1/3/5/21 个相邻变化点。
- 任一历史日期不含未来值。

### PR 3：建立 stock-flow daily partitions 并回补真实历史

**主要模块**

```text
src/trading_desk/etf_stock_flow.py
src/trading_desk/stock_pressure.py
src/trading_desk/history_bundle.py
scripts/build_etf_stock_flow.py
scripts/build_stock_pressure_state.py
scripts/publish_history_artifacts.py
scripts/build_read_models.py
tests/unit/test_etf_stock_flow.py
tests/unit/test_stock_pressure.py
tests/unit/test_history_publish.py
```

**工作**

1. 先输出 PCF inventory。
2. 为每个有真实份额 pair 和有效 PCF 的日期生成 daily partition。
3. 从 R2 既有原始对象回补至少最近 21 个有效交易日；如果官方 PCF 缺失，记录缺失，不伪造。
4. current release 从 daily partitions 计算历史，不再读取上一 active 的完整 stock shards 作为唯一来源。
5. `stock_search` 继续是当前证券维表；不要每日复制 21 份搜索索引。
6. 每日 partition、当前 pressure 和 stock shard 三者做逐股总额对账。

**验收**

- `availableDates` 反映真实有效分区。
- 达到 21 个有效日后 rolling20 可计算；不足时显示实际覆盖。
- 中天科技等固定样本可从 daily partition 逐日复算。
- 当前 release 中 `pressure.summary == sum(stock shards current nodes)`。
- 回补任务可断点续跑、重复执行不产生重复对象。

### PR 4：补齐 NAV 与北交所两融

**主要模块**

```text
src/trading_desk/adapters/official.py
src/trading_desk/quality.py
src/trading_desk/models.py
src/trading_desk/dashboard.py
src/trading_desk/release_qa.py
web/db/schema.ts
web/drizzle/<new additive migration>.sql
web/app/lib/market-history.ts
web/app/dashboard.tsx
```

**工作**

1. 重新核验 SSE/SZSE 官方 NAV 的下载与日期字段，补采可获得日期。
2. 增加 NAV coverage capability，金额模式按 capability 呈现。
3. 增加 BSE 两融官方 adapter、原始归档、规范化和 QA。
4. 给 `market_history_daily` 添加 BSE 字段和 scope version；迁移只能 additive。
5. 页面显示三市场拆分与合计，旧历史按原 scope 标签展示。

**验收**

- 每个金额值能追溯到同日 NAV。
- NAV 缺失不产生 0 或最新值回填。
- BSE 不混入 SZSE。
- 三市场合计恒等式通过。
- 本地和远端迁移均验证，旧 release 仍可读。

### PR 5：发布指针、生产读取、缓存与 Worker 资源治理

**主要模块**

```text
web/app/lib/read-model-contract.ts
web/app/lib/release-store.ts
web/app/api/v1/admin/read-model-import/route.ts
web/app/api/v1/admin/releases/artifact/route.ts
web/app/api/v2/releases/latest/route.ts
web/app/api/v2/dashboard/route.ts
web/app/api/v2/views/[view]/route.ts
web/app/api/v2/stocks/[shard]/route.ts
web/worker/index.ts
scripts/publish_read_models.py
scripts/verify_published_release.py
.github/workflows/deploy-worker.yml
```

**工作**

1. Worker 先兼容 v3 的新增语义字段；只有合同验证确实需要时才引入 v4，并保持 v3 可读。
2. 增加 build SHA/部署时间诊断与响应 header。
3. candidate canary 通过后才允许原子激活。
4. 所有 endpoint 使用同一 release resolver；历史日期与 latest 分开。
5. Worker 只做 pointer + R2 stream。
6. pointer 与 immutable artifact 使用不同缓存策略。
7. deploy workflow 验证远端 migration、build、deploy SHA 和 production canary。

**验收**

- 生产 `X-Flow-Build-Sha` 等于部署 workflow 的 `GITHUB_SHA`。
- 所有视图 response header 与 body 日期一致。
- 100 次连续默认页/API 冒烟不出现 1102/5xx。
- 默认页不加载 Radar、pressure 或 stock shards。
- release 未变化的 30 分钟检查只请求 pointer。

### PR 6：前端搜索、国家队、日期、状态与错误体验

**主要模块**

```text
web/app/dashboard-bootstrap.tsx
web/app/dashboard.tsx
web/app/national-team-view.tsx
web/app/stock-detail-view.tsx
web/app/etf-pressure-view.tsx
web/app/lib/dashboard-data.ts
web/tests/*.test.mjs
```

**工作**

1. 统一全局搜索使用当前 release 的 `stock_search + etfSearch`。
2. 搜索名称、代码、去空格与大小写；固定回归样本“中天科技 / 600522”。
3. ETF 下钻、搜索直达和独立个股页使用稳定 URL：

```text
/?view=stock&venue=<market>&code=<code>&date=<asOf>
```

4. 国家队无查询时渲染 12 行，搜索只过滤视图不改变数据源。
5. 主日期选择器只展示 complete release dates。
6. 自动更新状态分四层，不再泛化成“采集失败”。
7. 有完整旧数据时，局部加载失败不显示全屏 loading。
8. capability 不满足时禁用金额、20 日或两融相关控件，并显示覆盖原因。

**验收**

- 中文名和代码搜索均成功。
- ETF→个股、搜索→个股、刷新个股 URL 三条路径结果一致。
- 国家队 12 行与 release 数据一致。
- 历史页不读取最新 stock shard。
- 所有 `null` 显示 `—`，不显示 0。

### PR 7：采集窗口状态机与自动化恢复

当前 workflow 仍含最多 18 次、每次 120 秒的 SZSE readiness polling，并使用 `cancel-in-progress: true`。这会让窗口重叠、取消与归档/发布状态难以解释。

**目标状态机**

```text
evening
  ├─ 采 SSE/HKEX/已到达来源
  └─ source-not-ready = PENDING，不激活，不算任务故障

morning
  ├─ 采 SZSE/两融/已到达来源
  └─ 完整则进入候选构建

reconcile
  ├─ 补 PCF/NAV/缺失来源
  ├─ 对同一 asOf 做幂等 finalize
  └─ 只有完整且 QA 通过才激活
```

**工作**

1. 每个窗口单次采集，只保留短网络重试，不在 job 内等待 36 分钟。
2. `SOURCE_NOT_READY` 是 pending/neutral；parser/schema/auth/network failure 才是 failure。
3. 每个 source artifact 独立幂等归档。
4. 同一 asOf 的 read model 只在完整条件首次满足或官方修订时构建。
5. Cloudflare Cron 为主触发，GitHub schedule 继续错峰 failover；通过 ingest run key 去重。
6. 任务失败保留证据；不因失败覆盖 active release。

**验收**

- 没有 `sleep 120` 的长 readiness loop。
- source 尚未发布时 job 状态和用户提示是 pending，而非笼统失败。
- 同一交易日多个触发不会相互覆盖较新的同日修订。
- 连续 5 个交易日没有漏采；缺失能在 reconcile 自动补齐。

---

## 8. 自动测试合同

### 8.1 测试路径图

```text
CODE PATHS                                      USER FLOWS

官方响应                                        打开最新页
├─ HTTP/认证失败                                 ├─ pointer 可读 → 完整页面
│  └─ [UNIT] 保存错误证据，不激活                ├─ pointer 未变 → 不重载大对象
├─ source date 未就绪                            └─ pointer 失败 → 保留旧完整页并提示读取层
│  └─ [UNIT] PENDING，不当作 parser failure
└─ 完整响应                                      选择历史日
   ├─ [UNIT] raw SHA/日期/单位                   ├─ 只列 complete release
   ├─ [UNIT] canonical 唯一键                    ├─ 全视图同 releaseId
   └─ [UNIT] source→fact 对账                    └─ 缺工件不回退最新

canonical facts                                 搜索与下钻
├─ [CRITICAL REGRESSION] 沪深 ETF 合并           ├─ 搜“中天科技”
├─ [CRITICAL] market/category totals             ├─ 搜“600522”
├─ [CRITICAL] margin SSE+SZSE+BSE                 ├─ ETF → 个股
└─ [CRITICAL] date-compatible PCF                 └─ 刷新个股 URL

candidate release                               国家队
├─ [UNIT] deterministic hash                     ├─ 无查询显示 12 行
├─ [UNIT] required artifacts                     ├─ 查询显示真实过滤数
├─ [UNIT] coverage non-regression                 └─ 数据来自同 release Radar 子集
├─ [UNIT] cross-artifact invariants
└─ [E2E] explicit release canary                 自动更新
                                                  ├─ 30 分钟仅检查 pointer
active switch                                     ├─ release 变化才下载新数据
├─ [INTEGRATION] monotonic observedAt             └─ 失败不清空旧数据
├─ [INTEGRATION] atomic pointer
└─ [E2E] production headers/build SHA            Worker 资源
                                                  ├─ 单请求单工件流式返回
                                                  ├─ 100 次冒烟无 1102
                                                  └─ artifact 大小门禁
```

### 8.2 Python 必须新增/修改

| 文件 | 必须覆盖 |
| --- | --- |
| `tests/unit/test_dashboard.py` | SSE+SZSE 首页合计、Top 5、市场代码防碰撞、缺某市场时 pending |
| `tests/unit/test_etf_flow_state.py` | 相邻真实交易日、长周期覆盖、未归类、同日幂等 |
| `tests/unit/test_release_qa.py` | overview/radar/rotation 恒等式、市场拆分、coverage regression |
| `tests/unit/test_read_models.py` | 长历史输入、扩展 manifest 合同、capabilities、deterministic hash、大小 |
| `tests/unit/test_etf_stock_flow.py` | PCF 日期、ETF 市场键、逐股贡献、lineage |
| `tests/unit/test_stock_pressure.py` | daily partitions 合并、21 日、缺口、损坏分区、rolling20 |
| `tests/unit/test_market_history_rebuild.py` | SSE/SZSE/BSE、互通 404、截止日期、可重建性 |
| `tests/unit/test_official_adapters.py` | BSE、NAV、响应 schema 变化、空响应、日期语义 |
| `tests/unit/test_workflow_contract.py` | 无长轮询、归档先于构建、candidate 先于 activate、canary 顺序 |

### 8.3 Worker/API 必须覆盖

1. manifest v3/v4 过渡兼容。
2. stage-only 不改变 active pointer。
3. required artifact 缺一个不激活。
4. 同日旧 `observedAt` 晚到不能覆盖新版本。
5. 历史日期按精确 releaseId 读取。
6. 每个 response header 与 body release/date 一致。
7. stock search 的 shard 指向可读取对象。
8. build SHA 与部署提交一致。
9. R2 对象不存在、SHA/bytes 不一致、ETag 条件请求分别走明确错误。
10. Worker 路径没有大对象 `JSON.parse` 或多工件聚合。

### 8.4 前端必须覆盖

1. `中天科技` 与 `600522` 搜索。
2. ETF 下钻到个股。
3. 直接 URL 打开个股并刷新。
4. release mismatch 时拒绝拼接。
5. 国家队无搜索词渲染 12 行。
6. 1d/1w/1m coverage 不足时显示 unavailable，而不是重复 1d。
7. 金额 capability 关闭时控件禁用并说明。
8. 历史日期不会跳回最新。
9. 视图加载失败保留 base snapshot。
10. 自动刷新 pointer 未变时不请求大工件。
11. 南北互通信息卡在 1440/2048/390 像素宽度下不重叠、不截断。
12. 日期选择器可键盘操作、可选择所有 complete release dates，并明确显示未发布日期。
13. 国家队相对变化与绝对份额图均有可辨识 y 轴范围，不把小变化压成直线。
14. 正文、表头、辅助说明维持可读字号；不通过缩小字体解决拥挤。

### 8.5 固定人工抽样

每个 candidate 至少人工复核：

- 3 只 ETF：SSE、SZSE、跨境/特殊日期各一只。
- 3 个板块：一个净流入、一个净流出、一个未归类。
- 3 只个股：中天科技 600522、一个深市、一个 BSE 两融标的。
- 两融三市场当日官方总额。
- 互通南向净买入与北向成交额。

抽样必须记录原始 URL、原始日期、代码、名称、单位、手算公式和 release ID。

---

## 9. 发布门禁

### 9.1 构建前

- 原始响应已保存或明确标记 source-not-ready。
- 日期语义能映射到 `asOf`。
- 规范业务键唯一。
- 核心来源完整；可选来源缺失只关闭 capability。

### 9.2 candidate 激活前

- `quality.fail == 0`。
- 全部 required artifacts 已上传，SHA/bytes 匹配。
- ETF 跨工件恒等式通过。
- 历史 coverage 不退化。
- pressure 与 stock shards 对账。
- 搜索索引与 stock shards 对账。
- candidate canary 全部通过。
- artifact 硬上限通过。

### 9.3 激活后

- active pointer 指向 candidate release ID。
- production response build SHA 正确。
- `/api/v2/history/dates` 包含 `asOf`。
- 7 个主页面、搜索与一个 stock shard 可读。
- 条件请求返回正确 304。
- 无 1102/5xx。

---

## 10. 失败模式与用户可见行为

| 失败模式 | 自动处理 | 用户看到 | 是否允许激活 |
| --- | --- | --- | --- |
| SZSE 尚未发布目标日 | 归档已到来源，标记 pending | “深市数据待发布，当前仍显示上一完整日” | 否 |
| parser/schema 变化 | 失败并保存原文 | “官方源结构变化，候选版本未发布” | 否 |
| overview 与 Radar 不一致 | cross-artifact gate 阻断 | 当前 active 不变 | 否 |
| market history 只有 3 点 | coverage regression 阻断 | 当前 active 不变 | 否 |
| PCF 历史缺失 | 对应日不建 partition | 实际覆盖率与缺失原因 | 可激活，但不能启用 20 日指标 |
| NAV 缺失 | amount=null，关闭金额 capability | 金额模式不可用及原因 | 是 |
| BSE 未到齐 | 三市场合计 pending | 分市场状态 | 否，若 manifest 声明三市场口径 |
| candidate 某工件缺失 | 保持 staged | 当前 active 不变 | 否 |
| Worker 读取 R2 失败 | 返回局部错误，保留已显示数据 | “当前视图暂不可读” | 不改变 active |
| 浏览器 release mismatch | 拒绝合并并稍后重试 | “新版本仍在发布” | 不适用 |
| production build SHA 不符 | deploy canary 失败 | 不宣称上线 | 否 |
| raw 存在但派生历史缺失 | 触发重建任务，不重新采集/覆盖 raw | “原始已归档，派生历史待重建” | 否，若影响必需视图 |

任何“无测试 + 无错误处理 + 用户无提示”的路径都视为 critical gap。

---

## 11. 免费资源与成本约束

不新增付费数据库、Queue、Durable Object、常驻服务器或第三方调度服务。

### 11.1 允许使用

- GitHub Actions 日频离线计算。
- 现有 Cloudflare Worker。
- 现有 D1：小索引、manifest、pointer、compact market history。
- 现有 R2：不可变 raw、normalized、daily partitions、read models。
- Cloudflare Cron 主触发 + GitHub schedule failover。

### 11.2 成本控制

1. daily stock-flow 只保存紧凑字段，不复制 contributor 和搜索索引。
2. read model 维持约 18 个工件，不因历史新增数十个请求时工件。
3. daily partitions 只在每天离线构建时批量读取；用户请求不读取 21 天分区。
4. D1 不保存大 JSON 正文。
5. 每次构建输出：

```text
R2 当前总字节
本次 raw/normalized/daily/release 增量
对象数量增量
D1 行数与数据库大小
Actions 用时
Worker 请求数/CPU/1102/5xx
```

6. 70% 发预警，80% 阻止新增非必要派生副本；不得自动删除原始证据。
7. 免费额度必须以 Cloudflare/GitHub 当前账户用量实测，不能只引用文档中的理论限额。

---

## 12. 生产发布与回滚

### 12.1 上线顺序

```text
服务端 additive 兼容
  → 远端 D1 additive migration
  → capabilities 验证
  → producer 生成 candidate
  → 显式 releaseId canary
  → 人工抽样
  → 原子切 active pointer
  → production canary
  → 5 个交易日观察
```

### 12.2 回滚原则

- 原始对象、normalized facts 和 candidate 对象都不可删除。
- 代码回滚与数据 pointer 回滚分开。
- 新 release 误导用户时，active pointer 可回指最后一个已知正确且完整的 release。
- 不使用 `git reset --hard`、R2 覆盖或删除对象回滚。
- 回滚后保留失败 release、QA、canary 和原因，便于纠正发布。

---

## 13. 本地与 CI 验证命令

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
PYTHONPATH=. .venv/bin/python -m pytest -q

cd web
npm ci
npm run db:migrate:local
npm test
npm run lint
cd ..

# 用生产规模但不写远端的已下载 fixture 构建 candidate。
.venv/bin/python scripts/build_read_models.py \
  --snapshot dashboard-snapshot.json \
  --output-dir read-models \
  --stock-catalog stock-pressure-catalog.json \
  --require-materialized

.venv/bin/python scripts/report_read_model_sizes.py \
  --read-model-dir read-models \
  --output artifact-size-report.json
```

远端迁移、发布、workflow dispatch 和 active pointer 切换只有在上述测试全部通过后进行。命令输出不得打印任何 Cloudflare/GitHub secret。

---

## 14. Definition of Done

只有以下全部满足，才能回复用户“完整修复并上线”：

### 数据正确性

- [ ] Overview、Radar、rotation ETF 1d 精确对账。
- [ ] 沪市 + 深市 = 全市场。
- [ ] 分类标准和版本全页面一致。
- [ ] ETF 名称为官方基金简称，代码/市场/日期一致。
- [ ] 两融 SSE/SZSE/BSE 分开且合计正确。
- [ ] 互通南向净买入口径与北向成交活跃度口径没有混淆。
- [ ] 金额估算只使用同日官方 NAV。

### 历史

- [ ] 两融和互通历史没有从上一 active 退化。
- [ ] ETF 周期来自真实相邻官方快照。
- [ ] stock-flow daily partitions 可从原始数据重建。
- [ ] 达到 21 个真实有效日后 rolling20 可计算。
- [ ] 官方历史 PCF 不足时，页面明确显示真实覆盖，不伪造历史。

### 发布与读取

- [ ] candidate 先验证、后激活。
- [ ] required artifacts 缺一个都不能激活。
- [ ] 所有页面/接口使用同一 releaseId 和 asOf。
- [ ] production build SHA 与部署提交一致。
- [ ] 日期选择器只列 complete release。
- [ ] 历史日期不回退到最新。

### 页面

- [ ] “中天科技”和“600522”均可搜索。
- [ ] ETF 下钻和搜索直达均能进入个股页。
- [ ] 国家队无搜索词显示 12 行。
- [ ] 1周/1月/20日覆盖不足时不重复展示 1日数值。
- [ ] 自动更新不高频、不全屏闪烁、不误报采集失败。
- [ ] `null` 显示 `— + 原因`，不是 0。

### 稳定性与成本

- [ ] 每个 read model < 5 MB，软预算报告通过或有明确解释。
- [ ] Worker 请求期间不解析/聚合大 JSON。
- [ ] 100 次冒烟无 1102/5xx。
- [ ] 连续 5 个交易日自动采集、补齐、构建和激活没有遗漏。
- [ ] R2/D1/Actions/Worker 用量仍在免费计划预算内。
- [ ] 原始数据健康摘要无无法读取对象，随机历史日恢复演练通过。
- [ ] 所有 workflow action 仍为 SHA 固定且 Node 24 兼容，无 Node 20 deprecated 警告。
- [ ] 未覆盖、还原、暂存或提交用户原有 dirty files。

---

## 15. 明确不在本轮范围

- 盘中实时 ETF 申赎或个股资金流。当前产品是官方日频数据。
- 把 ETF 份额变化解释为真实现金流、主力资金或某机构交易。
- 用第三方价格、NAV、PCF 或名称静默填补官方缺口。
- 依据截图推断国家队实际持仓；持有人披露仍需基金定期报告。
- 删除原始 R2 对象来降低成本。
- 购买付费 Cloudflare 资源来掩盖 Worker 架构问题。
- 重写为新的数据库、队列或微服务架构。
- 同时合并 `risk-sizer` 的认证体系；这是独立产品变更。

---

## 16. 实施依赖与并行策略

这项修复看似可以分工，但核心合同高度共享。推荐顺序实施，不并行修改同一发布链。

| PR | 主要模块 | 依赖 |
| --- | --- | --- |
| PR 0 | tests、scripts、candidate publish | 无 |
| PR 1 | dashboard、ETF state、release QA | PR 0 |
| PR 2 | market history、read models、workflow | PR 1 |
| PR 3 | ETF→stock、pressure、R2 history | PR 1，最好在 PR 2 后 |
| PR 4 | official adapters、D1 schema、UI | PR 1 |
| PR 5 | Worker、release store、deploy | PR 1-4 candidate 合同稳定 |
| PR 6 | frontend views/search/status | PR 5 |
| PR 7 | workflow scheduling | monotonic activation 与 canary 已上线 |

可并行的只有：PR 3 的 PCF inventory 与 PR 4 的 BSE/NAV source research。任何并行 worktree 都不能同时修改 `daily-data.yml`、`read_models.py`、manifest contract 或 D1 migration journal。

---

## 17. Luna 每个 PR 的交付格式

每个 PR 必须在回复中给出：

1. 修复的问题编号。
2. 实际修改文件。
3. 公式、日期或 schema 的变化。
4. 新增的回归测试和完整测试命令结果。
5. candidate release ID、`asOf`、build SHA。
6. cross-artifact reconciliation 摘要。
7. 历史 coverage 与上一 active 的对比。
8. artifact 大小和免费用量估算。
9. 是否部署、是否迁移、是否激活。
10. 生产 canary 证据与回滚点。
11. 仍未完成或因官方源缺失而无法完成的项。

不能只回复“已修复”“已推送”或“workflow 成功”。成功的定义是数据、发布、读取和页面四层证据同时成立。

---

## 18. 给实现者的最后提醒

Flow 是一个依赖事实匹配的决策产品。最危险的失败不是页面报错，而是页面正常、数字看起来合理、实际却来自不同日期、不同市场或不同 release。

实现顺序必须始终是：

```text
官方原始字段
→ 日期语义
→ 市场+代码业务键
→ canonical fact
→ 可复算公式
→ 跨工件恒等式
→ coverage 门禁
→ immutable candidate
→ canary
→ active pointer
→ 页面
```

遇到官方历史不存在、字段不稳定或口径不能确认时，正确结果是：

```text
null + 覆盖率 + 原因 + 原始证据
```

不是一个看起来合理的数字。
