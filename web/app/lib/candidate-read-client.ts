import type { ReadModelArtifactDescriptor, ReadModelManifest } from "./read-model-contract.ts";

export type CandidateIdentity = {
  releaseId: string;
  asOf: string;
  manifestSha256: string;
  slot: "dashboard" | "etf_penetration";
};

export type CandidateEnvelope = CandidateIdentity & {
  status: "STAGED";
  manifest: ReadModelManifest;
};

export class CandidateReadError extends Error {
  readonly code: string;

  constructor(code: string) {
    super(code);
    this.name = "CandidateReadError";
    this.code = code;
  }
}

/** A small request-generation gate used to discard late responses after refresh or selection changes. */
export class CandidateRequestGate {
  private generation = 0;
  private controller: AbortController | null = null;

  begin(): { signal: AbortSignal; isCurrent: () => boolean } {
    this.controller?.abort();
    const controller = new AbortController();
    this.controller = controller;
    const generation = ++this.generation;
    return { signal: controller.signal, isCurrent: () => generation === this.generation && !controller.signal.aborted };
  }

  cancel(): void {
    this.generation += 1;
    this.controller?.abort();
    this.controller = null;
  }
}

function identityKey(value: CandidateIdentity): string {
  return `${value.releaseId}|${value.asOf}|${value.manifestSha256}|${value.slot}`;
}

function requireIdentityHeaders(response: Response, expected: CandidateIdentity): void {
  if (
    response.headers.get("x-flow-release-id") !== expected.releaseId ||
    response.headers.get("x-flow-as-of") !== expected.asOf ||
    response.headers.get("x-flow-manifest-sha256") !== expected.manifestSha256 ||
    response.headers.get("x-flow-release-slot") !== expected.slot ||
    response.headers.get("x-flow-release-status") !== "STAGED"
  ) {
    throw new CandidateReadError("candidate_response_identity_mismatch");
  }
}

async function requireOk(response: Response): Promise<void> {
  if (response.ok) return;
  if (response.status === 401 || response.status === 403) throw new CandidateReadError("unauthorized");
  if (response.status === 404) throw new CandidateReadError("candidate_not_found_or_no_longer_staged");
  if (response.status === 409) throw new CandidateReadError("candidate_manifest_changed_refresh_required");
  throw new CandidateReadError(`candidate_read_http_${response.status}`);
}

export async function readCandidateManifest(
  fetcher: typeof fetch,
  releaseId: string,
  apiKey: string,
  signal: AbortSignal,
): Promise<CandidateEnvelope> {
  const response = await fetcher(`/api/v1/admin/candidate-release?releaseId=${encodeURIComponent(releaseId)}`, {
    method: "GET",
    cache: "no-store",
    credentials: "same-origin",
    redirect: "error",
    signal,
    headers: { Authorization: `Bearer ${apiKey}`, Accept: "application/json" },
  });
  await requireOk(response);
  const value: unknown = await response.json();
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new CandidateReadError("candidate_response_invalid");
  const candidate = value as Partial<CandidateEnvelope>;
  if (
    candidate.status !== "STAGED" || typeof candidate.releaseId !== "string" || candidate.releaseId !== releaseId ||
    typeof candidate.asOf !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(candidate.asOf) ||
    typeof candidate.manifestSha256 !== "string" || !/^[a-f0-9]{64}$/.test(candidate.manifestSha256) ||
    (candidate.slot !== "dashboard" && candidate.slot !== "etf_penetration") ||
    !candidate.manifest || candidate.manifest.releaseId !== candidate.releaseId || candidate.manifest.asOf !== candidate.asOf
  ) {
    throw new CandidateReadError("candidate_response_identity_mismatch");
  }
  return candidate as CandidateEnvelope;
}

export async function readCandidateArtifact<T>(
  fetcher: typeof fetch,
  identity: CandidateIdentity,
  artifact: string,
  descriptor: Pick<ReadModelArtifactDescriptor, "sha256" | "bytes">,
  apiKey: string,
  signal: AbortSignal,
): Promise<T> {
  if (!/^[a-f0-9]{64}$/.test(descriptor.sha256) || !Number.isSafeInteger(descriptor.bytes) || descriptor.bytes <= 0 || descriptor.bytes > 5_000_000) {
    throw new CandidateReadError("candidate_artifact_descriptor_invalid");
  }
  const params = new URLSearchParams({
    releaseId: identity.releaseId,
    manifestSha256: identity.manifestSha256,
    artifact,
  });
  const response = await fetcher(`/api/v1/admin/candidate-release?${params.toString()}`, {
    method: "GET",
    cache: "no-store",
    credentials: "same-origin",
    redirect: "error",
    signal,
    headers: { Authorization: `Bearer ${apiKey}`, Accept: "application/json" },
  });
  await requireOk(response);
  requireIdentityHeaders(response, identity);
  if (response.headers.get("x-flow-artifact-sha256") !== descriptor.sha256 ||
      response.headers.get("x-flow-artifact-bytes") !== String(descriptor.bytes)) {
    throw new CandidateReadError("candidate_artifact_descriptor_mismatch");
  }
  const bytes = await response.arrayBuffer();
  if (bytes.byteLength !== descriptor.bytes || bytes.byteLength > 5_000_000) {
    throw new CandidateReadError("candidate_artifact_size_mismatch");
  }
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  const actualSha256 = [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  if (actualSha256 !== descriptor.sha256) throw new CandidateReadError("candidate_artifact_sha256_mismatch");
  let payload: unknown;
  try {
    payload = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    throw new CandidateReadError("candidate_artifact_json_invalid");
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload) || (payload as { asOf?: unknown }).asOf !== identity.asOf) {
    throw new CandidateReadError("candidate_artifact_date_mismatch");
  }
  if (identityKey(identity) !== identityKey({
    releaseId: response.headers.get("x-flow-release-id") ?? "",
    asOf: response.headers.get("x-flow-as-of") ?? "",
    manifestSha256: response.headers.get("x-flow-manifest-sha256") ?? "",
    slot: (response.headers.get("x-flow-release-slot") ?? "") as CandidateIdentity["slot"],
  })) {
    throw new CandidateReadError("candidate_response_identity_mismatch");
  }
  return payload as T;
}

export function candidateErrorMessage(error: unknown): string {
  const code = error instanceof CandidateReadError ? error.code : error instanceof Error ? error.message : "unknown_error";
  const messages: Record<string, string> = {
    unauthorized: "后台密钥无效或已过期；密钥只保存在当前页面内存，不会写入浏览器存储。",
    candidate_not_found_or_no_longer_staged: "候选不存在或已不再是 STAGED 状态；不会回退读取生产版本。",
    candidate_manifest_changed_refresh_required: "候选清单版本已变化，请重新读取并核对 releaseId。",
    candidate_response_identity_mismatch: "响应的版本、日期或清单哈希与选中候选不一致，已丢弃。",
    candidate_artifact_date_mismatch: "候选工件日期与清单不一致，已拒绝显示。",
    candidate_artifact_descriptor_mismatch: "工件响应与候选清单声明的哈希或大小不一致，已拒绝显示。",
    candidate_artifact_size_mismatch: "候选工件实际大小与清单不一致或超过读取上限，已拒绝显示。",
    candidate_artifact_sha256_mismatch: "候选工件内容哈希与清单不一致，已拒绝显示。",
    candidate_artifact_json_invalid: "候选工件不是有效 UTF-8 JSON，已拒绝显示。",
    candidate_artifact_descriptor_invalid: "候选清单中的工件大小或哈希不合法，已拒绝读取。",
  };
  return messages[code] ?? `候选读取失败：${code}`;
}
