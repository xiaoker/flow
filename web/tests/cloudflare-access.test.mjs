import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import test from "node:test";
import { verifyCloudflareAccessRequest } from "../app/lib/cloudflare-access.ts";

const keyPair = await webcrypto.subtle.generateKey(
  { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: Uint8Array.of(1, 0, 1), hash: "SHA-256" },
  true,
  ["sign", "verify"],
);
const publicJwk = {
  ...await webcrypto.subtle.exportKey("jwk", keyPair.publicKey),
  kid: "test-access-key",
  use: "sig",
  alg: "RS256",
};

function accessFixture(label) {
  const issuer = `https://candidate-${label}-${process.pid}.cloudflareaccess.com`;
  const audience = "d3ffcc0ae6d15e7e43053d5499c90775b82ca47f5f684747fd77cc3447a3e67e";
  const config = { issuer, audience };
  const fetcher = async (input) => {
    assert.equal(String(input), `${issuer}/cdn-cgi/access/certs`);
    return Response.json({ keys: [publicJwk] }, { headers: { "Cache-Control": "public, max-age=300" } });
  };
  return { config, fetcher };
}

async function signAccessToken(config, claims = {}, signingKey = keyPair.privateKey) {
  const header = Buffer.from(JSON.stringify({ alg: "RS256", kid: "test-access-key", typ: "JWT" })).toString("base64url");
  const payload = Buffer.from(JSON.stringify({
    iss: config.issuer,
    aud: config.audience,
    iat: Math.floor(Date.now() / 1000),
    exp: Math.floor(Date.now() / 1000) + 3600,
    ...claims,
  })).toString("base64url");
  const input = `${header}.${payload}`;
  const signature = await webcrypto.subtle.sign(
    { name: "RSASSA-PKCS1-v1_5" },
    signingKey,
    new TextEncoder().encode(input),
  );
  return `${input}.${Buffer.from(signature).toString("base64url")}`;
}

function accessRequest(token) {
  return new Request("https://flow.xiaoker.com/api/v1/admin/candidate-release", {
    headers: token ? { "Cf-Access-Jwt-Assertion": token } : {},
  });
}

test("accepts only a valid, signed Access JWT for the configured issuer and application audience", async () => {
  const { config, fetcher } = accessFixture("valid");
  const token = await signAccessToken(config);
  assert.deepEqual(
    await verifyCloudflareAccessRequest(accessRequest(token), config, { fetcher }),
    { authorized: true },
  );
});

test("rejects missing tokens and configuration that could widen trust", async () => {
  const { config, fetcher } = accessFixture("missing");
  assert.deepEqual(
    await verifyCloudflareAccessRequest(accessRequest(null), config, { fetcher }),
    { authorized: false, reason: "missing_token" },
  );
  assert.deepEqual(
    await verifyCloudflareAccessRequest(accessRequest("anything"), { issuer: "https://attacker.example", audience: config.audience }, { fetcher }),
    { authorized: false, reason: "misconfigured" },
  );
  assert.deepEqual(
    await verifyCloudflareAccessRequest(accessRequest("anything"), { issuer: config.issuer, audience: "*" }, { fetcher }),
    { authorized: false, reason: "misconfigured" },
  );
});

test("rejects wrong issuer, wrong audience, expired, and not-yet-valid claims before trusting the signature", async () => {
  const { config, fetcher } = accessFixture("claims");
  const cases = [
    { iss: "https://other.cloudflareaccess.com" },
    { aud: "ffffffffffffffffffffffffffffffff" },
    { exp: Math.floor(Date.now() / 1000) - 1 },
    { nbf: Math.floor(Date.now() / 1000) + 120 },
  ];
  for (const claims of cases) {
    const token = await signAccessToken(config, claims);
    assert.deepEqual(
      await verifyCloudflareAccessRequest(accessRequest(token), config, { fetcher }),
      { authorized: false, reason: "invalid_token" },
    );
  }
});

test("rejects a modified signature and refreshes JWKS once when the key id is unknown", async () => {
  const { config, fetcher } = accessFixture("signature");
  const token = await signAccessToken(config);
  const [header, payload, signature] = token.split(".");
  const changedSignatureBytes = Buffer.from(signature, "base64url");
  changedSignatureBytes[0] ^= 1;
  const changedSignature = `${header}.${payload}.${changedSignatureBytes.toString("base64url")}`;
  assert.deepEqual(
    await verifyCloudflareAccessRequest(accessRequest(changedSignature), config, { fetcher }),
    { authorized: false, reason: "invalid_token" },
  );

  const unknownFixture = accessFixture("unknown-kid");
  const unknownToken = await signAccessToken(unknownFixture.config);
  const [, encodedPayload, encodedSignature] = unknownToken.split(".");
  const unknownHeader = Buffer.from(JSON.stringify({ alg: "RS256", kid: "missing-key", typ: "JWT" })).toString("base64url");
  const unknownKidToken = `${unknownHeader}.${encodedPayload}.${encodedSignature}`;
  let fetchCount = 0;
  const countingFetcher = async (input) => {
    fetchCount += 1;
    return unknownFixture.fetcher(input);
  };
  assert.deepEqual(
    await verifyCloudflareAccessRequest(accessRequest(unknownKidToken), unknownFixture.config, { fetcher: countingFetcher }),
    { authorized: false, reason: "invalid_token" },
  );
  assert.equal(fetchCount, 2);
});

test("fails closed as temporarily unavailable if the Access signing-key service cannot be reached", async () => {
  const { config } = accessFixture("unavailable");
  const token = await signAccessToken(config);
  assert.deepEqual(
    await verifyCloudflareAccessRequest(accessRequest(token), config, { fetcher: async () => { throw new Error("network detail"); } }),
    { authorized: false, reason: "verifier_unavailable" },
  );
});
