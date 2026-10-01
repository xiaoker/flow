type AccessJwk = JsonWebKey & {
  kid?: string;
  use?: string;
  alg?: string;
  key_ops?: string[];
};

type AccessKeyCacheEntry = {
  keys: AccessJwk[];
  expiresAt: number;
};

export type CloudflareAccessConfig = {
  issuer?: string;
  audience?: string;
};

export type CloudflareAccessVerification =
  | { authorized: true }
  | { authorized: false; reason: "missing_token" | "invalid_token" | "misconfigured" | "verifier_unavailable" };

const ACCESS_JWKS_MAX_BYTES = 64 * 1024;
const ACCESS_JWKS_MAX_KEYS = 32;
const ACCESS_JWT_MAX_CHARS = 16 * 1024;
const ACCESS_JWKS_DEFAULT_TTL_MS = 5 * 60 * 1000;
const ACCESS_JWKS_MAX_TTL_MS = 60 * 60 * 1000;
const ACCESS_FETCH_TIMEOUT_MS = 5 * 1000;

const keyCache = new Map<string, AccessKeyCacheEntry>();
const keyFetches = new Map<string, Promise<AccessKeyCacheEntry>>();

class AccessVerifierUnavailable extends Error {}

function validIssuer(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === "https:" &&
      url.username === "" && url.password === "" && url.port === "" &&
      url.pathname === "/" && url.search === "" && url.hash === "" &&
      /^[a-z0-9-]+\.cloudflareaccess\.com$/.test(url.hostname) &&
      url.origin === value;
  } catch {
    return false;
  }
}

function decodeBase64Url(value: string): Uint8Array | null {
  if (!value || !/^[A-Za-z0-9_-]+$/.test(value)) return null;
  try {
    const base64 = value.replace(/-/g, "+").replace(/_/g, "/");
    const binary = atob(base64 + "=".repeat((4 - base64.length % 4) % 4));
    return Uint8Array.from(binary, (character) => character.charCodeAt(0));
  } catch {
    return null;
  }
}

function decodeJsonSegment(value: string): Record<string, unknown> | null {
  const bytes = decodeBase64Url(value);
  if (!bytes) return null;
  try {
    const parsed: unknown = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : null;
  } catch {
    return null;
  }
}

async function readBoundedText(response: Response, maximumBytes: number): Promise<string> {
  const declaredBytes = Number(response.headers.get("content-length"));
  if (Number.isFinite(declaredBytes) && declaredBytes > maximumBytes) {
    await response.body?.cancel();
    throw new AccessVerifierUnavailable("access_jwks_too_large");
  }
  if (!response.body) throw new AccessVerifierUnavailable("access_jwks_body_missing");

  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let totalBytes = 0;
  while (true) {
    const part = await reader.read();
    if (part.done) break;
    totalBytes += part.value.byteLength;
    if (totalBytes > maximumBytes) {
      await reader.cancel();
      throw new AccessVerifierUnavailable("access_jwks_too_large");
    }
    chunks.push(part.value);
  }

  const bytes = new Uint8Array(totalBytes);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw new AccessVerifierUnavailable("access_jwks_encoding_invalid");
  }
}

function jwksTtl(response: Response): number {
  const maxAge = response.headers.get("cache-control")?.match(/(?:^|,)\s*max-age=(\d+)/i)?.[1];
  if (!maxAge) return ACCESS_JWKS_DEFAULT_TTL_MS;
  return Math.max(30_000, Math.min(Number(maxAge) * 1000, ACCESS_JWKS_MAX_TTL_MS));
}

