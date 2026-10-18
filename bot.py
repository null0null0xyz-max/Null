"""Telegram-бот для Claude Code на Wispbyte.

- стриминг ответа в черновик (sendRichMessageDraft) + блок <tg-thinking> со статусами шагов
- нативная кнопка Stop (can_stop) и событие stopped_message_generation
- выбор модели, провайдеры (добавить/убрать/переключить), сессии с продолжением
- файлы проекта живут в Telegram-канале (tgfs.py), команды выполняются на Render (rexec)

Env: BOT_TOKEN, REXEC_URL, REXEC_TOKEN (OWNER_ID, CHANNEL_ID — необязательно, определяются сами)
     необязательно: CLAUDE_BIN (по умолчанию "claude"), ANTHROPIC_API_KEY
"""
from dotenv_min import load_env; load_env()
import os, re, json, time, html, threading, subprocess, itertools, requests
from tgfs import TgFS

TOKEN = os.environ["BOT_TOKEN"]
CLAUDE = os.environ.get("CLAUDE_BIN", "claude")
HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.join(HERE, "workspace")
CFG_PATH = os.path.join(HERE, "config.json")
API = f"https://api.telegram.org/bot{TOKEN}/"

DEFAULT_CFG = {
    "providers": {"anthropic": {"base_url": "", "token": "", "models": ["sonnet", "opus", "haiku"]}},
    "provider": "anthropic", "model": "sonnet", "session": None,
}
SYSTEM_HINT = (
    "Рабочая папка проекта — текущая директория. Файлы читай и правь здесь. "
    "Любые тяжёлые команды (установка пакетов, запуск, тесты, сборка) выполняй через "
    'Bash: rexec "команда" — она выполнится на удалённой машине с копией папки, '
    "изменённые файлы вернутся сюда. Отвечай по-русски."
)
TOOL_ICONS = {"Bash": "⚙️", "Read": "📖", "Write": "✏️", "Edit": "✏️", "MultiEdit": "✏️",
              "Grep": "🔎", "Glob": "🔎", "WebFetch": "🌐", "WebSearch": "🌐", "Task": "🧩", "TodoWrite": "📝"}

cfg = json.load(open(CFG_PATH)) if os.path.exists(CFG_PATH) else json.loads(json.dumps(DEFAULT_CFG))
# OWNER_ID / CHANNEL_ID можно не задавать: владелец = первый, кто написал /start,
# канал = первый канал, куда бота добавили админом. Оба запоминаются в config.json.
OWNER = int(os.environ.get("OWNER_ID") or cfg.get("owner") or 0)
CHANNEL = os.environ.get("CHANNEL_ID") or cfg.get("channel") or ""
fs = TgFS(TOKEN, CHANNEL or 0, WS, os.path.join(HERE, "index.json"))
fs_lock = threading.Lock()
draft_ids = itertools.count(int(time.time()) % 1_000_000)
runs = {}       # draft_id -> {"proc", "stopped"}
waiting = {}    # что бот ждёт от владельца текстом: "add_provider"


def save_cfg():
    json.dump(cfg, open(CFG_PATH, "w"), ensure_ascii=False, indent=1)


def tg(method, **data):
    try:
        return requests.post(API + method, json=data, timeout=60).json()
    except requests.RequestException as e:
        return {"ok": False, "description": str(e)}


# ---------------- форматирование ----------------
def md_to_html(md):
    """Минимальный Markdown -> HTML: ```код```, `код`, **жирный**, заголовки."""
    out, parts = [], re.split(r"```(\w*)\n?(.*?)```", md, flags=re.S)
    for i in range(0, len(parts), 3):
        t = html.escape(parts[i])
        t = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        t = re.sub(r"(?m)^#{1,6}\s*(.+)$", r"<b>\1</b>", t)
        out.append(t)
        if i + 2 < len(parts):
            lang = parts[i + 1]
            cls = f' class="language-{lang}"' if lang else ""
            out.append(f"<pre><code{cls}>{html.escape(parts[i + 2])}</code></pre>")
    return "".join(out).strip()


def chunks(text, n=3500):
    while text:
        cut = text.rfind("\n", 0, n) if len(text) > n else len(text)
        cut = cut if cut > 0 else n
        yield text[:cut]
        text = text[cut:].lstrip("\n")


