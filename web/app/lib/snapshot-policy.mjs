export function isPublishableSnapshot(value) {
  const publication = value && typeof value === "object" ? value.publication : null;
  return Boolean(
    value &&
      typeof value === "object" &&
      value.gate !== "blocked" &&
      value.quality &&
      Number.isInteger(value.quality.fail) &&
      value.quality.fail === 0 &&
      (!publication || publication.status === "confirmed"),
  );
}
