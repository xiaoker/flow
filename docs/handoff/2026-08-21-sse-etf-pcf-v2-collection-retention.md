# Flow 上交所 ETF PCF v2：精确采集、可恢复发布与分层留存实施方案

> 交接对象：GPT-5.6 Luna（或后续实现者）<br>
> 编写日期：2026-08-21<br>
> 仓库：`xiaoker/flow`<br>
> 生产地址：`https://flow.xiaoker.com`<br>
> 状态：PR0–PR3 已合并；PR4 shadow 工具已实施，5 个交易日证据待积累<br>
> 优先级：P0 数据日期与覆盖正确性，P0 可恢复采集，P1 免费额度与存储治理<br>
> 实施原则：严格从 PR 0 开始，不跨阶段，不提前切换生产读取路径，不追溯删除旧对象

## 0. 执行摘要

本方案只解决上交所 ETF PCF（申购赎回清单）及其 ETF→个股穿透链路的五个根因：

1. 当前每只 ETF 请求两次 JSON 接口，且最多 4 路并发，容易放大源站压力和失败概率；
2. 当前把 `TradingDay=D` 与 `PreTradingDay=D` 两种 PCF 混入同一正式结果，日期语义不够严格；
3. 当前变化 ETF 使用内连接生成，新上市、消失或缺基准的 ETF 会静默漏出；
4. 当前 PCF 没有逐项持久状态，进程中断后只能整轮重来，容易重复请求源站；
5. 当前 PCF 原始响应进入永久 R2 前缀，长期运行无法保证一直处于免费存储额度内。

锁定的目标方案：

- 每只 ETF 只下载一次上交所官方 XML；
- 正式 D 日穿透只接受 XML 中 `TradingDay == D`；
- 用 D1 保存采集任务和逐 ETF 检查点，支持中断续跑；
- 默认串行、带随机间隔，遇到 403/429 立即停止，避免持续撞击源站；
- PCF 原始 XML 不永久保存：单文件 30 天、每日原始打包 180 天；
- 规范化 PCF、manifest、哈希、来源日期、覆盖结果永久保存；
- ETF 份额、融资融券、互联互通等体量较小的核心官方原始数据继续永久保存；
- 核心市场数据发布不被 PCF 阻塞；ETF 穿透按同日能力单独发布，缺失时明确不可用，不回退到其他日期；
- 全部对象使用 R2 Standard，不使用 Infrequent Access，因为 R2 免费额度只适用于 Standard。

这个方案允许删除“以后还可以重解析的原始字节”，但不会删除“以后重新计算产品指标所需的规范事实”。180 天以后若发现 XML 解析器曾经漏字段，不能再从原 XML 修复更早日期；因此规范化层必须做低损失保存，而不是只保留当前公式需要的几列。

## 1. 开始前的仓库基线与保护规则

实施每个 PR 前必须运行：

```bash
git status --short --branch
git fetch origin
git log -8 --oneline --decorate
```

本文编写时：

```text
branch: codex/deploy-worker-workflow-v2
HEAD:   702bc8f fix: enforce data-ready release pipeline
```

工作树已有用户改动：

```text
M  reports/phase0/latest-manifest.json
M  reports/phase0/latest-quality-summary.json
M  reports/phase0/latest-release-qa.json
?? memory/
```

这些内容不属于本任务。不得还原、覆盖、暂存或提交。禁止使用 `git add -A`、`git add .`、`git reset --hard` 或清理未跟踪文件的命令。每个提交只显式 `git add` 本 PR 的文件。

本文件只替代以下旧约定中“PCF v2 原始字节永久保存”的部分：

- `docs/history-backfill.md` 中 `market-data/raw/` 全部无限期保留的泛化描述；
- `docs/data-lineage-manual-checklist.md` 中 PCF 通过通用永久原始前缀保存的描述；
- `docs/handoff/2026-08-16-master-data-integrity-remediation.md` 的 P2-17 第 1 条，仅限新 PCF v2 前缀。

其他数据域的原始数据永久保留策略不变。旧 `market-data/raw/` 对象及其 Bucket Lock 不做追溯修改。

## 2. 官方事实与日期合同

### 2.1 官方入口

- 上交所 PCF 列表：<https://www.sse.com.cn/disclosure/fund/etflist/>
- 单只 ETF XML：`https://query.sse.com.cn/etfDownload/downloadETF2Bulletin.do?fundCode={6位基金代码}`

上交所页面明确说明：网站栏目在交易日上午 08:30 统一更新；若与 08:00 后证通云盘文件不一致，以证通云盘为准。Flow 当前只能自动读取网站公开 XML，因此页面公开 XML 是系统可验证的官方来源，但必须保留该来源边界说明。

XML 一次响应已经包含：

```text
FundInstrumentID
CreationRedemptionUnit
TradingDay
PreTradingDay
NAVperCU / NAV
PreCashComponent / EstimatedCashComponent
RecordNumber
ComponentList / Component
  ├── InstrumentID
  ├── InstrumentName
  ├── Quantity
  ├── SubstitutionFlag
  ├── CreationPremiumRate
  ├── RedemptionDiscountRate
  ├── SubstitutionCashAmount
  └── UnderlyingSecurityID
```

因此不再为每只 ETF 分别请求“基本信息 JSON”和“成分 JSON”。

### 2.2 D 日正式计算只接受同日 PCF

定义：

- `D`：ETF 官方份额变化对应的交易日；
- `D-1`：该 ETF 的前一份可比官方交易日快照；
- `pcfAsOf`：XML 的 `TradingDay`；
- `pcfPreviousAsOf`：XML 的 `PreTradingDay`。

正式结果必须满足：

```text
pcfAsOf == D
pcfPreviousAsOf == D-1
FundInstrumentID == 请求的 ETF 代码
```

`pcfPreviousAsOf == D`、`pcfAsOf == D+1` 的次日 PCF 只能作为 `ROLLED_OVER_EVIDENCE` 保存，不能进入 D 日正式 ETF→个股穿透，也不能与同日 PCF 混算。

原因：D 日份额变化发生在 D 日，D+1 的篮子可能已经调整。两者通常相近，但不是同一个可审计事实。

### 2.3 计算公式与产品表述

对每只可比 ETF：

