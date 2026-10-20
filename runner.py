#!/usr/bin/env python3
"""Запускается в GitHub Actions (workflow claude.yml): берёт задачу у Render-реле, гоняет Claude Code
на машине GitHub (4 ядра, 16 ГБ) и шлёт поток событий обратно боту через реле.

Секретов в GitHub нет: к реле runner входит токеном GitHub OIDC (реле проверяет подпись и репозиторий).
Всё, что выводит Claude, идёт только в реле — в публичные логи Actions ничего не печатается.
Env: JOB, RELAY (из входов workflow).
"""
import os, io, sys, json, time, base64, signal, shutil, tarfile, threading, subprocess
import urllib.request, urllib.error

JOB, RELAY = os.environ["JOB"], os.environ["RELAY"].rstrip("/")
ECC_URL = "https://codeload.github.com/affaan-m/ECC/tar.gz/refs/heads/main"
ECC_PARTS = ("commands/", "agents/", "skills/", "rules/common/")
SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", ".cache", "Cache", "Code Cache", "GPUCache",
        "Service Worker", "GrShaderCache", "ShaderCache", "component_crx_cache"}
CLAUDE_SKIP = {"shell-snapshots", "statsig", "todos", "ide", "cache", "local", "downloads",
               "debug", "telemetry", "logs", "plugins"}


def oidc():
    url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"] + "&audience=claude-relay"
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())["value"]


TOK = {"v": oidc(), "t": time.time()}


def call(method, path, data=None, ctype="application/json", timeout=60):
    if time.time() - TOK["t"] > 240:  # токен OIDC живёт ~5 мин
        TOK.update(v=oidc(), t=time.time())
    for attempt in range(6):
        try:
            req = urllib.request.Request(RELAY + path, data=data, method=method,
                                         headers={"X-OIDC": TOK["v"], "Content-Type": ctype})
            return urllib.request.urlopen(req, timeout=timeout).read()
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise
        except Exception:
            pass
        time.sleep(3 + attempt * 3)
    raise RuntimeError("relay unavailable")


# ---------- поток событий ----------
out_buf, out_lock, stop = [], threading.Lock(), threading.Event()
inbox = []  # сообщения владельца, дописанные во время работы (приходят в ответе реле на /ev)


def emit(obj):
    with out_lock:
        out_buf.append(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False))


def status(text):
    emit({"type": "_status", "text": text})


def pusher():
    while True:
        time.sleep(0.3)
        with out_lock:
            batch = out_buf[:]
            del out_buf[:]
        if batch:
            try:
                r = json.loads(call("POST", f"/relay/{JOB}/ev", "\n".join(batch).encode()))
                if r.get("stop"):
                    stop.set()
                inbox.extend(r.get("msgs") or [])
            except Exception:
                with out_lock:
                    out_buf[:0] = batch
        elif not stop.is_set():
            try:  # проверка кнопки Stop и новых сообщений, даже когда событий нет
                r = json.loads(call("POST", f"/relay/{JOB}/ev", b""))
                if r.get("stop"):
                    stop.set()
                inbox.extend(r.get("msgs") or [])
            except Exception:
                pass
            time.sleep(1)


def frames():
    """Если модель запустила локальный Chromium (tgbrowser), шлём его кадры в реле — владелец смотрит по ссылке."""
    try:
        import websocket
    except ImportError:
        sh("pip install -q websocket-client")
        try:
            import websocket
        except ImportError:
            return
    ws, n = None, 0
    while not stop.is_set():
        try:
            if ws is None:
                pages = [t for t in json.loads(urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=3).read())
                         if t.get("type") == "page"]
                if not pages:
                    time.sleep(2); continue
                ws = websocket.create_connection(pages[-1]["webSocketDebuggerUrl"], timeout=20, suppress_origin=True)
                t_conn = time.time()
            n += 1
            ws.send(json.dumps({"id": n, "method": "Page.captureScreenshot", "params": {"format": "jpeg", "quality": 55}}))
            while True:
                m = json.loads(ws.recv())
                if m.get("id") == n:
                    break
            data = (m.get("result") or {}).get("data")
            if data:
                call("POST", f"/relay/{JOB}/frame", base64.b64decode(data), "image/jpeg", timeout=20)
            if time.time() - t_conn > 8:  # переподключаемся — вдруг открыта новая вкладка
                ws.close(); ws = None
            time.sleep(0.8)
        except Exception:
            try:
                ws and ws.close()
            except Exception:
                pass
            ws = None
            time.sleep(2)


def flush():
    for _ in range(100):
        with out_lock:
            if not out_buf:
                return
        time.sleep(0.2)


