# Flow 运行时架构

## 目标

交易所原始文件只在采集任务中获取和校验；用户请求只读取已经通过质量门禁的发布对象。Worker 不在请求期间加载 ETF 全量状态、不做板块聚合、不计算长周期。

## 数据路径

```text
Cloudflare Cron（主触发）
        │ dispatch
        ▼
GitHub Actions（官方采集 / QA / 离线计算）
        │
        ├─ R2 immutable raw + normalized evidence
        ├─ R2 read-model views
        └─ D1 release_manifest → active_release（最后一步切指针）
        │
        ▼
Cloudflare Worker
        ├─ 静态 SPA 壳
        ├─ D1 读取发布指针
        └─ R2 body 流式返回
```

## 发布约束

- `asOf` 是单一交易日；不把不同交易日的数据拼成一个页面。
- `quality.fail > 0` 或 `gate=blocked` 的快照不能替换 active release。
- ETF `historyCoverage` 必须在离线构建阶段生成；请求时不得回退到 R2 rolling state。
- 视图对象使用内容哈希和固定 key，重复运行不会覆盖原始对象。
- `active_release` 只保存当前指针；切换前先把所有对象和清单写完。

## 采集触发

Cloudflare Cron 仍是主调度器，按晚间、早间和 reconciliation 三个窗口 dispatch `daily-data.yml`。GitHub schedule 在稍后时间作为 failover。相同 GitHub run 重试由 `ingest_run` 幂等预检拦截；新一轮发布必须通过新的质量门禁。

## 主要接口

- `/api/v2/dashboard`：流式返回当前或指定历史发布快照。
- `/api/v2/views/{view}`：按视图读取已发布对象，允许 `overview / radar / rotation / national / connect / margin`。
- `/api/v2/releases/latest`：返回 D1 active release 清单。
- `/api/v2/history/dates`、`/api/v2/market-history`：只读有限历史索引，不读取 R2 ETF 状态。

## 本地验证

```bash
PYTHONPATH=. .venv/bin/pytest -q
cd web && npm test && npm run lint
python scripts/build_read_models.py --snapshot dashboard-snapshot.json --output-dir read-models --require-materialized
```
