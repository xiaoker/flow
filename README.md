# 个人资金流向交易台

当前 Phase 0 已完成本地实数验证，结论为 `conditional_pass`：可进入 Phase 1 骨架开发，但深交所 ETF 只能从定时采集启用日起向前积累快照，Cloudflare R2/D1 写入链路也尚待在配置凭据后实测。

私有托管版：[flow-desk.xiaoaoker.chatgpt.site](https://flow-desk.xiaoaoker.chatgpt.site)

## 本地运行

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/trading-desk collect-phase0 \
  --dates 2026-07-10 2026-07-13 2026-07-14 \
  --szse-etf-inferred-date 2026-07-15
.venv/bin/python -m pytest
.venv/bin/python scripts/build_phase0_notebook.py
```

原始响应和规范化 CSV 保存在 `data/`，默认不提交 Git。可审计的质量摘要、Notebook 和报告保存在 `reports/`、`notebooks/`。

## Phase 1 本地总览页

```bash
cd web
npm install
npm run dev
```

本地访问 `http://localhost:3000/`。页面使用 Phase 0 已验证的真实快照，并提供 `/api/v1/overview`、`/api/v1/data-status`、`/api/v1/etf/flows`、`/api/v1/connect/flows` 和 `/api/v1/margin/flows` 只读接口。D1 表结构和迁移已生成，但云端资源尚未创建。

## 文档

- [产品与技术架构](docs/trading-desk-plan.md)
- [测试与验收计划](docs/trading-desk-test-plan.md)
- [字段字典](schemas/field-dictionary.csv)
- [Phase 0 可交付报告](reports/phase0/phase0-report.html)
- [Phase 0 可复现 Notebook](notebooks/phase0_data_quality.ipynb)

## 当前限制

深交所 ETF 官方下载只提供无统计日期的当前快照，不能直接进行官方历史回填。系统只能从定时采集启用日起每天留存，并明确标记推断日期。本轮未配置 GitHub/Cloudflare 资源和密钥，因此报告不宣称 R2/D1 云端写入已验证。