def step_label(name, inp):
    arg = inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("url") or ""
    arg = os.path.relpath(arg, WS) if arg.startswith(WS) else arg
    arg = (arg[:48] + "…") if len(arg) > 48 else arg
    return f"{TOOL_ICONS.get(name, '🔧')} {name} {arg}".strip()


# ---------------- запуск Claude Code ----------------
def claude_env():
    env = dict(os.environ)
    env.update(WORKSPACE=WS, PATH=HERE + os.pathsep + env.get("PATH", ""))
    p = cfg["providers"][cfg["provider"]]
    if p.get("base_url"):
        env["ANTHROPIC_BASE_URL"] = p["base_url"]
        env["ANTHROPIC_AUTH_TOKEN"] = p["token"]
        env.pop("ANTHROPIC_API_KEY", None)
        env["ANTHROPIC_MODEL"] = cfg["model"]
        env["ANTHROPIC_SMALL_FAST_MODEL"] = cfg["model"]
        env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = cfg["model"]
    elif p.get("token"):
        env["ANTHROPIC_API_KEY"] = p["token"]
    return env


def run_claude(chat_id, prompt):
    did = next(draft_ids)
    started = time.time()
    st = {"steps": [], "text": "", "thinking": "", "result": "", "error": "", "dirty": True}
    cmd = [CLAUDE, "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--include-partial-messages", "--dangerously-skip-permissions",
           "--model", cfg["model"], "--append-system-prompt", SYSTEM_HINT]
    if cfg.get("session"):
        cmd += ["--resume", cfg["session"]]
    try:
        proc = subprocess.Popen(cmd, cwd=WS, env=claude_env(), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
    except FileNotFoundError:
        tg("sendMessage", chat_id=chat_id, text=f"Не найден {CLAUDE}. Проверь CLAUDE_BIN.")
        return
    runs[did] = {"proc": proc, "stopped": False}

    def draft_html():
        sec = int(time.time() - started)
        steps = "\n".join(html.escape(s) for s in st["steps"][-5:])
        status = f"Думаю · {sec} с" + (f"\n{steps}" if steps else "")
        if st["thinking"] and not st["text"]:
            status += "\n" + html.escape(st["thinking"][-300:])
        body = md_to_html(st["text"][-3000:]) if st["text"] else ""
        return f"<tg-thinking>{status}</tg-thinking>{body}"

    def drafter():
        # Плавное появление: частые обновления, пока идёт текст; не реже раза в 3 с (черновик живёт 30 с)
        last = 0
        while proc.poll() is None and not runs[did]["stopped"]:
            if st["dirty"] or time.time() - last > 3:
                st["dirty"] = False
                last = time.time()
                tg("sendRichMessageDraft", chat_id=chat_id, draft_id=did, can_stop=True,
                   rich_message={"html": draft_html()})
            time.sleep(0.7)

    threading.Thread(target=drafter, daemon=True).start()
    errlines = []

    for line in proc.stdout:
        try:
            ev = json.loads(line)
        except ValueError:
            errlines.append(line)
            continue
        t = ev.get("type")
        if t == "stream_event":
            e = ev.get("event", {})
            if e.get("type") == "content_block_delta":
                d = e.get("delta", {})
                if d.get("type") == "text_delta":
                    st["text"] += d.get("text", "")
                    st["dirty"] = True
                elif d.get("type") == "thinking_delta":
                    st["thinking"] += d.get("thinking", "")
                    st["dirty"] = True
        elif t == "assistant":
            for c in ev.get("message", {}).get("content", []):
                if c.get("type") == "tool_use":
                    st["steps"].append(step_label(c.get("name", ""), c.get("input") or {}))
                    st["text"] = ""  # между шагами начинается новый текст
                    st["dirty"] = True
        elif t == "result":
            st["result"] = ev.get("result") or st["text"]
            if ev.get("session_id"):
                cfg["session"] = ev["session_id"]
                save_cfg()
            if ev.get("is_error"):
                st["error"] = ev.get("subtype", "error")
    proc.wait()
    err = "".join(errlines)[-1500:]
    stopped = runs.pop(did)["stopped"]

    with fs_lock:
        try:
            added, removed = fs.sync()
        except Exception as e:
            added, removed = 0, 0
            err += f"\nСинхронизация с каналом: {e}"

    if stopped:
        tg("sendMessage", chat_id=chat_id, text="⏹ Остановлено.")
        return
    text = st["result"] or (f"Ошибка:\n```\n{err}\n```" if err else "(пустой ответ)")
    footer = f"\n\n<i>📁 файлов изменено: {added}, удалено: {removed}</i>" if added or removed else ""
    parts = list(chunks(text))
    for i, part in enumerate(parts):
        body = md_to_html(part) + (footer if i == len(parts) - 1 else "")
        r = tg("sendRichMessage", chat_id=chat_id, rich_message={"html": body})
        if not r.get("ok"):  # запасной путь для старых клиентов
            tg("sendMessage", chat_id=chat_id, text=body, parse_mode="HTML")


# ---------------- меню ----------------
def kb(rows):
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


def menu_main():
    p, m = cfg["provider"], cfg["model"]
    return (f"🤖 <b>Claude Code</b>\nПровайдер: <code>{html.escape(p)}</code>\nМодель: <code>{html.escape(m)}</code>",
            kb([[("🧠 Модель", "models"), ("🔌 Провайдеры", "provs")],
                [("🆕 Новая сессия", "new"), ("📁 Синхронизировать", "sync")]]))


def menu_models():
    models = cfg["providers"][cfg["provider"]]["models"]
    rows = [[(("✅ " if m == cfg["model"] else "") + m, "m:" + m)] for m in models]
    return "Выбери модель:", kb(rows + [[("⬅️ Назад", "main")]])


def menu_provs():
    rows = []
    for name in cfg["providers"]:
        mark = "✅ " if name == cfg["provider"] else ""
        row = [(mark + name, "p:" + name)]
        if name != "anthropic":
            row.append(("🗑", "del:" + name))
        rows.append(row)
    rows += [[("➕ Добавить", "add")], [("⬅️ Назад", "main")]]
    return "Провайдеры:", kb(rows)


ADD_HELP = ("Пришли одной строкой:\n<code>имя | base_url | токен | модель1, модель2</code>\n\n"
            "Пример: <code>myproxy | https://api.example.com | sk-xxx | claude-sonnet-4-5, glm-4.6</code>\n"
            "Сообщение с токеном я сразу удалю.")


def show(chat_id, msg_id, screen):
    text, markup = screen
    tg("editMessageText", chat_id=chat_id, message_id=msg_id, text=text,
       parse_mode="HTML", reply_markup=markup)


def on_callback(q):
    if q["from"]["id"] != OWNER:
        return tg("answerCallbackQuery", callback_query_id=q["id"], text="Нет доступа")
    d, chat, mid = q["data"], q["message"]["chat"]["id"], q["message"]["message_id"]
    note = ""
    if d == "main":
        show(chat, mid, menu_main())
    elif d == "models":
        show(chat, mid, menu_models())
    elif d == "provs":
        show(chat, mid, menu_provs())
    elif d.startswith("m:"):
        cfg["model"] = d[2:]; save_cfg(); note = "Модель: " + d[2:]
        show(chat, mid, menu_main())
    elif d.startswith("p:") and d[2:] in cfg["providers"]:
        cfg["provider"] = d[2:]
        cfg["model"] = cfg["providers"][d[2:]]["models"][0]
        cfg["session"] = None  # сессии разных провайдеров несовместимы
        save_cfg(); note = "Провайдер: " + d[2:]
        show(chat, mid, menu_main())
    elif d.startswith("del:"):
        cfg["providers"].pop(d[4:], None)
        if cfg["provider"] not in cfg["providers"]:
            cfg.update(provider="anthropic", model="sonnet", session=None)
        save_cfg(); note = "Удалён"
        show(chat, mid, menu_provs())
    elif d == "add":
        waiting[OWNER] = "add_provider"
        tg("sendMessage", chat_id=chat, text=ADD_HELP, parse_mode="HTML")
    elif d == "new":
        cfg["session"] = None; save_cfg(); note = "Новая сессия"
    elif d == "sync":
        with fs_lock:
            a, r = fs.sync()
        note = f"Синхронизировано: +{a} −{r}"
    tg("answerCallbackQuery", callback_query_id=q["id"], text=note)


def add_provider(msg):
    tg("deleteMessage", chat_id=msg["chat"]["id"], message_id=msg["message_id"])
    parts = [x.strip() for x in msg["text"].split("|")]
    if len(parts) != 4 or not parts[0] or not parts[1].startswith("http"):
        return tg("sendMessage", chat_id=msg["chat"]["id"], text="Не понял формат.\n\n" + ADD_HELP, parse_mode="HTML")
    name, url, token, models = parts
    models = [m.strip() for m in models.split(",") if m.strip()] or ["sonnet"]
    cfg["providers"][name] = {"base_url": url.rstrip("/"), "token": token, "models": models}
    save_cfg()
    text, markup = menu_provs()
    tg("sendMessage", chat_id=msg["chat"]["id"], text=f"✅ Добавлен {html.escape(name)}\n\n" + text,
       parse_mode="HTML", reply_markup=markup)


def on_message(msg):
    global OWNER
    if not OWNER and msg.get("chat", {}).get("type") == "private" and msg.get("text") == "/start":
        OWNER = cfg["owner"] = msg["from"]["id"]
        save_cfg()
        tg("sendMessage", chat_id=OWNER, text="👋 Ты владелец. Теперь добавь меня админом в приватный канал для файлов.")
    if msg.get("from", {}).get("id") != OWNER or not msg.get("text"):
        return
    chat, text = msg["chat"]["id"], msg["text"].strip()
    if waiting.pop(OWNER, None) == "add_provider":
        return add_provider(msg)
    if text in ("/start", "/menu"):
        t, m = menu_main()
        return tg("sendMessage", chat_id=chat, text=t, parse_mode="HTML", reply_markup=m)
    if text == "/new":
        cfg["session"] = None; save_cfg()
        return tg("sendMessage", chat_id=chat, text="🆕 Новая сессия")
    if text.startswith("/model "):
        cfg["model"] = text.split(None, 1)[1]; save_cfg()
        return tg("sendMessage", chat_id=chat, text="Модель: " + cfg["model"])
    if not CHANNEL:
        return tg("sendMessage", chat_id=chat, text="Сначала добавь меня админом в канал для файлов.")
    if runs:
        return tg("sendMessage", chat_id=chat, text="Ещё работаю над прошлой задачей — нажми Stop или подожди.")
    threading.Thread(target=run_claude, args=(chat, text), daemon=True).start()


def on_my_chat_member(u):
    global CHANNEL
    chat, new = u["chat"], u["new_chat_member"]
    if chat["type"] == "channel" and not CHANNEL and new["status"] == "administrator" \
            and u["from"]["id"] == OWNER:
        CHANNEL = cfg["channel"] = str(chat["id"])
        fs.cid = chat["id"]
        save_cfg()
        with fs_lock:
            fs.load(); fs.rebuild_nav()
        tg("sendMessage", chat_id=OWNER, text=f"📁 Канал «{chat.get('title', '')}» подключён. Пиши задачу!")


def ensure_claude():
    global CLAUDE
    os.chmod(os.path.join(HERE, "rexec"), 0o755)
    local = os.path.expanduser("~/.local/bin/claude")
    if subprocess.run(["sh", "-c", f"command -v {CLAUDE}"], capture_output=True).returncode == 0:
        return
    if not os.path.exists(local):
        print("installing Claude Code…", flush=True)
        subprocess.run(["sh", "-c", "curl -fsSL https://claude.ai/install.sh | bash"])
    if os.path.exists(local):
        CLAUDE = local


def main():
    save_cfg()
    ensure_claude()
    if CHANNEL:
        fs.load()
        fs.restore()
    tg("setMyCommands", commands=[{"command": "menu", "description": "Модель и провайдеры"},
                                  {"command": "new", "description": "Новая сессия"}])
    print("bot started", flush=True)
    offset = 0
    while True:
        upd = tg("getUpdates", offset=offset, timeout=50,
                 allowed_updates=["message", "callback_query", "channel_post", "my_chat_member",
                                  "stopped_message_generation"])
        if not upd.get("ok"):
            time.sleep(3)
            continue
        for u in upd["result"]:
            offset = u["update_id"] + 1
            try:
                if "stopped_message_generation" in u:  # нажата нативная кнопка Stop
                    r = runs.get(u["stopped_message_generation"].get("draft_id"))
                    if r:
                        r["stopped"] = True
                        r["proc"].terminate()
                elif "my_chat_member" in u:
                    on_my_chat_member(u["my_chat_member"])
                elif "callback_query" in u:
                    on_callback(u["callback_query"])
                elif "channel_post" in u and str(u["channel_post"]["chat"]["id"]) == str(CHANNEL):
                    with fs_lock:
                        fs.on_channel_post(u["channel_post"])
                elif "message" in u:
                    on_message(u["message"])
            except Exception as e:
                print("update error:", e, flush=True)


if __name__ == "__main__":
    main()
