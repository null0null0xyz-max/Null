"""Исполнитель на Render: принимает рабочую папку (tar.gz) и bash-команду, выполняет,
возвращает вывод и изменённые файлы. Плюс публичный показ сайтов, которые сделала модель:

  POST /exec                 (X-Token) выполнить команду в bash   — только с токеном
  GET  /s/<имя>/...          статический сайт из папки sites/<имя>/ — открыт всем, только чтение
  ANY  /p/<порт>/...         прокси на приложение, запущенное в фоне на порту 3000–9999
  GET  /                     проверка, что сервер жив

Управления через сайт нет: команды принимаются только с EXEC_TOKEN.
Env: EXEC_TOKEN (обязательно), PORT (Render задаёт сам).
"""
import os, io, json, time, base64, hmac, shutil, tarfile, tempfile, mimetypes, subprocess
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

    def _route(self):
        path = urlsplit(self.path).path
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
