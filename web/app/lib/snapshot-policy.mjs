export function isPublishableSnapshot(value) {
  return Boolean(
    value &&
      typeof value === "object" &&
      value.gate !== "blocked" &&
      value.quality &&
      Number.isInteger(value.quality.fail) &&
      value.quality.fail === 0,
  );
}
