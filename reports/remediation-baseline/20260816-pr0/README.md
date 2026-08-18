# PR0 baseline（2026-08-16）

本目录是 `docs/handoff/2026-08-16-master-data-integrity-remediation.md` 要求的只读基线。PR0 只保存证据和候选发布能力，不切换生产读取路径，不修改 Cloudflare 的 active pointer。

## 工作树

- 分支：`codex/deploy-worker-workflow-v2`
- HEAD：`e86158f (ci: deploy flow worker from main)`
- active release：`release:2026-08-13:ff4b307e291c4534`
- active asOf：`2026-08-13`
- active observedAt：`2026-08-14T03:17:42.000Z`
- active manifest SHA-256：`76d8e9a427ebea58c647341f917ea950f3eb4e9fc5af0b3b6343c862f7363e16`

原有未提交改动已在实施前核对，PR0 不触碰：

```text
 M .github/workflows/backfill-history.yml
 M .github/workflows/daily-data.yml
 M .github/workflows/data-qa.yml
 M .github/workflows/deploy-worker.yml
 M .gitignore
 M reports/phase0/latest-manifest.json
 M reports/phase0/latest-quality-summary.json
 M reports/phase0/latest-release-qa.json
 M tests/unit/test_workflow_contract.py
?? memory/
?? docs/handoff/2026-08-16-master-data-integrity-remediation.md
```

## 远端只读核对

通过 Wrangler 的只读 D1 查询确认：

- `active_release`、`release_manifest`、`historical_snapshot`、`market_history_daily` 均存在。
- `active_release.dashboard` 仍指向 `release:2026-08-13:ff4b307e291c4534`。
- `historical_snapshot`：488 行，覆盖 `2024-08-01` 至 `2026-08-13`。
- `market_history_daily`：488 行，覆盖 `2024-08-01` 至 `2026-08-13`。
- 当前 active release 的 `marginSeries` 只有 3 个点，`connectSeries` 只有 3 个点；这是 PR0 要留下的 P0 回归证据，不在 PR0 修复。
- 当前 `radar` ETF 终端自身显示 260 个交易日；压力历史和个股穿透历史是另一条读模型链路，PR0 不重建。

## R2 active release 快照

`active-release/manifest.json` 以及 manifest 声明的全部 18 个分片（实际清单为 19 个 artifact，含 bootstrap、18 个业务/分片对象）已只读下载到本目录。`artifact-checksums.json` 记录本地下载后的字节数和 SHA-256；未覆盖任何已有对象。

## 约束

后续 PR1 之前不得：

1. 修改生产读取路径、active pointer 或 `release-store` 的选择逻辑；
2. 运行迁移、补采、重建历史或正式发布；
3. 覆盖上述基线文件或用户已有未提交改动。
