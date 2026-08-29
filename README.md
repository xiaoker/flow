# FLOW 个人市场数据交易台

Phase 1 动态版已经上线。系统从交易所官方来源采集 ETF、互联互通和融资融券数据，区分“官方事实”与“公式计算”后生成统一快照，经受保护的写入接口把交易所原始文件存入 R2、规范化 ETF 日表存入 D1，并由总览页和只读 API 展示最新成功快照。

2026-07-16 已完成所有者 Cloudflare 资源的云端端到端验证：快照日期为 `2026-07-15`，基础质量门禁为 `conditional_pass`，46 项通过、2 项已知口径警告、0 项失败；发布级 Data QA 93 项全部通过。流水线、写入接口和读取端均会拒绝质量失败的快照覆盖上一个有效版本。

Cloudflare 自有资源版：[flow.xiaoker.com](https://flow.xiaoker.com)（Cloudflare Access 私有入口，仅项目所有者可登录）

## 本地运行

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/trading-desk collect-phase0 \
  --dates 2026-07-10 2026-07-13 2026-07-14
.venv/bin/python -m pytest
.venv/bin/python scripts/build_dashboard_snapshot.py --output dashboard-snapshot.json
.venv/bin/trading-desk qa-release
.venv/bin/python scripts/build_ingest_bundle.py --output ingest-bundle.json
.venv/bin/python scripts/build_history_bundle.py --output history-bundle.json
.venv/bin/python scripts/build_phase0_notebook.py
```

原始响应和规范化 CSV 保存在 `data/`，默认不提交 Git。可审计的质量摘要、Notebook 和报告保存在 `reports/`、`notebooks/`。

`qa-release` 是发布前的第二道独立门禁。它会复验官方域名、原始响应 SHA-256、元数据、单源与合并行数、交易所/市场/币种映射、每日覆盖量，以及首页四个指标能否从明细独立反算。任一关键检查失败都会返回非零退出码，GitHub Actions 不会覆盖线上最后一个有效快照。报告写入 `reports/phase0/latest-release-qa.json`。

## Phase 1 本地总览页

```bash
cd web
npm install
npm run dev
```

本地访问 `http://localhost:3000/`。没有 D1 数据时页面回退到仓库内的已验证快照；Cloudflare 生产环境会读取所有者账号中 D1 `flow` 的最新成功快照。

只读接口包括 `/api/v1/overview`、`/api/v1/data-status`、`/api/v1/etf/flows`、`/api/v1/connect/flows`、`/api/v1/margin/flows` 和 `/api/v1/history/dates`。页面支持使用 `/?date=YYYY-MM-DD` 查看归档交易日。`POST /api/v1/admin/import` 和 `POST /api/v1/admin/history-import` 仅接受 Bearer 写入密钥认证、通过结构校验及 SHA-256 复验的发布包。

## 每日自动采集

数据管线按职责拆分在 `.github/workflows/`：

- `market-data-watchdog.yml`：GitHub 的轻量兜底定时器，只检查 `pipeline_window`，确认窗口缺失、租约过期或可重试失败后才补发任务；正常情况下不 checkout、不安装依赖、不运行测试；
- `market-data-collect.yml`：仅接受 `workflow_dispatch`，负责一次来源采集、R2 证据归档和 readiness 更新，并把已确认的来源工件交给独立发布任务；
- `market-data-publish.yml`：下载采集工件，离线构建/校验候选 read model，并执行 candidate canary 与单调激活；
- `pcf-v2-capture.yml`：独立按官方精确日期捕获 PCF v2，并以 D1 checkpoint 做有界恢复和影子门禁摘要，不触发核心市场采集或发布；
- `ci.yml`：PR/main 的完整 Python、Web build/test/lint 与 workflow contract 检查；
- `daily-data.yml`：保留旧 Actions 手动入口的兼容 wrapper，只转发到 `market-data-collect.yml`，不再承载重任务或 schedule。

Cloudflare Cron 是主触发器；GitHub 只运行 watchdog。`concurrency` 仅用于串行化，跨调度器去重由 `pipeline_window` 的原子 claim/status 负责。

旧版完整管线的运行参数仍可在兼容入口中手动传入。以下窗口说明描述业务目标，不代表 GitHub 重任务 schedule：

- Cloudflare Cron 主触发按来源发布时间分三个窗口运行：北京时间 18:30（上交所/港交所）、次日 07:45（深交所晨间数据）和 08:45（PCF 归档与完整日期核验）；GitHub Actions 在 18:45、08:00、08:55 作为错峰故障转移；同一交易日只有全部核心来源到齐才会发布；
- 如果某个官方源临时失败，已到达的原始数据仍会归档，但任务会以失败状态结束并保留错误摘要，后续 watchdog/Cloudflare 窗口自动重试，不会把半份数据伪装成成功快照；
- 也可在 Actions 页面手动执行，并可选传入三个 ISO 日期；
- 每次运行保留快照、采集清单和质量摘要 14 天，便于追溯；
- 完整单元/字段契约测试由 `ci.yml` 在 PR/main 上运行；定时采集只执行目标 source/parser/date smoke，数据 QA 仍在每次采集窗口执行；
- 每个原始工件独立提交到 Worker，先写入 R2 不可变对象和状态检查点，最后一次请求才校验完整工件集合并建立历史快照；因此单个交易所延迟不会让整包请求超时或覆盖半成品；
- ETF→个股穿透的 PCF 规范化 CSV 只在传输层 gzip 压缩，Worker 校验哈希后以普通 CSV 归档；PCF 可同日对齐，或按官方 `PRE_TRADING_DAY` 对齐到次日披露的前一交易日，避免宽成分篮子触碰 12 MB 请求上限；
- 深交所 ETF 用列表页发布日和“当前规模”列标注日校验前一交易日；港交所使用简体中文历史每日页的中文数据文件；
- 发布需要私有仓库 Actions Secrets：`FLOW_INGEST_API_KEY`、`FLOW_CF_ACCESS_CLIENT_ID` 和 `FLOW_CF_ACCESS_CLIENT_SECRET`。后两项只用于通过 Cloudflare Access 的 Service Auth 策略。

## Cloudflare 资源归属

- Worker：`flow`
- D1：`flow`（APAC）
- R2：`flow`（Standard，`dashboard/` 保留 365 天；`market-data/` 不自动过期；`market-data/raw/` 使用无限期 Bucket Lock）
- D1：ETF 规范化日表滚动保留 365 天，体积较大的最新页面派生快照保留 30 天；完整历史快照正文放在 R2，D1 只保存日期索引
- ETF 长周期：R2 保存最近 260 个交易日的紧凑日变化状态，按内容哈希发布；页面周期值为每日官方份额变化累计，不把派生数据冒充原始数据
- 部署配置：`web/wrangler.jsonc`

上述资源均位于项目所有者自己的 Cloudflare 账号，不依赖 Sites 控制面。

生产域名 `flow.xiaoker.com` 由 Cloudflare Access 默认拒绝，仅允许项目所有者邮箱登录；GitHub Actions 使用独立 Service Token 访问写入路由。`workers.dev` 与 Worker Preview URL 均已在部署配置中关闭，管理写入接口仍叠加独立 Bearer 密钥，不允许仅凭 Access 身份写入数据。

本地生成同结构快照：

```bash
.venv/bin/python scripts/select_recent_trading_dates.py --count 3
.venv/bin/python scripts/build_dashboard_snapshot.py --output dashboard-snapshot.json
```

从远端历史快照重新生成 ETF 长周期状态（只读取历史对象，不修改原始文件）：

```bash
.venv/bin/python scripts/rebuild_remote_etf_flow_state.py \
  --output /tmp/flow-etf-flow-state-v3.json
```

人工复核摘要后增加 `--publish`，会写入新的内容哈希对象和 `latest.json` 指针。

## 文档

- [产品与技术架构](docs/trading-desk-plan.md)
- [测试与验收计划](docs/trading-desk-test-plan.md)
- [历史回补、覆盖边界与按日查看](docs/history-backfill.md)
- [字段字典](schemas/field-dictionary.csv)
- [Phase 0 可交付报告](reports/phase0/phase0-report.html)
- [Phase 0 可复现 Notebook](notebooks/phase0_data_quality.ipynb)

## 当前限制

深交所 ETF 不提供可任意按历史日下载的份额序列；系统依据列表页规模日期从自动采集启用日起逐日积累，不伪造上线前的历史。港交所中文“历史每日数据”页面当前只暴露最近 7 个月文件，早期日期的官方 404 会作为缺失证据归档，不用第三方数字代填。ETF “份额变化”仍不等于精确资金净流入，页面会持续明示该口径。
