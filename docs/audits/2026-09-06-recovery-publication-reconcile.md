# 恢复流程复盘与最小修复（2026-09-06）

## 结论与当前停止点

本文记录提交 PR 前的本地代码修复及验证，**没有执行补采、合并、部署、migration、开关变更或恢复计划注册**。用户已授权创建一个独立 PR 并运行一轮 CI；`CODE_READY` 须由该 PR 的实际 CI 结果证明，不能称为 `DATA_RECOVERED`。最终阶段状态见本地 `stage-evidence.json` 和 PR 检查。

这是现有受控恢复方案的阻断项修复，不是新架构或新的恢复阶段。固定审计截止日仍为 **2026-09-03（含）**；8 月 28 日、9 月 1/2 日只是待核实的缺口候选，不是本次已批准执行的数据 scope。

依据：

- 原仓库的 `docs/handoff/2026-09-04-controlled-recovery-through-20260903.md`，以“2026-09-05 执行修订”为优先。
- 原仓库的 `docs/handoff/2026-09-01-etf-stock-flow-date-anchor-recovery.md`。
- 代码基线 `781d8d2eb9267496b4a2bd71bb0dd0687542d02b`，独立分支 `codex/recovery-publication-reconcile`。
- 原工作区仍处于远端已删除的旧分支；所有原有未提交文件均保持原状。

## 必须纠正的历史判断

### 1. 不能用窗口日期代替数据日期

旧恢复文档已有运行证据：`daily-market-data:2026-09-01:reconcile:v1` 对应原请求 8 月 27、28、31 日。

因此，查询 `pipeline_window.target_date=2026-09-01` 或看到这个 runKey 为 `COMPLETED`，都不能证明 9 月 1 日的市场数据已发布；也不能为了恢复 8 月 28 日而另造一个 runKey 绕过原窗口。

### 2. 最近一次失败不是这三天的受控补采

