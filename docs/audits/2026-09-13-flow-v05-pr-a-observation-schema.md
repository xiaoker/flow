# Flow v0.5 / PR A：来源观察账本 additive schema

状态：`CODE_READY`（本 PR 仅新增本地可验证的 schema/migration；未执行远程 migration、未开启影子写入）

## 目标与边界

v0.5 的目标是从新增采集开始保留可复核的官方来源事实，区分“来源尚未发布”和“请求/解析失败”。本 PR 只完成 additive schema：新增 `source_observation` 表、Drizzle 元数据、receipt JSON Schema 和本地回归测试。采集器双写、source-readiness projection、轻量 probe、feature flag 和生产读取路径属于后续 PR，不在本 PR 中实现。

## 为什么现有表不够

现有 `source_readiness` 是 `(target_date, source_id)` 一行的可变投影，适合回答“当前是否 ready”，但不能完整表达一次 observation：

| 需要审计的事实 | `source_readiness` 现状 | 影响 |
| --- | --- | --- |
| 同一来源的多次观察和官方修订 | 主键覆盖旧行 | 无法重放或比较修订 |
| 官方 URL、请求参数、HTTP/Content-Type、响应大小 | 仅在自由 JSON `evidence_json` 中可选 | 无强制字段，无法稳定审计 |
| 原始对象 key、raw/normalized SHA-256 | 没有列 | 不能证明来源响应与规范输入的对应关系 |
| 发布日期、来源业务日期、日期映射规则 | 只有 `observed_dates_json`/`watermark_json` | 日期语义不受 schema 约束 |
| 内容指纹幂等 | 没有唯一 fingerprint | 相同响应可能重复写入 |
| 运行窗口与事实身份 | 只有 `run_id` | 运行 ID 不能作为事实幂等键 |

因此新增一个不可变 receipt 表；`source_readiness` 继续作为后续 PR 生成的当前状态 projection，不被替换或回填。

## 新表合同

`source_observation` 的主键 `observation_id` 由后续 producer 根据 receipt 内容确定；`source_fingerprint` 是不包含运行 ID、观察时间或随机数的 canonical SHA-256，并设唯一索引。相同 fingerprint 重放时不应产生第二个 receipt 或 raw 对象。

核心日期语义：

- `target_date`：请求/调度窗口对应的业务日期。
- `source_business_date`：官方响应实际表达的业务日期；未发布或不可用时保持 `NULL`。
- `published_date`：官方页面/文件的发布日期；与业务日期可不同。
- `date_basis`：日期映射规则版本，不能用页面显示日期替代。
- `observed_at`：UTC 观察时间，不能代替业务日期。

`OBSERVED`/`VERIFIED` receipt 必须有原始对象 key 和 raw SHA-256；`NOT_PUBLISHED`、`SOURCE_UNAVAILABLE`、`REJECTED_QUALITY` 必须有 reason/evidence，但不得用 0 或前值伪造业务数据。详细 JSON 约束见 `schemas/source-observation-receipt.schema.json`。

## 迁移 SQL 与写入估算

Drizzle 生成的下一序号迁移为 `web/drizzle/0013_unusual_zeigeist.sql`，只包含：

```sql
CREATE TABLE `source_observation` (...);
CREATE UNIQUE INDEX `source_observation_fingerprint_uq`
  ON `source_observation` (`source_fingerprint`);
CREATE INDEX `source_observation_target_source_idx`
  ON `source_observation` (`target_date`, `source_id`, `observed_at`);
```

无 `DROP`、`DELETE`、`TRUNCATE`、表重建或旧表回填。迁移本身只创建空表和索引；本 PR 未对远程 D1 执行 migration。

后续 producer 的事实行写入上限按“本窗口每个 source/dataset 每个新 fingerprint 一行”计算。当前 source 定义为 8 个 dataset，因此单个 collection window 的最坏新增 receipt 为 8 行；相同 fingerprint 的重试为 0 行，来源未发布也只写 1 行状态 receipt。实际窗口若有修订，新增量等于新 fingerprint 数，不按 ETF 行数放大。`source_readiness` projection 的写入属于后续 PR，不计入本表估算。

## 查询计划边界

后续 readiness/reconcile 只允许使用有界查询：

```sql
SELECT observation_id, source_business_date, published_date, status,
       source_fingerprint, observed_at
  FROM source_observation
 WHERE target_date = ? AND source_id = ?
 ORDER BY observed_at DESC
 LIMIT ?;
```

`source_observation_target_source_idx(target_date, source_id, observed_at)` 覆盖过滤和排序前缀；fingerprint 去重使用唯一索引。测试在本地 SQLite 用 `EXPLAIN QUERY PLAN` 断言出现 `USING INDEX`，并以 LIMIT 硬上限避免无界读取。生产 D1 的真实 rows_read/rows_written 要在后续影子运行中采集，不能用本地计划冒充生产遥测。

本地内存 SQLite 对上述查询的实际计划为：

```text
SEARCH source_observation USING INDEX source_observation_target_source_idx (target_date=? AND source_id=?)
```

## 合并副作用

本 PR 自身不会 dispatch workflow、写入远程 D1/R2 或改变生产路径。由于 `web/db/schema.ts` 和 Drizzle migration 位于 `web/**`，合并到 `main` 会触发现有 `deploy-worker.yml`：它会先运行 Web 检查，再执行 `wrangler d1 migrations apply flow --remote`，随后部署 Worker。该副作用已记录在 `docs/audits/2026-09-13-flow-v05-pr-a-merge-side-effects.json`，因此 CI 全绿后必须单独等待“合并并执行远程 additive migration/Worker 部署”的明确授权；本 PR 不自动合并。

## 验收与停止点

- schema、migration journal/snapshot 与 JSON Schema 一致。
- migration 仅 additive，索引查询不全表扫描，重复 fingerprint 被拒绝。
- receipt 的日期、hash、状态组合按合同校验；未知/缺失证据 fail closed。
- 只运行本地测试；无 D1/R2 写入、workflow dispatch、active pointer 或 feature flag 变化。

CI 全绿后停止，等待明确合并授权。合并后下一步仍是 v0.5 / PR B（默认关闭影子 receipt 双写）；不得在本 PR 中运行采集或切换生产读取路径。
