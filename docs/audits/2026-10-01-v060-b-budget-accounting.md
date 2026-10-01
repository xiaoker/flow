# v0.6-B 资源计量与预算准入说明

本文件描述 060-B 的候选实现边界，不代表生产预算切换或账户余量证明。

## 合同

- 七个资源维度统一使用 `value + unit + basis + measuredAt + scope`：D1
  `rowsRead/rowsWritten`、Worker 请求、Actions 分钟、R2 字节和 Class A/B
  操作。
- `basis` 只能是 `measured`、`estimated` 或 `unknown`。缺失、超时、无法
  解释的指标保持 `unknown`，不能写成数值 0。
- 任务必须提供完整的成本上界；账户快照、活动 reservation、任务成本或
  保守上限任一缺失，准入结果为 `unknown_metrics`。
- 保守上限按 `min(official, hard)` 计算。R2 Class A/B 若配置没有官方/内部
  上限，仍为未知，不因当前使用量为零而放行。

## 原子账本参考实现

`BudgetLedger` 只在内存中实现同一原子状态机，供测试和后续适配器复用：

```text
reserve -> RUNNING -> debit(delta) -> SUCCEEDED/FAILED_KNOWN
                          \-> UNKNOWN/TIMEOUT（保留完整 reservation）
```

- reservation 在同一锁内与现有 reservation 一起检查，避免并发超卖；重复
  task id 是幂等操作，成本不同则冲突。
- `debit` 记录真实物理计数；D1 行数不由逻辑 changes 推算。
- 已确认结算的实际计量会进入后续可用预算；结果不明、计量超时或超出上界
  的 reservation 不自动释放，等待显式对账。
- 该实现没有 D1 migration、R2 写入、GitHub dispatch 或生产调用；060-C
  才讨论工作流/本地入口接入。

## 现有阶段报告的兼容行为

`scripts/report_stage_budget.py` 仍保留既有 D1 阶段预算字段，但额外输出
`resourceMetrics`。工作流传入的 `--actions-minutes 0` 和 `--r2-bytes 0`
被记录为 `unknown`，不再冒充实测零；没有账户快照时，报告只做诊断，不成为
生产准入。完整候选准入由 `scripts/check_resource_budget.py` 离线执行，未知
或超预算返回非零退出码。

## 不在本阶段

- 不修改 `.github/workflows/*`、Cloudflare Scheduler、Worker 入口或生产 flags。
- 不新增 D1 账本表，不向远程 D1/R2 写入预算状态。
- 不改变采集、发布、恢复和 active release 行为，不运行数据任务或 PCF。
