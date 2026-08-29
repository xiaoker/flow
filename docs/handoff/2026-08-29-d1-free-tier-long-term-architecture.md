# Flow 免费额度下的长期数据架构优化实施方案

> 编写日期：2026-08-29 ｜ 实施对象：GPT-5.6 Luna ｜ 状态：实施基线，尚未授权直接切换生产读取路径
> 核心目标：在不牺牲数据真实性、历史连续性和可恢复性的前提下，让日常运行长期稳定落在 Cloudflare 免费额度内。

---

## 0. 给 Luna 的执行结论

当前问题不是“再加一个索引”或“少跑一次重试”就能长期解决。根因是三类职责混在一起：

1. Cloudflare Cron 和 GitHub schedule 都会启动完整任务，串行并不等于去重；
2. R2 已经保存原始/规范证据，但 D1 又被当成长历史明细库重复物化；
3. 采集、归档、状态计算、read model 构建、PCF 捕获和发布耦合在一个重任务中，任何补采都会放大整条链路。

锁定的长期架构是：

```text
官方来源
  │
  ▼
GitHub Actions：一次采集、离线规范化、QA、计算
  │
  ├── R2：原始证据、canonical daily facts、历史分区、read models
  └── D1：调度租约、来源水位、发布清单、active pointer、紧凑市场历史
  │
  ▼
Cloudflare Worker：小指针查询 + R2 流式返回
  │
  ▼
浏览器：按 releaseId 懒加载已发布视图
```

实施必须遵守以下六条决定：

1. **Cloudflare Cron 是唯一主调度器。** GitHub schedule 只运行轻量 watchdog；确认主任务缺失后才补发。
2. **执行可以至少一次，效果必须恰好一次。** 调度重复、断电或网络重试都不能重复写相同事实、重复构建或重复激活。
3. **R2 是数据平面，D1 是控制平面。** ETF 行级长历史最终不再保存在 D1。
4. **同一数据域只允许一个 canonical writer。** `admin/import` 与 `admin/history-import` 不能继续同时写 `etf_daily`。
5. **先影子验证，再切换。** 前几个 PR 只增加能力、指标和对账，不改变生产读取结果，不删除旧表。
6. **免费额度是发布门禁，不是事后报告。** 正常日目标必须远低于上限，给补采、诊断和同账号其他项目保留余量。

不要把 PCF v2 当成主要超额原因。近期 PCF v2 约贡献数千行写入；真正的主要问题是重复 ETF 写入和反复扫描 ETF 长历史。

---

## 1. 本文与既有方案的边界

本文不重写已有数据正确性方案，只负责长期运行拓扑、D1/R2 职责、调度幂等和免费额度治理。

| 领域 | 权威文档 | 本文如何处理 |
| --- | --- | --- |
| 数据真实性、日期、代码、单位、缺失语义 | `docs/data-quality-contract.md` | 完全继承，不降低门禁 |
| 主数据、跨工件恒等式、历史连续性 | `docs/handoff/2026-08-16-master-data-integrity-remediation.md` | 完全继承，不改变公式和分类 |
| manifest、单调激活、Worker 轻量读取 | `docs/handoff/release-reliability-and-worker-budget.md` | 完全继承，复用已有 release contract |
| SSE ETF PCF v2 精确日期、逐项检查点、留存 | `docs/handoff/2026-08-21-sse-etf-pcf-v2-collection-retention.md` | PCF 正确性和留存仍以该文为准；本文只隔离其执行和预算 |
| 本文 | 调度、职责边界、D1 热点、预算、迁移 | 对这些主题为最新实施基线 |

发生冲突时按以下优先级判断：

```text
原始事实与日期合同
  > 数据完整性与发布门禁
  > 本文的运行和成本优化
  > 旧的任务时间或重试实现细节
```

任何优化都不能通过少采、伪造 0、回退到错误日期、降低 PCF 覆盖率或删除不可替代证据来省额度。

---

## 2. 开始实施前的仓库保护规则

本文编写时的本地基线：

```text
branch: codex/pcf-v2-production-fix
HEAD:   41bac34 fix: decouple PCF v2 from global market date
```

用户已有且与本任务无关的工作树内容：

```text
 M reports/phase0/latest-manifest.json
 M reports/phase0/latest-quality-summary.json
 M reports/phase0/latest-release-qa.json
?? memory/
```

实施者必须：

1. 开始每个 PR 前运行 `git status --short --branch`、`git fetch origin`、`git log -8 --oneline --decorate`；
2. 默认从最新 `origin/main` 建立 `codex/...` 分支，除非用户明确要求继续当前分支；
3. 不覆盖、还原、暂存或提交上述用户文件；
4. 禁止 `git add .`、`git add -A`、`git reset --hard` 和清理未跟踪文件；
5. 每个 PR 只显式暂存本 PR 的文件；
6. 每个 PR 合并后，下一 PR 开始前先同步最新 `origin/main`，避免再次形成长期分叉。

本文是实施计划，不授权现在删除 D1 行、修改 R2 生命周期或切换生产读取路径。

---

## 3. 官方限制与内部预算

截至 2026-08-29，Cloudflare 官方文档列出的 D1 Free 限制为：

| 指标 | 官方免费上限 | Flow 正常日目标 | Flow 内部硬门槛 |
| --- | ---: | ---: | ---: |
| D1 rows read | 5,000,000/日 | < 500,000/日 | 2,500,000/日 |
| D1 rows written | 100,000/日 | < 20,000/日 | 50,000/日 |
| D1 总存储 | 5 GB/账号 | < 1 GB | 2.5 GB |

官方计费语义必须进入工程设计：

- D1 按实际扫描行计 reads，不按返回行计；
- `INSERT`、`UPDATE`、`DELETE` 都计 writes；
- 写入被索引字段时，表和每个索引都可能增加写入计数；
- Free 限制在 00:00 UTC 重置；
- 额度是账号级风险，不能只看 `flow` 一库后把剩余额度全部占满。

