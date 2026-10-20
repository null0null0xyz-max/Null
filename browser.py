#!/usr/bin/env python3
"""Настоящий Chromium в облаке (Steel.dev) для моделей. Владелец получает ссылку и смотрит вживую.

CLI (команда tgbrowser):
  tgbrowser open [URL]        — запустить браузер (или взять уже открытый), владельцу уйдёт ссылка «смотреть»
  tgbrowser goto URL          — открыть страницу
  tgbrowser read              — текст страницы + пронумерованные ссылки/кнопки/поля [N]
  tgbrowser click N|"css"|X,Y — нажать элемент по номеру из read, CSS-селектору или координатам
  tgbrowser type N|"css" ТЕКСТ [--enter]  — ввести текст в поле
  tgbrowser key Enter|Tab|Escape|PageDown
  tgbrowser scroll [down|up]
  tgbrowser shot [файл.png]   — скриншот (покажи владельцу: ![](file:путь))
  tgbrowser eval "JS"         — выполнить JavaScript, вернуть результат
  tgbrowser tabs | tab N      — вкладки
  tgbrowser link              — ссылка для просмотра ещё раз
  tgbrowser close             — закрыть браузер (куки и входы сохранятся для следующего раза)
Env: STEEL_API_KEY. Куки/логины сохраняются между сессиями (browser/context.json).
"""
import os, re, sys, json, time, base64, subprocess

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(HERE, "browser")
STATE = os.path.join(DIR, "state.json")
CTX = os.path.join(DIR, "context.json")
API = "https://api.steel.dev/v1"


def key():
    k = os.environ.get("STEEL_API_KEY", "")
    if not k:
        raise SystemExit("нет STEEL_API_KEY — браузер не настроен")
    return k


def H():
    return {"steel-api-key": key(), "Content-Type": "application/json"}


def load():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def save(st):
    os.makedirs(DIR, exist_ok=True)
    json.dump(st, open(STATE, "w"))


def notify(text, url=None):
    tok, chat = os.environ.get("BOT_TOKEN"), os.environ.get("TG_CHAT")
    if not tok or not chat:
        return
    data = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
    if url:
        data["reply_markup"] = {"inline_keyboard": [[{"text": "👁 Смотреть браузер вживую", "url": url}]]}
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", json=data, timeout=20)
    except Exception:
        pass


def alive(st):
    if not st.get("id"):
        return False
    try:
        r = requests.get(f"{API}/sessions/{st['id']}", headers=H(), timeout=20)
        return r.ok and r.json().get("status") in ("live", "idle", None)
    except Exception:
        return False


def open_session(quiet=False):
    st = load()
    if alive(st):
        return st
    body = {"timeout": 15 * 60 * 1000, "blockAds": True, "dimensions": {"width": 1280, "height": 800}}
    if os.path.exists(CTX):
        try:
            body["sessionContext"] = json.load(open(CTX))
        except Exception:
            pass
    r = requests.post(f"{API}/sessions", headers=H(), json=body, timeout=60)
    if not r.ok and "sessionContext" in body:  # контекст устарел — без него
        body.pop("sessionContext")
        r = requests.post(f"{API}/sessions", headers=H(), json=body, timeout=60)
    if not r.ok:
        raise SystemExit(f"Steel: {r.status_code} {r.text[:300]}")
    j = r.json()
    st = {"id": j["id"], "ws": j.get("websocketUrl") or f"wss://connect.steel.dev?apiKey={key()}&sessionId={j['id']}",
          "view": j.get("sessionViewerUrl") or j.get("debugUrl"), "debug": j.get("debugUrl"), "t": time.time()}
    if "apiKey=" not in st["ws"]:
        st["ws"] += ("&" if "?" in st["ws"] else "?") + f"apiKey={key()}&sessionId={j['id']}"
    save(st)
    if not quiet:
        notify("🌐 Модель открыла браузер — можно смотреть, что она делает.", st["view"])
    return st