```text
ETF 份额变化       Δshare = share(D) - share(D-1)
申赎单位变化       Δunit  = Δshare / CreationRedemptionUnit(D)
个股等价数量       stockEquivalent = Δunit × Component.Quantity(D)
```

产品必须继续使用“估算”“等价数量”或“穿透变化”，不得写成真实成交、真实持仓或现金净流入。`SubstitutionFlag` 表明成分可能使用现金替代；零 `Quantity` 也可能是合法官方记录。规范化层保留这些记录，A 股等价数量聚合时按明确规则处理，不能在解析阶段丢弃。

## 3. 现有实现中必须修复的问题

| 问题 | 当前位置 | 风险 | 目标处理 |
| --- | --- | --- | --- |
| 两次 JSON 请求/ETF | `src/trading_desk/adapters/official.py` | 请求量翻倍、4 路并发形成突发流量 | 单次 XML 下载 |
| 最多 800 只后截断 | 同上 | 高变化日静默少算 | 不截断；异常规模阻断并报警 |
| 接受次日 PCF | adapter、QA、read model | D 日口径混入 D+1 篮子 | 正式结果只接受 `TradingDay=D` |
| PCF 只在 morning/reconcile 采集 | `src/trading_desk/phase0.py` | 错过 D 日收盘后至次日 08:30 的精确窗口 | 晚间优先采，次日 08:30 前恢复 |
| 变化 ETF 使用 inner join | `phase0.py` | 新上市、缺失、退出静默消失 | outer join 并显式分类 |
| PCF readiness 是 optional 且无覆盖率 | `source_readiness.py` | 少采一部分仍可能被当成可用 | 单独的 ETF 穿透能力门禁 |
| R2 历史接口按 prefix list | `history-artifact/route.ts` | 同日多 revision 返回 409 ambiguous | D1 精确指针，不在请求时 list |
| 通用 history bundle 永久保存 PCF raw | `history_bundle.py` | R2 长期增长 | PCF v2 使用独立分层前缀 |
| PCF 与核心 release 强绑定 | `daily-data.yml`、manifest | PCF 失败导致完整市场日不可见 | 核心与 ETF 穿透按能力分槽发布 |

不得只改重试次数。没有逐项检查点时，更多重试只会重复请求并增加被限制的概率。

## 4. 目标架构

```text
                  上交所 ETF 份额 D 与 D-1
                            │
                            ▼
                outer join 生成采集计划
          ┌────────────┬─────────────┬────────────┐
          │可比且变化   │可比但未变化  │新/缺失/异常 │
          │REQUIRED     │NO_CHANGE    │显式状态     │
          └──────┬─────┴─────────────┴────────────┘
                 │
                 ▼
      D1 pcf_capture_job + pcf_capture_item
          │ lease 领取 20~25 个待办项
          ▼
     GitHub Actions 串行下载官方 XML
          │ 0.8~1.3 秒随机间隔
          │ 403/429 立即熔断并保存进度
          ▼
  Worker 流式上传端点 ───────────────► R2 raw-xml（30 天）
          │ 不解析 body
          │ R2 put 使用调用方提供的 SHA-256 校验
          ▼
      D1 更新单只 ETF 检查点
          │
          ├── 未完成：下个窗口只续跑剩余项
          │
          └── 100% REQUIRED 精确匹配
                    │
                    ▼
        GitHub Actions 离线生成并复核
          ├── 每日原始 XML 包（180 天）
          ├── canonical header/components（永久）
          ├── compatibility data.csv.gz（永久）
          └── manifest + QA + lineage（永久）
                    │
                    ▼
          D1 history_artifact_pointer
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
  核心 market release     etf_penetration release
  不被 PCF 阻塞           只发布同日完整能力
          │                   │
          └─────────┬─────────┘
                    ▼
          Worker 只读小指针并流式返回 R2
```

这里的“熔断”是指源站明确返回限制信号后，当前任务立即停止，不继续高频请求；后续窗口从 D1 检查点恢复。

## 5. 变化 ETF 采集计划合同

### 5.1 不能再使用 inner join

使用 `(market, instrument_code)` 对 D 与 D-1 做 outer join：

| D-1 | D | 状态 | 是否请求 PCF | 是否进入正式流量 |
| --- | --- | --- | --- | --- |
| 有 | 有，份额不同 | `COMPARABLE_CHANGED` | 是，REQUIRED | 是 |
| 有 | 有，份额相同 | `COMPARABLE_UNCHANGED` | 否 | 0，保留计数 |
| 无 | 有 | `NEW_BASELINE_MISSING` | 否 | 否，不假设 D-1 为 0 |
| 有 | 无 | `MISSING_CURRENT` | 否 | 否，阻断能力并要求核查 |
| 任意 | 任意，份额无效 | `INVALID_SHARE` | 否 | 否，阻断能力 |

计划必须确定性排序：`abs(share_delta) DESC, etf_code ASC`。同一输入生成同一 `planSha256`。

### 5.2 不得静默截断

- 删除当前 `max_codes=800` 截断行为；
- 配置安全上限 `PCF_PLAN_HARD_LIMIT=2000`，只用于识别源数据异常；
- 超过上限时状态为 `BLOCKED_UNIVERSE_ANOMALY`，不采样、不发布；
- 页面和 Actions Summary 显示实际变化数量及阻断原因。

### 5.3 无变化日也是正式状态

当 `requiredCount == 0` 且份额输入通过 QA：

- job 直接进入 `READY_NO_CHANGE`；
- 生成空但结构完整的 canonical 与 manifest；
- ETF 穿透结果显示“当日上交所 ETF 份额无变化”，不能一直显示采集中。

## 6. D1 控制面数据模型

PR 0 新增迁移，建议编号取当前 journal 的下一个可用编号。不得修改已经应用的迁移文件。

### 6.1 `pcf_capture_job`

```text
job_id                    TEXT PRIMARY KEY
target_date               TEXT NOT NULL
previous_date             TEXT NOT NULL
plan_sha256               TEXT NOT NULL UNIQUE
calculation_version       TEXT NOT NULL             -- pcf-capture-v2
status                    TEXT NOT NULL
expected_count            INTEGER NOT NULL
required_count            INTEGER NOT NULL
unchanged_count           INTEGER NOT NULL
new_baseline_count        INTEGER NOT NULL
missing_current_count     INTEGER NOT NULL
captured_count            INTEGER NOT NULL DEFAULT 0
valid_count               INTEGER NOT NULL DEFAULT 0
failed_count              INTEGER NOT NULL DEFAULT 0
coverage_ppm              INTEGER NOT NULL DEFAULT 0
lease_owner               TEXT
lease_expires_at          TEXT
next_attempt_at           TEXT
canonical_manifest_key    TEXT
canonical_manifest_sha256 TEXT
raw_pack_key              TEXT
raw_pack_sha256           TEXT
error_json                TEXT NOT NULL DEFAULT '{}'
started_at                TEXT NOT NULL
completed_at              TEXT
updated_at                TEXT NOT NULL
```

