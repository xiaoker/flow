import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";
import { bindRollingCheckpoint } from "../app/lib/rolling-checkpoint-binding.ts";

const expected = { releaseId: "release:2026-10-30:1234567890abcdef",
  asOf: "2026-10-30", manifestSha256: "a".repeat(64) };
const bytes = new TextEncoder().encode("fixture");
const sha256 = createHash("sha256").update(bytes).digest("hex");

for (const race of [0, 1, 2, 3]) {
  test(`checkpoint binding checks active identity (race ${race})`, async () => {
    let reads = 0;
    const writes = [];
    const action = bindRollingCheckpoint({
      expected, bytes, sha256,
      readActiveDashboard: async () => ++reads === race ? null : expected,
      putImmutable: async (...args) => { writes.push(args); },
    });
    if (race) {
      await assert.rejects(action, /rolling_checkpoint_active_changed/);
      assert.equal(writes.length, race - 1);
    } else {
      const binding = await action;
      assert.equal(writes.length, 2);
      assert.deepEqual(JSON.parse(new TextDecoder().decode(writes[1][1])), binding);
      assert.ok(writes.every(([key]) => key.includes(expected.releaseId)));
    }
  });
}

test("invalid digest never reads or writes", async () => {
  await assert.rejects(bindRollingCheckpoint({ expected, bytes, sha256: "b".repeat(64),
    readActiveDashboard: async () => assert.fail("unexpected read"),
    putImmutable: async () => assert.fail("unexpected write"),
  }), /rolling_checkpoint_hash_mismatch/);
});
