import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("national-team chart exposes a change-focused view without replacing official shares", async () => {
  const [source, css] = await Promise.all([
    readFile(new URL("../app/national-team-view.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
  ]);
  assert.match(source, /useState<NationalChartMode>\("relative"\)/);
  assert.match(source, /变化%/);
  assert.match(source, /绝对份额/);
  assert.match(source, /relative: chartBase/);
  assert.match(source, /官方总份额：/);
  assert.match(source, /最大变化/);
  assert.match(source, /markLine: chartMode === "relative"/);
  assert.match(css, /\.national-chart-mode\s*\{/);
  assert.match(css, /\.national-chart-callout\s*\{/);
});

test("national-team periods come from the matched ETF rows, not terminal metadata", async () => {
  const [nationalSource, dashboardSource] = await Promise.all([
    readFile(new URL("../app/national-team-view.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/dashboard.tsx", import.meta.url), "utf8"),
  ]);
  assert.match(nationalSource, /export function getNationalAvailablePeriods/);
  assert.match(nationalSource, /codes\.has\(item\.code\) && typeof item\.periods\[period\] === "number"/);
  assert.match(nationalSource, /const periodValues = matchedRows/);
  assert.match(nationalSource, /const periodFlow = periodValues\.length/);
  assert.match(dashboardSource, /const nationalAvailablePeriods = useMemo/);
  assert.match(dashboardSource, /view === "national"\s*\?\s*nationalAvailablePeriods/);
  assert.match(dashboardSource, /activeAvailablePeriods\.has\(next\)/);
});