索引：

```text
(target_date, status)
(status, next_attempt_at)
```

`job_id` 由 `targetDate + previousDate + share source hashes + calculationVersion` 的 canonical JSON 哈希生成。同一天官方纠正后 share hash 改变，会生成新的 revision，而不是覆盖旧 job。

### 6.2 `pcf_capture_item`

```text
job_id                 TEXT NOT NULL
etf_code               TEXT NOT NULL
etf_name               TEXT NOT NULL
current_shares         INTEGER
previous_shares        INTEGER
share_delta            INTEGER
baseline_status        TEXT NOT NULL
state                  TEXT NOT NULL
attempts               INTEGER NOT NULL DEFAULT 0
next_attempt_at        TEXT
http_status            INTEGER
source_trading_day     TEXT
source_previous_day    TEXT
raw_object_key         TEXT
raw_object_sha256      TEXT
raw_content_sha256     TEXT
raw_bytes              INTEGER
component_count        INTEGER
normalized_sha256      TEXT
failure_reason         TEXT
observed_at            TEXT
updated_at             TEXT NOT NULL
PRIMARY KEY (job_id, etf_code)
```

索引：

```text
(job_id, state, next_attempt_at)
```

### 6.3 `history_artifact_pointer`

用于替换 `history-artifact` route 的 R2 prefix list：

```text
source_id        TEXT NOT NULL
dataset          TEXT NOT NULL
trade_date       TEXT NOT NULL
file_name        TEXT NOT NULL
revision_id      TEXT NOT NULL
object_key       TEXT NOT NULL
sha256           TEXT NOT NULL
content_encoding TEXT
row_count        INTEGER NOT NULL
observed_at      TEXT NOT NULL
status           TEXT NOT NULL
updated_at       TEXT NOT NULL
PRIMARY KEY (source_id, dataset, trade_date, file_name)
```

同日修订只在完整 finalize 后用条件 UPSERT 切换 pointer。历史接口按主键读取一个对象，禁止再列前缀后猜哪一个是正式版本。

### 6.4 状态机

```text
PLANNED
  │ acquire lease
  ▼
RUNNING
  ├── 仍有 PENDING/RETRYABLE ─► PARTIAL ─► 下个窗口恢复 RUNNING
  ├── 403/429 ───────────────► RATE_LIMITED ─► 到 nextAttemptAt 后恢复
  ├── XML 已滚到 D+1 ────────► EXPIRED_EXACT_WINDOW
  ├── 日期/代码/结构不匹配 ──► BLOCKED_QUALITY
  ├── requiredCount == 0 ────► READY_NO_CHANGE
  └── valid == required == captured
                              ▼
                         FINALIZING
                              ├── pack/canonical/QA 失败 ─► FINALIZE_FAILED
                              └── 全部成功 ───────────────► READY
```

终态：`READY`、`READY_NO_CHANGE`、`EXPIRED_EXACT_WINDOW`、`BLOCKED_QUALITY`。`FAILED` 不能成为含糊的通用状态，必须保留具体原因。

### 6.5 租约与并发

- 领取 job 使用 D1 条件 UPDATE，只有租约为空或已过期才成功；
- 默认租约 10 分钟，每完成一个 microbatch 续租；
- GitHub run 崩溃后租约到期，下一 run 从未完成 item 继续；
- 不使用 `cancel-in-progress: true` 取消正在写检查点的任务；
- 同一 `job_id` 同时只能有一个 worker；不同日期也默认串行，避免同时访问上交所。

## 7. XML 下载、解析与逐项校验

### 7.1 下载策略

默认值：

```text
并发数              1
microbatch          25 只
ETF 间隔            0.8~1.3 秒随机抖动
连接超时            10 秒
响应超时            45 秒
网络/5xx 同轮重试    最多 2 次，等待 5 秒、15 秒
403/429              不同轮重试 0 次，立即打开熔断
404/日期不匹配        不重试，记录事实
```

每完成 25 只就写 D1 检查点。不得先把全部 XML 放在内存里再一次性保存。

### 7.2 安全解析

- 新增 `defusedxml` 依赖，使用 `defusedxml.ElementTree`；
- 拒绝 DTD、外部实体和异常深度 XML；
- 限制单 XML 压缩前最大 5 MB；
- 接受 `application/xml`、`text/xml`；上交所当前实际返回 `application/octet-stream;charset=UTF-8`，此时必须同时满足 `Content-Disposition` 文件名以 `.xml` 结尾且正文前缀为 XML；
- 不把响应正文、token、Access header 写入日志。

### 7.3 单只 ETF 阻断校验

以下任一失败，该 ETF 不得进入 canonical：

1. `FundInstrumentID` 不是请求的 6 位代码；
2. `TradingDay != targetDate`；
3. `PreTradingDay != previousDate`；
4. `CreationRedemptionUnit <= 0`；
5. `RecordNumber` 存在但不等于实际 `Component` 数量；
6. 同一 ETF 的 `(UnderlyingSecurityID, InstrumentID)` 重复；
7. `Quantity < 0` 或不是整数；零允许保留；
8. 关键日期、代码或数值无法解析；
9. R2 对象 checksum 与调用方上传的对象 SHA-256 不一致。

未识别的 component tag 不应导致丢字段。将其按 key 排序写入 `source_extra_json`。

### 7.4 成分分类

规范化层保存所有官方成分，再由计算层分类：

```text
A_SHARE_CONFIRMED
HK_SHARE_CONFIRMED
OTHER_SECURITY
CASH_SUBSTITUTION
UNRESOLVED_SECURITY
```

`UNRESOLVED_SECURITY` 不得猜测成 A 股；它保留在 canonical，ETF 穿透 A 股汇总中排除，并在覆盖信息中计数。

## 8. R2 对象布局与留存策略

