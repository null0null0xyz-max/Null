# Null — HTTPS API-key proxy

A tiny zero-dependency Node.js reverse proxy with API-key authentication, built to run anywhere (Freebuff hosting, Render, Fly, VPS, localhost).

## How it works

Requests are authenticated with **your** proxy key, then forwarded 1:1 to `UPSTREAM_URL`:

```
Client ──(your proxy key)──▶ null-proxy ──(server key, optional)──▶ UPSTREAM_URL
```

## Running

```bash
# 1. configure (see env.example.txt)
#    set UPSTREAM_URL (where to proxy), API_KEYS (client keys), SERVER_KEY (upstream key if needed)

# 2. run
npm start          # or: node server.js
```

Server listens on `0.0.0.0:$PORT` (defaults to 3000).

## Calling from a phone (HTTP.Toolkit / HTTP Shortcuts / curl)

Three ways to authenticate — pick one:

| Method | Example |
|---|---|
| Header (recommended) | `Authorization: Bearer sk-abc123...` |
| Header (alt) | `x-api-key: sk-abc123...` |
| In URL path | `GET https://your-host/api/sk-abc123.../chat/completions` |
| Query param | `?key=sk-abc123...` (auto-stripped before forwarding) |

Example POST:

```bash
curl -X POST https://your-host/api/sk-abc123.../chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"Привет"}]}'
```

The path-embedded form is the phone-friendly one: put your key between `/api/` and the rest of the path, everything after it is appended to `UPSTREAM_URL` and forwarded verbatim.

## What's protected

- API-key auth via timing-safe comparison, no keys in logs
- Per-key rate limit (`RATE_LIMIT` req/min, 429 on exceed)
- Request body cap (`MAX_BODY_BYTES`) without buffering whole bodies
- Only `GET`/`POST` allowed by default (`ALLOWED_METHODS` to change)
- Path allowlist regex (`ALLOW_PATH_REGEX`), path traversal blocked by URL parsing
- Streaming responses passthrough (SSE works, e.g. `"stream": true`)
- Hop-by-hop headers stripped in both directions; `?key=` never forwarded upstream
- `/health` endpoint (excluded from auth) for uptime checks

## Env reference

| Var | Default | Meaning |
|---|---|---|
| `PORT` | 3000 | listen port (Freebuff injects the right one) |
| `UPSTREAM_URL` | — | base URL requests are proxied to |
| `API_KEYS` | — | comma-separated client keys; empty = auth disabled (dev only) |
| `SERVER_KEY` | — | optional key sent upstream as `Authorization: Bearer ...` |
| `RATE_LIMIT` | 60 | req/min per key |
| `MAX_BODY_BYTES` | 25 MiB | request body cap |
| `UPSTREAM_TIMEOUT_MS` | 120000 | upstream timeout |
| `ALLOWED_METHODS` | GET,POST | allowed HTTP methods |
| `ALLOW_PATH_REGEX` | `^(/[A-Za-z0-9/_.-]{0,200})?$` | path allowlist |
