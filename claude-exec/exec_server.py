"""Исполнитель на Render: принимает рабочую папку (tar.gz) и bash-команду, выполняет,
возвращает вывод и изменённые файлы. Плюс публичный показ сайтов, которые сделала модель:

  POST /exec                 (X-Token) выполнить команду в bash   — только с токеном
  GET  /s/<имя>/...          статический сайт из папки sites/<имя>/ — открыт всем, только чтение
  ANY  /p/<порт>/...         прокси на приложение, запущенное в фоне на порту 3000–9999
  GET  /                     проверка, что сервер жив
  /relay/<job>/...           реле задач Claude: бот (X-Token) <-> GitHub Actions runner (X-OIDC)

Управления через сайт нет: команды принимаются только с EXEC_TOKEN.
Env: EXEC_TOKEN (обязательно), PORT (Render задаёт сам).
"""
import os, io, json, time, base64, hmac, shutil, hashlib, tarfile, tempfile, threading, mimetypes, subprocess
import urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

TOKEN = os.environ["EXEC_TOKEN"]
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # к localhost без прокси
WS = "/tmp/ws"  # живёт, пока Render не уснёт; node_modules и т.п. переживают вызовы
SITES = os.path.join(WS, "sites")
SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", ".cache"}
BASH = shutil.which("bash") or "/bin/sh"
os.makedirs(WS, exist_ok=True)


def extract(b64):
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(b64)), mode="r:gz") as t:
        try:
            t.extractall(WS, filter="data")
        except TypeError:  # Python < 3.12
            t.extractall(WS)


def changed_since(ts):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for root, dirs, files in os.walk(WS):
            dirs[:] = [d for d in dirs if d not in SKIP]
            for n in files:
                p = os.path.join(root, n)
                try:
                    if os.path.getmtime(p) >= ts and os.path.getsize(p) < 20_000_000:
                        t.add(p, arcname=os.path.relpath(p, WS))
                except OSError:
                    pass
    return base64.b64encode(buf.getvalue()).decode()


def run(cmd, cwd, timeout):
    """bash-команда. Вывод — в файлы, ждём только сам bash: фоновые процессы (cmd &) не вешают ответ."""
    with tempfile.TemporaryFile() as fo, tempfile.TemporaryFile() as fe:
        p = subprocess.Popen([BASH, "-lc", cmd], cwd=cwd, stdout=fo, stderr=fe, stdin=subprocess.DEVNULL)
        try:
            code = p.wait(timeout=timeout)
            tail = ""
        except subprocess.TimeoutExpired:
            p.kill()
            code, tail = 124, "\n[timeout]"
        fo.seek(0); fe.seek(0)
        return code, fo.read().decode(errors="replace"), fe.read().decode(errors="replace") + tail


# ---------- реле задач для GitHub Actions ----------
GH_REPO = os.environ.get("GH_REPO", "null0null0xyz-max/Null")
JOBS = {}            # job -> {"in", "ev", "done", "stop", "out", "t", "cv"}
JOBS_LOCK = threading.Lock()
_JWKS = {"keys": {}, "t": 0}
_OK_TOKENS = {}      # sha256(token) -> exp


