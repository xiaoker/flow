/** Versioned archive transport. Never substitutes the wire hash for source identity. */
export const RAW_WIRE_CONTRACT = "flow-raw-gzip-wire-v1";
const MAX_WIRE_BYTES = 2_000_000;
const MAX_SOURCE_BYTES = 4_000_000;

async function digest(bytes: Uint8Array): Promise<string> {
  const hash = await crypto.subtle.digest("SHA-256", Uint8Array.from(bytes));
  return Array.from(new Uint8Array(hash), b => b.toString(16).padStart(2, "0")).join("");
}

export async function encodeRawWire(source: Uint8Array): Promise<Record<string, unknown>> {
  if (!source.length || source.length > MAX_SOURCE_BYTES) throw new Error("raw_wire_source_size_invalid");
  const stream = new Blob([Uint8Array.from(source)]).stream().pipeThrough(new CompressionStream("gzip"));
  const wire = new Uint8Array(await new Response(stream).arrayBuffer());
  if (wire.length > MAX_WIRE_BYTES) throw new Error("raw_wire_size_exceeded");
  let binary = "";
  for (let offset = 0; offset < wire.length; offset += 8192) {
    binary += String.fromCharCode(...wire.subarray(offset, offset + 8192));
  }
  return {contract: RAW_WIRE_CONTRACT, encoding: "gzip", sourceBytes: source.length,
    sourceSha256: await digest(source), wireSha256: await digest(wire), contentBase64: btoa(binary)};
}

export async function decodeRawWire(value: unknown): Promise<Uint8Array> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("raw_wire_contract_invalid");
  const v = value as Record<string, unknown>;
  const keys = ["contract", "encoding", "sourceBytes", "sourceSha256", "wireSha256", "contentBase64"];
  if (Object.keys(v).length !== keys.length || keys.some(k => !(k in v))
      || v.contract !== RAW_WIRE_CONTRACT || v.encoding !== "gzip"
      || typeof v.sourceBytes !== "number" || !Number.isSafeInteger(v.sourceBytes)
      || v.sourceBytes < 1 || v.sourceBytes > MAX_SOURCE_BYTES
      || typeof v.sourceSha256 !== "string" || !/^[a-f0-9]{64}$/.test(v.sourceSha256)
      || typeof v.wireSha256 !== "string" || !/^[a-f0-9]{64}$/.test(v.wireSha256)) {
    throw new Error("raw_wire_contract_invalid");
  }
  const encoded = v.contentBase64;
  if (typeof encoded !== "string" || encoded.length > Math.ceil(MAX_WIRE_BYTES / 3) * 4
      || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(encoded)) {
    throw new Error("raw_wire_size_or_encoding_invalid");
  }
  const wire = Uint8Array.from(atob(encoded), c => c.charCodeAt(0));
  if (wire.length > MAX_WIRE_BYTES || await digest(wire) !== v.wireSha256) throw new Error("raw_wire_hash_mismatch");
  const reader = new Blob([wire]).stream().pipeThrough(new DecompressionStream("gzip")).getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    for (;;) {
      const next = await reader.read();
      if (next.done) break;
      size += next.value.byteLength;
      if (size > v.sourceBytes) throw new Error("raw_wire_expansion_exceeded");
      chunks.push(next.value);
    }
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
  const source = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { source.set(chunk, offset); offset += chunk.length; }
  if (size !== v.sourceBytes || await digest(source) !== v.sourceSha256) throw new Error("raw_wire_source_mismatch");
  return source;
}
