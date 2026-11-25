import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { gzipSync } from "node:zlib";
import test from "node:test";
import { decodeRawWire, encodeRawWire, RAW_WIRE_CONTRACT } from "../app/lib/raw-wire.ts";

const hash = b => createHash("sha256").update(b).digest("hex");
function envelope(source) {
  const wire = gzipSync(source);
  return {contract: RAW_WIRE_CONTRACT, encoding: "gzip", sourceBytes: source.length,
    sourceSha256: hash(source), wireSha256: hash(wire), contentBase64: wire.toString("base64")};
}
test("large historical source retains exact bytes", async () => {
  const source = Buffer.alloc(2_049_642, 120);
  assert.deepEqual(Buffer.from(await decodeRawWire(envelope(source))), source);
  assert.deepEqual(Buffer.from(await decodeRawWire(await encodeRawWire(source))), source);
});
test("declared expansion fence stops decompression", async () => {
  const e = envelope(Buffer.alloc(2_049_642, 120));
  e.sourceBytes = 1;
  await assert.rejects(decodeRawWire(e), /raw_wire_expansion_exceeded/);
});
test("source and wire hash conflicts are rejected", async () => {
  for (const key of ["sourceSha256", "wireSha256"]) {
    const e = envelope(Buffer.from("official bytes")); e[key] = "0".repeat(64);
    await assert.rejects(decodeRawWire(e), /raw_wire_.*mismatch/);
  }
});
test("unknown contract and oversized declaration are rejected", async () => {
  for (const change of [{contract:"unknown"}, {sourceBytes:4_000_001}, {extra:true}]) {
    await assert.rejects(decodeRawWire({...envelope(Buffer.from("x")), ...change}), /raw_wire_contract_invalid/);
  }
});
