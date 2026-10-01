# v0.6-C 候选执行入口说明

本文件记录 v0.6-C 的候选实现边界。它不是生产 runner、调度器、恢复
授权器或 active release 写入路径。

## 一个入口、两个执行环境

本 PR 新增 `trading-desk pipeline`，本地命令和手动 GitHub workflow 都调用
同一 `trading_desk.pipeline_runner.run_pipeline_request`，再由它调用
`pipeline_application.execute_application_plan`。执行器负责：

- 校验固定 `runKey/windowDate/targetDate` 身份和 `pipeline-application-v1` 合同；
- 校验精确 producer SHA、依赖锁文件摘要、现有管理员鉴权或批准恢复鉴权；
- 校验 lease owner、ACTIVE 状态、未过期时间和迟到 owner；
- 用同一阶段输入哈希和 checkpoint 自哈希支持中断续跑；
- 把 R2 成功而 D1 失败保持为失败状态，禁止伪装成 COMPLETED；
- 对历史 target 保留 `latestDataDate`，不移动 active release；
- 只写调用者指定的隔离 state/output 文件，并拒绝 `memory/` 与受保护
  `reports/phase0/latest-*` 输出路径。

## 请求合同

请求必须包含 `identity`、`mode`、`lease`、`authorization`、
`dependencyLockSha256`、`stages`、`stageInputs` 和 `stageResults`。

- ordinary 使用 `existing-admin`；不允许携带 recovery 字段。
- recovery 必须同时携带 `recoveryId`、批准计划 `planSha256` 和
  `approved-recovery` 鉴权；此入口不会创建、注册或批准计划。
- producer SHA 由本地 `git rev-parse HEAD` 或 GitHub `$GITHUB_SHA` 传入，
  不接受工作树内另一个版本的声明。
- 依赖锁摘要由实际读取的 `web/package-lock.json`（可扩展为多文件）计算，
  请求声明不匹配就 fail closed。

## 运行与副作用边界

`.github/workflows/pipeline-executor-candidate.yml` 只有
`workflow_dispatch`，没有 push、PR、schedule 或生产密钥。它 checkout
精确 `$GITHUB_SHA`，运行同一个 CLI，并仅上传 7 天的候选 fixture evidence。
它不调用 Worker、D1、R2、GitHub dispatch 或 active release。

本地和 Linux fixture 均覆盖：

1. 完整运行后再次运行只复用精确 checkpoint；
2. FAILED_RETRYABLE 中断后只重跑不匹配阶段；
3. recovery 必须有批准计划且迟到/过期 owner 被拒绝；
4. R2 成功、D1 失败不能成为 COMPLETED；
5. 历史 target 不回退 latest；
6. 依赖锁不匹配和受保护输出路径 fail closed。

## 不在本 PR

- 不改 `market-data-collect.yml`、`market-data-publish.yml`、Watchdog、
  Cloudflare Scheduler 或 recovery workflow 的生产调用；
- 不切换新入口、不启用 flag、不运行真实采集/恢复/canary、不写远程 D1/R2；
- 不把本地 fixture 或手动 workflow 成功解释为生产恢复验收；
- 不进入 v0.7 能力清单或 UI。
