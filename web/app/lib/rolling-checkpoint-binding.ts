type ReleaseIdentity = { releaseId: string; asOf: string; manifestSha256: string };

const equal = (a: ReleaseIdentity | null, b: ReleaseIdentity) =>
  a?.releaseId === b.releaseId && a.asOf === b.asOf && a.manifestSha256 === b.manifestSha256;

export async function bindRollingCheckpoint(input: {
  expected: ReleaseIdentity;
  bytes: Uint8Array<ArrayBuffer>;
  sha256: string;
  readActiveDashboard: () => Promise<ReleaseIdentity | null>;
  putImmutable: (key: string, bytes: Uint8Array<ArrayBuffer>, metadata: Record<string, string>) => Promise<void>;
}) {
  const { expected, bytes, sha256, readActiveDashboard, putImmutable } = input;
  const { asOf, releaseId, manifestSha256 } = expected;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(asOf) ||
      !releaseId.startsWith(`release:${asOf}:`) || !/^release:[0-9-]+:[a-f0-9]{16}$/.test(releaseId) ||
      !/^[a-f0-9]{64}$/.test(manifestSha256) || !/^[a-f0-9]{64}$/.test(sha256) ||
      bytes.byteLength === 0 || bytes.byteLength > 2_000_000) {
    throw new Error("rolling_checkpoint_binding_invalid");
  }
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
  if (Array.from(digest, b => b.toString(16).padStart(2, "0")).join("") !== sha256) {
    throw new Error("rolling_checkpoint_hash_mismatch");
  }
  if (!equal(await readActiveDashboard(), expected)) throw new Error("rolling_checkpoint_active_changed");
  const prefix = `market-data/releases/${asOf}/${releaseId}/rolling-checkpoint/`;
  const metadata = { asOf, releaseId, sha256 };
  await putImmutable(`${prefix}${sha256}.checkpoint.gz`, bytes, metadata);
  if (!equal(await readActiveDashboard(), expected)) throw new Error("rolling_checkpoint_active_changed");
  const binding = { version: 1, ...expected, sha256, bytes: bytes.byteLength };
  await putImmutable(`${prefix}${manifestSha256}.json`, new TextEncoder().encode(JSON.stringify(binding)), metadata);
  if (!equal(await readActiveDashboard(), expected)) throw new Error("rolling_checkpoint_active_changed");
  return binding;
}
