/**
 * Null — minimal HTTPS API-key proxy.
 *
 * Usage:
 *   Authorization: Bearer <KEY>   (recommended)
 *   x-api-key: <KEY>
 *   https://host/api/<KEY>/<suffix>   (phone-friendly)
 *
 * Everything after auth is appended to UPSTREAM_URL and forwarded,
 * body and streams untouched.
 */
import http from "node:http";
import https from "node:https";
import { timingSafeEqual } from "node:crypto";

// ---------- config ----------
const PORT = Number(process.env.PORT || 3000);
const UPSTREAM_URL = (process.env.UPSTREAM_URL || "https://api.openai.com/v1").replace(/\/+$/, "");
const UPSTREAM_HOST_HEADER = process.env.UPSTREAM_HOST_HEADER || new URL(UPSTREAM_URL).host;
const API_KEYS = (process.env.API_KEYS || "")
  .split(",")
  .map((k) => k.trim())
  .filter(Boolean);
const SERVER_KEY = (process.env.SERVER_KEY || "").trim(); // sent upstream as Authorization: Bearer ...
const RATE_LIMIT = Number(process.env.RATE_LIMIT || 60); // requests / minute / key
const MAX_BODY_BYTES = Number(process.env.MAX_BODY_BYTES || 25 * 1024 * 1024);
const UPSTREAM_TIMEOUT_MS = Number(process.env.UPSTREAM_TIMEOUT_MS || 120000);
const ALLOWED_METHODS = new Set(
  (process.env.ALLOWED_METHODS || "GET,POST").split(",").map((m) => m.trim().toUpperCase())
);
const PATH_RE = new RegExp(process.env.ALLOW_PATH_REGEX || "^(/[A-Za-z0-9/_.-]{0,200})?$");

const upstream = new URL(UPSTREAM_URL);
const transport = upstream.protocol === "https:" ? https : http;

const json = (res, status, obj) => {
  const body = JSON.stringify(obj);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body),
    "cache-control": "no-store",
    "access-control-allow-origin": "*",
  });
  res.end(body);
};

// ---------- auth ----------
function extractKey(req, parsedUrl) {
  const auth = req.headers["authorization"];
  if (auth) {
    const m = /^Bearer\s+(.+)$/i.exec(auth.trim());
    if (m) return m[1].trim();
  }
  const x = req.headers["x-api-key"];
  if (x) return String(x).trim();
  return parsedUrl.searchParams.get("key") || "";
}

function keyMatches(candidate) {
  const a = Buffer.from(String(candidate));
  for (const k of API_KEYS) {
    const b = Buffer.from(k);
    if (a.length === b.length && timingSafeEqual(a, b)) return true;
  }
  return false;
}

// Extracts a key embedded in the path: /api/<KEY>/suffix -> { key, suffix }
function splitPathKey(pathname) {
  const m = /^\/api\/([^/]+)(\/.*)?$/.exec(pathname);
  if (!m) return null;
  return { key: decodeURIComponent(m[1]), suffix: m[2] || "" };
}

// ---------- rate limiting ----------
const hits = new Map(); // key -> { count, windowStart }
function rateLimited(key) {
  const now = Date.now();
  const win = 60_000;
  let rec = hits.get(key);
  if (!rec || now - rec.windowStart >= win) {
    rec = { count: 0, windowStart: now };
    hits.set(key, rec);
  }
  rec.count += 1;
  return rec.count > RATE_LIMIT;
}
setInterval(() => {
  const now = Date.now();
  for (const [k, v] of hits) if (now - v.windowStart >= 120_000) hits.delete(k);
}, 120_000).unref();