# ---------------- минимальный CDP ----------------
class CDP:
    def __init__(self, ws_url):
        try:
            import websocket
        except ImportError:
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "websocket-client"], check=False)
            import websocket
        self.ws = websocket.create_connection(ws_url, timeout=60, suppress_origin=True)
        self.n = 0
        self.sid = None

    def send(self, method, params=None, session=True, timeout=60):
        self.n += 1
        msg = {"id": self.n, "method": method, "params": params or {}}
        if session and self.sid:
            msg["sessionId"] = self.sid
        self.ws.send(json.dumps(msg))
        t0 = time.time()
        while time.time() - t0 < timeout:
            m = json.loads(self.ws.recv())
            if m.get("id") == self.n:
                if "error" in m:
                    raise RuntimeError(f"{method}: {m['error'].get('message')}")
                return m.get("result", {})
        raise RuntimeError(f"{method}: таймаут")

    def pages(self):
        return [t for t in self.send("Target.getTargets", session=False)["targetInfos"] if t["type"] == "page"]

    def attach(self, index=None):
        st = load()
        pages = self.pages()
        if not pages:
            tid = self.send("Target.createTarget", {"url": "about:blank"}, session=False)["targetId"]
        else:
            i = index if index is not None else st.get("tab", len(pages) - 1)
            tid = pages[min(i, len(pages) - 1)]["targetId"]
        self.sid = self.send("Target.attachToTarget", {"targetId": tid, "flatten": True}, session=False)["sessionId"]
        self.send("Page.enable")
        self.send("Runtime.enable")
        return tid

    def js(self, expr, timeout=60):
        r = self.send("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True}, timeout=timeout)
        if r.get("exceptionDetails"):
            raise RuntimeError("JS: " + str(r["exceptionDetails"].get("exception", {}).get("description", ""))[:400])
        return r.get("result", {}).get("value")

    def wait_load(self, t=25):
        t0 = time.time()
        while time.time() - t0 < t:
            try:
                if self.js("document.readyState") == "complete":
                    return
            except Exception:
                pass
            time.sleep(0.5)


READ_JS = r"""(() => {
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none' && r.bottom > 0 && r.top < innerHeight * 3; };
  let n = 0; const items = [];
  document.querySelectorAll('[data-tgref]').forEach(e => e.removeAttribute('data-tgref'));
  document.querySelectorAll('a[href],button,input,textarea,select,[role=button],[role=link],[onclick],[contenteditable=true]').forEach(e => {
    if (!vis(e) || n >= 150) return; n++; e.setAttribute('data-tgref', n);
    const t = (e.innerText || e.value || e.placeholder || e.getAttribute('aria-label') || e.title || e.name || '').trim().replace(/\s+/g,' ').slice(0, 80);
    const tag = e.tagName.toLowerCase(); const type = e.type ? ':' + e.type : '';
    items.push(`[${n}] ${tag}${type} ${t}` + (tag === 'a' ? ' → ' + (e.getAttribute('href') || '').slice(0, 80) : ''));
  });
  const text = (document.body ? document.body.innerText : '').replace(/\n{3,}/g, '\n\n').slice(0, 12000);
  return `URL: ${location.href}\nЗаголовок: ${document.title}\n\n${text}\n\n--- Элементы (для click/type) ---\n${items.join('\n')}`;
})()"""


def target_expr(t):
    if re.fullmatch(r"\d+", t):
        return f"document.querySelector('[data-tgref=\"{t}\"]')"
    return f"document.querySelector({json.dumps(t)})"


KEYS = {"Enter": (13, "\r"), "Tab": (9, "\t"), "Escape": (27, ""), "Backspace": (8, ""), "PageDown": (34, ""),
        "PageUp": (33, ""), "ArrowDown": (40, ""), "ArrowUp": (38, "")}


def press(c, name):
    code, text = KEYS.get(name, (0, ""))
    base = {"key": name, "code": name, "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code}
    c.send("Input.dispatchKeyEvent", dict(base, type="keyDown", **({"text": text} if text else {})))
    c.send("Input.dispatchKeyEvent", dict(base, type="keyUp"))


def run(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        return __doc__
    cmd, args = argv[0], argv[1:]
    if cmd == "close":
        st = load()
        if st.get("id"):
            try:  # сохраняем куки/входы на следующий раз
                ctx = requests.get(f"{API}/sessions/{st['id']}/context", headers=H(), timeout=30)
                if ctx.ok and ctx.json():
                    os.makedirs(DIR, exist_ok=True)
                    json.dump(ctx.json(), open(CTX, "w"))
            except Exception:
                pass
            requests.post(f"{API}/sessions/{st['id']}/release", headers=H(), timeout=30)
        save({})
        return "браузер закрыт, куки сохранены"
    if cmd == "link":
        st = open_session(quiet=True)
        notify("🌐 Браузер модели:", st["view"])
        return st["view"]
    st = open_session()
    c = CDP(st["ws"])
    try:
        if cmd == "tab":
            st["tab"] = int(args[0]) if args else 0
            save(st)
        c.attach()
        if cmd == "open":
            if args:
                c.send("Page.navigate", {"url": args[0]})
                c.wait_load()
            return f"браузер открыт, владелец видит его по ссылке: {st['view']}\n" + (c.js("location.href") or "")
        if cmd == "goto":
            url = args[0] if re.match(r"^\w+://", args[0]) else "https://" + args[0]
            c.send("Page.navigate", {"url": url})
            c.wait_load()
            time.sleep(1)
            return c.js(READ_JS)
        if cmd == "read":
            return c.js(READ_JS)
        if cmd == "click":
            t = args[0]
            if re.fullmatch(r"\d+,\d+", t):
                x, y = map(int, t.split(","))
                for typ in ("mousePressed", "mouseReleased"):
                    c.send("Input.dispatchMouseEvent", {"type": typ, "x": x, "y": y, "button": "left", "clickCount": 1})
            else:
                ok = c.js(f"(() => {{ const e = {target_expr(t)}; if (!e) return false; e.scrollIntoView({{block:'center'}}); e.click(); return true; }})()")
                if not ok:
                    return f"элемент {t} не найден — сначала read"
            time.sleep(1.5)
            c.wait_load(10)
            return "нажато. Сейчас: " + (c.js("location.href") or "")
        if cmd == "type":
            enter = "--enter" in args
            args = [a for a in args if a != "--enter"]
            t, text = args[0], " ".join(args[1:])
            ok = c.js(f"(() => {{ const e = {target_expr(t)}; if (!e) return false; e.scrollIntoView({{block:'center'}}); e.focus(); "
                      f"if ('value' in e) {{ e.value = ''; }} return true; }})()")
            if not ok:
                return f"поле {t} не найдено — сначала read"
            c.send("Input.insertText", {"text": text})
            if enter:
                press(c, "Enter")
                time.sleep(1.5)
                c.wait_load(10)
            return "введено" + (", Enter" if enter else "")
        if cmd == "key":
            press(c, args[0] if args else "Enter")
            time.sleep(1)
            return "ок"
        if cmd == "scroll":
            c.js(f"window.scrollBy(0, {'-' if args and args[0] == 'up' else ''}innerHeight*0.85)")
            return "прокручено"
        if cmd == "shot":
            data = c.send("Page.captureScreenshot", {"format": "png"})["data"]
            path = args[0] if args else os.path.join(os.environ.get("WORKSPACE") or HERE, "media",
                                                     time.strftime("shot-%m%d-%H%M%S.png"))
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            open(path, "wb").write(base64.b64decode(data))
            return path
        if cmd == "eval":
            return json.dumps(c.js(" ".join(args)), ensure_ascii=False)[:12000]
        if cmd in ("tabs", "tab"):
            return "\n".join(f"[{i}] {p.get('title', '')[:60]} — {p.get('url', '')[:100]}" for i, p in enumerate(c.pages()))
        return "неизвестная команда. tgbrowser help"
    finally:
        try:
            c.ws.close()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        print(run(sys.argv[1:]))
    except SystemExit as e:
        print(e, file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print("ошибка:", e, file=sys.stderr)
        sys.exit(1)
