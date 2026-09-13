# v0.5 / PR C：交易日投影与有界来源探测

## 目标与边界

本 PR 为 v0.5 的最后一个代码 PR，增加一个由现有 `source_observation` 账本派生的
紧凑 `market_day_state`，以及一个默认关闭、顺序执行、有界的官方来源 probe。它
只回答“来源是否出现了新的、可能属于目标交易日的事实”，不解析完整数据、不构建
read model，也不发布或切换任何 release。

本 PR 不做以下事情：

- 不开启 `MARKET_DAY_PROBE_ENABLED` 或 `SOURCE_OBSERVATION_SHADOW_ENABLED`；
- 不修改 active release、生产读取路径、ETF/PCF/分类公式或历史语义；
- 不 dispatch Watchdog、Collector、Publish，不补采、不回填、不运行 canary；
- 不新增第二套租约；`pipeline_window` 仍是唯一任务幂等和租约边界；
- 不执行远程 D1 migration。合并后的既有 deploy workflow 可能按授权执行 additive
  migration，详见同名 `merge-side-effects.json`。

## 来源与日期合同

投影只接受已有账本中明确的 `sourceId + dataset + targetDate + sourceBusinessDate +
sourceFingerprint`。所有 required 来源必须是 `VERIFIED` 且业务日期等于目标日期，才会
得到 `READY_TO_BUILD`；缺失、未发布、来源不可用、质量拒绝和日期错配保持独立状态，
不会被转换成 0、前值或估算值。

| 投影来源 | 官方来源 | probe 日期规则 |
| --- | --- | --- |
| `sse_etf` | SSE ETF 规模查询 | 请求目标日期，只有响应可证明目标日期才标记业务日期 |
| `szse_etf` | SZSE 基金列表接口 | 页面日期只记录为发布时间水位，不猜测前一交易日；精确映射由完整采集器负责 |
| `sse_margin` | SSE 两融市场汇总 | 请求目标日期的官方交易日查询 |
| `szse_margin` | SZSE 两融市场汇总 | 请求目标日期的官方交易日查询 |
| `bse_margin` | BSE 两融汇总 | 只有响应明确包含目标日期才标记业务日期 |
| `stock_connect_summary` | HKEX 每日 JS 文件 | 文件名和正文均以目标日期为准 |

`sse_etf_nav` 与 `sse_etf_pcf` 是 optional 能力，不会因为它们暂未出现而把六个
required 来源伪装成完整。SZSE 的“页面前一交易日”语义禁止在 probe 阶段自行推断，
避免把发布时间日期误当业务日期。

## Probe 约束

- 仅使用代码内固定的六个官方 HTTPS endpoint，不接受用户提供 URL；
- 每次请求只读取 `Range: bytes=0-65535`，响应体最多保留 64 KiB；
- 单请求超时 8 秒；六个来源严格串行，不产生并行突发；
- 只生成状态、日期标记、响应元数据和前缀哈希，不写 R2，不写
  `source_observation`，不安装依赖，不运行 Python/Node 构建；
- `404` 为 `NOT_PUBLISHED`，HTTP 错误为 `SOURCE_UNAVAILABLE`，其它响应为
  `PUBLISHED`，但 `PUBLISHED` 仍不等于 `VERIFIED`；
- fingerprint 包含 probe 合约版本、来源、目标日期、状态、日期标记、响应元数据和
  有界正文哈希；同一输入产生稳定指纹。

## 交易日投影与协调

`projectMarketDay` 按每个来源的最新有效观察生成一行投影，输出：

- `WAITING_SOURCES` 或 `READY_TO_BUILD`；
- required/verified/pending/unavailable 计数；
- 每个来源的日期、状态、观察 id、fingerprint、probe 和下一步动作；
- canonical `sourceFingerprint` 与 `probeFingerprint`。

Worker 只有在 `MARKET_DAY_PROBE_ENABLED=1` 且已有
`PIPELINE_WINDOW_GATE_ENABLED=1` 时才执行协调。协调会先做 probe、读取目标日期最多
64 条观察、读取同一目标日期的一行 `market_day_state`，再用一个有界 upsert 保存派生
状态。只有至少一个来源 probe 为 `PUBLISHED`，且首次出现或 probe fingerprint 变化
（或已有 `FAILED_RETRYABLE`）时，才允许进入现有 pipeline_window claim；稳定的未发布/
不可用状态不会反复消耗重采资源。claim 仍是最终跨调度器门禁，probe 不替代租约。

开关保持默认关闭时，Worker 的旧 dispatch 行为不变；打开 probe 而关闭
`PIPELINE_WINDOW_GATE_ENABLED` 会 fail closed，避免影子逻辑绕过既有租约。

## 数据库与读取面

新增 additive migration `web/drizzle/0014_warm_morlocks.sql` 和
`market_day_state` 表，仅保存有界投影 JSON、指纹、计数和时间戳。唯一索引为主键
`target_date`，辅助索引为 `(next_action, updated_at)`。管理员 GET
`/api/v1/admin/market-day-state?date=YYYY-MM-DD` 只做一次主键查找，并复用现有
`INGEST_API_KEY` 鉴权；没有 POST、没有 release 或 pointer 写入。

## 验收结果

已运行仓库真实链路：

- `cd web && npm test`：118 tests passed（包含 production build）；
- `cd web && npm run lint`：通过；
- `PYTHONPATH=src:. /Users/xiaoker/Documents/交易台/.venv/bin/python -m pytest -q`：全量通过。

测试覆盖 required 来源闭合、日期错配、稳定指纹、固定 endpoint、64 KiB 上限、串行
probe、probe 无 D1/R2/dispatch 副作用、单行有界 upsert、索引/分页限制、flag 默认关闭
和 Worker 现有 dispatch 路径不变。

## 停止点

本 PR 推送并创建 PR 后，等待 CI 全绿并停止，不能在本 PR 中合并或开启影子运行。合并
并确认部署后，才进入 v0.5 的后续步骤：在开关仍由明确授权控制的前提下，连续观察
五个真实交易日，对比旧流程和新投影；五日观察完成前不得开始 v0.6。
