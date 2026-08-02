# ETF 雷达详情页 Design QA

## Comparison target

- Source visual truth: `/private/tmp/flow-etf-radar-audit/02-etf-detail.png`
- Updated implementation: `/private/tmp/flow-etf-radar-audit/03-etf-detail-updated.jpg`
- Viewport: 1280 × 720 CSS pixels; same desktop route and selected ETF state
- State: ETF 雷达 → 科创半导（588170）详情抽屉；本地 2026-07-14 snapshot
- Density normalization: browser screenshots were captured at the same viewport; device density was not exposed by the in-app browser

## Full-view comparison

The original drawer gave the largest visual region to an empty “官方总份额走势” panel and showed every unavailable period as a full-height row. The updated drawer keeps the current 1-day change prominent, adds a three-column evidence strip, turns the chart into an honest coverage state when fewer than two nodes exist, and collapses unavailable periods into one expandable group.

## Focused region comparison

The detail drawer was the focused region because the requested changes affect evidence hierarchy, chart empty state, period availability, and source labeling. The updated screenshot confirms that the evidence strip fits within the drawer, the empty state is vertically centered, and the existing close control remains visible.

## Findings and comparison history

1. P1 — Chart title overstated the available data. Fixed by changing the label to “份额锚点走势”, requiring at least two nodes before rendering a line chart, and explaining that the nodes are recalculated rather than raw daily snapshots.
2. P1 — The drawer lacked local data credentials. Fixed by adding snapshot date, exchange/source type, and current total shares.
3. P1 — Eight unavailable periods consumed the same visual weight as available data. Fixed by showing available periods first and placing unavailable periods under “历史周期积累中”.
4. P2 — The hero value could be mistaken for cash flow. Fixed by labeling it “官方份额差” and adding “不等同现金净流入”.

## Verification

- Primary interactions tested: open ETF Radar, open ETF detail, inspect dialog content and close affordance.
- Build and tests: `npm test --prefix web` — 22 passed.
- Lint: `npm run lint --prefix web` — passed.
- `git diff --check` — passed.
- Mobile behavior was not captured at a true narrow viewport in the in-app browser; the existing responsive CSS was preserved and the 680px rules were reviewed separately.

final result: passed
