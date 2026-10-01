import assert from "node:assert/strict";
import test from "node:test";
import {
  CAPABILITY_CONTRACT_VERSION,
  CAPABILITY_IDS,
  ETF_PERIOD_IDS,
  hasCompleteReadModelManifest,
  hasReadyCapability,
  isCandidateOnlyReadModelManifest,
  isFullCapabilitySetReady,
  isReadModelManifest,
} from "../app/lib/read-model-contract.ts";

const asOf = "2026-08-06";
const releaseId = "release:2026-08-06:0123456789abcdef";
const dashboardArtifacts = ["bootstrap", "overview", "radar", "rotation", "national", "connect", "margin"];
const capabilityArtifacts = {
  etf_daily_core: ["bootstrap", "radar", "rotation"],
  etf_radar: ["radar"],
  rotation: ["rotation"],
  national_team: ["national"],
  connect: ["connect"],
  margin: ["margin"],
  etf_periods: ["radar", "rotation"],
  etf_penetration: ["radar_flow", "pressure"],
  stock_search: ["stock_search"],
  stock_detail: ["stock_0", "stock_1", "stock_2", "stock_3", "stock_4", "stock_5", "stock_6", "stock_7"],
};

function descriptors(names) {
  return Object.fromEntries(names.map((name) => [name, {
    key: `market-data/releases/${asOf}/${releaseId}/${name}.json`,
    sha256: "a".repeat(64),
    bytes: 10,
    contentType: "application/json; charset=utf-8",
  }]));
}

function refs(names, artifacts) {
  return names.map((name) => ({ name, key: artifacts[name].key, sha256: artifacts[name].sha256, bytes: artifacts[name].bytes }));
}

function periodMatrix(status = "UNKNOWN") {
  return Object.fromEntries(ETF_PERIOD_IDS.map((period) => [period, {
    shares: { status, sampleCount: 0, availableSampleCount: 0, missingSampleCount: 0, reasonCode: "period_not_proven" },
    estimatedAmount: { status, sampleCount: 0, availableSampleCount: 0, missingSampleCount: 0, reasonCode: "period_not_proven" },
  }]));
}

function localState(status, names, artifacts, overrides = {}) {
  return {
    status,
    asOf: status === "READY" ? asOf : null,
    sourceDates: {},
    artifactRefs: refs(names, artifacts),
    reasonCode: status === "READY" ? null : "source_not_proven",
    coverage: {},
    ...overrides,
  };
}

function dashboardCandidate() {
  const artifacts = descriptors(dashboardArtifacts);
  const capabilities = Object.fromEntries(CAPABILITY_IDS.map((id) => [id, {
    status: "UNKNOWN",
    asOf: null,
    sourceDates: {},
    artifactRefs: [],
    reasonCode: "separate_slot_not_bound",
    coverage: {},
  }]));
  capabilities.etf_daily_core = localState("READY", capabilityArtifacts.etf_daily_core, artifacts, {
    sourceDates: { snapshot: asOf, sseEtfAsOf: asOf, szseEtfAsOf: asOf },
    coverage: { instrumentCount: 2, tradingDays: 4 },
  });
  capabilities.etf_radar = localState("READY", capabilityArtifacts.etf_radar, artifacts, {
    sourceDates: { sseEtfAsOf: asOf, szseEtfAsOf: asOf },
    coverage: { sampleCount: 2 },
  });
  capabilities.rotation = localState("WAITING_SOURCES", capabilityArtifacts.rotation, artifacts);
  capabilities.national_team = localState("WAITING_SOURCES", capabilityArtifacts.national_team, artifacts);
  capabilities.connect = localState("SOURCE_UNAVAILABLE", capabilityArtifacts.connect, artifacts);
  capabilities.margin = localState("SOURCE_UNAVAILABLE", capabilityArtifacts.margin, artifacts);
  capabilities.etf_periods = localState("UNKNOWN", capabilityArtifacts.etf_periods, artifacts, {
    reasonCode: "etf_daily_core_not_ready",
    coverage: { periodCount: ETF_PERIOD_IDS.length, readyPeriodMetricCount: 0, expectedPeriodMetricCount: ETF_PERIOD_IDS.length * 2 },
    periods: periodMatrix(),
  });
  return {
    schemaVersion: 4,
    slot: "dashboard",
    releaseId,
    asOf,
    observedAt: "2026-08-06T01:00:00.000Z",
    sourceDates: { snapshot: asOf, sseEtfAsOf: asOf, szseEtfAsOf: asOf },
    pressureSchemaVersion: 2,
    stockShardCount: 8,
    requiredArtifacts: dashboardArtifacts,
    artifacts,
    capabilityContract: { version: CAPABILITY_CONTRACT_VERSION, scope: "dashboard", capabilities },
  };
}

test("accepts v4 core candidate while leaving unbound PCF capabilities unknown", () => {
  const manifest = dashboardCandidate();
  assert.equal(isReadModelManifest(manifest), true);
  assert.equal(hasCompleteReadModelManifest(manifest), true);
  assert.equal(isCandidateOnlyReadModelManifest(manifest), true);
  assert.equal(hasReadyCapability(manifest, "etf_daily_core"), true);
  assert.equal(hasReadyCapability(manifest, "etf_penetration"), false);
  assert.equal(hasReadyCapability(manifest, "stock_search"), false);
  assert.equal(isFullCapabilitySetReady(manifest), false);
});

test("rejects a declared READY capability with a damaged artifact reference", () => {
  const manifest = dashboardCandidate();
  manifest.capabilityContract.capabilities.etf_daily_core.artifactRefs[0].sha256 = "b".repeat(64);
  assert.equal(isReadModelManifest(manifest), false);
});

test("rejects a split-slot manifest that claims an unbound capability is READY", () => {
  const manifest = dashboardCandidate();
  manifest.capabilityContract.capabilities.etf_penetration = {
    status: "READY",
    asOf,
    sourceDates: { pcfEffectiveAsOf: asOf, pcfAsOf: asOf },
    artifactRefs: [],
    reasonCode: null,
    coverage: { stockCount: 12, matchedEtfCount: 4 },
  };
  assert.equal(isReadModelManifest(manifest), false);
});

test("rejects period status/count disagreement and keeps schema-v3 compatibility", () => {
  const manifest = dashboardCandidate();
  manifest.capabilityContract.capabilities.etf_periods.coverage.readyPeriodMetricCount = 1;
  assert.equal(isReadModelManifest(manifest), false);

  const schema3 = {
    schemaVersion: 3,
    slot: "dashboard",
    releaseId,
    asOf,
    observedAt: "2026-08-06T01:00:00.000Z",
    sourceDates: { snapshot: asOf },
    pressureSchemaVersion: 2,
    stockShardCount: 8,
    requiredArtifacts: dashboardArtifacts,
    artifacts: descriptors(dashboardArtifacts),
  };
  assert.equal(isReadModelManifest(schema3), true);
});
