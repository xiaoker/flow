# Flow v0.2 Design QA

Reference inputs:

- `docs/audits/2026-07-16-flow-reverse-engineering/01-sector-rotation.png`
- `docs/audits/2026-07-16-flow-reverse-engineering/04-etf-radar-heatmap.png`

Implementation captures:

- `docs/audits/2026-07-16-flow-reverse-engineering/08-v02-overview.png`
- `docs/audits/2026-07-16-flow-reverse-engineering/09-v02-radar.png`
- `docs/audits/2026-07-16-flow-reverse-engineering/10-v02-mobile.png`

## Findings

- P0: none.
- P1 data fidelity: periods without enough official snapshots must not look populated. Resolved by disabling unavailable periods, rendering explicit coverage states, and keeping amount estimation disabled until NAV data exists.
- P1 responsive layout: dense tables and controls must not overflow the viewport. Verified at 1840px desktop and 390px mobile; page-level horizontal overflow is zero and the radar table scrolls inside its own container.
- P1 interaction coverage: navigation, period selection, direction filters, search, sorting, pagination, refresh state, and ETF drill-down are implemented. Search and drill-down were browser-tested.
- P2 visual fidelity: matched the reference's near-black canvas, blue-gray panel hierarchy, cyan active states, Chinese-market red/green semantics, compact controls, monospace numbers, and high-density table treatment.
- P2 content fidelity: the product uses official share-change and disclosure terminology. Northbound turnover is not mislabeled as net flow; name-based ETF classification is disclosed.
- Accessibility: semantic controls and table markup are present, keyboard focus is visible, reduced motion is honored, and the mobile navigation retains practical tap targets.
- Browser console: no warnings or errors during the verified overview, radar, search, and drawer states.

final result: passed