### 8.1 新前缀

```text
market-data/pcf-v2/
├── raw-xml/{tradeDate}/{jobId}/{etfCode}/{objectSha}.xml.gz
├── degraded/{tradeDate}/{jobId}/{etfCode}/{objectSha}.xml.gz
├── batches/{tradeDate}/{jobId}/batch-{0001}-{sha}.ndjson.gz
├── raw-pack/{tradeDate}/{packSha}.tar.gz
├── canonical/{tradeDate}/{revisionId}/
│   ├── headers.csv.gz
│   ├── components.csv.gz
│   └── data.csv.gz
└── manifests/{tradeDate}/{revisionId}.json
```

所有 key 均包含内容哈希或 revision，禁止原地覆盖。

### 8.2 分层留存表

| 数据 | 前缀/位置 | 留存 | 是否可删除 | 理由 |
| --- | --- | ---: | --- | --- |
| ETF 份额、两融、互通等核心 raw | 现有 `market-data/raw/` | 永久 | 否 | 小且是核心事实来源 |
| 单只 PCF 原始 XML | `pcf-v2/raw-xml/` | 30 天 | 是 | 便于近期排错与重跑 |
| 已滚动或不匹配 PCF | `pcf-v2/degraded/` | 30 天 | 是 | 只作失败证据，不参与计算 |
| microbatch 中间件 | `pcf-v2/batches/` | 14 天 | 是 | job 可从逐项状态恢复 |
| 每日原始 XML 确定性打包 | `pcf-v2/raw-pack/` | 180 天 | 是 | 解析器问题的中期恢复窗 |
| canonical headers/components/data | `pcf-v2/canonical/` | 永久 | 否 | 180 天后仍可重算产品指标 |
| manifest、SHA、lineage、QA | `pcf-v2/manifests/` + D1 | 永久 | 否 | 审计、日期和指针真值 |
| ETF 穿透 read model | release 前缀 | 最近 60 个完整交易节点 | 是 | 可由 canonical 重建 |
| GitHub Actions artifact | Actions | 14 天；失败诊断最多 30 天 | 是 | 不是系统事实来源 |

### 8.3 canonical 必须低损失

原始 XML 到期后只能依赖 canonical，因此永久层不能只保留当前公式字段：

- `headers.csv.gz`：每只 ETF 一行，保留所有已知头字段与 `source_extra_json`；
- `components.csv.gz`：每个官方 Component 一行，保留全部已知字段与未知字段 JSON；
- `data.csv.gz`：现有 ETF 穿透计算可直接读取的兼容格式；
- 三个文件均记录解压后 SHA-256、行数、schema version 和 source raw SHA 集合；
- canonical gzip 输出必须确定性：固定列顺序、行顺序、换行、UTF-8、gzip `mtime=0`。

### 8.4 每日原始包

`raw-pack` 必须是确定性 `tar.gz`：

- ETF 代码升序；
- tar entry 名称固定为 `{etfCode}.xml`；
- uid/gid/mtime 固定；
- 包内带 `index.json`，记录每个 XML 的 raw SHA、object SHA、字节数和官方日期；
- finalize 时随机抽取至少 3 个 XML 解包复算 raw SHA；
- pack 未通过校验时，不得把 job 标成 `READY`。

### 8.5 Lifecycle 与 Bucket Lock

只对新 `market-data/pcf-v2/` 子前缀设置规则：

| 前缀 | Bucket Lock | Lifecycle |
| --- | ---: | ---: |
| `raw-xml/`、`degraded/` | 7 天有限锁 | 30 天删除 |
| `batches/` | 无 | 14 天删除 |
| `raw-pack/` | 30 天有限锁 | 180 天删除 |
| `canonical/`、`manifests/` | 无限期锁 | 无删除规则 |

Cloudflare 的 Bucket Lock 优先于 Lifecycle。不得对 `pcf-v2/` 根前缀设置无限期锁，否则 30/180 天删除规则不会生效。

Lifecycle PR 是不可逆操作，必须最后实施并由用户再次确认。实施前只读检查：

```bash
cd web
npx wrangler r2 bucket lifecycle list flow
npx wrangler r2 bucket lock list flow
```

先导出对象 inventory，逐日确认每个 `raw-xml` 日期都有已验证 `raw-pack + canonical + manifest`。有任何孤儿日期时不得启用删除规则。

不使用 R2 Infrequent Access。Cloudflare 官方说明免费 10 GB-month、100 万 Class A、1000 万 Class B 只适用于 Standard；Infrequent Access 另有操作和取回费用。

## 9. 流式上传，避免 Worker 1102

新增只用于管理面的流式上传端点，例如：

```text
PUT /api/v1/admin/pcf-capture/object
```

请求只通过 header 传递：

```text
X-Flow-Job-Id
X-Flow-ETF-Code
X-Flow-Trade-Date
X-Flow-Object-Key
X-Flow-Object-Sha256       -- gzip 对象字节哈希
X-Flow-Raw-Sha256          -- 解压后官方 XML 哈希
Content-Type: application/gzip
```

端点要求：

1. 同时验证 Bearer ingest key 与 Cloudflare Access；
2. 对 job、代码、日期、key 前缀做白名单校验；
3. 不 `arrayBuffer()`、不解压、不解析 XML、不计算全量哈希；
4. 将 `request.body` 直接传给 `RAW_BUCKET.put()`；
5. 使用 R2 `sha256` put option 校验上传对象字节；
6. 对内容寻址 key 使用条件写入；已存在时 HEAD 校验 metadata 后视为幂等成功；
7. R2 成功后更新 D1 item；D1 失败时下次重试通过 HEAD 恢复，不重复访问上交所；
8. 响应只返回小型 JSON：key、size、etag、checksum、item state。

Cloudflare Workers Free 单次 HTTP CPU 只有 10 ms。所有 XML 解析、gzip、tar、CSV 和聚合都必须在 GitHub Actions 离线完成，Worker 只做认证、轻量 D1 控制和 R2 body 流式传输。

## 10. 调度策略

### 10.1 官方窗口

同日 PCF 从 D 日上午发布后，通常在次日 08:30 页面更新前仍可获取。因此最优顺序是：

