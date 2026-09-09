import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { getEffectiveEtfSharePeriods } from "../app/lib/etf-period-availability.ts";

const declaredPeriods = ["1d", "3d", "1w", "2w"];

test("dashboard period tabs use the effective coverage gate", async () => {
  const source = await readFile(new URL("../app/dashboard.tsx", import.meta.url), "utf8");
  assert.match(source, /getEffectiveEtfSharePeriods\(terminal\)/);
  assert.match(source, /new Set\(getEffectiveEtfSharePeriods\(terminal\)\)/);
  assert.match(source, /缺少完整官方交易日快照，暂不可比/);
});

test("uses compact reconciliation evidence for the overview period gate", () => {
  const terminal = {
    availablePeriods: declaredPeriods,
    instruments: [],
    reconciliation: {
      "1d": { shares: { comparable: 903 } },
      "3d": { shares: { comparable: 900 } },
      "1w": { shares: { comparable: 0 } },
      "2w": { shares: { comparable: 0 } },
    },
  };

  assert.deepEqual(getEffectiveEtfSharePeriods(terminal), ["1d", "3d"]);
});

test("derives the Radar period gate from complete numeric instrument values", () => {
  const terminal = {
    availablePeriods: declaredPeriods,
    instruments: [
      { periods: { "1d": 1, "3d": 2, "1w": null, "2w": null } },
      { periods: { "1d": -1, "3d": 1, "1w": null, "2w": null } },
    ],
  };

  assert.deepEqual(getEffectiveEtfSharePeriods(terminal), ["1d", "3d"]);
});

test("prefers full-matrix evidence when compact metadata is also present", () => {
  const terminal = {
    availablePeriods: declaredPeriods,
    instruments: [{ periods: { "1d": 1, "3d": 2, "1w": 3, "2w": null } }],
    reconciliation: {
      "1d": { shares: { comparable: 1 } },
      "3d": { shares: { comparable: 1 } },
      "1w": { shares: { comparable: 0 } },
      "2w": { shares: { comparable: 0 } },
    },
  };

  assert.deepEqual(getEffectiveEtfSharePeriods(terminal), ["1d", "3d", "1w"]);
});

test("keeps legacy declared periods when no evidence is available", () => {
  assert.deepEqual(
    getEffectiveEtfSharePeriods({
      availablePeriods: declaredPeriods,
      instruments: [],
    }),
    declaredPeriods,
  );
});
