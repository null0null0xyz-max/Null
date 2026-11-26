import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import http from 'node:http';
import { test } from 'node:test';

async function startProxy(t, { upstream, ...config } = {}) {
  const requests = [];
  const server = http.createServer((req, res) => {
    requests.push(req);
    if (upstream) return upstream(req, res);
    req.resume();
    req.on('end', () => res.end('upstream'));
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(() => {
    server.closeAllConnections();
    return new Promise((resolve) => server.close(resolve));
  });

  // Discover the OS-assigned port without changing the production entry point.
  const child = spawn(process.execPath, ['--input-type=module', '--eval', `
    import http from 'node:http';
    const listen = http.Server.prototype.listen;
    http.Server.prototype.listen = function (...args) {
      this.once('listening', () => process.send({ port: this.address().port }));
      return listen.apply(this, args);
    };
    await import('./server.js');
  `], {
    cwd: new URL('..', import.meta.url),
    env: {
      ...process.env,
      PORT: '0',
      UPSTREAM_URL: `http://127.0.0.1:${server.address().port}/v1`,
      UPSTREAM_HOST_HEADER: 'upstream.test',
      API_KEYS: 'alpha,bravo',
      SERVER_KEY: 'server-secret',
      RATE_LIMIT: '100',
      MAX_BODY_BYTES: '1024',
      UPSTREAM_TIMEOUT_MS: '2000',
      ALLOWED_METHODS: 'GET,POST',
      ALLOW_PATH_REGEX: '^(/[A-Za-z0-9/_.-]{0,200})?$',
      ...config,
    },
    stdio: ['ignore', 'pipe', 'pipe', 'ipc'],
  });
  let output = '';
  child.stdout.on('data', (chunk) => { output += chunk; });
  child.stderr.on('data', (chunk) => { output += chunk; });
  t.after(async () => {
    if (child.exitCode !== null || child.signalCode !== null) return;
    const exited = once(child, 'exit');
    child.kill();
    await exited;
  });
  const port = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`Proxy startup timed out: ${output}`)), 5000);
    child.once('message', ({ port }) => { clearTimeout(timer); resolve(port); });
    child.once('error', (err) => { clearTimeout(timer); reject(err); });
    child.once('exit', (code) => {
      clearTimeout(timer);
      reject(new Error(`Proxy exited (${code}): ${output}`));
    });
  });
  const request = (path, { method = 'GET', headers = {}, body } = {}) => new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, path, method, headers }, resolve);
    req.on('error', reject);
    req.end(body);
  });
  return { request, requests };
}

async function read(res) {
  let body = '';
  for await (const chunk of res) body += chunk;
  return body;
}

async function expectError(res, status, type) {
  assert.equal(res.statusCode, status);
  assert.equal(res.headers['access-control-allow-origin'], '*');
  assert.equal(JSON.parse(await read(res)).error.type, type);
}

const options = { timeout: 10000 };

test('authentication rejects missing/incorrect keys and does not fall back from an invalid Bearer key', options, async (t) => {
  const { request, requests } = await startProxy(t);
  for (const [path, headers] of [
    ['/models', {}],
    ['/models', { authorization: 'Bearer wrong' }],
    ['/models', { authorization: 'Bearer alpha-extra' }],
    ['/models?key=alpha', { authorization: 'Bearer wrong', 'x-api-key': 'alpha' }],
    ['/api/alpha/models', { authorization: 'Bearer wrong' }],
  ]) {
    await expectError(await request(path, { headers }), 401, 'invalid_request_error');
  }
  assert.equal(requests.length, 0);
});

test('each supported credential reaches the same upstream without query/path keys', options, async (t) => {
  const { request, requests } = await startProxy(t);
  for (const [path, headers] of [
    ['/models', { authorization: 'bEaReR alpha' }],
    ['/models', { 'x-api-key': 'bravo' }],
    ['/models?key=alpha&key=bravo', {}],
    ['/api/alpha/models', {}],
  ]) {
    const res = await request(path, { headers });
    assert.equal(res.statusCode, 200);
    assert.equal(await read(res), 'upstream');
    assert.equal(requests.at(-1).url, '/v1/models');
    assert.equal(requests.at(-1).headers.authorization, 'Bearer server-secret');
  }
});

test('health and preflight bypass auth without contacting upstream', options, async (t) => {
  const { request, requests } = await startProxy(t);
  const health = await request('/health');
  assert.equal(health.statusCode, 200);
  const { uptimeSec, ...status } = JSON.parse(await read(health));
  assert.deepEqual(status, { ok: true, auth: true, upstreamConfigured: true });
  assert.ok(uptimeSec >= 0);
  const preflight = await request('/models', { method: 'OPTIONS' });
  assert.equal(preflight.statusCode, 204);
  assert.match(preflight.headers['access-control-allow-headers'], /Authorization/);
  assert.equal(await read(preflight), '');
  assert.equal(requests.length, 0);
});

