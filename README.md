# FLOW 个人市场数据交易台

Phase 1 动态版已经上线。系统从交易所官方来源采集 ETF、互联互通和融资融券数据，区分“官方事实”“公式计算”和“日期推断”后生成统一快照，经受保护的写入接口保存到 Cloudflare D1/R2，并由总览页和只读 API 展示最新成功快照。

2026-07-16 已完成所有者 Cloudflare 资源的云端端到端验证：快照日期为 `2026-07-15`，基础质量门禁为 `conditional_pass`，46 项通过、2 项已知口径警告、0 项失败；发布级 Data QA 93 项全部通过。流水线、写入接口和读取端均会拒绝质量失败的快照覆盖上一个有效版本。

Cloudflare 自有资源版：[flow.xiaoker.com](https://flow.xiaoker.com)（Cloudflare Access 私有入口，仅项目所有者可登录）

## 本地运行

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/trading-desk collect-phase0 \
  --dates 2026-07-10 2026-07-13 2026-07-14 \
  --szse-etf-inferred-date 2026-07-15
.venv/bin/python -m pytest
.venv/bin/python scripts/build_dashboard_snapshot.py --output dashboard-snapshot.json
.venv/bin/trading-desk qa-release
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

只读接口包括 `/api/v1/overview`、`/api/v1/data-status`、`/api/v1/etf/flows`、`/api/v1/connect/flows` 和 `/api/v1/margin/flows`。`POST /api/v1/admin/import` 仅接受 Bearer 写入密钥认证、通过结构校验的快照，并同时写入 D1 和 R2。

## 每日自动采集

GitHub Actions 工作流位于 `.github/workflows/daily-data.yml`：

- 北京时间每周二至周六 00:10 运行，采集刚结束交易日及此前两个交易日；
- 也可在 Actions 页面手动执行，并可选传入三个 ISO 日期；
- 每次运行保留快照、采集清单和质量摘要 14 天，便于追溯；
- 每次发布前自动运行单元/字段契约测试和发布级 Data QA；
- 发布需要私有仓库 Actions Secrets：`FLOW_INGEST_API_KEY`、`FLOW_CF_ACCESS_CLIENT_ID` 和 `FLOW_CF_ACCESS_CLIENT_SECRET`。后两项只用于通过 Cloudflare Access 的 Service Auth 策略。

## Cloudflare 资源归属

- Worker：`flow`
- D1：`flow`（APAC）
- R2：`flow`（Standard，`dashboard/` 对象保留 365 天）
- 部署配置：`web/wrangler.jsonc`

上述资源均位于项目所有者自己的 Cloudflare 账号，不依赖 Sites 控制面。

生产域名 `flow.xiaoker.com` 由 Cloudflare Access 默认拒绝，仅允许项目所有者邮箱登录；GitHub Actions 使用独立 Service Token 访问写入路由。`workers.dev` 与 Worker Preview URL 均已在部署配置中关闭，管理写入接口仍叠加独立 Bearer 密钥，不允许仅凭 Access 身份写入数据。

本地生成同结构快照：

```bash
.venv/bin/python scripts/select_recent_trading_dates.py --count 3
.venv/bin/python scripts/build_dashboard_snapshot.py --output dashboard-snapshot.json
```

## 文档

- [产品与技术架构](docs/trading-desk-plan.md)
- [测试与验收计划](docs/trading-desk-test-plan.md)
- [字段字典](schemas/field-dictionary.csv)
- [Phase 0 可交付报告](reports/phase0/phase0-report.html)
- [Phase 0 可复现 Notebook](notebooks/phase0_data_quality.ipynb)

## 当前限制

深交所 ETF 官方下载只提供无统计日期的当前快照，不能直接进行官方历史回填。系统从定时采集启用日起每天留存，并明确标记推断日期，因此该数据域当前保持黄色警告，不会伪装成完整官方历史。
