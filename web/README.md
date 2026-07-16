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

当前 `workers.dev` 读取入口可匿名访问；写入接口已要求独立的 `INGEST_API_KEY`。私有化收尾后，生产页面和只读 API 将由所有者账号中的 Cloudflare Access 管理；写入接口仍保留独立密钥，不允许仅凭 Access 身份写入数据。

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