```text
D 18:30  SSE 份额 D 可用后，立即计算变化 ETF 并采同日 PCF
D 19:15  只恢复未完成 job
D+1 07:45  08:30 滚动前最后一轮精确恢复
D+1 08:00  GitHub failover，只在无租约且未完成时运行
D+1 08:30 后  若仍未完成，标记 EXPIRED_EXACT_WINDOW，不用 D+1 代替
```

### 10.2 Cloudflare Cron（主触发）

Free plan 当前最多 5 个 Cron Trigger。建议调整为：

```text
18:30  evening：核心晚间来源 + PCF 首轮
19:15  evening_reconcile：只补缺失工件/PCF item
07:45  morning_exact：PCF 精确窗口最后恢复
08:45  morning：深市及其他晨间来源，不再用于 D 日正式 PCF
09:15  reconcile：完整市场日最终核验
```

删除 09:45 的重复触发。每个 Cron 先读 D1 readiness/job 状态；没有到期工作时不 dispatch GitHub Actions。

### 10.3 GitHub schedule（故障转移）

只保留：

```text
18:45  晚间 failover
08:00  PCF 滚动前 failover
```

schedule job 先调用小型状态端点：已完成、有有效租约、`nextAttemptAt` 未到时，90 秒内退出。不得每个窗口都运行完整 pytest、全量采集和全量 read-model 构建。

完整测试放在 PR/push；定时采集只运行 PCF 目标测试、发布 QA 和 canary。

## 11. 能力分槽发布

PCF 是 ETF 穿透能力，不是核心 ETF 份额、两融和互通的前置条件。

复用现有 `active_release.slot`：

```text
slot = market
  bootstrap / overview / radar / radar_flow(非PCF字段) /
  rotation / national / connect / margin

slot = etf_penetration
  pressure / stock_search / stock_0..7 /
  其他明确依赖 PCF 的穿透工件
```

规则：

1. `market` 可在核心来源完整后发布 D；
2. `etf_penetration` 只有 D 日 PCF `READY` 或 `READY_NO_CHANGE` 才能发布 D；
3. 浏览器选择 D 时，只请求 `asOf=D` 的穿透 release；不存在就显示“D 日 ETF 穿透尚不可用”；
4. 不使用 D-1 穿透伪装成 D，也不把 D+1 PCF映射回 D；
5. 主日期选择器仍以 `market` 完整 release 为准；穿透页面显示自己的同日 capability 状态；
6. 已显示的完整旧页面可以保留在浏览器中，但用户主动切到 D 时不能静默回退。

服务端先向后兼容，再改生产者 manifest，最后改浏览器读取。不得在同一个部署中同时删除旧合同并发布新合同。

## 12. 完整性门禁

### 12.1 job 门禁

```text
coverage = valid exact-date REQUIRED items / requiredCount
```

发布要求：

```text
requiredCount == capturedCount == validCount
failedCount == 0
coverage == 100%
missingCurrentCount == 0
canonical hashes verified
raw pack verified
manifest verified
```

不允许 99%、不允许“最大 800”、不允许把失败项当 0。

### 12.2 manifest 最小合同

```json
{
  "schemaVersion": 2,
  "dataset": "sse_etf_pcf",
  "calculationVersion": "pcf-capture-v2",
  "tradeDate": "2026-08-21",
  "previousDate": "2026-08-20",
  "jobId": "pcf:2026-08-21:...",
  "planSha256": "...",
  "source": {
    "publisher": "上海证券交易所",
    "urlTemplate": "https://query.sse.com.cn/etfDownload/downloadETF2Bulletin.do?fundCode={code}",
    "dateContract": "TradingDay == tradeDate"
  },
  "universe": {
    "required": 237,
    "unchanged": 642,
    "newBaselineMissing": 1,
    "missingCurrent": 0
  },
  "coverage": {
    "captured": 237,
    "valid": 237,
    "failed": 0,
    "ppm": 1000000
  },
  "artifacts": {
    "headers": {"key": "...", "sha256": "...", "rows": 237},
    "components": {"key": "...", "sha256": "...", "rows": 28410},
    "data": {"key": "...", "sha256": "...", "rows": 28410},
    "rawPack": {"key": "...", "sha256": "...", "members": 237}
  },
  "quality": {"pass": 12, "warn": 0, "fail": 0},
  "observedAt": "2026-08-21T11:02:31.000Z"
}
```

实现时沿用项目现有 QA 计数语义，测试 fixture 必须使用真实含义。

### 12.3 readiness 状态

`source_readiness` 仍记录上交所 PCF 来源水位，但新增 capability 输出：

```text
READY
READY_NO_CHANGE
PENDING_PLAN
CAPTURING n/m
RATE_LIMITED nextAttemptAt
EXPIRED_EXACT_WINDOW
BLOCKED_DATE_MISMATCH
BLOCKED_QUALITY
FINALIZE_FAILED
```

不得把 `PARTIAL` 映射成 `READY`。

## 13. 免费额度预算与硬护栏

官方当前免费额度：

| 服务 | 免费额度 | 本方案目标 |
| --- | ---: | ---: |
| R2 Standard 存储 | 10 GB-month/月 | 7.5 GB 告警，8 GB 停止扩大留存 |
| R2 Class A | 1,000,000/月 | < 50,000/月 |
| R2 Class B | 10,000,000/月 | < 100,000/月 |
| D1 读取 | 5,000,000 行/日 | < 250,000/日 |
| D1 写入 | 100,000 行/日 | < 25,000/日，50,000 告警 |
| D1 存储 | 5 GB | < 1 GB |
| Workers 请求 | 100,000/日 | < 5,000/日 |
| Worker HTTP CPU | 10 ms/请求 | 不解析大对象，p95 < 7 ms |
| GitHub Free private Actions | 2,000 分钟/月 | 900 目标，1,500 告警，1,800 阻断非必要任务 |

最坏日 800 只变化 ETF 时，约 800 个 raw PUT、32 个 batch、1 个 pack、3 个 canonical 和少量 manifest，仍远低于 R2 Class A 免费额度。真正长期风险是存储，不是对象写入次数。

每日生成 `storage-budget.json`：

```text
R2 总字节与各前缀字节
30/180 天滚动预测
当月 Class A / B 估算
D1 rows_read / rows_written
GitHub 当月分钟估算
最早/最新 canonical 日期
raw-pack 缺失日期
canonical/manifest 孤儿对象
```

若预计 30 天后 R2 超过 8 GB：先停止延长 raw 留存，提交测量结果给用户；不得自动删除 canonical 或核心 raw。把 raw-pack 从 180 天降到 90 天也需要单独确认。

