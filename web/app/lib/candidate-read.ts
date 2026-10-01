import {
  hasCompleteReadModelManifest,
  isCandidateOnlyReadModelManifest,
  isReadModelManifest,
  READ_MODEL_NAME,
  RELEASE_ID,
  SHA256,
  type ReadModelManifest,
} from "./read-model-contract.ts";

export const MAX_CANDIDATE_MANIFEST_BYTES = 256_000;

export type CandidateManifestRow = {
  release_id: string;
  as_of: string;
  manifest_sha256: string;
  schema_version: number;
  status: string;
  manifest_json: string;
};

function skipJsonString(value: string, start: number): number {
  if (value[start] !== '"') throw new Error("candidate_manifest_json_invalid");
  for (let index = start + 1; index < value.length; index += 1) {
    if (value[index] === "\\") {
      index += 1;
      continue;
    }
    if (value[index] === '"') return index + 1;
  }
  throw new Error("candidate_manifest_json_invalid");
}

function skipJsonValue(value: string, start: number): number {
  if (value[start] === '"') return skipJsonString(value, start);
  if (value[start] !== "{" && value[start] !== "[") {
    let index = start;
    while (index < value.length && value[index] !== "," && value[index] !== "}" && value[index] !== "]") index += 1;
    return index;
  }
  const closing: string[] = [value[start] === "{" ? "}" : "]"];
  for (let index = start + 1; index < value.length; index += 1) {
    if (value[index] === '"') {
      index = skipJsonString(value, index) - 1;
      continue;
    }
    if (value[index] === "{") closing.push("}");
    else if (value[index] === "[") closing.push("]");
    else if (value[index] === "}" || value[index] === "]") {
      if (closing.pop() !== value[index]) throw new Error("candidate_manifest_json_invalid");
      if (closing.length === 0) return index + 1;
    }
  }
  throw new Error("candidate_manifest_json_invalid");
}

function withoutRootMember(source: string, targetKey: string): string {
  const members: Array<{ key: string; start: number; end: number; commaAfter: number | null }> = [];
  const skipWhitespace = (index: number) => {
    while (index < source.length && /\s/.test(source[index])) index += 1;
    return index;
  };
  if (source[skipWhitespace(0)] !== "{") throw new Error("candidate_manifest_json_invalid");
  let index = skipWhitespace(0) + 1;
  while (true) {
    index = skipWhitespace(index);
    if (source[index] === "}") break;
    const start = index;
    const keyEnd = skipJsonString(source, index);
    let key: unknown;
    try {
      key = JSON.parse(source.slice(index, keyEnd));
    } catch {
      throw new Error("candidate_manifest_json_invalid");
    }
    index = skipWhitespace(keyEnd);
    if (source[index] !== ":") throw new Error("candidate_manifest_json_invalid");
    const valueStart = skipWhitespace(index + 1);
    const end = skipJsonValue(source, valueStart);
    const afterValue = skipWhitespace(end);
    const commaAfter = source[afterValue] === "," ? afterValue : null;
    members.push({ key: String(key), start, end, commaAfter });
    if (commaAfter === null) {
      index = afterValue;
      break;
    }
    index = commaAfter + 1;
  }
  const matches = members.filter((member) => member.key === targetKey);
  if (matches.length !== 1) throw new Error("candidate_manifest_hash_member_invalid");
  const member = matches[0];
  const memberIndex = members.indexOf(member);
  if (members.length === 1) return `${source.slice(0, member.start)}${source.slice(member.end)}`;
  if (member.commaAfter !== null) return `${source.slice(0, member.start)}${source.slice(member.commaAfter + 1)}`;
  const previous = members[memberIndex - 1];
  if (!previous || previous.commaAfter === null) throw new Error("candidate_manifest_json_invalid");
  return `${source.slice(0, previous.commaAfter)}${source.slice(member.end)}`;
}

export async function candidateManifestSha256(manifestJson: string): Promise<string> {
  // Hash the exact producer-canonical JSON bytes after removing only the
  // top-level self-hash field. Re-serializing a JS number would turn Python's
  // `1.0` into `1`, creating false hash failures for valid manifests.
  const unsigned = withoutRootMember(manifestJson, "manifestSha256");
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(unsigned));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function isValidDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const timestamp = Date.parse(`${value}T00:00:00.000Z`);
  return Number.isFinite(timestamp) && new Date(timestamp).toISOString().slice(0, 10) === value;
}

export async function parseStagedCandidate(row: CandidateManifestRow): Promise<ReadModelManifest> {
  const releaseDate = RELEASE_ID.exec(row.release_id)?.[0].split(":")[1];
  if (row.status !== "STAGED" || row.schema_version !== 4 || !RELEASE_ID.test(row.release_id) ||
      !isValidDate(row.as_of) || releaseDate !== row.as_of || !SHA256.test(row.manifest_sha256)) {
    throw new Error("candidate_manifest_identity_invalid");
  }
  if (new TextEncoder().encode(row.manifest_json).byteLength > MAX_CANDIDATE_MANIFEST_BYTES) {
    throw new Error("candidate_manifest_too_large");
  }
  let value: unknown;
  try {
    value = JSON.parse(row.manifest_json);
  } catch {
    throw new Error("candidate_manifest_json_invalid");
  }
  if (!isReadModelManifest(value) || !isCandidateOnlyReadModelManifest(value) || !hasCompleteReadModelManifest(value) ||
      value.releaseId !== row.release_id || value.asOf !== row.as_of ||
      value.manifestSha256 !== row.manifest_sha256) {
    throw new Error("candidate_manifest_contract_invalid");
  }
  if (await candidateManifestSha256(row.manifest_json) !== row.manifest_sha256) {
    throw new Error("candidate_manifest_sha256_mismatch");
  }
  return value;
}

export function isCandidateArtifactName(value: string): boolean {
  return READ_MODEL_NAME.test(value);
}

export function isValidCandidateRequest(releaseId: string, manifestSha256?: string): boolean {
  return RELEASE_ID.test(releaseId) && (manifestSha256 === undefined || SHA256.test(manifestSha256));
}
