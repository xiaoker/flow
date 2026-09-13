# Flow v0.4 可信发布合同

状态：`PROPOSED_PENDING_USER_CONFIRMATION`

本文件是 v0.4 / PR A 的产品和架构合同。它把 Flow 的产品完成定义、日期语义、数据能力分级和稳定性目标固定为可审查的机器可读配置；本 PR 不改变采集、发布、Worker 读取、D1/R2、feature flag 或 active pointer。

机器可读主文件：`config/flow-product-contract.json`
结构校验：`schemas/flow-product-contract.schema.json`
纯校验器：`src/trading_desk/product_contract.py`

## 产品完成定义

Flow 的一次成功发布不是 workflow 绿色、日期选择器出现日期或某个 dashboard 文件存在，而是一个交易日的 decision release 同时满足：

1. 官方来源业务日期可追溯，原始/规范化输入和内容指纹可复核。
2. 所有 required capability 的日期、版本和输入兼容。
3. 一个 release 只代表一个 `asOf`，不能跨日期拼接。
4. 读模型、历史覆盖和板块/ETF 对账通过质量门禁。
5. candidate 通过预算和 canary 后才可成为 active。
6. optional capability 缺失时，页面必须明确显示缺口，不能把部分结果称为全产品完成。

## 三层状态

### Source

`UNKNOWN`、`NOT_PUBLISHED`、`SOURCE_UNAVAILABLE`、`OBSERVED`、`VERIFIED`、`REJECTED_QUALITY` 和 `SUPERSEDED_BY_REVISION` 描述来源事实，不描述任务是否成功。`SOURCE_UNAVAILABLE` 只有带来源、业务日期、原因、观察时间和 fingerprint 的证据才是已知终态。

### Market day

`WAITING_SOURCES -> READY_TO_BUILD -> BUILDING -> CANDIDATE -> ACTIVE -> SUPERSEDED` 是交易日派生状态；质量失败进入 `QA_BLOCKED`。来源未发布不能直接成为 active。

### Decision release

`CANDIDATE -> ACTIVE -> SUPERSEDED`，质量或合同失败进入 `REJECTED`。decision release 位于 dashboard 和 `etf_penetration` 等 capability slot 之上，是页面唯一应依赖的完整日期判断。

## 能力分级（提案）

当前配置的分级是 v0.4 的提案，必须在后续生产切换前得到产品确认：

- `REQUIRED_FOR_DECISION`：dashboard、ETF 雷达、ETF 周期、国家队、互联互通、两融和板块轮动。缺失时不能激活全局 decision release。
- `OPTIONAL_EXPLICIT`：ETF→个股穿透、个股搜索和个股详情。可以与核心 dashboard 分开发布，但必须在 manifest 和页面明确日期/覆盖原因。
- `NOT_APPLICABLE`：只在某个日期或产品天然不适用时使用，不能用来掩盖未知或缺失。

这套分级不等于当前生产开关已经启用。`classificationStatus` 保持 `PROPOSED_PENDING_USER_CONFIRMATION`，因此 v0.4 只提供合同和测试，不改变运行时门禁。

## 日期与身份语义

- `windowDate`：Asia/Shanghai 调度窗口日期；不能代替行情业务日期。
- `dataDate`：官方原始事实的实际业务日期，必须由原文或官方日历确认。
- `asOf`：一个 decision release 所代表的唯一交易日。
- `observedAt`：UTC 观察/构建时刻；不能代替 `dataDate` 或 `asOf`。
- `sourceDates`：每个来源的业务日期映射，必须可审计。
- `releaseId`：不可变发布内容的身份，不能用 GitHub run ID 代替。
- `sourceFingerprint`：由来源身份、业务日期、输入对象 hash 和合同版本组成；不能包含运行时间、run ID 或随机数。

## 关键禁止组合

机器可读合同拒绝以下结论：

- required evidence 为 `UNKNOWN` 却标记 `DONE` 或 `ACTIVE`；
- required capability 未就绪却激活 decision release；
- dashboard 可见但 penetration 未就绪却宣称全产品恢复；
- 一个 release 包含多个 `asOf`；
- 预算未知却标记 `RECOVERY_READY` 或 active；
- workflow 成功却没有 active decision release，仍称数据恢复完成。

## 资源与 SLO

数字预算唯一来源仍是 `config/resource-budget.json`，本合同不复制 D1/R2/Worker/Actions 数值。v0.4 固定的产品目标包括：必要来源到达后 30 分钟内形成完整 decision、北京时间 09:15 的常规目标、连续 20 个交易日无需人工恢复、重复重任务/active 回退/跨日期拼页/Worker 1102 均为零。

预算未知必须保留 `unknown`，而不是当作零或“估计可用”。核心日任务和恢复/实验任务的优先级沿用长期 D1 方案。

## 旧路径退役原则

合同列出候选退役项，但 v0.4 不删除任何路径。只有在替代路径通过观察期、证明无生产调用者并完成独立可回滚 PR 后，才可在 v0.9/v1.0 处理：

- compatibility `daily-data.yml`；
- manifest v2 写入路径；
- mutable `latest-*` 跨任务协议；
- 重复 recovery inventory/provenance/repair 入口；
- 被 pipeline runner 替代的长 YAML/Shell 业务分支。

其中 `reports/phase0/latest-*` 和 `memory/` 仍属于用户保护路径；本合同只是记录退役候选，不授权现在修改、删除、暂存或提交它们。

## v0.4 验收与停止点

运行：

```bash
PYTHONPATH=src:. python scripts/validate_flow_product_contract.py
PYTHONPATH=src:. python -m pytest -q tests/unit/test_product_contract.py
```

同时运行仓库真实存在的完整 Python 测试和 `web` 的 `npm run lint`、`npm test`。验收只证明合同和纯校验器；不代表生产数据、部署、恢复或读取路径已切换。

PR A CI 通过后必须停止，等待明确合并授权。下一允许版本是 v0.5 来源观察账本与交易日协调器；不得在本 PR 中补采、运行 R0-D、注册恢复计划、执行 migration、部署 Worker、切换 flag 或修改 active pointer。