test('disallowed methods, paths and empty upstream suffixes never reach upstream', options, async (t) => {
  const { request, requests } = await startProxy(t);
  const method = await request('/models', { method: 'DELETE' });
  assert.equal(method.headers.allow, 'GET, POST');
  await expectError(method, 405, 'invalid_request_error');
  for (const path of ['/bad%20path', '/', '/v1', '/v1/', '/api/alpha']) {
    await expectError(await request(path, { headers: { authorization: 'Bearer alpha' } }), 404, 'invalid_request_error');
  }
  assert.equal(requests.length, 0);
});

test('rate limit is shared across credential sources but isolated between keys', options, async (t) => {
  const { request, requests } = await startProxy(t, { RATE_LIMIT: '1' });
  const first = await request('/models?key=alpha');
  assert.equal(first.statusCode, 200);
  await read(first);
  await expectError(await request('/models', { headers: { authorization: 'Bearer alpha' } }), 429, 'rate_limit_error');
  const otherKey = await request('/models', { headers: { 'x-api-key': 'bravo' } });
  assert.equal(otherKey.statusCode, 200);
  await read(otherKey);
  assert.equal(requests.length, 2);
});

test('POST forwards opaque bytes, method, query and response status without duplicating /v1', options, async (t) => {
  const body = Buffer.from([0, 255, 13, 10, 128]);
  const { request } = await startProxy(t, { upstream: async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    res.writeHead(201, { 'content-type': 'application/json', 'x-upstream': 'yes' });
    res.end(JSON.stringify({ method: req.method, url: req.url, headers: req.headers, body: [...Buffer.concat(chunks)] }));
  } });
  const res = await request('/v1/chat/completions?key=alpha&model=a%2Fb&tag=one&tag=two', {
    method: 'POST', headers: { 'content-type': 'application/octet-stream', connection: 'keep-alive', 'keep-alive': 'timeout=99' }, body,
  });
  assert.equal(res.statusCode, 201);
  assert.equal(res.headers['x-upstream'], 'yes');
  assert.equal(res.headers['access-control-allow-origin'], '*');
  const forwarded = JSON.parse(await read(res));
  assert.equal(forwarded.method, 'POST');
  assert.equal(forwarded.url, '/v1/chat/completions?model=a%2Fb&tag=one&tag=two');
  assert.deepEqual(forwarded.body, [...body]);
  assert.equal(forwarded.headers.host, 'upstream.test');
  assert.equal(forwarded.headers.authorization, 'Bearer server-secret');
  assert.equal(forwarded.headers['content-type'], 'application/octet-stream');
  assert.equal(forwarded.headers['keep-alive'], undefined);
});

test('SSE delivers the first event before upstream finishes', options, async (t) => {
  let finish;
  const { request } = await startProxy(t, { upstream: (req, res) => {
    req.resume();
    req.on('end', () => {
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      res.write('data: first\n\n');
      finish = () => res.end('data: [DONE]\n\n');
    });
  } });
  const res = await request('/models?key=alpha');
  assert.equal(res.statusCode, 200);
  assert.equal(res.headers['content-type'], 'text/event-stream');
  const stream = res[Symbol.asyncIterator]();
  assert.equal((await stream.next()).value.toString(), 'data: first\n\n');
  finish();
  let rest = '';
  for await (const chunk of stream) rest += chunk;
  assert.equal(rest, 'data: [DONE]\n\n');
});

test('upstream connection failure returns a structured 502', options, async (t) => {
  const { request } = await startProxy(t, { upstream: (req) => req.socket.destroy() });
  await expectError(await request('/models?key=alpha'), 502, 'proxy_error');
});

test('unresponsive upstream returns a structured 504', options, async (t) => {
  const { request } = await startProxy(t, { UPSTREAM_TIMEOUT_MS: '100', upstream: (req) => req.resume() });
  const res = await request('/models?key=alpha');
  assert.equal(res.statusCode, 504);
  assert.match(JSON.parse(await read(res)).error.message, /timed out after 100ms/);
});

test('body cap accepts the exact limit and rejects the next byte', options, async (t) => {
  const { request } = await startProxy(t, { MAX_BODY_BYTES: '4' });
  const accepted = await request('/models?key=alpha', { method: 'POST', body: '1234' });
  assert.equal(accepted.statusCode, 200);
  await read(accepted);
  await expectError(await request('/models?key=alpha', { method: 'POST', body: '12345' }), 413, 'proxy_error');
});

test('empty API_KEYS disables authentication for development', options, async (t) => {
  const { request } = await startProxy(t, { API_KEYS: '', SERVER_KEY: '' });
  const res = await request('/models');
  assert.equal(res.statusCode, 200);
  assert.equal(await read(res), 'upstream');
});
