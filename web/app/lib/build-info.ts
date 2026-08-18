function safeBuildValue(value: unknown, fallback: string): string {
  return typeof value === "string" && value.trim().length > 0 ? value : fallback;
}

// Vite replaces these constants during the deploy build. The local fallback
// keeps unit tests and local previews explicit instead of pretending to be a
// production commit.
export const FLOW_BUILD_SHA = safeBuildValue(
  typeof __FLOW_BUILD_SHA__ === "undefined" ? undefined : __FLOW_BUILD_SHA__,
  "local",
);
export const FLOW_DEPLOYED_AT = safeBuildValue(
  typeof __FLOW_DEPLOYED_AT__ === "undefined" ? undefined : __FLOW_DEPLOYED_AT__,
  "local",
);

export function flowBuildHeaders(): Record<string, string> {
  return {
    "X-Flow-Build-Sha": FLOW_BUILD_SHA,
    "X-Flow-Deployed-At": FLOW_DEPLOYED_AT,
  };
}

export function flowBuildInfo(): { buildSha: string; deployedAt: string } {
  return { buildSha: FLOW_BUILD_SHA, deployedAt: FLOW_DEPLOYED_AT };
}