因此本文把 Flow 的内部硬门槛设为官方上限的 50%。另外 50% 留给：

- 同账号的 `risk-sizer` 等项目；
- 同日官方修订与必要补采；
- 生产 canary、人工诊断和迁移；
- Cloudflare 统计延迟与估算误差。

参考：

- [Cloudflare Workers / D1 pricing](https://developers.cloudflare.com/workers/platform/pricing/#d1)
- [Cloudflare D1 indexes and billing](https://developers.cloudflare.com/d1/best-practices/use-indexes/#indexes-and-your-bill)
- [Cloudflare D1 limits](https://developers.cloudflare.com/d1/platform/limits/)

---

## 4. 2026-08-29 实测基线

### 4.1 近期滚动 24 小时用量

以下为只读审计得到的滚动切片，不等同于 Cloudflare UTC 自然日账单，但足以判断常态风险：

| 近似窗口（Asia/Shanghai） | rows read | rows written | 结论 |
| --- | ---: | ---: | --- |
| 08-28 09:00 → 08-29 09:00 | 15,039,723 | 160,614 | 读写均超限 |
| 08-27 09:00 → 08-28 09:00 | 13,245,688 | 167,466 | 读写均超限 |
| 08-26 09:00 → 08-27 09:00 | 5,243,078 | 401,952 | 读写均超限 |
| 08-25 09:00 → 08-26 09:00 | 4,097,107 | 153,016 | 写入超限 |
| 08-24 09:00 → 08-25 09:00 | 8,308,474 | 184,604 | 读写均超限 |
| 08-23 09:00 → 08-24 09:00 | 82 | 39,566 | 低活动日 |

最新窗口包含本次诊断查询约 176 万行读取，因此不能把 1,503 万全部归因于日常任务。但剔除诊断后，日常热点查询本身仍明显超过 500 万读行。

结论：**并非每天必然超限，但最近正常工作日已形成常态超额；回填和诊断会放大问题，却不是唯一原因。**

### 4.2 热点查询与写入

最新 24 小时主要指纹：

| 指纹 | 次数/规模 | 影响 |
| --- | ---: | --- |
| `loadOfficialEtfDailyChanges` 的历史 `ROW_NUMBER` 查询 | 9 次，合计扫描 12,842,733 行 | 主要读放大器 |
| 两类 ETF insert | 合计约 148,660 billed writes | 主要写放大器 |
| PCF v2 checkpoint/capture | 约 6,200 writes | 次要，不是根因 |

热点查询位于 `web/app/api/v1/admin/history-import/route.ts`。它为了找前一交易日，对所有 `trade_date < asOf` 的 ETF 历史做窗口排序；随着日期和重复 revision 增长，成本会持续上升。

### 4.3 D1 行级重复

审计时 `etf_daily`：

```text
总行数                                      245,676
唯一业务键 (trade_date, market, code)        47,928
平均物化倍数                                  5.13x
孤儿 release 行                              113,291
superseded release 行                         85,870
可判定冗余行                                 199,161（81.1%）
```

根因是两条写路径：

- `/api/v1/admin/import` 写 `data_release + etf_daily`；
- `/api/v1/admin/history-import` 又按自己的 `release_id` 写 `etf_daily`，但没有登记相同的 `data_release` 生命周期。

这不是正常的不可变版本设计。不可变版本已经在 R2 保存；D1 又完整复制每个 revision，只会同时放大扫描、写入、索引和清理成本。

### 4.4 当前调度重复

当前 `daily-data.yml` 有 5 个 GitHub schedules；`web/wrangler.jsonc` 又有 5 个 Cloudflare crons。近期工作日实际产生约 10 次完整 workflow run。

`concurrency.group` 只会串行执行，不会让后到任务判断“相同工作已经完成”。因此注释中“deduplicates overlapping dispatches”与实际行为不一致。

每个 run 还会执行：

```text
checkout
安装 dev 依赖
完整 pytest
采集全部来源
历史归档
ETF 状态计算
read-model 构建与发布
PCF v2 shadow/capture/finalize
```

即使官方来源没有变化，仍会重复支付整条管线成本。

---

## 5. 第一性原理与不变量

### 5.1 事实和任务分开

一次任务运行不是一份新事实。事实身份必须由内容决定：

```text
source fact id
  = source_id
  + dataset
  + effective_trade_date
  + raw_sha256
  + date_basis_version
```

任务可以重复，事实 ID 不变时不得重复物化。

### 5.2 至少一次执行，恰好一次效果

“恰好一次执行”无法靠 Cron 或 GitHub 保证。系统要保证的是：

- 同一窗口可能被触发多次；
- 同一请求可能因网络超时被重放；
- 任务可能在 R2 成功后、D1 成功前断电；
- 较早任务可能比官方修订后的较新任务更晚完成；
- 以上情况都不能制造第二份 canonical 事实，也不能让 active pointer 回退。

### 5.3 先保存证据，再改变状态

固定顺序：

```text
官方响应
→ R2 immutable raw
→ 规范化与 QA
→ R2 canonical fact
→ candidate read models
→ candidate canary
→ D1 原子切 active pointer
```

D1 暂时不可用时，R2 原始证据仍应成功归档；D1 控制状态在后续 reconcile 重放。D1 故障只能延迟发布，不能造成原始数据丢失。

### 5.4 没变化就不写

同一 `source fingerprint` 再次观察到时：

- 不重复上传相同 raw body；
- 不重复写行级事实；
- 不重建相同 read models；
- 不生成新 release；
- 只允许一次很小的“本窗口已检查”控制状态更新，或直接无写退出。

### 5.5 请求路径不计算历史

Worker 的职责只有：

```text
认证/路由
→ 查询小型 release pointer
→ 校验固定 key
→ 流式返回 R2 body
```

任何窗口函数、全市场聚合、21 日拼接、PCF 成分合并都只能在 GitHub Actions 离线完成。

---

## 6. 目标架构

```text
                         ┌──────────────────────┐
                         │ Cloudflare Cron 主触发 │
                         └──────────┬───────────┘
                                    │ claim(runKey)
                         ┌──────────▼───────────┐
GitHub watchdog ────────►│ D1 pipeline_window   │
仅缺失时补发             │ 小型租约/状态/指纹      │
                         └──────────┬───────────┘
                                    │ dispatch once
                                    ▼
                        GitHub collect workflow
                                    │
                 ┌──────────────────┼──────────────────┐
                 ▼                  ▼                  ▼
          R2 immutable raw   normalized/canonical   D1 source_readiness
          URL/body/SHA       daily fact partitions  fingerprint/status
                 │                  │                  │
                 └──────────────────┴──────────┬───────┘
                                               │ all required ready
                                               ▼
                                   GitHub publish workflow
                                               │
                                     离线计算 + QA + 对账
                                               │
                                    R2 immutable read models
                                               │
                                      candidate canary pass
                                               │
                                               ▼
                                  D1 manifest + active pointer
                                               │
                                               ▼
                                  Worker pointer + R2 stream
                                               │
                                               ▼
                                     Browser lazy by releaseId

PCF v2 capture workflow ──► D1 checkpoints + R2 PCF objects
                    独立预算、独立恢复、独立 capability slot
```

---

## 7. 各存储层的最终职责

| 数据 | R2 | D1 | 原因 |
| --- | --- | --- | --- |
| 官方 raw、请求元数据、SHA | 是，按既有留存合同 | 否 | 不可变大对象适合 R2 |
| ETF canonical daily fact | 是，按交易日分区 | 最终否 | 行级长历史不应进入控制库 |
| PCF canonical/components | 是 | 只存 job/item/pointer | 成分体量大，D1 只做恢复控制 |
| stock-flow daily partitions | 是 | 否 | 离线历史计算输入 |
| read models | 是，不可变 | 只存 manifest/pointer | Worker 流式读取 |
| `source_readiness` | 可保存审计副本 | 是，紧凑 | 需要快速协调来源水位 |
| `pipeline_window` | 可保存日志副本 | 是，紧凑 | 跨调度器租约和幂等 |
| `release_manifest` / `active_release` | manifest body 也归档 | 是 | 原子发布控制 |
| `market_history_daily` | 可重建原始证据 | 是，约数百行 | 页面小曲线和日期索引 |
| ETF 行级旧 `etf_daily` | R2 已有事实 | 迁移后只读、最终移除 | 当前最大读写放大器 |

这里不改变 `trade_date + market + instrument_code` 的 canonical ETF 业务键，只改变它的物理落点：长期事实从 D1 的多 revision 行改为 R2 的按日 canonical 分区。既有主数据文档中的字段、日期和质量合同继续生效。

若实施中发现还有 API 依赖 D1 的 ETF 行级明细，不得直接保留无限历史。过渡方案只能是：

```text
bounded_etf_projection
主键 = trade_date + market + instrument_code
只保留最近 5~10 个交易日
同业务键只保留一个 canonical revision
只保留实际查询所需索引
```

该表只是迁移桥梁，不是长期数据湖。

---

## 8. 调度与幂等设计

### 8.1 新增 `pipeline_window`

使用下一号 additive D1 migration 新增小型控制表：

```sql
CREATE TABLE pipeline_window (
  run_key text PRIMARY KEY NOT NULL,
  pipeline text NOT NULL,
  window_date text NOT NULL,
  window_name text NOT NULL,
  contract_version text NOT NULL,
  target_date text,
  status text NOT NULL,
  lease_owner text,
  lease_expires_at text,
  source_fingerprint text,
  github_run_id text,
  attempt_count integer NOT NULL DEFAULT 0,
  last_error_code text,
  created_at text NOT NULL,
  updated_at text NOT NULL,
  completed_at text
);
```

只增加一个确有查询用途的索引：

```sql
CREATE INDEX pipeline_window_status_lease_idx
ON pipeline_window(status, lease_expires_at);
```

`run_key`：

```text
daily-market-data:<Asia/Shanghai window date>:<window name>:<contract version>
```

不要把 GitHub run ID 当幂等键；它每次重试都会变化。

### 8.2 原子 claim

Cloudflare Cron 和 GitHub watchdog 必须调用同一 claim 逻辑：

```text
不存在 run_key              → 创建 lease，允许 dispatch
COMPLETED                   → skip
DISPATCHED/RUNNING 且租约有效 → skip
租约过期                   → 原子接管，允许恢复
FAILED_RETRYABLE 且到期      → 原子接管
BLOCKED_QUALITY             → 不自动重试，等待新 source fingerprint 或人工处理
```

claim 必须是 D1 条件写，不允许“先 SELECT、应用层判断、再 UPDATE”的竞态。

### 8.3 调度器分工

目标不是删除故障兜底，而是删除两个调度器同时执行重任务：

```text
Cloudflare Cron
  主调度；claim 成功才 dispatch

GitHub market-data-watchdog.yml
  稍后运行；先检查 pipeline_window
  只有缺失、租约过期或 retryable failure 才 dispatch

market-data-collect.yml
  只接受 workflow_dispatch
  第一项就是 runKey gate；重复 run 在安装依赖前退出
```

`concurrency` 继续作为最后一道串行保护，但文档和测试不能再把它称为去重机制。

### 8.4 窗口与来源

保留业务窗口，不把来源发布时间强行统一：

| 窗口 | 目标 |
| --- | --- |
| evening | SSE、HKEX、已到达的晚间来源；PCF 首轮按独立 job 运行 |
| morning_exact | PCF 精确窗口恢复，只处理未完成 item |
| morning | SZSE、两融及已到达来源 |
| reconcile | 只检查缺失来源和未发布 candidate，不全量重跑 |

具体 Cron 时间仍应以既有 PCF 文档与官方实测为准。本文要求的是状态化去重，不要求用更多时间窗口掩盖失败。

---

## 9. Workflow 拆分

### 9.1 最终工作流

```text
.github/workflows/ci.yml
  PR/push：完整 pytest、web test、lint、workflow contract tests

.github/workflows/market-data-collect.yml
  workflow_dispatch：一次来源采集、R2 归档、readiness 更新

.github/workflows/market-data-publish.yml
  workflow_dispatch：只在 composite fingerprint 改变且来源完整时构建候选和发布

.github/workflows/pcf-v2-capture.yml
  workflow_dispatch：按 D1 checkpoint 有界恢复，不运行全市场发布

.github/workflows/market-data-watchdog.yml
  schedule：只读状态、条件补发；正常时 90 秒内退出

.github/workflows/maintenance.yml
  手动或低频：用量报告、孤儿审计、经过批准的分批清理
```

迁移期间可以保留 `daily-data.yml` 名称，但职责必须逐 PR 抽离，不能一次大改全部 workflow。

### 9.2 定时任务不再运行完整测试

当前每次数据 run 都执行完整 `python -m pytest`。目标：

1. 先建立独立 PR/main CI 并设为必需检查；
2. 定时采集只运行 source mapping、parser、日期合同和 publish gate 的小型 smoke；
3. 代码 SHA 没变时，不在一天内重复跑相同完整测试；
4. 数据 QA 仍必须每次运行，因为它验证的是新数据，不是代码。

### 9.3 发布触发

采集完成后计算：

```text
composite_fingerprint
  = sorted(required source normalized fingerprints)
  + calculation versions
  + classification version
  + manifest contract version
```

规则：

- readiness 不完整：只归档，不构建；
- fingerprint 与已发布相同：直接结束；
- fingerprint 新增：构建 candidate；
- candidate QA/canary 失败：保留证据，active 不变；
- candidate 通过：按 `(asOf, observedAt)` 单调激活。

---

## 10. 消除 D1 ETF 长历史扫描

### 10.1 目标

删除生产热路径中的：

```sql
ROW_NUMBER() OVER (...)
FROM etf_daily
WHERE trade_date < ?
```

前一交易日不能通过扫描“所有更早 ETF revision”求得。离线采集端已经拥有当前和前一官方规范文件，应在 GitHub Actions 中直接计算相邻变化。

### 10.2 新合同

`history-import` bundle 增加已通过 QA 的紧凑输入：

```json
{
  "officialEtfDailyChanges": {
    "schemaVersion": 1,
    "asOf": "YYYY-MM-DD",
    "expectedPreviousDate": "YYYY-MM-DD",
    "sourceReleaseIds": [],
    "inputSha256": "...",
    "rows": [
      {
        "market": "SSE",
        "code": "510300",
        "previousTradeDate": "YYYY-MM-DD",
        "currentShares": 0,
        "previousShares": 0,
        "changeShares": 0,
        "aligned": true
      }
    ]
  }
}
```

服务端只做：

- schema、日期、市场、代码、整数单位校验；
- 行数、哈希和来源 release ID 校验；
- `current - previous == change` 复算；
- 未对齐行必须为 `null` 派生，不允许跨缺口标记 1d；
- 把已验证结果合并进 R2 ETF flow state。

服务端不再从 D1 重算。

### 10.3 影子对账

切换前只在有限 fixture 和最多 2 个生产候选日运行旧/新双算：

```text
市场+代码集合一致
previous date 一致
current/previous shares 一致
change 一致
未对齐原因一致
汇总值一致
```

不得为“连续 5 天影子”每天继续执行高成本旧查询。完成有限对账后，5 个交易日观察的是新链路的正确性、额度和发布稳定性。

---

## 11. 统一 canonical writer

### 11.1 `history-import` 变成 archive-only

长期职责：

```text
写 R2 immutable raw / normalized / snapshot / manifest
更新紧凑 market_history_daily
更新 history_artifact_pointer
校验并更新 R2 ETF flow state
不再写 etf_daily 行级明细
```

### 11.2 `admin/import`

迁移期它仍是唯一可写 `etf_daily` 的路径。完成全仓静态引用清单和影子验证后：

- 若没有在线读者：停止写 `etf_daily`；
- 若仍有明确读者：改写为最近 5~10 交易日的 bounded canonical projection；
- 同一业务键只保存一行，source revision/hash 作为列，不放进主键制造版本倍增；
- 删除没有生产查询价值的 category 索引；板块聚合由 R2 read model 提供。

### 11.3 不在热路径清理旧行

现有冗余行先保留只读，避免一次性 `DELETE` 及索引维护在 9 月 1 日后再次触发写限额。只有生产新链路稳定后，才按第 18 节执行清理。

---

## 12. PCF v2 在新架构中的位置

PCF v2 继续遵守既有 exact-date 合同：正式 D 日只接受 `TradingDay == D`，逐 ETF checkpoint，可恢复，缺 1 只也不能发布 penetration capability。

本方案只做四件事：

1. 从全市场 `daily-data` 重任务中拆出独立 workflow；
2. 每次只领取有界 item，已成功 item 不再请求上交所；
3. D1 只写真实状态转换，不为 heartbeat 重写整批 item；
4. PCF 完整后只触发 `etf_penetration` slot，不重新采集核心来源。

预算保留：

```text
PCF v2 正常目标：D1 writes < 15,000/日
达到 20,000：暂停非必要 parity/backfill，核心 exact capture 继续
任何 403/429：立即熔断，按 nextAttemptAt 恢复
```

PCF 5 个交易日影子门禁仍然有效。不能为了降低 D1 写入而复制当天节点伪造历史，不能用 D+1 PCF 回填 D。

---

## 13. 免费额度预算门禁

### 13.1 正常日预算包络

| 组件 | rows read 目标 | rows written 目标 |
| --- | ---: | ---: |
| scheduler/watchdog/readiness | 25,000 | 2,000 |
| 核心采集与 compact projection | 100,000 | 3,000 |
| candidate/publish/pointer | 50,000 | 500 |
| PCF v2 checkpoints | 150,000 | 12,000 |
| 页面/API D1 pointer 与索引查询 | 100,000 | 100 |
| 合计目标 | 425,000 | 17,600 |

表中是设计预算，不是用来掩盖实测的理论值。上线后以 D1 `meta.rows_read`、`meta.rows_written` 和 Cloudflare Analytics 为准修正。

### 13.2 分级动作

| 账号日用量预测 | 行为 |
| --- | --- |
| < 40% 官方额度 | 正常运行 |
| 40%~50% | 告警；停止 parity、回填、清理等非必要任务 |
| 50%~60% | 内部硬门禁；只允许核心归档、readiness、已就绪 candidate 激活和 PCF exact 恢复 |
| > 60% | 禁止任何批量 D1 物化；R2 证据优先，D1 状态待次日 reconcile |

不能等到 99% 再停止。Cloudflare 统计可能滞后，且同账号还有其他数据库。

### 13.3 预测与实测

新增 `scripts/check_d1_budget.py` 或等价工具：

输入：

```text
Cloudflare account rolling usage
本 job 预计逻辑行数
目标表索引数量
本 stage 类型
内部预算配置
```

输出：

```json
{
  "measured": {},
  "predicted": {},
  "budget": {},
  "decision": "allow|allow_core_only|defer_optional|block_d1_bulk",
  "reason": "..."
}
```

预算配置放在仓库，例如 `config/resource-budget.json`，禁止散落在 workflow 的多个 magic number 中。

### 13.4 用量报告不能写回 D1

每次 D1 查询返回的 `meta` 进入结构化日志；每日汇总保存到：

```text
R2 ops-budget/YYYY/MM/DD.json
GitHub Actions artifact（短期）
```

不要为了记录 D1 用量再向 D1 写逐查询日志。

---

## 14. Worker 与页面读取优化

1. `/api/v2/releases/latest` 使用 30~60 秒短缓存和 ETag；active pointer 改变时自然失效。
2. 浏览器取得 `releaseId` 后，后续视图显式携带同一 `releaseId`；禁止每个视图都重新解析 latest。
3. immutable R2 read model 使用长缓存；pointer 使用短缓存，二者不能混用。
4. Worker 不解析大 JSON，不拼接 21 天历史，不扫描 R2 prefix。
5. 所有大对象必须流式返回；请求期间内存不能持有 raw PCF/全量 ETF 状态。
6. 页面自动更新只检查轻量 pointer；release 没变化时不重新加载静态视图。
7. 局部刷新失败时保留上一完整 release，并显示来源/发布时间，不全屏清空。

这既降低 D1 rows read，也继续防止 Error 1102。

---

## 15. 可观测性与 SLO

### 15.1 每个 run 的统一字段

```text
runKey
pipeline/window
targetDate/asOf
source fingerprints
composite fingerprint
GitHub run ID / build SHA
lease owner / attempts
R2 objects + bytes
D1 rows_read / rows_written
candidate release ID
active release ID
gate result
failure code
```

### 15.2 SLO

| 目标 | SLO |
| --- | --- |
| 原始证据 RPO | 0；取得的官方响应必须先归档 |
| 自动恢复 RTO | 下一个有效窗口，最长 30 分钟内启动恢复 |
| 前一交易日完整市场发布 | 必要来源 09:10 前到达时 09:20 p95、09:30 hard deadline；晚到来源在观察后 15 分钟内完成 |
| 重复发布 | 0；同 composite fingerprint 只能有一个有效 release |
| Worker 稳定性 | 100 次连续核心 API 冒烟无 1102/5xx |
| D1 正常日用量 | reads < 500k；writes < 20k 的 5 日中位目标 |
| 数据完整性 | 缺必要来源不激活，旧 active 不回退 |

“来源未发布”与“系统失败”必须分开统计；不能用 pending 改写任务成功率，也不能把网络/解析失败伪装成 pending。

---

## 16. 失败模式与确定行为

| 失败模式 | 正确行为 | 禁止行为 |
| --- | --- | --- |
| Cloudflare 与 GitHub 同时触发 | 一个 claim 成功，另一个快速退出 | 两个完整 run 排队执行 |
| workflow 断电 | 租约到期后续跑未完成 stage | 整轮重抓已成功来源 |
| R2 成功、D1 失败 | 返回 `archived_control_pending`，后续从 R2 manifest 重建控制状态 | 再请求交易所获取相同 body |
| D1 达到内部预算 | 核心 raw 进 R2，推迟批量 D1 任务 | 丢弃原始响应或写 0 |
| 官方来源未推进 | 标记 `SOURCE_NOT_READY`，等待下一窗口 | job 内长时间 sleep 或使用旧日期冒充 |
| 同日官方修订 | 新 fingerprint 构建同日较新 candidate，按 observedAt 单调激活 | 较早任务晚结束后覆盖修订 |
| candidate QA 失败 | 证据和诊断保留，active 不变 | 部分发布 |
| PCF 403/429 | 立即熔断并保留 checkpoint | 增加并发或持续重试 |
| Worker 读取 R2 失败 | 保留浏览器上一完整 release，局部错误 | 请求时重算 read model |
| 清理任务超预算 | 停止并保留 deletion manifest | 一次性删除几十万索引行 |

---

## 17. 分阶段实施计划

该任务会涉及超过 8 个文件，因此必须拆成独立 PR。每个 PR 都要可部署、可回滚，不能把所有改动压进一个大提交。

### PR 0：基线、预算和失败测试，不改变生产行为

**目标**：把超额和重复执行变成可验证合同。

实施：

1. 新增本方案文档引用和资源预算配置；
2. 新增只读 D1 用量/查询指纹报告脚本；
3. 新增 workflow contract 测试，证明当前双调度会产生重复重任务；
4. 新增 `loadOfficialEtfDailyChanges` 生产规模 fixture 和 query-plan 基线；
5. 新增 `etf_daily` orphan/superseded/unique-business-key 只读审计脚本；
6. 记录当前 active release、R2 key、D1 表计数和回滚点。

验收：

- 所有新增工具默认只读；
- 能输出单 query 和单 stage 的 rows read/write；
- 旧测试通过；
- active pointer、远端 D1 和生产页面均无变化。

### PR 1：跨调度器 runKey 和早期退出，仍使用现有重工作流

**目标**：先停止同窗口重复重跑。

实施：

1. additive migration 新增 `pipeline_window`；
2. Worker 新增受保护 claim/status 接口和原子租约逻辑；
3. Cloudflare scheduled handler 先 claim 再 dispatch；
4. `daily-data.yml` 第一阶段在安装依赖前验证 runKey；
5. GitHub schedule 先调用 status；已完成/有租约时快速退出；
6. 修正文档和注释：concurrency 是串行保护，不是去重。

验收：

- 同一窗口并发 20 次 claim，只有 1 次得到执行权；
- D1 claim 写入有明确 `meta.rows_written` 上限；
- 重复 schedule 90 秒内退出，不安装 Python、不运行 pytest；
- 生产读取路径不变。

### PR 2：拆分 CI、collect、publish、PCF 和 watchdog

**目标**：补采一个 stage 不再重跑整条管线。

实施：

1. 建立独立 PR/main CI；
2. 把现有 `daily-data.yml` 拆成可恢复的 collect/publish reusable steps；
3. 重任务取消直接 schedule，只接受 dispatch；
4. 新建轻量 watchdog schedule；
5. PCF v2 独立 workflow，只消费 D1 checkpoint；
6. 定时采集用目标 smoke 代替完整 pytest，数据 QA 保留；
7. 每个 stage 记录 R2/D1/Actions 预算。

验收：

- 正常工作日不再出现约 10 个完整 market pipeline run；
- 同一源未变化的 reconcile 不构建 read model；
- PCF 失败不阻塞 market slot；
- CI 必需检查已启用后才能移除定时完整测试。

### PR 3：离线生成官方 ETF 相邻变化，有限影子对账

**目标**：替代 D1 全历史窗口扫描，但不切换生产状态计算。

实施：

1. Python 从同一 canonical ETF frame 生成 `officialEtfDailyChanges`；
2. 扩展 history bundle 合同和服务端校验；
3. fixture 对账旧/新算法；
4. 最多选择 2 个生产 candidate 日做一次性影子对账；
5. 输出逐市场、逐代码、前一日期和合计差异。

验收：

- 真实相邻日期样本 100% 一致；
- 延迟市场、缺前值、新上市和退市显式处理；
- 影子 candidate 不激活；
- 没有连续 5 日重复执行旧高成本查询。

### PR 4：`history-import` archive-only，停止双写

**目标**：让 R2 负责历史事实，D1 不再被第二条路径写爆。

实施顺序：

1. 服务端先支持 `HISTORY_IMPORT_ARCHIVE_ONLY=0|1`，默认 0；
2. 部署兼容能力；
3. producer 发送新的相邻变化合同；
4. candidate QA/canary 通过；
5. 开启 archive-only；
6. 连续 5 个交易日观察后删除旧 D1 查询调用，但保留代码回退一个观察周期。

验收：

- history-import 对 ETF scale 的 D1 row writes 为 0；
- R2 raw/normalized/snapshot/manifest 全部存在；
- ETF flow state 与旧链路一致；
- market_history_daily 仍可重建；
- active release 行为不变。

### PR 5：停止或收缩 `admin/import` 的 ETF 行级物化

**目标**：完成 D1 控制平面化。

实施：

1. 全仓 `etf_daily` 读者静态和运行时清单；
2. 若无读者，新增 `D1_ETF_FACT_WRITE_ENABLED`，先 shadow off；
3. 若有必要读者，引入 bounded canonical projection，而不是保留 release 版本长历史；
4. 删除无价值索引前先用 `EXPLAIN QUERY PLAN` 证明；
5. 所有 ETF 长周期和板块计算只读 R2 canonical state/read models。

验收：

- 正常日 ETF D1 行级写入降至 0 或明确的小型 bounded 数量；
- 任一页面和 API 不退化；
- 单个 D1 query rows read < 50,000；
- 正常日 D1 总 reads < 500,000、writes < 20,000，至少观测 5 个交易日。

### PR 6：内容指纹发布和预算硬门禁

**目标**：来源没变化时真正零重建、零新发布。

实施：

1. source/normalized/composite fingerprint 进入 manifest；
2. publish workflow 在构建前比较 fingerprint；
3. 新增 account-level 预算预检；
4. optional/core stage 分类；
5. 保存 R2 `ops-budget` 日报；
6. canary 校验 release ID、asOf、source fingerprint 和 producer SHA。

验收：

- 同一数据重复运行 10 次，只产生一个 release；
- 9 次重复运行不写 ETF 行、不上传重复 read model；
- 预算过线时 optional stage 被 defer，核心证据仍进 R2；
- 页面始终读取最后一个完整 active release。

### PR 7：Worker pointer 缓存和前端静态 release 复用

**目标**：减少页面轮询造成的小查询，并继续消除 1102 风险。

实施：

1. latest pointer 30~60 秒短缓存；
2. immutable artifact 长缓存；
3. 浏览器同 releaseId 视图去重；
4. 自动更新只检测 pointer；
5. API 响应统一 `X-Flow-Release-Id`、`X-Flow-As-Of`、ETag。

验收：

- release 未变化的 30 分钟前台运行不重复拉取大视图；
- 100 次连续 API 冒烟无 1102/5xx；
- 历史日期不混入 latest；
- 缓存不能让旧 pointer 长期滞留。

### PR 8：旧 D1 数据清理，需单独人工批准

**目标**：回收冗余存储，不把清理本身变成额度事故。

前置硬门禁：

- 新链路至少稳定 10 个交易日；
- `etf_daily` 已无生产读写，或 bounded projection 已独立；
- 每个待删 release 均有 R2 manifest/raw/normalized 证明；
- 生成 immutable deletion manifest；
- 在 D1 副本实测 `DROP`/分批 `DELETE` 的 rows_written；
- 用户再次明确批准。

策略：

1. 不在 daily pipeline 中清理；
2. 每批执行前检查 account budget；
3. 每批最多消耗当日内部写预算的 10%；
4. 达到阈值立即停止，次日续跑；
5. 旧表至少保留一个完整回滚观察期；
6. 不删除 R2 原始证据。

### PR 9：连续验收与旧开关收口

只有以下成立后才能移除 legacy：

- 连续 5 个交易日采集和发布完整；
- 连续 5 个交易日 D1 正常目标通过；
- PCF v2 按自己的 5 日门禁通过后再切 penetration slot；
- 10 个交易日无旧 D1 ETF fact 读取；
- 生产 canary、随机原始数据人工抽样和 R2 恢复演练通过；
- 回滚开关和旧代码按独立 PR 移除。

### 阶段依赖与并行边界

推荐严格按以下依赖实施：

```text
PR 0
  → PR 1
  → PR 2
  → PR 3
  → PR 4
  → PR 5
  → PR 6
  → PR 7
  → PR 8（人工批准）
  → PR 9
```

可以并行的只有只读审计、fixture 准备和文档补充。以下文件或合同不得被两个分支并行修改：

```text
.github/workflows/*market-data*
.github/workflows/daily-data.yml
web/worker/index.ts
web/db/schema.ts
web/drizzle/meta/_journal.json
web/app/api/v1/admin/import/route.ts
web/app/api/v1/admin/history-import/route.ts
release manifest / source readiness / PCF capability contracts
```

每个 PR 合并并同步 `main` 后再开始下一阶段。PR 3 的离线合同必须先被服务端兼容，PR 4 才能切 archive-only；PR 5 必须在 PR 4 连续观察通过后进行；PR 8 永远不能和日常采集或生产迁移同日执行。

---

## 18. 旧数据清理与保留

### 18.1 为什么不能立即删除 199,161 行

Cloudflare 把 `DELETE` 计入 rows written，索引也可能增加写入。当前目标是先停止继续增长，而不是在限额即将执行时用一次大删除制造新的写入峰值。

### 18.2 删除清单

清理工具必须先生成：

```json
{
  "generatedAt": "...",
  "table": "etf_daily",
  "reason": "orphan|superseded|legacy_table",
  "candidateRows": 0,
  "releaseIds": [],
  "r2Proofs": [],
  "estimatedRowsWritten": 0,
  "batchSize": 0,
  "sha256": "..."
}
```

该 manifest 先进入 R2，再允许执行。

### 18.3 最终保留策略

- 核心官方 raw：按现有数据质量合同永久保留；
- PCF raw/canonical：按 2026-08-21 PCF v2 文档执行，生命周期仍需单独批准；
- D1 pipeline window：保留 90 天紧凑状态，旧行低频清理；
- D1 failed diagnostics：保留必要错误码和 R2 key，不保存大 body；
- release manifest：保留发布历史；
- active pointer：只保留当前槽位指针；
- ETF 行级事实：R2 保留，D1 最终不保留长历史。

---

## 19. 测试设计

### 19.1 调度和租约

- Cloudflare/GitHub 同 runKey 并发，只有一个 claim winner；
- lease 未过期不能接管；过期后可以恢复；
- COMPLETED 不重复 dispatch；
- GitHub API 超时重试不会生成新 runKey；
- 晚到的旧任务不能覆盖较新的同日 source fingerprint。

### 19.2 数据与幂等

- 同 raw SHA 重放不产生第二对象或第二 canonical 行；
- 同日期官方修订产生新 fingerprint 和更晚 observedAt；
- R2 写成功、D1 更新失败后，从 R2 manifest 恢复而不请求源站；
- `history-import` archive-only 后 D1 ETF row writes 为 0；
- 重跑相同 composite fingerprint 不构建 read models。

### 19.3 SQL 与预算回归

构造至少：

```text
365 个交易日
1,600 只 ETF
每业务键 5 个历史 revision
ACTIVE / SUPERSEDED / orphan 混合
```

验收：

- 新生产查询不包含 ETF 长历史窗口扫描；
- `EXPLAIN QUERY PLAN` 对必要控制查询显示 `SEARCH ... USING INDEX`，不是全表 `SCAN`；
- 单 query rows read < 50,000；
- 相同输入第二次执行 rows_written 为 0 或仅一个小型控制更新；
- 索引新增必须给出读节省与写放大的实测对比。

### 19.4 发布一致性

- required artifact 缺一个不能激活；
- manifest fingerprint 与对象 SHA 可复算；
- current、history、views、stock shards 返回同一 releaseId/asOf；
- 同 asOf 的旧 observedAt 不能覆盖新 observedAt；
- source pending 时旧 active 不变。

### 19.5 PCF

沿用既有 PCF v2 测试，并新增：

- PCF workflow 不触发 core source 重采；
- 成功 item 不因全局任务重试而重抓；
- PCF D1 write budget 超过 optional 线时，parity/backfill defer，exact capture 不伪造完成；
- penetration slot 缺失不阻塞 market slot。

### 19.6 浏览器和生产 canary

- 30 分钟 release 未变化不重新加载大对象；
- 切日期不会回退到 latest；
- 页面保留上一完整数据并明确 stale/pending；
- 100 次默认页、Radar、国家队、个股和历史视图无 1102/5xx；
- D1 达到内部门禁的 fixture 下，页面仍读取上一 active。

---

## 20. Feature flags 与回滚

建议的过渡开关：

```text
PIPELINE_WINDOW_GATE_ENABLED
HISTORY_IMPORT_ARCHIVE_ONLY
D1_ETF_FACT_WRITE_ENABLED
OFFLINE_ETF_DAILY_CHANGE_ENABLED
PCF_CAPTURE_V2_READ_ENABLED   # 已存在，继续沿用
```

规则：

1. 服务端先兼容新合同，再启用 producer；
2. 开关默认保持旧行为，candidate/影子验证后逐项切换；
3. 原始 R2 对象从不回滚或覆盖；
4. 代码回滚、计算开关回滚和 active pointer 回滚分开；
5. 回滚只回到最后一个已知正确且完整的 release；
6. 不使用 `git reset --hard`、覆盖 R2 key 或删除失败 release 作为回滚。

如果 `HISTORY_IMPORT_ARCHIVE_ONLY` 回滚到 0，会暂时恢复旧 D1 写入，因此只能短时应急；回滚后仍需保留内部额度门禁，防止恢复写放大后击穿 Free 限额。

---

## 21. 明确不在本轮范围

- 不改 ETF 份额、金额估算、板块分类、两融、互通或 PCF 公式；
- 不购买 Cloudflare Paid 来掩盖架构问题；
- 不引入 Queue、Durable Object、常驻服务器或第三方数据库；
- 不做盘中实时数据；
- 不删除核心原始数据；
- 不为了省额度降低 PCF REQUIRED 覆盖；
- 不重新设计页面视觉；
- 不合并 `risk-sizer` 账号体系；
- 不在早期 PR 清理生产 D1 旧行；
- 不同时切 producer、server 和 browser 的不兼容合同。

---

## 22. 最终 Definition of Done

### 架构

- [ ] Cloudflare Cron 是主触发；GitHub schedule 只运行 watchdog。
- [ ] 相同 runKey 重复触发只有一次重任务效果。
- [ ] collect、publish、PCF capture 和 maintenance 可独立恢复。
- [ ] R2 保存事实，D1 只保存控制状态和紧凑索引。
- [ ] Worker 请求路径没有历史计算和大 JSON 聚合。

### D1

- [ ] `history-import` 不再写 ETF 行级事实。
- [ ] `admin/import` 不再保存 ETF 长历史版本，或已停止该写入。
- [ ] 生产不存在 `trade_date < asOf` 的 ETF 全历史窗口扫描。
- [ ] 单 query rows read < 50,000。
- [ ] 正常日 reads < 500,000、writes < 20,000 的目标至少连续观测 5 个交易日。
- [ ] Flow 内部硬门槛不超过官方额度 50%。

### 数据正确性

- [ ] Overview、Radar、rotation、national 仍使用同一 canonical fact 和 classificationVersion。
- [ ] 所有日期、市场、代码、单位和 coverage 合同不退化。
- [ ] source 未到达时不发布 0、不回退旧日期冒充。
- [ ] 相同事实重复运行不会产生新 release。
- [ ] 同日官方修订可以单调替换，旧任务不能回退指针。

### PCF v2

- [ ] 独立 workflow 按 checkpoint 恢复。
- [ ] market slot 不被 PCF 阻塞。
- [ ] penetration slot 仍通过 exact-date 100% coverage 门禁。
- [ ] 5 个真实交易日影子验证通过后才启用生产读取。

### 稳定性与运维

- [ ] 连续 5 个交易日无漏采，watchdog 能自动补发真正缺失的窗口。
- [ ] 100 次生产冒烟无 1102/5xx。
- [ ] 每日 R2 用量报告包含 D1/R2/Actions/Worker 关键指标。
- [ ] D1 达到预算时，原始证据仍能进入 R2，active 保持最后完整版本。
- [ ] 所有 cleanup 都有 R2 deletion manifest、实测预算和单独人工批准。
- [ ] 用户原有 `reports/phase0/latest-*` 与 `memory/` 没有被提交或修改。

---

## 23. Luna 每个 PR 的交付格式

每个 PR 完成后必须报告：

1. PR 对应本文哪一阶段；
2. 实际修改文件；
3. 是否改变生产行为、读取路径或 D1 schema；
4. migration 是否 additive，本地和远端如何验证；
5. 跑了哪些测试及准确结果；
6. D1 `rows_read/rows_written` 的 before/after；
7. GitHub run URL、runKey、target date、candidate release ID；
8. R2 新增对象数、字节和 fingerprint；
9. active pointer 是否改变；
10. production canary 结果和回滚点；
11. 当前 feature flags；
12. 仍未完成的风险和下一 PR 前置条件。

不能只回复“workflow 成功”“已修复”或“应该在免费额度内”。成功必须同时有数据、幂等、查询预算、发布和生产页面证据。

---

## 24. 实施者最后检查

每次准备优化 D1 时先问：

```text
这是事实数据还是控制状态？
这份数据是否已经在 R2 可靠保存？
重复任务是否真的带来新 fingerprint？
这个查询为什么需要扫描历史？
这个索引每天节省多少 reads，又增加多少 writes？
D1 失败时原始证据是否仍能保住？
页面看到的 releaseId、asOf 和来源日期是否仍一致？
```

长期最优解不是让每一次重任务“稍微便宜一点”，而是让没有新事实的任务根本不做重工作，让行级历史不进入 D1，让所有发布都由内容指纹和完整性门禁决定。
