# 流向台 Web

个人资金流向交易台的 Cloudflare Workers 全栈应用。Worker、D1 和 R2 均部署在项目所有者自己的 Cloudflare 账号中。

## Prerequisites

- Node.js `>=22.13.0`

## Quick Start

```bash
npm install
npm run dev
npm run build
```

本地开发使用 Wrangler 的本地 D1/R2 存储，不会默认写入远程资源。

## Cloudflare 资源

- Worker：`flow`
- D1：`flow`，代码绑定名 `DB`
- R2：`flow`，代码绑定名 `RAW_BUCKET`
- 配置文件：`wrangler.jsonc`
- D1 迁移目录：`drizzle/`

## 部署

```bash
npm run db:migrate:remote
npx wrangler secret put INGEST_API_KEY
npm run deploy
```

### GitHub Actions 自动部署

仓库中的 `Deploy Flow Worker` workflow 会在 `main` 分支的 `web/` 代码变更合并后自动运行，也可以在 GitHub Actions 页面手动运行。它会依次执行生产构建、远端 D1 迁移和 `npm run deploy`；构建或凭证检查失败时不会部署 Worker。

在仓库 `Settings → Secrets and variables → Actions` 中配置以下 Secrets：

- `CLOUDFLARE_ACCOUNT_ID`：当前 Cloudflare 账号 ID；
- `CLOUDFLARE_API_TOKEN`：仅授予该账号 Worker 部署和 D1 迁移所需权限的 API Token。

Token 只保存在 GitHub Secrets，不要写入仓库文件、日志或前端代码。数据采集由 `market-data-collect.yml` 负责，与代码部署相互独立；旧的 `daily-data.yml` 只保留手动兼容转发。

生产入口为 `https://flow.xiaoker.com`，页面和只读 API 由所有者账号中的 Cloudflare Access 管理，仅允许项目所有者邮箱登录。GitHub Actions 通过独立 Service Auth 策略访问写入路由；写入接口仍要求独立的 `INGEST_API_KEY`。`workers.dev` 与 Worker Preview URL 均已关闭。

### Market data pipeline stages

`market-data-watchdog.yml` 是唯一的 GitHub schedule，只做
`pipeline_window` 状态检查和条件补发；它不会 checkout、安装依赖或运行
pytest。`market-data-collect.yml` 是 dispatch-only 的来源采集和证据归档阶段，
完成后上传短期 immutable source artifact 并 dispatch
`market-data-publish.yml`。发布阶段独立下载该 artifact，离线构建候选 read
models、执行 canary，再更新发布状态。`pcf-v2-capture.yml` 独立按官方精确日期捕获
PCF，并用 D1 checkpoint 做有界恢复和影子摘要；PCF 失败不会阻塞核心 market slot。旧的
`daily-data.yml` 仅是手动兼容 wrapper。

完整 Python 与 Web build/test/lint 在 `ci.yml` 的 PR/main 检查中运行；定时采集
只运行目标 smoke 和数据质量门禁。`PIPELINE_WINDOW_GATE_ENABLED` 仍须在
Cloudflare Worker 与 GitHub repository variable 同时显式开启，PR2 不会自动
切换生产开关。

### Daily pipeline window gate

`pipeline_window` 是 Cloudflare Cron 与 GitHub schedule 之间的幂等边界。PR1
提供受保护的 `/api/v1/admin/pipeline-window/claim` 和 `/status` 接口；claim
只做单行条件写，不扫描历史市场或 ETF 数据。`PIPELINE_WINDOW_GATE_ENABLED`
默认是 `0`，因此部署新代码不会改变生产调度行为。启用时必须同时设置：

- Cloudflare Worker 变量 `PIPELINE_WINDOW_GATE_ENABLED=1`；
- GitHub Actions repository variable `PIPELINE_WINDOW_GATE_ENABLED=1`。

两者必须同时启用并先完成 canary；任一 claim/status 异常都会 fail closed，避免
在不确定时启动重采集。`concurrency` 仍只负责串行化，不能替代 `pipeline_window`
的跨调度器去重。

## Useful Commands

- `npm run dev`: start local development
- `npm run build`: verify the vinext build output
- `npm test`: build and verify dashboard/API rendering
- `npm run deploy`: deploy to the owner-managed Cloudflare Worker
- `npm run db:generate`: generate Drizzle migrations after schema changes
- `npm run db:migrate:remote`: apply pending migrations to the owner-managed D1

## Learn More

- [vinext Documentation](https://github.com/cloudflare/vinext)
- [Drizzle D1 Guide](https://orm.drizzle.team/docs/get-started/d1-new)