## 14. 分阶段实施顺序

### PR 0：合同、迁移与测试骨架，不切生产

目标：先把状态和失败语义锁定。

实施：

- 新增三张 D1 表及 Drizzle schema；
- 新增 capture plan、job/item/manifest Pydantic/TypeScript 合同；
- 新增纯函数 `build_changed_etf_capture_plan`；
- 新增 fixture：同日 XML、次日 XML、零 Quantity、现金替代、未知 tag、恶意 XML；
- source readiness 类型先兼容新状态，但生产仍使用旧 PCF 读取；
- 文档标记 shadow flag，默认关闭。

验收：迁移可在空库和现有生产 schema 副本上应用；所有旧测试通过；无路由读取新表，无生产行为变化。

### PR 1：单请求 XML adapter，影子对照

目标：证明 XML 可替代两次 JSON 请求。

实施：

- `SSE_ETF_PCF.url` 改为专用 XML SourceSpec，旧 JSON adapter 保留为 legacy；
- 新增安全 XML parser 与低损失 normalized 输出；
- 先对固定 5~20 只 ETF 影子采集；
- 对比代码、日期、creation unit、成分数、核心成分字段；
- 不把 XML 结果送入生产 dashboard。

验收：连续 3 次抽样字段一致；XML 每只 ETF 恰好 1 次 HTTP；日期不匹配测试 fail closed。

### PR 2：D1 可恢复 job + 流式 R2 上传，仍不切生产

目标：任务断电、超时或被取消后不重抓已成功 ETF。

实施：

- 新增 job 管理 GET/POST 与对象流式 PUT 管理端点；
- Actions 按 25 只领取、下载、上传、checkpoint；
- 403/429 circuit breaker；
- R2 写成功但 D1 更新失败的恢复路径；
- 对完整变化 universe 运行 shadow job；
- 不改生产 read model。

验收：在第 37 只后强制终止，再运行只请求剩余代码；同 job 并发第二 run 领不到租约；重复上传不产生第二对象。

### PR 3：canonical、raw-pack、精确 pointer，仍不切生产

目标：形成可长期重算、无 prefix 歧义的日事实。

实施：

- 离线生成 deterministic tar/canonical/manifest；
- finalize 覆盖率门禁；
- 写 `history_artifact_pointer`；
- `history-artifact` route 对 PCF 优先使用 pointer，不再 list；
- history builder 支持 `headers/components/data`；
- 旧 endpoint 继续兼容。

验收：同日两个 revision 只返回 pointer 指向者；不存在 409 ambiguous；随机 3 个 raw XML 可从 pack 复验；canonical 重建 ETF 穿透与 shadow 输出一致。

### PR 4：双轨构建与 5 个交易日影子验证

目标：证明新链路的完整性、速度和资源预算。

实施：

- 同时构建 legacy 和 v2 结果，不展示 v2；
- 输出逐 ETF、逐个股、全市场差异报告；
- 差异按“日期口径变更、旧解析丢零数量、真实异常”分类；
- 记录请求数、403/429、耗时、对象数、字节、D1 写入；
- 至少连续 5 个交易日验证。

切换门禁：5 天均达到 100% required coverage；无日期错配；无 silent omission；无 403/429 持续失败；预算低于本文件护栏。

PR4 工具实现：`src/trading_desk/pcf_parity.py` 提供逐 ETF、逐个股、全市场差异分类与预算合同；`scripts/run_pcf_shadow_parity.py` 执行单日顺序双轨采集；`scripts/summarize_pcf_shadow.py` 汇总至少 5 个交易日。`daily-data` 的 `run_pcf_shadow` 仅在手动 dispatch 明确开启时运行，默认关闭，报告以短期 Actions artifact 保存，不进入生产发布路径。

### PR 5：能力分槽与生产切换

目标：核心市场日与 ETF 穿透解耦，正式使用 v2 exact PCF。

实施顺序：

1. Worker/import/read APIs 先兼容 `market` 与 `etf_penetration` slot；
2. 部署并验证 capabilities；
3. 生产者发布双 slot；
4. 浏览器按 selected `asOf` 精确读取 penetration；
5. 设置 `PCF_CAPTURE_V2_READ_ENABLED=1`；
6. 保留 legacy 读取开关至少 7 个交易日。

验收：PCF 失败时 D 日核心数据仍可用，D 日穿透明确 unavailable；PCF 完整时两 slot 日期一致；任何页面不读取 D-1 穿透冒充 D。

### PR 6：生命周期与留存规则，单独人工批准

目标：停止 PCF 原始字节无限增长。

实施前硬门禁：

- 用户再次明确批准；
- `pcf-v2` inventory 完整；
- 每个 raw 日期都有 pack、canonical、manifest；
- 完成一次“删除本地缓存后从 raw-pack 重建 canonical”演练；
- 确认没有覆盖 `pcf-v2/` 的无限期 Bucket Lock；
- lifecycle JSON 进入仓库并经过测试/评审。

先在测试前缀用 1 天规则验证，再配置正式 14/30/180 天规则。应用后 48 小时只观察，不删除旧 `market-data/raw/`。

### PR 7：停止 legacy PCF 写入与文档收口

目标：避免新旧链路长期双写。

- `phase0.py` 不再 inline 调用 legacy PCF JSON；
- 通用 `history_bundle.py` 不再把 v2 PCF 写入永久 raw 前缀；
- 移除次日 PCF 作为正式输入的代码与测试，保留历史只读兼容说明；
- 更新 lineage、history-backfill、runtime architecture 和运维手册；
- legacy 代码先标 deprecated，一个稳定观察周期后再单独删除。

## 15. 测试覆盖图

