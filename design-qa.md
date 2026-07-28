# Flow v0.3 Design QA

Reference inputs:

- `docs/audits/2026-07-16-flow-reverse-engineering/02-market-etf-trend.png`
- `docs/audits/2026-07-16-flow-reverse-engineering/04-etf-radar-heatmap.png`

Implementation captures:

- `docs/audits/2026-07-16-flow-reverse-engineering/11-v03-overview.png`
- `docs/audits/2026-07-16-flow-reverse-engineering/12-v03-radar.png`
- `docs/audits/2026-07-16-flow-reverse-engineering/13-v03-connect.png`
- `docs/audits/2026-07-16-flow-reverse-engineering/14-v03-margin.png`
- `docs/audits/2026-07-16-flow-reverse-engineering/15-v03-mobile.png`

## Findings

- P0: none.
- P1 product scope: ETF beta pressure and stock-holding penetration are absent from navigation, data contracts, and the active roadmap, matching the user's explicit exclusion.
- P1 data fidelity: the generated 2026-07-15 snapshot contains 1,886 official-list ETF instruments: 878 SSE and 1,008 SZSE. Only 877 instruments with comparable official snapshots receive period changes; current-only SZSE rows remain blank rather than being treated as zero.
- P1 classification: official investment type is used before name keywords. Broad-index tokens such as `科创50` are evaluated before technology-theme keywords; classification version and source are returned with the snapshot.
- P1 responsive layout: verified at 1840x1080 desktop and 390x844 mobile. Mobile document width equals viewport width (390px), and the dense radar keeps horizontal scrolling inside the table.
- P1 interaction coverage: ETF market/category/direction filters, native category selection, radar sorting, navigation, connect charts, and margin metric switching were browser-tested. Selecting `科技` reduced the fixture table from 10 to 4 rows, and the margin metric control changed to the active state.
- P2 visual fidelity: side-by-side comparison with the supplied ETF radar confirms the near-black canvas, blue-gray separators, cyan control states, Chinese-market red/green semantics, fixed first columns, heat cells, compact typography, and dense terminal hierarchy.
- P2 trend fidelity: connect and margin views now use the same terminal visual language and make the official disclosure boundary visible. Northbound turnover is not presented as net flow; financing balance change is not renamed to inflow.
- Accessibility: semantic buttons, labels, native select, table markup, visible keyboard focus, signed numeric direction, and reduced-motion rules are present.
- Browser console: no errors during overview, radar, connect, margin, filter, and responsive states.

final result: passed

---

# 国家队持仓页面 Design QA（2026-07-28）

Reference input:

- `/var/folders/zx/r3tlq5gj4c3bxzy8hjnf9m8h0000gn/T/codex-clipboard-17b91121-c0b3-41c1-8e3a-4ee36069d0e8.png`

Implementation capture:

- `docs/audits/2026-07-28-national-team/implementation.png`

## Findings

- P0: none.
- P1 data boundary: the 12 reference ETF codes are a fixed observation list, while values come only from the existing official SSE/SZSE snapshot. Missing codes and unavailable periods render as `—`; the page does not copy the screenshot's holding ratios, market values, report dates, or estimated changes.
- P1 disclosure boundary: formal holder disclosure is explicitly marked “待接入”. The two Central Huijin entity names are presented only as candidates to verify against fund periodic reports.
- P1 semantic accuracy: the page states that ETF share change is neither national-team trading nor cash net flow.
- P1 navigation: the new view is available from the primary navigation and directly at `/?view=national`; collapsed icon navigation now retains an accessible label and tooltip.
- P1 interactions: browser-tested direct entry, search (`510300` reduces the list to one row), table-row selection (detail changes to `沪深300ETF 易方达`), and unavailable-period protection.
- P2 visual fidelity: browser-rendered comparison confirms the reference's near-black terminal canvas, cyan navigation/section accents, red-green directional encoding, wide visual summary, dense table, and right-side detail hierarchy while preserving the existing FLOW shell.
- Responsive safeguards: the page switches the table/detail workspace to a single column below 1000px and keeps the dense table horizontally scrollable below 680px.
- Browser console: no errors on direct entry, filtering, selection, and reload.
- Local fixture note: the checked-in Phase 0 snapshot currently matches one of the 12 codes. The UI exposes that coverage rather than substituting values; the hosted D1 snapshot determines production coverage.

final result: passed