2026-09-06 只读获取了 [运行 33863581907](https://github.com/xiaoker/flow/actions/runs/33863581907) 的实际日志与既有产物：

- 运行时间：2026-09-04 10:30:43 UTC；代码版本 `56c886f3138be25222120b1d0085f3397b7f4fde`。
- `INPUT_RUN_KEY=daily-market-data:2026-09-04:evening:v1`；`INPUT_TARGET_DATE=2026-09-04`。
- `INPUT_RECOVERY_ID` 为空，`PIPELINE_RECOVERY_ENABLED=0`；这是日常晚间采集。
- 产物 `asOf=2026-09-03`，stock flow 不可用，原因 `aligned_pcf_missing`。
- 旧工作流将空的 PCF catalog 当成“必须有 history-status”，在 source-bundle 验证退出；终态错误 `stock_flow_history_missing`。
- 此失败路径已经在 PR #79 修复，不属于本 PR 新修复内容。

已用当前 `scripts/verify_stock_flow_bundle.py` 验证该运行的**真实保存产物**：结果 `valid=true / NOT_REQUIRED / pcf_unavailable_empty_catalog_sidecar`。这证明 #79 覆盖了旧失败输入；**不证明该运行已重新发布，不证明穿透数据已经恢复**。

### 3. 单日目录没有对象，不等于原始数据不存在

复用 GitHub Artifact `market-data-33863581907`（约 1.7 MB），没有访问交易所，也没有重新采集。

其中 `history-bundle.json` SHA-256：

`a81e4d92ab034784d0f32226aceef161d3e51818b0a06a555205bc579f8fc81e`

20 份 raw 和 23 份 normalized 文件的内嵌 bytes 已重新计算 SHA，均与声明一致。与当前疑似缺口直接相关的结果：

| 证据 | 实际覆盖 | 能证明什么 / 不能证明什么 |
| --- | --- | --- |
| 沪市融资融券汇总 `requestedDate=2026-09-01_2026-09-03` | 标准化数据含 9/1、9/2、9/3，3 行；raw SHA `35fd7167fc2cc60dea34e475cdb440b1f043aaac175dd9d8204c3593cdee77c9` | 9/1、9/2 的原始及标准化证据已经保存；不能只查单日 R2 前缀就要求重采 |
| 沪市 ETF 份额 | 9/1：898 行；9/2：899 行；9/3：900 行 | 这些保存产物有对应日期；不能据此认定逐日读模型已发布 |
| 深市 ETF 份额 | 此包只包含 9/3，728 行 | 不能证明 9/1、9/2 缺失于整个 R2；还需按索引/manifest 核查其他归档 |
| 沪市 ETF NAV | 9/1、9/2 normalized 均为 0 行 | 保存了响应不等于保存了有效净值；必须保留数据缺口，不能补零 |

本次没有完成全桶盘点、没有读取生产 active pointer，也没有更新正式 recovery-plan 的 scope/hash。上述证据只能用于缩小后续动作，不能直接作为批准补采的替代品。

## 本 PR 的一个接受声明

**同一个受控恢复 runner 必须保留批准的业务日期及原调度窗口，真实执行访问间隔，并在历史检查点已完成之后验证其可读性；未完成不能返回 READY。**

只修改两个业务代码文件：

1. `src/trading_desk/recovery_execution.py`：移除“单日 dataDate 必须等于 runKey 中日期”的错误条件；保留合法 runKey、不可变 planHash、批准 hash、日期数量/排序、receipt scope/date 等校验，与已有 Worker plan 验证保持一致。
2. `scripts/run_live_recovery.py`：
   - 修复 `(last_by_host or {})[host] = ...` 在空字典时写入临时对象的问题，确保同 host 的请求间隔真的生效。
   - runner 不再把 windowDate 当 dataDate；证据中另列 `windowDate`。
   - ETF 状态已有某天、但 market-history 尚无该天时，先完成既有 history-import，再读取 market-history。保留目标日必须存在的验证，不删除断言。
   - history-import 的 HTTP 200 不自动代表完成：必须 `status=archived` 且 `asOf` 精确匹配。等待来源为 `FAILED_RETRYABLE`；日期错误或衍生质量失败为 `BLOCKED_QUALITY`。

没有修改 ETF 公式、单位、行业分类、PCF 算法、生产读取路径、租约 SQL、工作流、D1 schema、R2 retention 或 active pointer。

## 回归证据

先增加测试再修代码：新增回归初次运行有 6 个失败，分别暴露同 host 没有等待、窗口日期误绑定、历史检查点先读后写以及未区分待归档状态。随后修复，没有删除旧测试、降低 QA 断言或用 0/前值补数据。

最终本地命令（工作目录为独立 worktree）：

```sh
PYTHONPATH=src:. /Users/xiaoker/Documents/交易台/.venv/bin/python -m pytest -o addopts= -q --junitxml=.recovery/verification/python-tests.xml
cd web
npm run lint
CI=1 WRANGLER_SEND_METRICS=false npm test
```

- Python：306 passed。
- Web：64 passed，包含真实本地 SQLite 事务及并发 claim 测试。
- lint：通过。
- 生产构建：通过；存在既有前端 chunk >500 kB 提示及 Node experimental SQLite 提示，不是此次失败。
- runner fixture 的“ETF 状态已有、market-history 缺失”路径为 `state read -> archive chunk -> finalize -> history read -> latest read`，5 个模拟 HTTP 请求、1 次 finalize；没有 active pointer 写入。
- runner fixture 的窗口 9/1、数据 8/28 路径通过；将 receipt 改成 9/1 仍被 scope 校验拒绝。
- 限速 fixture 验证同 host 真实等待 2 秒/1.5 秒，其他 host 不被错误阻塞。
- 同时保留缺少目标 history 日、403/429、请求预算耗尽、多日误投单日 runner、归档日期错误和质量错误的阻断测试。

**fixture 的通过只是本地代码验证，不是生产 canary。**

## 尚未关闭的接受项

以下问题不在这两个文件的最小修复中“顺便解决”，也不允许因此直接运行恢复：

1. 完整 archive-first：当前 core runner 仍会进入 `collect_phase0`；只有 PCF/stock-flow 输入已有归档优先能力。必须先闭合原始对象/输入哈希的复用清单，不能因本 PR 通过就宣称能够只下载所有缺失项。
2. 多日原窗口：单日 canary 支持保留原窗口，但 runner 仍拒绝一次传入多个日期；多日 scope 的逐日执行和总 scope 完成仍需按交接文档单独验收，不能只恢复一天就关闭原多日窗口。
3. 真实缺口：8/28 原始来源、9/1/2 深市 ETF、历史有效 NAV、PCF 和读模型/slot 的完整性还需要有界证据。不得继续沿用“只剩三天/这些天全部原始数据缺失”的口头结论。
4. 全账户预算：本轮 GitHub billing API 返回 404，并明确提示当前授权缺少 `user` scope；没有扩大 OAuth 权限。Actions 可用免费余量保持 UNKNOWN；Cloudflare 全账户余量也没有在本轮重新测量。
5. 完成判定：尚无本轮 recovery receipt、未验证目标历史 URL/页面、未观测自然调度窗口。本轮 `DATA_RECOVERED=false / PRODUCT_VERIFIED=false`。

## 费用与下一步

本轮仅本地测试、读取 GitHub 已有日志和小型 Artifact。没有 dispatch；没有新增 D1/R2 生产读写，也没有新增采集/发布运行。

副作用清单位于本地 `.recovery/verification/merge-side-effects.json`，不提交运行产物。按本 PR 当前文件集：

- 推送独立分支本身不触发当前工作流。
- 创建 PR 会触发 Python/Web CI（分别配置 10/15 分钟 timeout）及轻量 Branch freshness 检查；后者未配置显式 timeout，不能把 25 分钟称为全 PR 硬上限，更不能承诺账单为零。
- 合并到 main 只命中 CI；不会命中 `Deploy Flow Worker` 的 `web/**` 或 deploy-workflow 路径过滤，不会自动 migration/deploy，也不触发采集/发布。
- 后续受控恢复运行会使用 main 上的新 Python 代码；合并代码仍不等于运行了数据恢复。

当前授权只覆盖：推送单一独立分支、创建一个 PR、运行一轮 CI 并等待结果；**不合并、不部署、不补采**。CI 完成后停止报告，不自动重跑。再按原交接文档闭合 R0-D 证据/预算和 R2-A 的剩余接受项，然后才能批准精确 planHash 与单日 canary。