```text
CODE PATHS                                      OPERATIONAL / USER FLOWS
[+] outer-join capture plan                     [+] D 日自动采集
  ├── comparable changed                         ├── 晚间首次完成 [E2E]
  ├── unchanged -> READY_NO_CHANGE                ├── 晚间失败、07:45 恢复 [E2E]
  ├── new baseline -> explicit                    ├── 08:30 后不伪造 D 日 [E2E]
  ├── missing current -> blocked                  └── 核心已发布、穿透不可用 [E2E]
  └── invalid shares -> blocked

[+] XML adapter                                 [+] 断点恢复
  ├── exact D/D-1                                ├── crash after item 37 [integration]
  ├── D+1 rolled over -> degraded                ├── lease expiry takeover [integration]
  ├── zero quantity preserved                    ├── R2 success/D1 failure [integration]
  ├── unknown tags preserved                     └── duplicate upload [integration]
  ├── malformed/DTD/entity rejected
  └── 403/429 circuit breaker

[+] finalize                                    [+] 历史读取
  ├── 100% coverage -> READY                     ├── exact pointer, no prefix list [E2E]
  ├── partial -> no canonical pointer            ├── same-day correction monotonic [E2E]
  ├── deterministic pack/canonical               └── 21/60 nodes only from exact facts
  └── checksum/row-count mismatch -> blocked

[+] retention                                   [+] 页面状态
  ├── inventory dry-run                          ├── CAPTURING n/m
  ├── finite lock vs lifecycle                   ├── RATE_LIMITED + next check
  ├── canonical/manifest permanent               ├── EXPIRED exact window
  └── raw-pack restore drill                     └── no stale-date fallback
```

### 15.1 Python 单元测试

建议新增/扩展：

```text
tests/unit/test_pcf_capture_plan.py
tests/unit/test_sse_pcf_xml.py
tests/unit/test_pcf_capture_state.py
tests/unit/test_pcf_canonical.py
tests/unit/test_pcf_retention_inventory.py
tests/unit/test_official_adapters.py
tests/unit/test_release_qa.py
tests/unit/test_etf_stock_flow.py
```

必须断言：

- inner join 回归用例：新 ETF 不再静默消失；
- current 800 cap 回归用例：801 只不会只取前 800；
- current next-day acceptance 回归用例：D+1 不再产生 D 正式结果；
- XML 一只 ETF 只有一个请求；
- zero Quantity 被保留，负数被拒绝；
- component 计数与 RecordNumber 不一致阻断；
- canonical 同输入字节和 SHA 完全相同；
- partial job 永不写 active pointer。

### 15.2 Worker/TypeScript 测试

扩展 `web/tests/rendered-html.test.mjs` 或拆出 route contract test：

- 未认证/错误 Access 返回 401；
- key/date/code 注入被拒绝；
- request body 不被 `arrayBuffer()`；
- R2 checksum 不匹配失败；
- R2 已存在 + metadata 匹配是幂等成功；
- R2 已存在 + metadata 冲突返回 409；
- D1 更新失败后对象仍可被下次恢复；
- pointer route 不调用 R2 list；
- selected D 没有 penetration 时不回退；
- core release 不因 penetration 缺失而阻断。

### 15.3 Workflow 合同测试

扩展 `tests/unit/test_workflow_contract.py`：

- schedule 只保留两个 GitHub failover；
- CF Cron 使用五个目标窗口；
- 没有 due job 时快速退出；
- scheduled job 不运行完整 pytest；
- `cancel-in-progress` 不会取消同 job 的 checkpoint；
- 生产读取 flag 在 PR 0~4 默认关闭；
- lifecycle 不得在 PR 0~5 被应用。

## 16. 生产失败模式与处理

| 失败 | 测试 | 处理 | 用户可见 |
| --- | --- | --- | --- |
| 上交所返回 429/403 | 有 | 立即停止，保存 nextAttemptAt | “源站限流，等待恢复” |
| GitHub run 被取消/断电 | 有 | 租约到期后从 pending item 恢复 | 进度保持，不从 0 开始 |
| XML 已滚到 D+1 | 有 | `EXPIRED_EXACT_WINDOW`，不采用次日篮子 | D 日穿透不可用，核心仍可用 |
| 单只 XML 日期错 | 有 | item blocked，coverage <100% | 显示缺失代码数 |
| R2 PUT 成功、D1 失败 | 有 | 通过内容 key HEAD 后补 checkpoint | 无重复源站请求 |
| D1 有 item、R2 对象缺失 | 有 | item 回到 retryable，重新取官方源（窗口内） | 采集中 |
| canonical 生成中断 | 有 | pointer 不切换，重跑 finalize | 旧完整穿透保持；D 日不伪装可用 |
| 同日较旧 revision 晚到 | 有 | 条件 UPSERT 拒绝旧 observedAt | 无回退 |
| Lifecycle 与无限锁冲突 | 有 inventory 检查 | 阻断 PR 6 | 运维报警，不影响页面 |
| R2 预测超过 8 GB | 有预算测试 | 阻断非必要 raw 扩张 | 管理状态提示 |
| Worker CPU 接近 10 ms | canary | 禁止解析对象，回滚 route | 已发布数据继续流式服务 |

不得存在“无测试 + 无错误处理 + 对用户静默”的失败路径。

## 17. 发布、回滚与恢复

### 17.1 部署顺序

```text
D1 migration
  ↓
向后兼容 Worker routes/capabilities
  ↓
生产冒烟验证
  ↓
GitHub producer shadow
  ↓
5 个交易日 parity
  ↓
双 slot producer
  ↓
浏览器按 capability 读取
  ↓
切 PCF v2 read flag
  ↓
7 个交易日观察
  ↓
用户批准后配置 lifecycle
```

### 17.2 回滚

- PR 0~4：关闭 shadow，不影响生产；
- PR 5：关闭 `PCF_CAPTURE_V2_READ_ENABLED`，active core release 不变；
- 新 v2 对象和 D1 job 保留用于诊断，不直接删除；
- 任何 read-model 回滚只切指针，不覆盖 R2 对象；
- PR 6 生命周期一旦删除对象不可恢复，所以它不能与生产切换同 PR；
- canonical/manifest 永久对象保证即使 raw XML 到期，产品指标仍可重建。

### 17.3 历史迁移

- 不复制当前节点伪造历史；
- 旧 v1 PCF 只有在 `pcf_date == trade_date` 且 raw/normalized/QA 哈希一致时，才可导入 v2 canonical；
- 旧 `next_day_previous_trade_date` 只能标记为 legacy estimate，不进入 exact 历史；
- exact 节点不足 21 时，页面显示真实覆盖数，不能把 1 个节点复制成 21 个；
- 新 exact 数据逐日积累到 21/60 节点后自动开放相应周期。

## 18. 需要修改的模块

按 PR 分配，避免单 PR 同时改完所有模块：

