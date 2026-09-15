export function isPublishableSnapshot(value) {
  const publication = value && typeof value === "object" ? value.publication : null;
  const publicationReady =
    !publication ||
    (publication.status === "confirmed" &&
      (publication.targetAsOf === undefined || publication.targetAsOf === value.asOf) &&
      (publication.targetComplete === undefined || publication.targetComplete === true) &&
      (publication.pendingDomains === undefined ||
        (Array.isArray(publication.pendingDomains) && publication.pendingDomains.length === 0)));
  return Boolean(
    value &&
      typeof value === "object" &&
      value.gate !== "blocked" &&
      value.quality &&
      Number.isInteger(value.quality.fail) &&
      value.quality.fail === 0 &&
      publicationReady,
  );
}
