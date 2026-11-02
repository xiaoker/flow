/**
 * Bounded discovery for legacy, unindexed history objects.
 *
 * The caller supplies at most the ISO and compact spelling of one date. We
 * intentionally do not follow R2 cursors: a truncated listing is incomplete
 * evidence and must not be used to choose a "latest" object.
 */
export const MAX_UNINDEXED_HISTORY_LIST_PREFIXES = 2;
export const MAX_UNINDEXED_HISTORY_OBJECTS_PER_PREFIX = 128;

export class HistoryArtifactCandidateLimitError extends Error {
  readonly code = "artifact_candidate_limit_exceeded";

  constructor() {
    super("artifact_candidate_limit_exceeded");
    this.name = "HistoryArtifactCandidateLimitError";
  }
}

export type HistoryArtifactListedObject = {
  key: string;
  uploaded: Date;
};

type BoundedListBucket = {
  list(options: { prefix: string; limit: number }): Promise<{
    objects?: HistoryArtifactListedObject[];
    truncated?: boolean;
  }>;
};

/** List no more than two bounded pages and fail closed on an incomplete page. */
export async function listUnindexedHistoryArtifactCandidates(
  bucket: BoundedListBucket,
  prefixes: string[],
  file: string,
): Promise<HistoryArtifactListedObject[]> {
  if (
    !Array.isArray(prefixes) ||
    prefixes.length < 1 ||
    prefixes.length > MAX_UNINDEXED_HISTORY_LIST_PREFIXES ||
    prefixes.some((prefix) => typeof prefix !== "string" || prefix.length > 200) ||
    typeof file !== "string" ||
    !/^[a-z0-9-]{1,24}$/.test(file)
  ) {
    throw new TypeError("invalid_history_artifact_list_scope");
  }

  const candidates = new Map<string, HistoryArtifactListedObject>();
  for (const prefix of prefixes) {
    const listed = await bucket.list({
      prefix,
      limit: MAX_UNINDEXED_HISTORY_OBJECTS_PER_PREFIX,
    });
    const objects = listed.objects;
    if (
      listed.truncated !== false ||
      !Array.isArray(objects) ||
      objects.length > MAX_UNINDEXED_HISTORY_OBJECTS_PER_PREFIX
    ) {
      throw new HistoryArtifactCandidateLimitError();
    }
    for (const object of objects) {
      if (
        object.key.endsWith(`/${file}.csv`) ||
        object.key.endsWith(`/${file}.csv.gz`)
      ) {
        candidates.set(object.key, object);
      }
    }
  }
  return [...candidates.values()];
}
