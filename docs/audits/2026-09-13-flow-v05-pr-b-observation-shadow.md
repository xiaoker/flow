# v0.5 / PR B：采集端 observation receipt 影子双写

## 范围

本 PR 只在现有 `history-import` 采集归档入口增加来源观察凭证（observation
receipt）的影子双写。`SOURCE_OBSERVATION_SHADOW_ENABLED` 默认值为 `0`，本 PR
不打开生产开关，不改变 active release、dashboard 读取路径、ETF 公式、PCF、
分类、恢复流程或 R2 retention。

采集器在每个官方 artifact 的 raw 对象和 metadata 对象成功写入 R2 后，才生成
receipt。每个 artifact 独立处理；一个来源的等待或错误不会删除其他来源已经写入
的不可变证据。

## Receipt 合约

- schema：`schemas/source-observation-receipt.schema.json`（schemaVersion 1）
- contractVersion：`flow-product-v0.5`
- R2 key：`market-data/source-observations/<targetDate>/<sourceId>/<dataset>/<sourceFingerprint>.json`
- `sourceFingerprint` 是稳定的内容/日期身份哈希，不包含观察时间、runner id、URL
  或 workflow run id；同一官方 bytes、语义日期和请求分区重放得到同一个 fingerprint。
- `observationId` 由 source、dataset、targetDate 和 fingerprint 前缀确定。
- raw、raw sha256、source business date、published date（仅在来源明确给出时）、
  observedAt、dateBasis、HTTP 状态、请求参数（敏感字段脱敏）和有限 evidence 均被记录。
- `NOT_PUBLISHED`、`SOURCE_UNAVAILABLE` 与 `OBSERVED`/`VERIFIED` 分开表达；不能
  以 0、前值或页面日期代替缺失来源日期。

## 写入顺序与幂等

1. 现有 `putImmutableRaw` 先以内容地址 raw key 写入 raw 和 metadata。
2. 影子开关开启时写入 receipt R2 对象。R2 key 是 fingerprint 地址；已有对象必须
   通过 fingerprint 和对象 sha 校验，否则返回冲突，不覆盖旧证据。
3. R2 成功后尝试向现有 `source_observation` D1 表执行一条
   `INSERT ... ON CONFLICT(source_fingerprint) DO NOTHING`。D1 失败只返回
   `d1Status=unavailable`，不删除或重写 R2 receipt；后续有界 reconcile 可以重新物化。
4. 若 receipt R2 写入失败，本请求在处理完当前有界 artifacts 后返回可重试的
   `source_observation_receipt_failed`，不会继续任何 snapshot/release 发布动作。

receipt 序列化中的 `createdAt` 使用 canonical `observedAt`，避免重试因当前时间
不同而产生不同对象内容。重复执行因此只产生一次 R2 对象和一次 D1 行。

## 预算与生产边界

影子开关关闭时新增请求、D1 行和 R2 对象均为 0。开关开启后，每个 artifact 最多
增加 1 次 receipt R2 写入与 1 条 D1 insert（重复 fingerprint 的 D1 insert 为
`changes=0`，R2 为 head 命中）；receipt evidence 上限为 32 KiB。没有全表扫描、
历史回填、workflow dispatch、D1 migration 或 active pointer 写入。

本 PR 携带 workflow 的稳定 `runKey`，但只作为 receipt 关联字段；runKey 仍由现有
pipeline window 合约生成，不能用 GitHub run id 代替。

## 验收与后续停止点

- Node 测试覆盖 fingerprint 稳定性、R2/D1 幂等、D1 暂时不可用、来源状态和非法
  runKey。
- Python 测试覆盖 history bundle 传递稳定 runKey。
- 完整 Python 测试和 `web` 的真实 lint/test/build 必须通过。
- 本 PR 合并会按现有 `deploy-worker.yml` 触发 Worker 部署；当前没有新增 migration，
  但合并/部署必须单独授权。
- 合并后才可在开关仍为 0 的情况下部署；开关切换和连续 5 个真实交易日影子观察属于
  后续 v0.5 验收，不在本 PR 执行。
