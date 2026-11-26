import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import net from "node:net";
import { test } from "node:test";

async function startProxy(t, config = {}) {
  const socket = net.createServer();
  socket.listen(0, "127.0.0.1");
  await once(socket, "listening");
  const { port } = socket.address();
  await new Promise((resolve) => socket.close(resolve));

  const child = spawn(process.execPath, ["server.js"], {
    cwd: new URL("../", import.meta.url),
    env: {
      ...process.env,
      PORT: String(port),
      UPSTREAM_URL: "http://127.0.0.1:1/private/v1",
      API_KEYS: "test-key",
      ALLOWED_METHODS: "GET,POST",
      RATE_LIMIT: "60",
      ...config,
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  t.after(async () => {
    if (child.exitCode !== null || child.signalCode !== null) return;
    const exited = once(child, "exit");
    child.kill();
    await exited;
  });
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("Proxy startup timed out")), 5000);
    child.once("error", reject);
    child.once("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`Proxy exited during startup: ${code}`));
    });
    child.stdout.on("data", (chunk) => {
      if (chunk.toString().includes("listening on")) {
        clearTimeout(timer);
        resolve();
      }
    });
  });
  return `http://127.0.0.1:${port}`;
}

for (const methods of ["GET,POST", " get, patch,DELETE "]) {
  test(`browser preflight reflects configured methods: ${methods}`, async (t) => {
    const base = await startProxy(t, { ALLOWED_METHODS: methods });
    const response = await fetch(`${base}/models`, { method: "OPTIONS" });
    assert.equal(response.status, 204);
    assert.equal(response.headers.get("access-control-allow-origin"), "*");
    assert.deepEqual(
      response.headers.get("access-control-allow-methods").split(/,\s*/),
      [...methods.split(",").map((method) => method.trim().toUpperCase()), "OPTIONS"]
    );
  });
}

test("rate-limited clients receive browser-readable retry timing", async (t) => {
  const base = await startProxy(t, { RATE_LIMIT: "1" });
  const headers = { authorization: "Bearer test-key" };
  assert.equal((await fetch(base, { headers })).status, 404);
  const response = await fetch(base, { headers });
  assert.equal(response.status, 429);
  assert.equal((await response.json()).error.type, "rate_limit_error");
  const retryAfter = response.headers.get("retry-after");
  assert.match(retryAfter || "", /^\d+$/);
  assert.ok(Number(retryAfter) >= 1 && Number(retryAfter) <= 60);
  assert.match(response.headers.get("access-control-expose-headers") || "", /retry-after/i);
});

test("missing endpoint guidance points to the proxy, not its private upstream", async (t) => {
  const base = await startProxy(t);
  for (const path of ["/", "/v1", "/v1/", "/api/test-key"]) {
    const response = await fetch(`${base}${path}`, {
      headers: { authorization: "Bearer test-key" },
    });
    assert.equal(response.status, 404);
    const { error } = await response.json();
    assert.equal(error.type, "invalid_request_error");
    assert.match(error.message, /proxy URL/);
    assert.match(error.message, /Authorization: Bearer <KEY>/);
    assert.match(error.message, /\/api\/<KEY>\/chat\/completions/);
    assert.doesNotMatch(error.message, /127\.0\.0\.1|private/);
  }
});
