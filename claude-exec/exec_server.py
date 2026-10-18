"""Исполнитель на Render: принимает рабочую папку (tar.gz) и команду, выполняет,
возвращает вывод и изменённые файлы. Только стандартная библиотека.

Env: EXEC_TOKEN (обязательно), PORT (Render задаёт сам).
"""
import os, io, json, time, base64, hmac, tarfile, subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ["EXEC_TOKEN"]
WS = "/tmp/ws"  # живёт, пока Render не уснёт; node_modules и т.п. переживают вызовы
SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", ".cache"}
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
                if os.path.getmtime(p) >= ts and os.path.getsize(p) < 20_000_000:
                    t.add(p, arcname=os.path.relpath(p, WS))
    return base64.b64encode(buf.getvalue()).decode()


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # health check / пробуждение
        self._send(200, {"ok": True})

    def do_POST(self):
        if not hmac.compare_digest(self.headers.get("X-Token", ""), TOKEN):
            return self._send(401, {"error": "bad token"})
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if req.get("tar"):
            extract(req["tar"])
        start = time.time() - 1
        try:
            p = subprocess.run(req["cmd"], shell=True, cwd=os.path.join(WS, req.get("cwd", "")),
                               capture_output=True, text=True, timeout=min(int(req.get("timeout", 600)), 900))
            out, err, code = p.stdout, p.stderr, p.returncode
        except subprocess.TimeoutExpired as e:
            out, err, code = e.stdout or "", (e.stderr or "") + "\n[timeout]", 124
        self._send(200, {"code": code, "stdout": out[-200_000:], "stderr": err[-50_000:],
                         "tar": changed_since(start)})

    def log_message(self, *a):
        pass


ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", 10000))), H).serve_forever()
