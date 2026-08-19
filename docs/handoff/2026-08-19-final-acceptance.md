# Flow 最终验收状态（2026-08-19）

本记录对应 `docs/handoff/2026-08-16-master-data-integrity-remediation.md` 的收尾验收，不改变生产读取路径，也不把缺少官方 PCF 的日期伪造成完整历史。

## 已完成

- PR0 的候选发布、回归测试和基线合同已落地。
- PR1–PR7 及其后续数据源/归档纠偏 PR 已合并到 `main`。
- 最新基线发布运行：[Daily market data #32231482376](https://github.com/xiaoker/flow/actions/runs/32231482376)，耗时约 5 分 30 秒并成功完成采集、归档、读模型构建、QA 和激活。
- 当前读模型：`release:2026-08-18:4d8ae263005ae1d7`。
- 发布 QA：166 pass、0 warn、0 fail。
- 两融历史：2024-08-01 至 2026-08-18，共 491 个点。
- ETF Radar：260 个真实交易日。
- `中天科技` / `600522` 已进入 `stock_search`。
- 国家队读模型包含 12 个 ETF，来源仍是 Radar 子集。
- candidate 校验、原子激活、统一 releaseId、部署 SHA 校验均已通过。
- [Deploy Flow Worker #32235571233](https://github.com/xiaoker/flow/actions/runs/32235571233) 已成功部署当前 `main`，生产接口具备可追踪的部署 SHA。
- [Production release canary #32235771190](https://github.com/xiaoker/flow/actions/runs/32235771190) 通过 100 轮、1,600 次只读请求；releaseId、asOf、8 个视图和缓存条件均稳定。
- [Cloudflare Access smoke #32237018779](https://github.com/xiaoker/flow/actions/runs/32237018779) 通过私有状态接口和官方 SSE source-proxy 验证。
- PR44（[Worker recovery route](https://github.com/xiaoker/flow/pull/44)，合并提交 `3ecb62e99d00a88779e6f52d783194ba3de1bd40`）已将 R2 恢复演练改为通过受保护的 Worker 绑定读取快照，不要求 GitHub Actions 的 Cloudflare API Token 直接读取 R2。
- PR46（[stock-flow inventory](https://github.com/xiaoker/flow/pull/46)，合并提交 `e728c683b1ceb5ac39b1973b9e51d04787b55614`）增加了只读的 ETF/PCF 归档库存探针；它不下载大对象、不请求交易所、不写入生产。

## 当前真实覆盖

ETF→个股穿透历史目前有 7 个有效分区：

```text
2026-08-10  2026-08-11  2026-08-12  2026-08-13
2026-08-14  2026-08-17  2026-08-18
```

其余日期因官方 PCF 与份额快照不能形成完整配对，保持缺失状态。覆盖不足时不计算 rolling20，也不复制当前节点冒充历史。

库存探针运行 [#32240813249](https://github.com/xiaoker/flow/actions/runs/32240813249) 检查了 2026-07-01 至 2026-08-18 的 30 个交易日：份额对象较早已归档，但带可验证 lineage 的 PCF 只有上述 7 天；因此更早日期不能仅凭份额快照重建 ETF→个股穿透。后续由每日真实 PCF 归档逐日累积分区，达到 21 个有效日后才启用 rolling20。

## 本次收尾增强

`verify_published_release.py` 新增 `--repeat 1-100`，并将 100 轮完整读取合同放入独立的 `Production release canary` 工作流（每周一 10:30 Asia/Shanghai，亦可手动触发）。每日采集只执行一次发布验证，避免把 100 轮只读请求叠加到数据采集窗口，导致发布超时或浪费免费额度。canary 输出 `iterations` 和 `requests`，用于发现 1102、5xx、releaseId 漂移和日期漂移。

部署后首次 canary 曾因生产 Worker 缺少 `buildSha` 在解析阶段失败（[失败运行 #32235465504](https://github.com/xiaoker/flow/actions/runs/32235465504)）；按部署工作流补齐版本身份后，100 轮 canary 成功。这证明 1102/发布稳定性合同现在可执行，而不是把“接口能打开”当作部署完成。

历史归档分片也已修复为“非 final 分片不重复携带约 4 MB 的 dashboard snapshot，最后一个 finalize 请求才携带并校验快照”，避免 Worker 在每个分片重复解析同一份大对象。

每日 PCF 采集也采用有界策略：每个 `reconcile` 窗口最多请求份额变化绝对值最大的 16 只 ETF（每只 ETF 两个官方 PCF 请求），超出部分在原始工件中记录为 deferred，不进入“已覆盖”统计。完整 PCF inventory/backfill 必须通过独立的可断点任务完成，不能让可选增强源耗尽每日采集窗口，也不能把未请求或未返回的数据填成 0。

本次受控运行实际记录 `changed_etf_count_exceeds_cap:504`，因此最新读模型同时暴露 `captureLimited=true`、`deferredEtfCount=504` 和原始 warning；页面不得把 16 只 ETF 的 PCF 结果解释为全市场完整覆盖。

受控验证运行 [#32226876853](https://github.com/xiaoker/flow/actions/runs/32226876853) 在旧逻辑下于采集步骤达到 20 分钟上限，之后未构建或激活候选版本；该证据促成了上述 PCF 请求预算修复。修复后的 [#32229701520](https://github.com/xiaoker/flow/actions/runs/32229701520) 和合并 PR40 后的 [#32231482376](https://github.com/xiaoker/flow/actions/runs/32231482376) 均证明采集步骤不再被未界定的 PCF 请求拖住。

## R2 恢复演练证据

第一次直接使用 Actions Token 执行 `wrangler r2 object get` 的尝试（[#32238311670](https://github.com/xiaoker/flow/actions/runs/32238311670)）因 Token 没有 R2 读取权限返回 403；D1 查询和其他步骤正常。这是权限边界问题，不是归档对象损坏。

部署 PR44 后，针对 `2026-08-18` 的独立只读恢复演练 [#32239467791](https://github.com/xiaoker/flow/actions/runs/32239467791) 已通过：

- `status=pass`、`readOnly=true`；未写入 D1/R2，也未激活或切换 release。
- `rawSha256`、`snapshotSchema`、`etfStateReplay`、`marketHistoryReplay`、`normalizedArtifacts` 全部为 `true`。
- 恢复 `2026-08-17` 与 `2026-08-18` 两个相邻快照，ETF 状态重放质量为 `pass`，894 个 ETF、1,787 个已填充值。
- ETF/PCF 标准化输入完整：两日份额各 1,608 行，PCF 合计 53,502 行，目标日 PCF 2,900 行。

因此“至少一次独立 R2 恢复演练”这一项已满足；后续仍可按月或在归档策略变更后重复执行。

## 自动采集连续观察证据（截至 2026-08-19 18:18 CST）

连续观察按 `asOf` 的**不同交易日**计数，而不是按 GitHub Actions 运行次数计数。同一交易日的手动重试、Cron failover 或同日修订，只能作为该日的补齐证据，不能增加观察天数。

当前证据如下：

| 运行 | 触发方式 | `asOf` | 结果 | 是否计入 |
| --- | --- | --- | --- | --- |
| [#32231482376](https://github.com/xiaoker/flow/actions/runs/32231482376) | workflow_dispatch | 2026-08-18 | read-model activated，`release:2026-08-18:4d8ae263005ae1d7` | 计入 1 天 |
| [#32229701520](https://github.com/xiaoker/flow/actions/runs/32229701520) | workflow_dispatch | 2026-08-18 | 同日重试并激活 | 不重复计入 |
| [#32221504254](https://github.com/xiaoker/flow/actions/runs/32221504254) | workflow_dispatch | 2026-08-18 | 同日重试并激活 | 不重复计入 |
| [#32207497293](https://github.com/xiaoker/flow/actions/runs/32207497293) | schedule | 2026-08-18 | failover 同日激活 | 不重复计入 |
| [#32204792088](https://github.com/xiaoker/flow/actions/runs/32204792088) | schedule | 2026-08-18 | `archived_pending`，等待北交所融资融券 | 不计入 |
| [#32167401954](https://github.com/xiaoker/flow/actions/runs/32167401954) | workflow_dispatch | 2026-08-17 | `archived_pending`，等待北交所融资融券 | 不计入 |

所以“连续 5 个交易日自动采集、补齐、激活”目前为 **1/5**，不能宣称已完成。2026-08-19 的晚间采集窗口在本记录时尚未开始；它只有在产生新的、完整且通过 QA 的 `asOf=2026-08-19` release 后，才可作为下一天观察。若某交易日仅归档 pending，或只出现同日重试，不得计入连续观察。

## 尚未满足的严格 DoD

1. 穿透历史尚未达到 21 个有效交易日；需要恢复官方 PCF，或等待真实交易日累积。
2. 连续 5 个交易日的自动采集、补齐、激活目前为 `1/5`，尚需 4 个不同交易日的有效记录。
3. R2/D1/Actions/Worker 免费额度尚未形成月度用量证据；当前 read-model 仅有软预算提示，没有硬限制失败。
4. 最新生产版本仍需人工验证日期选择、历史读取、个股搜索和国家队周期控件。

以上项目属于验收或官方数据覆盖工作，不应通过伪造历史数据来“修复”。