# ---------- подготовка машины ----------
def sh(cmd):
    return subprocess.run(["bash", "-lc", cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode


def ensure_dir(p):
    if not os.path.isdir(p):
        sh(f"sudo mkdir -p '{p}' && sudo chown -R $(id -u):$(id -g) '{p}'")


def install_ecc(ecc_dir):
    if os.path.isdir(ecc_dir):
        return
    tmp = ecc_dir + ".tmp"
    with urllib.request.urlopen(ECC_URL, timeout=120) as r:
        with tarfile.open(fileobj=r, mode="r|gz") as tf:
            for m in tf:
                rel = m.name.split("/", 1)[1] if "/" in m.name else ""
                if not m.isfile() or not rel.startswith(ECC_PARTS) or ".." in rel.split("/"):
                    continue
                dst = os.path.join(tmp, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, "wb") as f:
                    shutil.copyfileobj(tf.extractfile(m), f)
    os.rename(tmp, ecc_dir)


def install_claude():
    local = os.path.expanduser("~/.local/bin/claude")
    if os.path.exists(local):
        return local
    sh("curl -fsSL https://claude.ai/install.sh | bash")
    if os.path.exists(local):
        return local
    sh("sudo npm i -g @anthropic-ai/claude-code")
    return shutil.which("claude") or "claude"


# ---------- упаковка результата ----------
def walk(top, skip):
    for root, dirs, files in os.walk(top):
        dirs[:] = [d for d in dirs if d not in skip]
        for n in files:
            yield os.path.join(root, n)


def pack_back(job, since):
    ws, home = job["ws"], os.path.expanduser("~")
    claude_dir = os.path.join(home, ".claude")
    buf = io.BytesIO()
    manifest = []
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        def add(p):
            try:
                if os.path.islink(p) or (os.path.getmtime(p) >= since and os.path.getsize(p) < 50_000_000):
                    t.add(p, arcname=p.lstrip("/"), recursive=False)
            except OSError:
                pass
        for p in walk(ws, SKIP):
            manifest.append(os.path.relpath(p, ws))
            add(p)
        for p in walk(claude_dir, CLAUDE_SKIP):
            add(p)
        for extra in job.get("extra_dirs", []):
            for p in walk(extra, SKIP):
                add(p)
        if os.path.exists(os.path.join(home, ".claude.json")):
            add(os.path.join(home, ".claude.json"))
        m = json.dumps(manifest).encode()
        ti = tarfile.TarInfo("__manifest__.json"); ti.size = len(m)
        t.addfile(ti, io.BytesIO(m))
    return buf.getvalue()


def main():
    threading.Thread(target=pusher, daemon=True).start()
    status("машина GitHub получила задачу")
    job = json.loads(call("GET", f"/relay/{JOB}/in"))
    home = job["home"]
    ensure_dir(home); ensure_dir(job["here"]); ensure_dir(job["ws"])
    os.environ["HOME"] = home
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(job["tar"])), mode="r:gz") as t:
        try:
            t.extractall("/", filter="tar")
        except TypeError:
            t.extractall("/")
    status("ставлю Claude Code")
    try:
        install_ecc(job["ecc_dir"])
    except Exception:
        pass
    claude = install_claude()
    sh("python3 -c 'import requests' || pip install -q requests")
    env = dict(os.environ)
    for k in ("ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "JOB", "RELAY"):
        env.pop(k, None)
    env.update(job["env"])
    env["HOME"] = home
    for n in ("tgctl", "rexec", "tgmedia", "tgbrowser"):  # contents API не хранит бит исполнения
        for d in (job["here"], os.path.dirname(os.path.abspath(__file__))):
            try:
                os.chmod(os.path.join(d, n), 0o755)
            except OSError:
                pass
    env["PATH"] = job["here"] + os.pathsep + os.path.join(home, ".local/bin") + os.pathsep + env.get("PATH", "")
    cmd = [claude] + job["cmd"][1:]
    threading.Thread(target=frames, daemon=True).start()
    status("Claude думает")
    since = time.time() - 2
    streaming = bool(job.get("first"))  # --input-format stream-json: задачу и уточнения пишем в stdin
    p = subprocess.Popen(cmd, cwd=job["ws"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         stdin=subprocess.PIPE if streaming else subprocess.DEVNULL, text=True, bufsize=1,
                         start_new_session=True)
    sent = {"n": 0, "ack": 0}
    wlock = threading.Lock()

    def write(obj):
        with wlock:
            try:
                p.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
                p.stdin.flush()
                return True
            except Exception:
                return False

    if streaming:
        write(job["first"])

        def feeder():
            while p.poll() is None:
                while inbox:
                    m = inbox.pop(0)
                    try:
                        obj = json.loads(m)
                    except ValueError:
                        continue
                    if write(obj):
                        sent["n"] += 1
                time.sleep(0.3)
        threading.Thread(target=feeder, daemon=True).start()

    def watch_stop():
        stop.wait()
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except Exception:
            pass
    threading.Thread(target=watch_stop, daemon=True).start()
    got_result = False
    marker = job.get("marker", "\u0000")
    for line in p.stdout:
        emit(line.rstrip("\n"))
        try:
            ev = json.loads(line)
        except Exception:
            ev = {}
        if ev.get("type") == "user" and not ev.get("parent_tool_use_id") and marker in json.dumps(ev, ensure_ascii=False):
            sent["ack"] += 1  # уточнение дошло до модели (--replay-user-messages)
        if ev.get("type") == "result":
            got_result = True
            time.sleep(1.5)  # вдруг как раз пришло уточнение
            if streaming and (inbox or sent["n"] > sent["ack"]):
                continue  # модель ещё ответит на уточнение
            if streaming:
                with wlock:
                    try:
                        p.stdin.close()
                    except Exception:
                        pass
            break
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    try:  # фоновые процессы модели не должны держать машину
        os.killpg(p.pid, signal.SIGKILL)
    except Exception:
        pass
    code = p.returncode if p.returncode is not None else (0 if got_result else -9)
    if stop.is_set():
        code = -15
    flush()
    try:
        call("PUT", f"/relay/{JOB}/out", pack_back(job, since), "application/gzip", timeout=300)
    except Exception as e:
        emit({"type": "_status", "text": f"не удалось вернуть файлы: {e}"})
    emit({"type": "_done", "code": code})
    flush()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            emit({"type": "_done", "code": 1, "error": f"runner: {type(e).__name__}: {e}"})
            flush()
        except Exception:
            pass
        sys.exit(1)