// ---------- proxy core ----------
function pipeProxy(req, res, suffix, query) {
  const target = suffix.startsWith("/v1/") && UPSTREAM_URL.endsWith("/v1")
    ? suffix.slice(3) // avoid /v1/v1/ when upstream already includes it
    : suffix;
  let full = UPSTREAM_URL + target;
  if (query) full += query;

  const headers = { ...req.headers };
  delete headers.host;
  delete headers.connection;
  delete headers["keep-alive"];
  delete headers["transfer-encoding"];
  delete headers.upgrade;
  delete headers["content-length"]; // recomputed by node
  headers.host = UPSTREAM_HOST_HEADER;
  if (SERVER_KEY) headers.authorization = `Bearer ${SERVER_KEY}`;

  const abort = new AbortController();
  const onClientClose = () => abort.abort();
  req.on("close", onClientClose);

  const upReq = transport.request(
    full,
    {
      method: req.method,
      headers,
      signal: abort.signal,
      timeout: UPSTREAM_TIMEOUT_MS,
    },
    (upRes) => {
      const out = { ...upRes.headers };
      delete out.connection;
      delete out["keep-alive"];
      delete out["transfer-encoding"];
      out["access-control-allow-origin"] = "*";
      res.writeHead(upRes.statusCode || 502, out);
      upRes.pipe(res);
    }
  );

  upReq.on("timeout", () => abort.abort());
  upReq.on("error", (err) => {
    if (res.headersSent) return res.destroy();
    const aborted = err.name === "AbortError";
    json(res, aborted ? 504 : 502, {
      error: {
        message: aborted
          ? `Upstream timed out after ${UPSTREAM_TIMEOUT_MS}ms`
          : `Upstream request failed: ${err.message}`,
        type: "proxy_error",
      },
    });
  });

  // Stream body with a hard size cap (never buffered).
  let sent = 0;
  req.on("data", (chunk) => {
    sent += chunk.length;
    if (sent > MAX_BODY_BYTES) {
      abort.abort();
      if (!res.headersSent) {
        json(res, 413, { error: { message: "Request body too large", type: "proxy_error" } });
      }
      req.destroy();
      return;
    }
    upReq.write(chunk);
  });
  req.on("end", () => upReq.end());
  req.on("error", () => abort.abort());
}

const server = http.createServer((req, res) => {
  const started = Date.now();
  const parsedUrl = new URL(req.url, "http://localhost");

  // CORS preflight
  if (req.method === "OPTIONS") {
    res.writeHead(204, {
      "access-control-allow-origin": "*",
      "access-control-allow-methods": "GET, POST, OPTIONS",
      "access-control-allow-headers":
        "Authorization, Content-Type, x-api-key, OpenAI-Organization, OpenAI-Project, Anthropic-Version",
      "access-control-max-age": "86400",
    });
    res.end();
    return;
  }

  if (req.method === "GET" && parsedUrl.pathname === "/health") {
    json(res, 200, {
      ok: true,
      auth: API_KEYS.length > 0,
      upstreamConfigured: Boolean(process.env.UPSTREAM_URL),
      uptimeSec: Math.round(process.uptime()),
    });
    return;
  }

  if (!PATH_RE.test(parsedUrl.pathname)) {
    json(res, 404, { error: { message: "Not found", type: "invalid_request_error" } });
    return;
  }
  if (!ALLOWED_METHODS.has(req.method)) {
    res.setHeader("allow", [...ALLOWED_METHODS].join(", "));
    json(res, 405, { error: { message: `Method ${req.method} not allowed`, type: "invalid_request_error" } });
    return;
  }

  // Resolve proxy key from header / query / path
  let pathname = parsedUrl.pathname;
  let key = extractKey(req, parsedUrl);
  const pathKey = splitPathKey(pathname);
  if (pathKey && !key) {
    key = pathKey.key;
    pathname = pathKey.suffix || "/";
  } else if (pathKey && keyMatches(pathKey.key)) {
    pathname = pathKey.suffix || "/"; // header key already validated below; path wins if equal
  }

  if (API_KEYS.length > 0) {
    if (!key || !keyMatches(key)) {
      json(res, 401, { error: { message: "Invalid or missing API key", type: "invalid_request_error" } });
      return;
    }
    if (rateLimited(key)) {
      json(res, 429, { error: { message: "Rate limit exceeded", type: "rate_limit_error" } });
      return;
    }
  }

  if (!pathname || pathname === "/" || pathname === "/v1" || pathname === "/v1/") {
    json(res, 404, {
      error: {
        message: "Append an upstream path after the key, e.g. " + UPSTREAM_URL.replace(/^[a-z]+:\/\//i, "") + "/chat/completions",
        type: "invalid_request_error",
      },
    });
    return;
  }

  // Drop the ?key=... so it is never forwarded upstream
  parsedUrl.searchParams.delete("key");
  const query = parsedUrl.searchParams.toString();

  pipeProxy(req, res, pathname, query ? `?${query}` : "");

  res.on("finish", () => {
    console.log(`${req.method} ${pathname} -> ${res.statusCode} (${Date.now() - started}ms)`);
  });
});

server.listen(PORT, "0.0.0.0", () => {
  console.log(`null-proxy listening on 0.0.0.0:${PORT} -> ${UPSTREAM_URL}`);
  console.log(`auth: ${API_KEYS.length > 0 ? `enabled (${API_KEYS.length} key(s))` : "DISABLED — set API_KEYS"}`);
});