async function loadJwks(
  issuer: string,
  fetcher: typeof fetch,
  nowMs: number,
  forceRefresh = false,
): Promise<AccessKeyCacheEntry> {
  const cached = keyCache.get(issuer);
  if (!forceRefresh && cached && cached.expiresAt > nowMs) return cached;

  const existing = keyFetches.get(issuer);
  if (existing) return existing;

  const pending = (async () => {
    try {
      const response = await fetcher(`${issuer}/cdn-cgi/access/certs`, {
        method: "GET",
        headers: { Accept: "application/json" },
        redirect: "error",
        signal: AbortSignal.timeout(ACCESS_FETCH_TIMEOUT_MS),
      });
      if (!response.ok) throw new AccessVerifierUnavailable("access_jwks_http_error");
      const document: unknown = JSON.parse(await readBoundedText(response, ACCESS_JWKS_MAX_BYTES));
      if (!document || typeof document !== "object" || !Array.isArray((document as { keys?: unknown }).keys)) {
        throw new AccessVerifierUnavailable("access_jwks_shape_invalid");
      }
      const keys = (document as { keys: unknown[] }).keys;
      if (keys.length === 0 || keys.length > ACCESS_JWKS_MAX_KEYS) {
        throw new AccessVerifierUnavailable("access_jwks_key_count_invalid");
      }
      const entry: AccessKeyCacheEntry = {
        keys: keys.filter((item): item is AccessJwk => Boolean(
          item && typeof item === "object" &&
          typeof (item as AccessJwk).kid === "string" &&
          (item as AccessJwk).kty === "RSA" &&
          typeof (item as AccessJwk).n === "string" &&
          typeof (item as AccessJwk).e === "string",
        )),
        expiresAt: nowMs + jwksTtl(response),
      };
      if (entry.keys.length === 0) throw new AccessVerifierUnavailable("access_jwks_no_usable_keys");
      keyCache.set(issuer, entry);
      return entry;
    } catch (error) {
      keyCache.delete(issuer);
      if (error instanceof AccessVerifierUnavailable) throw error;
      throw new AccessVerifierUnavailable("access_jwks_fetch_failed");
    } finally {
      if (keyFetches.get(issuer) === pending) keyFetches.delete(issuer);
    }
  })();
  keyFetches.set(issuer, pending);
  return pending;
}

function audienceMatches(value: unknown, expected: string): boolean {
  return value === expected || (Array.isArray(value) && value.some((item) => item === expected));
}

async function verifyToken(
  token: string,
  issuer: string,
  audience: string,
  fetcher: typeof fetch,
  nowMs: number,
): Promise<boolean> {
  if (token.length > ACCESS_JWT_MAX_CHARS) return false;
  const [encodedHeader, encodedPayload, encodedSignature, extra] = token.split(".");
  if (!encodedHeader || !encodedPayload || !encodedSignature || extra !== undefined) return false;

  const header = decodeJsonSegment(encodedHeader);
  const payload = decodeJsonSegment(encodedPayload);
  const signature = decodeBase64Url(encodedSignature);
  if (!header || !payload || !signature || header.alg !== "RS256" || typeof header.kid !== "string" || header.kid.length > 128) {
    return false;
  }

  const currentSeconds = Math.floor(nowMs / 1000);
  if (payload.iss !== issuer || !audienceMatches(payload.aud, audience) ||
      typeof payload.exp !== "number" || !Number.isFinite(payload.exp) || payload.exp <= currentSeconds ||
      (typeof payload.nbf === "number" && (!Number.isFinite(payload.nbf) || payload.nbf > currentSeconds)) ||
      typeof payload.iat !== "number" || !Number.isFinite(payload.iat) || payload.iat > currentSeconds + 60) {
    return false;
  }

  let jwks = await loadJwks(issuer, fetcher, nowMs);
  let jwk = jwks.keys.find((candidate) => candidate.kid === header.kid);
  if (!jwk) {
    jwks = await loadJwks(issuer, fetcher, nowMs, true);
    jwk = jwks.keys.find((candidate) => candidate.kid === header.kid);
  }
  if (!jwk || (jwk.use !== undefined && jwk.use !== "sig") ||
      (jwk.alg !== undefined && jwk.alg !== "RS256") ||
      (jwk.key_ops !== undefined && !jwk.key_ops.includes("verify"))) {
    return false;
  }

  try {
    const publicKey = await crypto.subtle.importKey(
      "jwk",
      jwk,
      { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
      false,
      ["verify"],
    );
    const signingInput = new TextEncoder().encode(`${encodedHeader}.${encodedPayload}`);
    return await crypto.subtle.verify("RSASSA-PKCS1-v1_5", publicKey, signature, signingInput);
  } catch {
    return false;
  }
}

export async function verifyCloudflareAccessRequest(
  request: Request,
  config: CloudflareAccessConfig,
  options: { fetcher?: typeof fetch; nowMs?: number } = {},
): Promise<CloudflareAccessVerification> {
  const issuer = config.issuer ?? "";
  const audience = config.audience ?? "";
  if (!validIssuer(issuer) || !/^[a-f0-9]{32,128}$/i.test(audience)) {
    return { authorized: false, reason: "misconfigured" };
  }

  const token = request.headers.get("Cf-Access-Jwt-Assertion") ?? "";
  if (!token) return { authorized: false, reason: "missing_token" };
  try {
    const authorized = await verifyToken(
      token,
      issuer,
      audience,
      options.fetcher ?? fetch,
      options.nowMs ?? Date.now(),
    );
    return authorized
      ? { authorized: true }
      : { authorized: false, reason: "invalid_token" };
  } catch (error) {
    return {
      authorized: false,
      reason: error instanceof AccessVerifierUnavailable ? "verifier_unavailable" : "invalid_token",
    };
  }
}