def _b64u(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _jwks(kid):
    if kid not in _JWKS["keys"] or time.time() - _JWKS["t"] > 3600:
        j = json.loads(urllib.request.urlopen(
            "https://token.actions.githubusercontent.com/.well-known/jwks", timeout=20).read())
        _JWKS["keys"] = {k["kid"]: (int.from_bytes(_b64u(k["n"]), "big"), int.from_bytes(_b64u(k["e"]), "big"))
                         for k in j["keys"]}
        _JWKS["t"] = time.time()
    return _JWKS["keys"].get(kid)


def oidc_ok(token):
    """Проверка токена GitHub OIDC (RS256) без сторонних библиотек: подпись, издатель, аудитория, репозиторий."""
    if not token:
        return False
    h = hashlib.sha256(token.encode()).hexdigest()
    if _OK_TOKENS.get(h, 0) > time.time():
        return True
    try:
        head, body, sig = token.split(".")
        hdr, cl = json.loads(_b64u(head)), json.loads(_b64u(body))
        if hdr.get("alg") != "RS256":
            return False
        key = _jwks(hdr.get("kid"))
        if not key:
            return False
        n, e = key
        k = (n.bit_length() + 7) // 8
        em = pow(int.from_bytes(_b64u(sig), "big"), e, n).to_bytes(k, "big")
        digest = hashlib.sha256(f"{head}.{body}".encode()).digest()
        want = b"\x00\x01" + b"\xff" * (k - 54) + b"\x00" + \
            bytes.fromhex("3031300d060960864801650304020105000420") + digest
        if not hmac.compare_digest(em, want):
            return False
        if cl.get("iss") != "https://token.actions.githubusercontent.com" or cl.get("aud") != "claude-relay" \
                or cl.get("repository") != GH_REPO or cl.get("exp", 0) < time.time():
            return False
        _OK_TOKENS[h] = cl["exp"]
        return True
    except Exception:
        return False


def job_get(job, create=False):
    with JOBS_LOCK:
        now = time.time()
        for k in [k for k, v in JOBS.items() if now - v["t"] > 7 * 3600]:
            JOBS.pop(k, None)
        if create and job not in JOBS:
            JOBS[job] = {"in": None, "ev": [], "done": False, "stop": False, "out": None,
                         "t": now, "cv": threading.Condition(), "started": False}
        return JOBS.get(job)


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # ---------- сайты ----------
    def _static(self, path):
        rest = unquote(path[3:])  # после /s/
        name, _, sub = rest.partition("/")
        if not name:
            sites = sorted(os.listdir(SITES)) if os.path.isdir(SITES) else []
            links = "".join(f'<li><a href="/s/{n}/">{n}</a></li>' for n in sites) or "<li>пока пусто</li>"
            return self._send(200, f"<meta charset=utf-8><h3>Сайты</h3><ul>{links}</ul>", "text/html; charset=utf-8")
        if not sub and not path.endswith("/"):
            return self._send(301, b"", "text/plain", {"Location": f"/s/{name}/"})
        root = os.path.realpath(os.path.join(SITES, name))
        f = os.path.realpath(os.path.join(root, sub))
        if not f.startswith(root + os.sep) and f != root:
            return self._send(403, "forbidden", "text/plain")
        if os.path.isdir(f):
            f = os.path.join(f, "index.html")
        if not os.path.isfile(f):
            msg = ("<meta charset=utf-8><p>Сайт не найден. Если Render засыпал — попроси бота "
                   "опубликовать его ещё раз (rexec publish).</p>")
            return self._send(404, msg, "text/html; charset=utf-8")
        ctype = mimetypes.guess_type(f)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(f, "rb") as fh:
            self._send(200, fh.read(), ctype, {"Cache-Control": "no-cache"})

    def _proxy(self, path):
        port, _, rest = path[3:].partition("/")
        if not port.isdigit() or not 3000 <= int(port) <= 9999 or int(port) == int(os.environ.get("PORT", 10000)):
            return self._send(400, "порт 3000–9999", "text/plain; charset=utf-8")
        q = urlsplit(self.path).query
        url = f"http://127.0.0.1:{port}/{rest}" + (f"?{q}" if q else "")
        n = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(n) if n else None
        hdr = {k: v for k, v in self.headers.items()
               if k.lower() not in ("host", "content-length", "connection", "accept-encoding", "x-token")}
        req = urllib.request.Request(url, data=data, method=self.command, headers=hdr)
        try:
            r = DIRECT.open(req, timeout=60)
            code, body, rh = r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            code, body, rh = e.code, e.read(), e.headers
        except Exception as e:
            return self._send(502, f"приложение на порту {port} не отвечает: {e}", "text/plain; charset=utf-8")
        extra = {k: v for k, v in rh.items()
                 if k.lower() in ("location", "set-cookie", "cache-control")}
        self._send(code, body, rh.get("Content-Type", "application/octet-stream"), extra)

    def _relay(self, path):
        parts = path.split("/")  # ['', 'relay', job, what]
        if len(parts) != 4 or not parts[2].isalnum() or len(parts[2]) > 64:
            return self._send(404, {"error": "not found"})
        job, what, m = parts[2], parts[3], self.command
        bot = hmac.compare_digest(self.headers.get("X-Token", ""), TOKEN)
        runner = not bot and oidc_ok(self.headers.get("X-OIDC", ""))
        if not bot and not runner:
            return self._send(401, {"error": "auth"})
        n = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(n) if n else b""
        if bot and what == "in" and m == "POST":
            j = job_get(job, create=True)
            j["in"] = data
            return self._send(200, {"ok": True})
        j = job_get(job)
        if not j:
            return self._send(404, {"error": "no job"})
        cv = j["cv"]
        if runner and what == "in" and m == "GET":
            if not j["in"]:
                return self._send(410, {"error": "taken"})
            body, j["in"], j["started"] = j["in"], None, True  # отдаём один раз
            return self._send(200, body)
        if runner and what == "ev" and m == "POST":
            with cv:
                if data:
                    for line in data.decode(errors="replace").split("\n"):
                        if line.strip():
                            j["ev"].append(line)
                            if line.startswith('{"type": "_done"') or line.startswith('{"type":"_done"'):
                                j["done"] = True
                cv.notify_all()
            return self._send(200, {"stop": j["stop"]})
        if runner and what == "out" and m == "PUT":
            j["out"] = data
            return self._send(200, {"ok": True})
        if bot and what == "ev" and m == "GET":
            q = urlsplit(self.path).query
            frm = int(dict(x.split("=", 1) for x in q.split("&") if "=" in x).get("from", 0))
            with cv:
                cv.wait_for(lambda: len(j["ev"]) > frm or j["done"], timeout=25)
                lines = j["ev"][frm:]
            return self._send(200, {"lines": lines, "next": frm + len(lines), "done": j["done"],
                                    "started": j["started"]})
        if bot and what == "stop" and m == "POST":
            j["stop"] = True
            return self._send(200, {"ok": True})
        if bot and what == "out" and m == "GET":
            body = j["out"] or b""
            with JOBS_LOCK:
                JOBS.pop(job, None)
            return self._send(200, body, "application/gzip")
        return self._send(400, {"error": "bad request"})

    def _route(self):
        path = urlsplit(self.path).path
        if path.startswith("/relay/"):
            return self._relay(path)
        if path.startswith("/s/") or path == "/s":
            return self._static(path if path != "/s" else "/s/")
        if path.startswith("/p/"):
            return self._proxy(path)
        if self.command in ("GET", "HEAD"):
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})

    do_GET = do_HEAD = do_PUT = do_DELETE = do_PATCH = _route

    def do_POST(self):
        path = urlsplit(self.path).path
        if path != "/exec":
            return self._route()
        if not hmac.compare_digest(self.headers.get("X-Token", ""), TOKEN):
            return self._send(401, {"error": "bad token"})
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if req.get("tar"):
            extract(req["tar"])
        start = time.time() - 1
        cwd = os.path.join(WS, req.get("cwd", ""))
        os.makedirs(cwd, exist_ok=True)
        code, out, err = run(req["cmd"], cwd, min(int(req.get("timeout", 600)), 900))
        self._send(200, {"code": code, "stdout": out[-200_000:], "stderr": err[-50_000:],
                         "tar": changed_since(start)})

    def log_message(self, *a):
        pass


ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", 10000))), H).serve_forever()