| 模块 | 作用 |
| --- | --- |
| `src/trading_desk/adapters/` | XML 下载与解析 |
| `src/trading_desk/` 新 capture plan/state/canonical 模块 | 纯业务合同与确定性输出 |
| `scripts/` | plan、capture、resume、finalize、inventory、restore drill |
| `web/db/schema.ts` + `web/drizzle/` | D1 控制面与 artifact pointer |
| `web/app/api/v1/admin/` | job 控制、流式上传、精确历史读取 |
| `src/trading_desk/read_models.py` + publish scripts | capability 分槽 manifest |
| `.github/workflows/daily-data.yml` | 状态驱动调度和快速跳过 |
| `web/worker/index.ts` + `web/wrangler.jsonc` | 五个 CF Cron 与条件 dispatch |
| `web/app/` dashboard/store/components | 同日 capability 状态，不回退 |
| `docs/` | lineage、retention、runbook 收口 |

关键状态机和 R2 pipeline 的 ASCII 图应同时写入对应 service/module 的代码注释。实现改动后必须同步更新图，不能让注释继续描述旧 JSON 双请求路径。

## 19. 并行化建议

本方案有共享 schema 和合同，PR 0~3 必须顺序实施。PR 4 之后可以少量并行：

| Lane | 内容 | 依赖 |
| --- | --- | --- |
| A | PR 0 → PR 1 → PR 2 → PR 3 | 严格顺序 |
| B | PR 4 parity 报告与预算观测 | PR 3 |
| C | PR 5 浏览器 capability UI | PR 5 服务端合同先部署 |
| D | PR 6 lifecycle inventory/restore 工具 | PR 3；只开发不应用 |

执行顺序：先完成 A；B 与 D 的“工具开发”可并行；PR 5 服务端合并部署后再做 C；PR 6 的实际生命周期应用必须最后串行并人工批准。

冲突提醒：A、B 都会碰 `daily-data.yml` 与 capture scripts；不得在两个 worktree 同时修改这些模块。C 不应修改 producer manifest 逻辑。

## 20. 明确不在本轮范围

- 不采集深交所 PCF；需要先找到同等官方、稳定、可审计的公开来源后单独设计；
- 不把 ETF 穿透估算解释为真实资金成交或真实持仓；
- 不引入 Cloudflare Queues、Durable Objects、Workers Paid 或第三方付费调度；
- 不使用盘中实时数据；
- 不改变 ETF 分类、板块轮动或融资融券公式；
- 不追溯删除旧 `market-data/raw/`；
- 不永久保存新的 PCF v2 原始 XML；
- 不用 Infrequent Access 省空间；它不享受 R2 免费额度；
- 不为补足历史复制当前 PCF 或 read model；
- 不在 Worker 请求路径解析、聚合、排序或哈希大对象。

## 21. 最终验收清单

只有全部通过，方案才算实施完成：

### 数据正确性

- [ ] 变化 universe 使用 outer join，无静默截断；
- [ ] 正式 D 日只接受 `TradingDay=D`、`PreTradingDay=D-1`；
- [ ] 每只 ETF 只有 1 次 XML 请求；
- [ ] REQUIRED 覆盖 100%，少 1 只也不发布 penetration；
- [ ] 无变化日发布 `READY_NO_CHANGE`；
- [ ] zero Quantity 和未知字段在 canonical 中可见；
- [ ] canonical 与 manifest 哈希可复算；
- [ ] history endpoint 不再通过 prefix list 选择 PCF revision。

### 可恢复性

- [ ] 第 37 只断电后只续跑剩余项；
- [ ] 403/429 后停止请求并显示 next check；
- [ ] R2 成功/D1 失败不导致重新请求上交所；
- [ ] raw-pack 能在无本地缓存条件下重建 canonical；
- [ ] 同日旧 revision 晚到不能覆盖新 revision。

### 产品行为

- [ ] 核心 D 日可独立发布；
- [ ] penetration 缺失时明确显示 D 日不可用；
- [ ] 不回退到 D-1 或 D+1；
- [ ] 日期选择器、header 和 capability 日期一致；
- [ ] 页面加载 PCF 工件时 Worker 不出现 Error 1102。

### 免费额度

- [ ] R2 使用 Standard；
- [ ] R2 30 天预测 < 8 GB；
- [ ] D1 日写入 < 25,000 目标；
- [ ] GitHub 月分钟预测 < 1,500 告警线；
- [ ] Cloudflare Cron 不超过 5 个；
- [ ] 无 due job 的 schedule 90 秒内结束。

### 留存治理

- [ ] 旧 raw 不追溯删除；
- [ ] 新 raw XML 30 天、batch 14 天、raw-pack 180 天；
- [ ] canonical/manifest 永久；
- [ ] 无覆盖 `pcf-v2/` 的无限期根锁；
- [ ] lifecycle 应用前用户再次确认；
- [ ] lifecycle 配置、inventory 与恢复演练证据进入 Actions artifact 和文档。

## 22. 实施者交付格式

每个 PR 完成时必须报告：

```text
PR / commit
本 PR 的行为变化
没有变化的生产路径
迁移是否已应用（local / remote）
测试命令与结果
shadow/canary 证据
R2 / D1 / Actions 预算变化
仍未完成的下一 PR
是否已推送、合并、部署
生产 build SHA
```

“代码已写完”“已推送”“已合并”“已部署”“生产已验证”是五个不同状态。没有 deploy workflow 成功、生产 build SHA 对齐和 canary 通过，不能回复“已经上线”。

## 23. 参考资料

- 上交所 ETF PCF 披露页：<https://www.sse.com.cn/disclosure/fund/etflist/>
- Cloudflare R2 Object Lifecycle：<https://developers.cloudflare.com/r2/buckets/object-lifecycles/>
- Cloudflare R2 Bucket Locks：<https://developers.cloudflare.com/r2/buckets/bucket-locks/>
- Cloudflare R2 Pricing：<https://developers.cloudflare.com/r2/pricing/>
- Cloudflare R2 Workers API checksum：<https://developers.cloudflare.com/r2/api/workers/workers-api-reference/>
- Cloudflare Workers Limits：<https://developers.cloudflare.com/workers/platform/limits/>
- Cloudflare D1 Pricing：<https://developers.cloudflare.com/d1/platform/pricing/>
- GitHub Actions included usage：<https://docs.github.com/en/billing/reference/product-usage-included>
