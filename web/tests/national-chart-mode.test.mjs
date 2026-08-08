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
