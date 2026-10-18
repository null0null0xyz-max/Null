"""Telegram-бот для Claude Code на Wispbyte.

- стриминг ответа в черновик (sendRichMessageDraft) + блок <tg-thinking> со статусами шагов
- нативная кнопка Stop (can_stop) и событие stopped_message_generation
- выбор модели, провайдеры (добавить/убрать/переключить), сессии с продолжением
- файлы проекта живут в Telegram-канале (tgfs.py), команды выполняются на Render (rexec)

Env: BOT_TOKEN, REXEC_URL, REXEC_TOKEN (OWNER_ID, CHANNEL_ID — необязательно, определяются сами)
     необязательно: CLAUDE_BIN (по умолчанию "claude"), ANTHROPIC_API_KEY
"""
from dotenv_min import load_env; load_env()
import os, re, io, json, glob, time, html, queue, base64, shutil, secrets, tarfile, mimetypes, threading, subprocess, itertools, requests
import importlib.machinery, importlib.util
from tgfs import TgFS
import fast


def _load_tgctl():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tgctl")
    loader = importlib.machinery.SourceFileLoader("tgctl", p)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("tgctl", loader))
    loader.exec_module(mod)
    return mod


tgctl = _load_tgctl()

TOKEN = os.environ["BOT_TOKEN"]
CLAUDE = os.environ.get("CLAUDE_BIN", "claude")
HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.join(HERE, "workspace")
CFG_PATH = os.path.join(HERE, "config.json")
API = f"https://api.telegram.org/bot{TOKEN}/"

DEFAULT_CFG = {
    "providers": {"anthropic": {"base_url": "", "token": "", "models": ["sonnet", "opus", "haiku"]}},
    "provider": "anthropic", "model": "sonnet", "chats": {},
    "effort": "", "mode": "bypass", "ecc_skills": True,
}
EFFORTS = ["low", "medium", "high", "xhigh", "max", "ultrathink"]
MODES = {"bypass": "🚀 Всё разрешено", "plan": "📋 План (только чтение)", "acceptEdits": "✏️ Правки без Bash"}
CLAUDE_DIR = os.path.expanduser("~/.claude")
ECC_URL = "https://codeload.github.com/affaan-m/ECC/tar.gz/refs/heads/main"
ECC_PARTS = ("commands/", "agents/", "skills/", "rules/common/")
# команды Claude Code, которые бот передаёт в Claude как есть (работают в -p)
PASS = {"compact": "/compact", "context": "/context", "init": "/init",
        "review": "/review", "security_review": "/security-review"}
BUILTIN = [("menu", "Модель, провайдеры, effort, режим"), ("new", "Новая сессия"),
           ("clear", "Очистить чат и удалить сессию"), ("resume", "Прошлые сессии"),
           ("effort", "Effort: low/medium/high/xhigh/max"), ("model", "Сменить модель"),
           ("mode", "Режим разрешений (план / всё)"), ("plan", "Включить/выключить режим плана"),
           ("compact", "Сжать контекст сессии"), ("context", "Сколько занято контекста"),
           ("init", "Создать CLAUDE.md для проекта"),
           ("review", "Ревью кода"), ("security_review", "Проверка безопасности"),
           ("stop", "Остановить задачу"), ("fast", "Быстрые ответы: вкл/выкл"), ("where", "Где работает Claude: GitHub или здесь"), ("status", "Состояние бота и сервера"),
           ("files", "Файлы проекта"), ("usage", "Кредиты, токены, остаток"),
           ("agents", "Чаты агентов последней задачи"), ("skills", "Навыки"),
           ("repair", "Восстановить оглавление и index в канале"),
           ("chats", "Группы и каналы, где я отвечаю"), ("prompt", "Инструкции для этого чата"),
           ("ecc", "Команды Everything Claude Code"),
           ("help", "Справка")]
TOOL_ICONS = {"Bash": "⚙️", "Read": "📖", "Write": "✏️", "Edit": "✏️", "MultiEdit": "✏️",
              "Grep": "🔎", "Glob": "🔎", "WebFetch": "🌐", "WebSearch": "🌐", "Task": "🧩", "Agent": "🧩", "Skill": "🧠", "TodoWrite": "📝"}

cfg = json.load(open(CFG_PATH)) if os.path.exists(CFG_PATH) else {}
for _k, _v in json.loads(json.dumps(DEFAULT_CFG)).items():
    cfg.setdefault(_k, _v)
# OWNER_ID / CHANNEL_ID можно не задавать: владелец = первый, кто написал /start,
# канал = первый канал, куда бота добавили админом. Оба запоминаются в config.json.
OWNER = int(os.environ.get("OWNER_ID") or cfg.get("owner") or 0)
CHANNEL = os.environ.get("CHANNEL_ID") or cfg.get("channel") or ""
fs = TgFS(TOKEN, CHANNEL or 0, WS, os.path.join(HERE, "index.json"))
fs_lock = threading.Lock()
draft_ids = itertools.count(int(time.time()) % 1_000_000)
runs = {}       # draft_id -> {"proc", "stopped"}
waiting = {}    # что бот ждёт от владельца текстом: "add_provider"
ecc_cmds = {}   # имя команды ECC -> описание
media_buf = {}  # media_group_id -> собранные файлы


def save_cfg():
    json.dump(cfg, open(CFG_PATH, "w"), ensure_ascii=False, indent=1)


def tg(method, **data):
    try:
        r = requests.post(API + method, json=data, timeout=60).json()
    except requests.RequestException as e:
        return {"ok": False, "description": str(e)}
    res = r.get("result")
    if r.get("ok") and method.startswith("send") and isinstance(res, dict) and res.get("message_id") \
            and str(data.get("chat_id")) != str(CHANNEL):
        tgctl.log_msg(data.get("chat_id"), res["message_id"], "bot", data.get("text") or "[" + method[4:] + "]")
    return r


CHAT_DEFAULT = {"session": None, "history": [], "access": "owner", "reply": "mention", "trust": False,
                "title": "", "type": ""}


def C(chat):
    """Настройки и сессии конкретного чата (личка, группа, канал)."""
    ch = cfg.setdefault("chats", {}).setdefault(str(chat), {})
    for k, v in CHAT_DEFAULT.items():
        ch.setdefault(k, list(v) if isinstance(v, list) else v)
    return ch


def chat_prompt(chat, value=None):
    p = os.path.join(tgctl.PROMPT_DIR, f"{chat}.txt")
    if value is not None:
        os.makedirs(tgctl.PROMPT_DIR, exist_ok=True)
        if value:
            open(p, "w", encoding="utf-8").write(value)
        elif os.path.exists(p):
            os.remove(p)
    return open(p, encoding="utf-8").read().strip() if os.path.exists(p) else ""


def session_file(sid):
    return glob.glob(os.path.join(CLAUDE_DIR, "projects", "*", f"{sid}.jsonl"))


def delete_session(chat, sid):
    for f in session_file(sid):
        try:
            os.remove(f)
        except OSError:
            pass
    ch = C(chat)
    ch["history"] = [h for h in ch["history"] if h["id"] != sid]
    if ch["session"] == sid:
        ch["session"] = None
    save_cfg()


def clear_chat(chat):
    """Удалить сообщения чата (за 48 ч, что успел записать бот) и текущую сессию."""
    fast.clear_hist(HERE, chat)
    p = os.path.join(tgctl.LOG_DIR, f"{chat}.jsonl")
    ids = []
    if os.path.exists(p):
        now = time.time()
        for line in open(p, encoding="utf-8"):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if now - e.get("ts", 0) < 47 * 3600 and isinstance(e.get("id"), int):
                ids.append(e["id"])
        os.remove(p)
    for i in range(0, len(ids), 100):
        tg("deleteMessages", chat_id=chat, message_ids=ids[i:i + 100])
    sid = C(chat)["session"]
    if sid:
        delete_session(chat, sid)
    return len(ids)


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
    arg = inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("url") \
        or inp.get("description") or inp.get("skill") or ""
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
        for k in ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                  "ANTHROPIC_DEFAULT_OPUS_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
            env[k] = cfg["model"]  # агенты ECC просят sonnet/opus — отдаём модель провайдера
    elif p.get("token"):
        env["ANTHROPIC_API_KEY"] = p["token"]
    return env


QUOTA_PER_USD = 500000  # new-api / one-api: 500 000 единиц квоты = $1
last_agents = {}        # агенты последней задачи — для /agents


def provider_balance():
    """Остаток у провайдера (new-api/one-api совместимые: /api/usage/token/). None, если не умеет."""
    p = cfg["providers"][cfg["provider"]]
    if not p.get("base_url") or not p.get("token"):
        return None
    try:
        j = requests.get(p["base_url"] + "/api/usage/token/", timeout=10,
                         headers={"Authorization": "Bearer " + p["token"]}).json()
        d = j.get("data") or {}
        if "total_available" in d:
            return {"left": d["total_available"] / QUOTA_PER_USD, "used": d.get("total_used", 0) / QUOTA_PER_USD,
                    "unlimited": d.get("unlimited_quota", False)}
    except Exception:
        pass
    return None


def fmt_k(n):
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def bq(title, body, limit=3300):
    """Сворачиваемая цитата: в чате видно 3 строки, по нажатию раскрывается."""
    body = body if len(body) <= limit else "…" + body[-limit:]
    return f"<blockquote expandable>{title}\n{html.escape(body)}</blockquote>"


def agents_md():
    return "\n\n".join(details(f"🧩 {a['name']}", "\n\n".join(a["log"])[-6000:] or "—") for a in last_agents.values())


def system_hint(chat, guest, who, mid):
    ch = C(chat)
    where = "личный чат с владельцем" if int(chat) > 0 else f"{ch['type']} «{ch['title']}»"
    h = [f"Ты — Telegram-бот @{BOT_USERNAME} на базе Claude Code. Сейчас ты в чате: {where} (chat_id {chat}). "
         f"Пишет: {who} (message_id {mid}). Отвечай по-русски, если пользователь не пишет на другом языке.",
         "Твой ответ уйдёт в чат rich-сообщением в формате Markdown (GFM): можно заголовки #, таблицы, "
         "блоки кода с языком, формулы $x^2$ и $$...$$, списки задач, ==выделение==, ||спойлер||, "
         "<details><summary>Заголовок</summary>...</details> для сворачиваемых блоков. "
         "Оформляй ответы, когда это помогает читать."]
    if not guest:
        h += ["Рабочая папка проекта — текущая директория, файлы читай и правь здесь; можно создавать и удалять "
              "файлы и папки — изменения сами уйдут в канал-хранилище. Тяжёлые команды (установка пакетов, запуск, "
              'тесты, сборка) выполняй через Bash: rexec "команда" — на удалённой машине с копией папки.',
              "Сайты и веб-приложения НЕ запускай на этой машине и не поднимай туннели (cloudflared, ngrok) и не "
              "давай IP-адреса. Статический сайт: положи в sites/<имя>/ (index.html и файлы) и выполни "
              f"rexec publish — ссылка будет {os.environ.get('REXEC_URL', '').rstrip('/')}/s/<имя>/ . "
              "Сервер с бэкендом: запусти в фоне на Render, например "
              'rexec "cd sites/app && nohup python3 -m http.server 8000 > app.log 2>&1 &", порт 3000–9999 — '
              f"ссылка {os.environ.get('REXEC_URL', '').rstrip('/')}/p/8000/ (пути в приложении делай относительными). "
              "Render — это Linux с bash; rexec выполняет команду в bash. Он может уснуть — тогда просто снова rexec publish.",
              "Сайты и веб-приложения НЕ запускай на этой машине и не поднимай туннели (cloudflared, ngrok) и не "
              "давай IP-адреса. Статический сайт: положи в sites/<имя>/ (index.html и файлы) и выполни "
              f"rexec publish — ссылка будет {os.environ.get('REXEC_URL', '').rstrip('/')}/s/<имя>/ . "
              "Сервер с бэкендом: запусти в фоне на Render, например "
              'rexec "cd sites/app && nohup python3 -m http.server 8000 > app.log 2>&1 &", порт 3000–9999 — '
              f"ссылка {os.environ.get('REXEC_URL', '').rstrip('/')}/p/8000/ (пути в приложении делай относительными). "
              "Render — это Linux с bash; rexec выполняет команду в bash. Он может уснуть — тогда просто снова rexec publish.",
              "Графики и картинки: построй PNG (например matplotlib через rexec или локально) в папке проекта "
              "и вставь в ответ строкой ![подпись](file:путь/к/файлу.png) — он прикрепится к сообщению.",
              "Чатом управляешь через Bash-команду tgctl: tgctl history 30 (последние сообщения с id), "
              'tgctl send "markdown", tgctl photo путь, tgctl file путь, tgctl pin ID, tgctl unpin ID|all, '
              'tgctl delete ID..., tgctl edit ID "текст", tgctl react ID 👍, tgctl prompt "постоянные инструкции '
              "для этого чата\", tgctl api МЕТОД '{json}'. В канал-хранилище ничего не отправляй — только файлы, "
              "их синхронизирует бот.",
              "Навыки: ~/.claude/skills. По просьбе или когда задача повторяется — создай навык "
              "~/.claude/skills/<имя>/SKILL.md (frontmatter name, description, дальше инструкция). "
              "Новая команда бота: ~/.claude/commands/<имя>.md (frontmatter description, дальше промпт; "
              "$ARGUMENTS — аргументы) — бот сам добавит её в меню как /<имя>."]
    else:
        h.append("Это гость: доступа к серверу и файлам у тебя нет, просто отвечай в чате.")
    p = chat_prompt(chat)
    if p:
        h.append("Инструкции владельца для этого чата: " + p)
    return "\n".join(h)


def run_claude(chat_id, prompt, guest=False, who="владелец", mid=None):
    did = next(draft_ids)
    started = time.time()
    st = {"steps": [], "text": "", "thinking": "", "result": "", "error": "", "dirty": True, "res": {}}
    agents = {}  # tool_use_id агента -> {"name", "log": [], "last": ""}
    ch = C(chat_id)
    private = int(chat_id) > 0
    bal0 = provider_balance()
    cmd = [CLAUDE, "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--include-partial-messages", "--forward-subagent-text",
           "--model", cfg["model"], "--append-system-prompt", system_hint(chat_id, guest, who, mid)]
    mode = cfg.get("mode", "bypass")
    if guest:
        cmd += ["--allowedTools", "WebSearch WebFetch",
                "--disallowedTools", "Bash Edit Write MultiEdit NotebookEdit Read Grep Glob Task Agent"]
    else:
        cmd += ["--dangerously-skip-permissions"] if mode == "bypass" else ["--permission-mode", mode]
    if cfg.get("effort") == "ultrathink":  # максимум размышлений: effort max + ключевое слово ultrathink
        cmd += ["--effort", "max"]
        cmd[2] = "ultrathink. " + cmd[2]
    elif cfg.get("effort"):
        cmd += ["--effort", cfg["effort"]]
    new_session = not ch["session"]
    if ch["session"]:
        cmd += ["--resume", ch["session"]]
    env = claude_env()
    env.update(TG_CHAT=str(chat_id), TG_MSG=str(mid or ""), TG_STORAGE=str(CHANNEL))
    # Память: на 512 МБ Claude Code (node) мог съесть всё, и Wispbyte гасил весь сервер.
    # Ограничиваем кучу node, а ядру говорим убивать при нехватке в первую очередь Claude, а не бота.
    heap = os.environ.get("CLAUDE_HEAP_MB", "320")
    if "max-old-space-size" not in env.get("NODE_OPTIONS", ""):
        env["NODE_OPTIONS"] = (env.get("NODE_OPTIONS", "") + f" --max-old-space-size={heap}").strip()
    try:
        if remote_on():
            proc = RemoteProc(cmd, env, ch["session"])
        else:
            proc = subprocess.Popen(cmd, cwd=WS, env=env, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True,
                                    preexec_fn=oom_first)
    except FileNotFoundError:
        tg("sendMessage", chat_id=chat_id, text=f"Не найден {CLAUDE}. Проверь CLAUDE_BIN.")
        return
    except Exception as e:
        tg("sendMessage", chat_id=chat_id, text=f"Не удалось запустить Claude: {e}")
        return
    runs[did] = {"proc": proc, "stopped": False, "chat": chat_id}

    def draft_md():
        sec = int(time.time() - started)
        lines = [f"Думаю · {sec} с · шагов {len(st['steps'])}"] + st["steps"][-5:]
        if st.get("phase") and not st["steps"] and not st["text"]:
            lines.append("☁️ " + st["phase"])
        for a in list(agents.values())[-3:]:
            lines.append(f"🧩 {a['name']}: {a['last'][:70] or 'работает…'}")
        if st["thinking"] and not st["text"]:
            lines.append(st["thinking"][-300:])
        status = "<br>".join(html.escape(x) for x in lines)
        return f"<tg-thinking>{status}</tg-thinking>\n\n" + safe_tail(st["text"])

    def drafter():
        # Плавное появление: частые обновления, пока идёт текст; не реже раза в 3 с (черновик живёт 30 с)
        last = 0
        while proc.poll() is None and not runs[did]["stopped"] and not st.get("done"):
            if not private:
                tg("sendChatAction", chat_id=chat_id, action="typing")
                time.sleep(4)
                continue
            if (st["dirty"] and time.time() - last > 1.5) or time.time() - last > 3:
                st["dirty"] = False
                last = time.time()
                r = tg("sendRichMessageDraft", chat_id=chat_id, draft_id=did, can_stop=True,
                       rich_message={"markdown": draft_md()})
                if not r.get("ok") and "parse" in (r.get("description") or "").lower():
                    tg("sendRichMessageDraft", chat_id=chat_id, draft_id=did, can_stop=True,
                       rich_message={"html": "<tg-thinking>" + html.escape(st["steps"][-1] if st["steps"] else "Думаю…")
                                     + "</tg-thinking>"})
            time.sleep(0.5)

    threading.Thread(target=drafter, daemon=True).start()
    threading.Thread(target=cpu_throttle, args=(proc, runs[did]), daemon=True).start()
    errlines = []

    for line in proc.stdout:
        try:
            ev = json.loads(line)
        except ValueError:
            errlines.append(line)
            continue
        t, pid = ev.get("type"), ev.get("parent_tool_use_id")
        if t == "_status":
            st["phase"] = ev.get("text", "")
            st["dirty"] = True
            continue
        if t == "stream_event" and not pid:
            e = ev.get("event", {})
            if e.get("type") == "content_block_delta":
                d = e.get("delta", {})
                if d.get("type") == "text_delta":
                    st["text"] += d.get("text", "")
                    st["dirty"] = True
                elif d.get("type") == "thinking_delta":
                    st["thinking"] += d.get("thinking", "")
                    st["dirty"] = True
        elif t == "assistant" and pid:  # сообщение субагента
            a = agents.setdefault(pid, {"name": "агент", "log": [], "last": ""})
            for c in ev.get("message", {}).get("content", []):
                if c.get("type") == "text" and c.get("text", "").strip():
                    a["log"].append(c["text"].strip())
                    a["last"] = c["text"].strip().splitlines()[0]
                elif c.get("type") == "thinking" and c.get("thinking"):
                    a["log"].append("💭 " + c["thinking"].strip()[:600])
                elif c.get("type") == "tool_use":
                    lbl = step_label(c.get("name", ""), c.get("input") or {})
                    a["log"].append("→ " + lbl)
                    a["last"] = lbl
            st["dirty"] = True
        elif t == "assistant":
            for c in ev.get("message", {}).get("content", []):
                if c.get("type") == "tool_use":
                    inp = c.get("input") or {}
                    if c.get("name") in ("Task", "Agent"):
                        name = inp.get("subagent_type") or "агент"
                        if inp.get("description"):
                            name += " · " + inp["description"][:40]
                        agents[c.get("id")] = {"name": name, "log": [], "last": ""}
                    st["steps"].append(step_label(c.get("name", ""), inp))
                    st["text"] = ""  # между шагами начинается новый текст
                    st["dirty"] = True
        elif t == "result":
            st["result"] = ev.get("result") or st["text"]
            st["res"] = ev
            if ev.get("session_id"):
                ch["session"] = ev["session_id"]
                if new_session:
                    title = re.sub(r"\s+", " ", prompt.split("\n\nПриложены файлы")[0])[:40]
                    ch["history"] = ([{"id": ev["session_id"], "title": title, "ts": int(time.time())}]
                                     + [h for h in ch["history"] if h["id"] != ev["session_id"]])[:10]
                save_cfg()
            if ev.get("is_error"):
                st["error"] = ev.get("subtype", "error")
            # Ответ готов. Дальше не ждём: фоновые процессы (сервер, туннель), запущенные моделью,
            # держат stdout открытым — из-за этого задача «не заканчивалась».
            st["done"] = True
            break
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.stdout.close()
    except Exception:
        pass
    err = "".join(errlines)[-1500:]
    if proc.returncode in (-9, 137) and not st["result"]:
        err = ("Claude Code был убит из-за нехватки памяти на сервере. Бот работает. Попробуй ещё раз; "
               "тяжёлые команды (сборки, npm, pip, сервера) пусть выполняются через rexec на Render.\n" + err)
    elif "heap out of memory" in err.lower() and not st["result"]:
        err = "Claude Code упёрся в лимит памяти (CLAUDE_HEAP_MB). Начни новую сессию /new или увеличь лимит.\n" + err
    stopped = runs.pop(did)["stopped"]
    last_agents.clear()
    last_agents.update(agents)

    with fs_lock:
        try:
            added, removed = fs.sync()
        except Exception as e:
            added, removed = 0, 0
            err += f"\nСинхронизация с каналом: {e}"

    # ---- статистика: токены, время, кредиты
    sec = int(time.time() - started)
    res, usage = st["res"], st["res"].get("usage") or {}
    tin = usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0) + \
        usage.get("cache_creation_input_tokens", 0)
    tout = usage.get("output_tokens", 0)
    bal1 = provider_balance() if bal0 else None
    spent = round(bal0["left"] - bal1["left"], 4) if bal0 and bal1 else None
    cost = spent if spent is not None else res.get("total_cost_usd")
    stats = cfg.setdefault("stats", {"cost": 0.0, "in": 0, "out": 0, "runs": 0})
    stats["cost"] += cost or 0; stats["in"] += tin; stats["out"] += tout; stats["runs"] += 1
    cfg["last_run"] = {"cost": cost, "real": spent is not None, "in": tin, "out": tout, "sec": sec,
                       "turns": res.get("num_turns", 0), "left": bal1["left"] if bal1 else None}
    save_cfg()
    info = [f"⏱ {sec} с", f"🔁 {res.get('num_turns', 0)}", f"🔤 {fmt_k(tin)} → {fmt_k(tout)}"]
    if cost is not None:
        info.append(f"💰 ${cost:.4f}" + ("" if spent is not None else "≈"))
    if bal1:
        info.append(f"💳 ${bal1['left']:.2f}")
    if added or removed:
        info.append(f"📁 +{added} −{removed}")
    footer = "<footer>" + "  •  ".join(info) + "</footer>"

    # ---- ход работы и агенты — сворачиваемыми блоками <details> (стрелочка)
    blocks = []
    if st["steps"] or st["thinking"].strip():
        body = "\n".join(f"{i + 1}. {md_esc(x)}" for i, x in enumerate(st["steps"][-60:]))
        if st["thinking"].strip():
            th = st["thinking"].strip()[-4000:]
            body += "\n\n**Размышления**\n\n" + "\n".join("> " + md_esc(l) for l in th.splitlines() if l.strip())
        blocks.append(details(f"🧠 Ход работы · {len(st['steps'])} шагов", body))
    for a in agents.values():
        if a["log"]:
            blocks.append(details(f"🧩 {a['name']}", "\n\n".join(a["log"])[-4000:]))

    if stopped:
        answer = "⏹ Остановлено."
    else:
        answer = st["result"] or (f"Ошибка:\n```\n{err}\n```" if err else "(пустой ответ)")
    parts = list(chunks(answer, 28000))
    for i, part in enumerate(parts):
        md = ("\n\n".join(blocks) + "\n\n" if i == 0 and blocks else "") + part + \
            ("\n\n" + footer if i == len(parts) - 1 else "")
        tgctl.rich_send(chat_id, md, WS, reply_to=None if private else mid)
    if user_cmds_changed():
        register_commands()


# ---------------- Claude на машине GitHub Actions ----------------
# Бот остаётся на Wispbyte (лёгкий, 24/7), а сам Claude Code запускается на каждую задачу в GitHub Actions
# (4 ядра, 16 ГБ). Render-реле передаёт задачу и поток событий. Без GH_TOKEN — всё как раньше, локально.
GH_TOKEN = os.environ.get("GH_TOKEN", "")
GH_REPO = os.environ.get("GH_REPO", "null0null0xyz-max/Null")
GH_REF = os.environ.get("GH_REF", "main")
PACK_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", ".cache"}
CLAUDE_SKIP = {"projects", "shell-snapshots", "statsig", "todos", "ide", "cache", "local", "downloads",
               "debug", "telemetry", "logs", "plugins"}
ENV_PASS = ("ANTHROPIC_", "CLAUDE_CODE_", "TG_", "BOT_TOKEN", "REXEC_", "WORKSPACE", "MAX_THINKING")


def remote_on():
    return bool(GH_TOKEN and os.environ.get("REXEC_URL")) and cfg.get("where", "github") == "github"


def _walk(top, skip):
    for root, dirs, files in os.walk(top):
        dirs[:] = [d for d in dirs if d not in skip]
        for n in files:
            yield os.path.join(root, n)


class RemoteProc:
    """Похоже на subprocess.Popen: .stdout (строки stream-json), poll(), wait(), returncode."""
    pid = None

    def __init__(self, cmd, env, session):
        self.url = os.environ["REXEC_URL"].rstrip("/")
        self.hdr = {"X-Token": os.environ.get("REXEC_TOKEN", "")}
        self.job = secrets.token_hex(16)
        self.returncode = None
        self.q = queue.Queue()
        self.done = threading.Event()
        self.applied = False
        self.sent_ws = set()
        home = os.path.expanduser("~")
        payload = {"cmd": cmd, "home": home, "here": HERE, "ws": WS, "ecc_dir": ECC_DIR,
                   "extra_dirs": [os.path.join(HERE, "chatlog"), os.path.join(HERE, "chat_prompts")],
                   "env": {k: v for k, v in env.items() if k.startswith(ENV_PASS)},
                   "tar": base64.b64encode(self._pack(home, session)).decode()}
        payload["env"]["WORKSPACE"] = WS
        for i in range(8):  # Render может просыпаться
            try:
                r = requests.post(f"{self.url}/relay/{self.job}/in", data=json.dumps(payload),
                                  headers=self.hdr, timeout=60)
                if r.ok:
                    break
                raise RuntimeError(f"реле: {r.status_code} {r.text[:200]}")
            except requests.RequestException:
                time.sleep(8)
        else:
            raise RuntimeError("Render-реле не отвечает")
        r = requests.post(f"https://api.github.com/repos/{GH_REPO}/actions/workflows/claude.yml/dispatches",
                          json={"ref": GH_REF, "inputs": {"job": self.job, "relay": self.url}}, timeout=30,
                          headers={"Authorization": "Bearer " + GH_TOKEN, "Accept": "application/vnd.github+json"})
        if r.status_code != 204:
            raise RuntimeError(f"GitHub не запустил задачу: {r.status_code} {r.text[:300]}")
        self.q.put(json.dumps({"type": "_status", "text": "жду машину GitHub"}) + "\n")
        self.stdout = self._lines()
        threading.Thread(target=self._poll, daemon=True).start()

    def _pack(self, home, session):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            def add(p):
                try:
                    if os.path.islink(p) or os.path.getsize(p) < 50_000_000:
                        t.add(p, arcname=p.lstrip("/"), recursive=False)
                except OSError:
                    pass
            for p in _walk(WS, PACK_SKIP):
                self.sent_ws.add(os.path.relpath(p, WS))
                add(p)
            cd = os.path.join(home, ".claude")
            for p in _walk(cd, CLAUDE_SKIP):
                add(p)
            if session:  # только нужная сессия, а не вся история
                proj = os.path.join(cd, "projects", re.sub(r"[^A-Za-z0-9]", "-", WS))
                for p in [os.path.join(proj, session + ".jsonl")] + list(_walk(os.path.join(proj, session), set())):
                    if os.path.exists(p):
                        add(p)
            for d in ("chatlog", "chat_prompts"):
                for p in _walk(os.path.join(HERE, d), set()):
                    add(p)
            if os.path.exists(os.path.join(home, ".claude.json")):
                add(os.path.join(home, ".claude.json"))
        return buf.getvalue()

    def _lines(self):
        while True:
            line = self.q.get()
            if line is None:
                return
            yield line

    def _poll(self):
        n, t0, started = 0, time.time(), False
        while True:
            try:
                j = requests.get(f"{self.url}/relay/{self.job}/ev", params={"from": n},
                                 headers=self.hdr, timeout=40).json()
            except Exception:
                time.sleep(3)
                if time.time() - t0 > 6 * 3600:
                    break
                continue
            started = started or j.get("started")
            for line in j.get("lines", []):
                try:
                    ev = json.loads(line)
                except ValueError:
                    ev = {}
                if ev.get("type") == "_done":
                    self.returncode = ev.get("code", 0)
                    if ev.get("error"):
                        self.q.put(ev["error"] + "\n")
                    continue
                self.q.put(line + "\n")
            n = j.get("next", n)
            if j.get("done"):
                break
            if not started and time.time() - t0 > 300:
                self.q.put("GitHub Actions не взял задачу за 5 минут.\n")
                self.returncode = 1
                break
        if self.returncode is None:
            self.returncode = 1
        self.done.set()
        self.q.put(None)

    def poll(self):
        return self.returncode if self.done.is_set() else None

    def wait(self, timeout=None):
        self.done.wait(max(timeout or 0, 240))  # файлы возвращаются после ответа
        self._apply()
        return self.returncode

    def _apply(self):
        if self.applied or not self.done.is_set():
            return
        self.applied = True
        try:
            r = requests.get(f"{self.url}/relay/{self.job}/out", headers=self.hdr, timeout=120)
            if not r.content:
                return
            with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as t:
                man = None
                members = []
                for m in t.getmembers():
                    if m.name == "__manifest__.json":
                        man = set(json.loads(t.extractfile(m).read()))
                    else:
                        members.append(m)
                t.extractall("/", members=members, filter="tar")
            if man is not None:  # удалённые Claude файлы удаляем и здесь
                for rel in self.sent_ws - man:
                    try:
                        os.remove(os.path.join(WS, rel))
                    except OSError:
                        pass
        except Exception as e:
            print("remote apply:", e, flush=True)

    def stop(self):
        try:
            requests.post(f"{self.url}/relay/{self.job}/stop", headers=self.hdr, timeout=20)
        except Exception:
            pass


def oom_first():
    """В дочернем процессе: при нехватке памяти ядро убьёт его, а не бота (повышать можно без root)."""
    try:
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("1000")
    except Exception:
        pass


CPU_LIMIT = float(os.environ.get("CLAUDE_CPU", "0.2"))  # доля ядра для Claude и его команд
_CLK = os.sysconf("SC_CLK_TCK")


def group_cpu(pgid):
    """Суммарное CPU-время (с) всех процессов группы, включая завершившихся детей."""
    tot = 0
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat") as f:
                st = f.read().rsplit(")", 1)[1].split()
        except Exception:
            continue
        if int(st[2]) == pgid:  # pgrp
            tot += int(st[11]) + int(st[12]) + int(st[13]) + int(st[14])
    return tot / _CLK


def cpu_throttle(proc, run):
    """Wispbyte гасит сервер за высокую нагрузку на CPU. Пока Claude работает, держим его группу
    процессов в пределах CPU_LIMIT: если превысил — ненадолго замораживаем (SIGSTOP/SIGCONT)."""
    if CPU_LIMIT <= 0 or isinstance(proc, RemoteProc):
        return
    last_t, last_c = time.time(), group_cpu(proc.pid)
    while proc.poll() is None and not run.get("stopped"):
        time.sleep(0.5)
        now, c = time.time(), group_cpu(proc.pid)
        used, dt = c - last_c, now - last_t
        pause = used / CPU_LIMIT - dt
        if pause > 0.05:
            try:
                os.killpg(proc.pid, 19)  # SIGSTOP
                time.sleep(min(pause, 3))
            finally:
                try:
                    os.killpg(proc.pid, 18)  # SIGCONT
                except Exception:
                    pass
        last_t, last_c = time.time(), group_cpu(proc.pid)


def kill_run(proc):
    """Остановить Claude и всё, что он запустил (вся группа процессов)."""
    if proc is None:  # быстрый режим останавливается по флагу stopped
        return
    if isinstance(proc, RemoteProc):
        return proc.stop()
    try:
        os.killpg(proc.pid, 15)
        os.killpg(proc.pid, 18)  # разморозить, если стоял на паузе ограничителя CPU
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass


def safe_tail(text, n=3500):
    """Хвост текста для черновика без «рваной» разметки: с границы абзаца, с закрытым ```."""
    if len(text) > n:
        cut = text.find("\n\n", len(text) - n)
        text = "…\n\n" + text[cut + 2 if cut != -1 else len(text) - n:]
    if text.count("```") % 2:
        text += "\n```"
    text = re.sub(r"<details[^>]*>(?![\s\S]*</details>)", "", text)
    return text


def md_esc(t):
    return re.sub(r"([\\`*_\[\]<>|~=#$])", r"\\\1", t)


def details(title, body):
    return f"<details><summary>{html.escape(title)}</summary>\n\n{body}\n\n</details>"


def send_rich(chat_id, body):
    """HTML-текст (меню, справка): переводы строк -> <br>, т.к. rich HTML их схлопывает."""
    r = tg("sendRichMessage", chat_id=chat_id, rich_message={"html": body.replace("\n", "<br>")})
    if not r.get("ok"):  # запасной путь для старых клиентов
        r = tg("sendMessage", chat_id=chat_id, text=body, parse_mode="HTML")
        if not r.get("ok"):
            tg("sendMessage", chat_id=chat_id, text=re.sub(r"<[^>]+>", "", body)[:4000])


# ---------------- меню ----------------
def kb(rows):
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


def menu_main():
    p, m = cfg["provider"], cfg["model"]
    return (f"🤖 <b>Claude Code</b>\nПровайдер: <code>{html.escape(p)}</code>\nМодель: <code>{html.escape(m)}</code>"
            f"\nEffort: <code>{cfg.get('effort') or 'по умолчанию'}</code>\nРежим: {MODES.get(cfg['mode'], cfg['mode'])}"
            f"\nECC: {len(ecc_cmds)} команд, навыки {'вкл' if cfg.get('ecc_skills') else 'выкл'}",
            kb([[("🧠 Модель", "models"), ("🔌 Провайдеры", "provs")],
                [("⚡ Effort", "effort"), ("🛡 Режим", "mode")],
                [("🆕 Новая сессия", "new"), ("📜 Сессии", "sess")],
                [("👥 Группы и каналы", "chats"), ("🧩 ECC", "ecc")],
                [("📁 Синхронизировать", "sync")]]))


def menu_effort():
    cur = cfg.get("effort") or ""
    rows = [[(("✅ " if e == cur else "") + e, "e:" + e) for e in EFFORTS[:3]],
            [(("✅ " if e == cur else "") + e, "e:" + e) for e in EFFORTS[3:]] + [(("✅ " if not cur else "") + "авто", "e:")],
            [("⬅️ Назад", "main")]]
    return "Effort — сколько модель думает. У сторонних провайдеров может не поддерживаться.", kb(rows)


def menu_mode():
    rows = [[(("✅ " if k == cfg["mode"] else "") + v, "mo:" + k)] for k, v in MODES.items()]
    return "Режим разрешений:", kb(rows + [[("⬅️ Назад", "main")]])


def menu_sessions(chat):
    ch, rows = C(chat), []
    for h in ch["history"]:
        mark = "✅ " if h["id"] == ch["session"] else ""
        when = time.strftime("%d.%m %H:%M", time.localtime(h["ts"]))
        rows.append([(f"{mark}{when} · {h['title'] or '…'}", "r:" + h["id"]), ("🗑", "rd:" + h["id"])])
    text = "Сессии — нажми, чтобы продолжить, 🗑 — удалить:" if rows else "Сессий пока нет."
    return text, kb(rows + [[("🧹 Очистить чат и удалить текущую", "clear")], [("⬅️ Назад", "main")]])


def menu_chats():
    rows = []
    for cid, ch in cfg.get("chats", {}).items():
        if ch.get("type") in ("group", "supergroup", "channel"):
            icon = "📢" if ch["type"] == "channel" else "👥"
            rows.append([(f"{icon} {ch.get('title') or cid}", "ch:" + cid)])
    text = ("Чаты, где я есть. Добавь меня в группу или канал — он появится тут." if not rows
            else "Группы и каналы — нажми, чтобы настроить:")
    return text, kb(rows + [[("⬅️ Назад", "main")]])


def menu_chat(cid):
    ch = C(cid)
    acc = {"owner": "👤 только ты", "all": "👥 все"}[ch["access"]]
    rep_ = {"mention": "🔔 по @упоминанию/ответу", "all": "💬 на все сообщения"}[ch["reply"]]
    pr = chat_prompt(cid)
    text = (f"<b>{html.escape(ch.get('title') or cid)}</b>\nКто может писать: {acc}\nКогда отвечаю: {rep_}\n"
            f"Доступ к серверу и файлам: {'✅ да' if ch['trust'] else '❌ нет (только чат)'}\n"
            f"Инструкции: {html.escape(pr[:300]) if pr else '— (задать: /prompt в том чате или скажи мне там)'}")
    return text, kb([[("Кто пишет: " + acc, "ca:" + cid)], [("Когда: " + rep_, "cr:" + cid)],
                     [("Сервер: " + ("✅" if ch["trust"] else "❌"), "ct:" + cid)],
                     [("🧹 Сбросить инструкции", "cp:" + cid), ("🆕 Новая сессия", "cn:" + cid)],
                     [("⬅️ Назад", "chats")]])


def menu_ecc():
    sk = "✅ вкл" if cfg.get("ecc_skills") else "❌ выкл"
    return (f"🧩 <b>Everything Claude Code</b>\nКоманд: {len(ecc_cmds)}. Список — /ecc.\n"
            "Навыки (311 шт.) добавляют контекст к каждому запросу; на слабой модели можно выключить.",
            kb([[("Навыки: " + sk, "ecc_sk")], [("🔄 Обновить ECC", "ecc_up")], [("⬅️ Назад", "main")]]))


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
        for c in cfg.get("chats", {}).values():
            c["session"] = None  # сессии разных провайдеров несовместимы
        save_cfg(); note = "Провайдер: " + d[2:]
        show(chat, mid, menu_main())
    elif d.startswith("del:"):
        cfg["providers"].pop(d[4:], None)
        if cfg["provider"] not in cfg["providers"]:
            cfg.update(provider="anthropic", model="sonnet")
        save_cfg(); note = "Удалён"
        show(chat, mid, menu_provs())
    elif d == "add":
        waiting[OWNER] = "add_provider"
        tg("sendMessage", chat_id=chat, text=ADD_HELP, parse_mode="HTML")
    elif d == "new":
        C(chat)["session"] = None; save_cfg(); note = "Новая сессия"
    elif d == "clear":
        n = clear_chat(chat); note = f"Удалено сообщений: {n}"
        return tg("answerCallbackQuery", callback_query_id=q["id"], text=note)
    elif d.startswith("rd:"):
        delete_session(chat, d[3:]); note = "Сессия удалена"
        show(chat, mid, menu_sessions(chat))
    elif d == "chats":
        show(chat, mid, menu_chats())
    elif d[:3] in ("ch:", "ca:", "cr:", "ct:", "cp:", "cn:"):
        cid, ch = d[3:], C(d[3:])
        if d.startswith("ca:"):
            ch["access"] = "all" if ch["access"] == "owner" else "owner"
        elif d.startswith("cr:"):
            ch["reply"] = "all" if ch["reply"] == "mention" else "mention"
        elif d.startswith("ct:"):
            ch["trust"] = not ch["trust"]
        elif d.startswith("cp:"):
            chat_prompt(cid, "")
        elif d.startswith("cn:"):
            ch["session"] = None
        save_cfg()
        show(chat, mid, menu_chat(cid))
    elif d == "effort":
        show(chat, mid, menu_effort())
    elif d.startswith("e:"):
        cfg["effort"] = d[2:]; save_cfg(); note = "Effort: " + (d[2:] or "авто")
        show(chat, mid, menu_main())
    elif d == "mode":
        show(chat, mid, menu_mode())
    elif d.startswith("mo:") and d[3:] in MODES:
        cfg["mode"] = d[3:]; save_cfg(); note = MODES[d[3:]]
        show(chat, mid, menu_main())
    elif d == "sess":
        show(chat, mid, menu_sessions(chat))
    elif d.startswith("r:"):
        C(chat)["session"] = d[2:]; save_cfg(); note = "Продолжаю эту сессию"
        show(chat, mid, menu_main())
    elif d == "ecc":
        show(chat, mid, menu_ecc())
    elif d == "ecc_sk":
        cfg["ecc_skills"] = not cfg.get("ecc_skills"); save_cfg(); link_ecc()
        show(chat, mid, menu_ecc())
    elif d == "ecc_up":
        tg("answerCallbackQuery", callback_query_id=q["id"], text="Обновляю ECC…")
        threading.Thread(target=lambda: (install_ecc(force=True), register_commands(),
                                         tg("sendMessage", chat_id=chat, text=f"🧩 ECC обновлён: {len(ecc_cmds)} команд")),
                         daemon=True).start()
        return
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
    url = re.sub(r"/v1$", "", url.rstrip("/"))  # Claude Code сам добавляет /v1/messages
    cfg["providers"][name] = {"base_url": url, "token": token, "models": models}
    save_cfg()
    text, markup = menu_provs()
    tg("sendMessage", chat_id=msg["chat"]["id"], text=f"✅ Добавлен {html.escape(name)}\n\n" + text,
       parse_mode="HTML", reply_markup=markup)


HELP = """<b>Как пользоваться</b>
Пиши задачу текстом — Claude Code выполнит её в папке проекта.
📎 Можно прислать фото, скриншот или файл (до 20 МБ) с подписью-задачей — он посмотрит.

<b>Команды</b>
/menu — модель, провайдеры, effort, режим, ECC
/new — новая сессия · /clear — очистить чат и удалить сессию · /resume — сессии (🗑 удалить)
/effort high — уровень размышлений · /model имя — модель
/plan — режим плана · /mode — режим разрешений
/compact /context /init /review /security_review — команды Claude Code
/stop — остановить · /status — состояние · /files — файлы
/usage — кредиты и остаток · /agents — чаты агентов · /skills — навыки · /repair — починить канал
/chats — группы и каналы · /prompt текст — инструкции для чата
📊 Внизу ответа: время • ходы • токены • 💰 списано • 💳 остаток
/ecc — все команды Everything Claude Code (например /code_review, /plan_prd, /tdd)
Любую команду ECC можно писать и через дефис: /code-review"""


def fast_ok(prompt):
    """Быстрый режим: обычные сообщения. Команды Claude Code и присланные файлы — сразу в Claude Code."""
    return cfg.get("speed", "auto") == "auto" and not prompt.startswith("/") and "Приложены файлы" not in prompt


def fast_system(chat, guest, who, mid):
    ch = C(chat)
    where = "личный чат с владельцем" if int(chat) > 0 else f"{ch['type']} «{ch['title']}»"
    h = [f"Ты — Telegram-бот @{BOT_USERNAME}, быстрый режим. Чат: {where}. Пишет: {who}. "
         "Отвечай сразу, кратко и по делу, по-русски, если пользователь не пишет на другом языке. "
         "Ответ уходит rich-сообщением в Markdown (GFM): заголовки, таблицы, код, формулы $x^2$, ||спойлер||.",
         "Для фактов из интернета используй web_fetch (поиск: https://html.duckduckgo.com/html/?q=...)."]
    if not guest:
        h.append("bash выполняет команды на сервере Render (Linux, есть интернет) — для быстрых проверок и расчётов. "
                 "Если просят написать, создать или изменить код, файлы, сайт, проект, если задача большая, "
                 "многошаговая или нужна сборка — НЕ делай сам, сразу вызови claude_code с полной задачей.")
    p = chat_prompt(chat)
    if p:
        h.append("Инструкции владельца для этого чата: " + p)
    return "\n".join(h)


def run_fast(chat_id, prompt, guest=False, who="владелец", mid=None):
    did = next(draft_ids)
    started = time.time()
    private = int(chat_id) > 0
    st = {"text": "", "steps": [], "dirty": True, "done": False}
    runs[did] = {"proc": None, "stopped": False, "chat": chat_id}
    p = cfg["providers"][cfg["provider"]]
    model = cfg.get("fast_model") or cfg["model"]

    def drafter():
        last = 0
        while not st["done"] and not runs[did]["stopped"]:
            if not private:
                tg("sendChatAction", chat_id=chat_id, action="typing"); time.sleep(4); continue
            if (st["dirty"] and time.time() - last > 1.0) or time.time() - last > 3:
                st["dirty"], last = False, time.time()
                head = "⚡ " + (st["steps"][-1] if st["steps"] else "Отвечаю…")
                tg("sendRichMessageDraft", chat_id=chat_id, draft_id=did, can_stop=True,
                   rich_message={"markdown": f"<tg-thinking>{html.escape(head)}</tg-thinking>\n\n" + safe_tail(st["text"])})
            time.sleep(0.3)

    ui = {"text": lambda d: (st.__setitem__("text", st["text"] + d), st.__setitem__("dirty", True)),
          "step": lambda l: (st["steps"].append(l), st.__setitem__("dirty", True)),
          "new_text": lambda: st.__setitem__("text", "")}
    threading.Thread(target=drafter, daemon=True).start()
    try:
        res = fast.answer(HERE, chat_id, prompt, p.get("base_url"), p.get("token", ""), model,
                          fast_system(chat_id, guest, who, mid), os.environ.get("REXEC_URL", ""),
                          os.environ.get("REXEC_TOKEN", ""), ui, lambda: runs[did]["stopped"], guest)
    except Exception as e:
        res = {"text": "", "error": str(e), "delegate": None, "steps": [], "in": 0, "out": 0}
    st["done"] = True
    stopped = runs.pop(did)["stopped"]
    if res.get("error") and not stopped:  # быстрый путь не сработал — отдаём задачу Claude Code
        res["delegate"] = prompt
    if res["delegate"] and not stopped:
        return run_claude(chat_id, res["delegate"] if res["delegate"] != prompt else prompt, guest, who, mid)
    sec = round(time.time() - started, 1)
    body = "⏹ Остановлено." if stopped and not res["text"] else (res["text"] or "(пустой ответ)")
    blocks = details(f"⚙️ Шаги · {len(res['steps'])}", "\n".join(f"{i + 1}. {md_esc(x)}" for i, x in
                                                          enumerate(res["steps"]))) + "\n\n" if res["steps"] else ""
    footer = f"<footer>⚡ {sec} с  •  🔤 {fmt_k(res['in'])} → {fmt_k(res['out'])}  •  {html.escape(model)}</footer>"
    tgctl.rich_send(chat_id, blocks + body + "\n\n" + footer, WS, reply_to=None if private else mid)


def start_task(chat, prompt, guest=False, who="владелец", mid=None):
    if not CHANNEL:
        return tg("sendMessage", chat_id=chat, text="Сначала добавь меня админом в канал для файлов.")
    if runs:
        return tg("sendMessage", chat_id=chat, text="Ещё работаю над прошлой задачей — подожди или /stop.",
                  **({"reply_parameters": {"message_id": mid, "allow_sending_without_reply": True}} if mid else {}))
    target = run_fast if fast_ok(prompt) else run_claude
    threading.Thread(target=target, args=(chat, prompt, guest, who, mid), daemon=True).start()


def send_long(chat, text, **kw):
    for part in chunks(text):
        tg("sendMessage", chat_id=chat, text=part, **kw)


def status_text(chat):
    try:
        ver = subprocess.run([CLAUDE, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception as e:
        ver = f"не найден ({e.__class__.__name__})"
    du = shutil.disk_usage(HERE)
    mem = ""
    try:
        for f in ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes"):
            if os.path.exists(f):
                mem = f"{int(open(f).read()) // 2**20} МБ"
                break
    except Exception:
        pass
    s = C(chat)["session"]
    return (f"🤖 Claude Code: <code>{html.escape(ver or '?')}</code>\n"
            f"Провайдер: <code>{html.escape(cfg['provider'])}</code> · модель <code>{html.escape(cfg['model'])}</code>\n"
            f"Effort: {cfg.get('effort') or 'авто'} · режим: {MODES.get(cfg['mode'])}\n"
            f"Сессия: <code>{s[:8] if s else 'новая'}</code> · задача: {'идёт' if runs else 'нет'}\n"
            f"Где работает Claude: {'GitHub Actions' if remote_on() else 'здесь (Wispbyte)'}\n"
            f"ECC: {len(ecc_cmds)} команд, навыки {'вкл' if cfg.get('ecc_skills') else 'выкл'}\n"
            f"Диск: свободно {du.free // 2**20} МБ из {du.total // 2**20} МБ" + (f"\nПамять: {mem}" if mem else ""))


def usage_text():
    lr, stt = cfg.get("last_run"), cfg.get("stats") or {}
    lines = [f"📊 <b>Расход</b> · {html.escape(cfg['provider'])} / <code>{html.escape(cfg['model'])}</code>"]
    b = provider_balance()
    if b:
        lines.append("Баланс провайдера: " + ("безлимит" if b["unlimited"] else f"${b['left']:.2f}")
                     + f" · всего потрачено ${b['used']:.2f}")
    if lr:
        c = f"${lr['cost']:.4f}" + ("" if lr.get("real") else " (оценка)") if lr.get("cost") is not None else "—"
        lines.append(f"Последняя задача: {c} · {fmt_k(lr['in'])} → {fmt_k(lr['out'])} токенов · "
                     f"{lr['sec']} с · {lr['turns']} ходов")
    if stt:
        lines.append(f"Через бота всего: ${stt.get('cost', 0):.4f} · {stt.get('runs', 0)} задач · "
                     f"{fmt_k(stt.get('in', 0))} → {fmt_k(stt.get('out', 0))} токенов")
    return "\n".join(lines)


def handle_command(chat, text):
    """Возвращает промпт для Claude или None, если команда обработана ботом."""
    head, _, arg = text.partition(" ")
    name = head[1:].split("@")[0].lower()
    arg = arg.strip()
    key = name.replace("-", "_")
    if key in ("start", "menu"):
        t, m = menu_main()
        tg("sendMessage", chat_id=chat, text=t, parse_mode="HTML", reply_markup=m)
    elif key == "help":
        tg("sendMessage", chat_id=chat, text=HELP, parse_mode="HTML")
    elif key == "fast":
        if arg.startswith("model"):
            cfg["fast_model"] = arg[5:].strip(); save_cfg()
        elif arg in ("on", "off", "вкл", "выкл"):
            cfg["speed"] = "auto" if arg in ("on", "вкл") else "full"; save_cfg()
        elif not arg:
            cfg["speed"] = "full" if cfg.get("speed", "auto") == "auto" else "auto"; save_cfg()
        tg("sendMessage", chat_id=chat, text=("⚡ Быстрые ответы ВКЛ: обычные сообщения — сразу, сложные задачи "
                                              "сами уходят в Claude Code. Модель: " + (cfg.get("fast_model") or cfg["model"])
                                              + ". /fast model ИМЯ — сменить, /fast off — всё через Claude Code")
           if cfg.get("speed", "auto") == "auto" else "🐢 Быстрые ответы ВЫКЛ: всё идёт в Claude Code. /fast on — включить")
    elif key == "new":
        fast.clear_hist(HERE, chat)
        C(chat)["session"] = None; save_cfg()
        tg("sendMessage", chat_id=chat, text="🆕 Новая сессия")
    elif key == "clear":
        clear_chat(chat)
        tg("sendMessage", chat_id=chat, text="🧹 Чат очищен, сессия удалена.")
    elif key == "chats":
        t, m = menu_chats()
        tg("sendMessage", chat_id=chat, text=t, reply_markup=m)
    elif key == "prompt":
        if arg in ("off", "сброс", "-"):
            chat_prompt(chat, ""); tg("sendMessage", chat_id=chat, text="Инструкции чата сброшены.")
        elif arg:
            chat_prompt(chat, arg); tg("sendMessage", chat_id=chat, text="✅ Запомнил, как вести себя в этом чате.")
        else:
            tg("sendMessage", chat_id=chat, text="Инструкции чата: " + (chat_prompt(chat) or "—") +
               "\nЗадать: /prompt текст · сбросить: /prompt off")
    elif key == "resume":
        t, m = menu_sessions(chat)
        tg("sendMessage", chat_id=chat, text=t, reply_markup=m)
    elif key == "effort":
        if arg in EFFORTS or arg in ("auto", "авто", "off"):
            cfg["effort"] = arg if arg in EFFORTS else ""; save_cfg()
            tg("sendMessage", chat_id=chat, text="⚡ Effort: " + (cfg["effort"] or "авто"))
        else:
            t, m = menu_effort()
            tg("sendMessage", chat_id=chat, text=t, reply_markup=m)
    elif key == "model":
        if arg:
            cfg["model"] = arg; save_cfg()
            tg("sendMessage", chat_id=chat, text="🧠 Модель: " + arg)
        else:
            t, m = menu_models()
            tg("sendMessage", chat_id=chat, text=t, reply_markup=m)
    elif key == "mode":
        t, m = menu_mode()
        tg("sendMessage", chat_id=chat, text=t, reply_markup=m)
    elif key == "plan" and not arg:
        cfg["mode"] = "bypass" if cfg["mode"] == "plan" else "plan"; save_cfg()
        tg("sendMessage", chat_id=chat, text="Режим: " + MODES[cfg["mode"]])
    elif key == "stop":
        for r in runs.values():
            r["stopped"] = True
            kill_run(r["proc"])
        tg("sendMessage", chat_id=chat, text="⏹ Останавливаю." if runs else "Сейчас ничего не выполняется.")
    elif key == "status":
        tg("sendMessage", chat_id=chat, text=status_text(chat), parse_mode="HTML")
    elif key == "where":
        if arg in ("github", "gh", "here", "local", "здесь"):
            cfg["where"] = "github" if arg in ("github", "gh") else "local"; save_cfg()
        elif not GH_TOKEN:
            cfg["where"] = "local"
        tg("sendMessage", chat_id=chat, text=("☁️ Claude работает на GitHub Actions (4 ядра, 16 ГБ). /where here — здесь"
                                              if remote_on() else "🏠 Claude работает здесь, на Wispbyte. "
                                              + ("/where github — на GitHub" if GH_TOKEN else "Для GitHub нужен GH_TOKEN в .env")))
    elif key == "files":
        paths = sorted(fs.idx.get("files", {}).keys()) or \
            sorted(os.path.relpath(os.path.join(d, f), WS) for d, _, fs_ in os.walk(WS) for f in fs_)
        send_long(chat, "📁 Файлы проекта:\n" + ("\n".join(paths) or "пусто"))
    elif key == "ecc":
        if not ecc_cmds:
            tg("sendMessage", chat_id=chat, text="ECC ещё ставится или не установился. /menu → 🧩 ECC → Обновить.")
        else:
            lines = [f"/{n.replace('-', '_')} — {html.escape(d[:90])}" for n, d in sorted(ecc_cmds.items())]
            send_long(chat, "🧩 Команды Everything Claude Code:\n\n" + "\n".join(lines))
    elif key in ("usage", "cost"):
        tg("sendMessage", chat_id=chat, text=usage_text(), parse_mode="HTML")
    elif key == "agents":
        if not last_agents:
            tg("sendMessage", chat_id=chat, text="В последней задаче агентов не было.")
        else:
            tgctl.rich_send(chat, agents_md(), WS)
    elif key == "skills":
        d = os.path.join(CLAUDE_DIR, "skills")
        names = sorted(os.listdir(d)) if os.path.isdir(d) else []
        own = [n for n in names if not _ours(os.path.join(d, n))]
        txt = (f"🧠 Навыков: {len(names)} (ECC: {len(names) - len(own)}, свои: {len(own)})\n"
               + ("Свои: " + ", ".join(own) if own else "Своих пока нет — попроси: «создай навык для …»"))
        send_long(chat, txt)
    elif key == "repair":
        with fs_lock:
            try:
                fs.idx["dir_text"] = {}
                fs.rebuild_nav()
                note = "🛠 Оглавление и index.json в канале восстановлены."
            except Exception as e:
                note = f"Не получилось: {e}"
        tg("sendMessage", chat_id=chat, text=note)
    elif key in PASS:
        return (PASS[key] + " " + arg).strip()
    else:
        # команда ECC или любая другая слэш-команда Claude Code: /code_review → /code-review
        real = next((n for n in list(user_cmds) + list(ecc_cmds) if n.replace("-", "_") == key), name)
        return ("/" + real + " " + arg).strip()
    return None


def media_of(msg):
    if msg.get("photo"):
        p = msg["photo"][-1]
        return p["file_id"], f"photo_{msg['message_id']}.jpg", p.get("file_size", 0)
    for k in ("document", "video", "audio", "voice", "animation", "video_note"):
        d = msg.get(k)
        if d:
            ext = mimetypes.guess_extension(d.get("mime_type") or "") or ""
            name = d.get("file_name") or f"{k}_{msg['message_id']}{ext}"
            return d["file_id"], name, d.get("file_size", 0)
    return None


def download(file_id, name):
    r = tg("getFile", file_id=file_id)
    if not r.get("ok"):
        raise RuntimeError(r.get("description", "getFile"))
    name = re.sub(r"[^\w.\-]+", "_", os.path.basename(name)) or "file"
    os.makedirs(os.path.join(WS, "uploads"), exist_ok=True)
    rel = os.path.join("uploads", name)
    if os.path.exists(os.path.join(WS, rel)):
        base, ext = os.path.splitext(name)
        rel = os.path.join("uploads", f"{base}_{int(time.time())}{ext}")
    with requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{r['result']['file_path']}",
                      stream=True, timeout=120) as resp:
        resp.raise_for_status()
        with open(os.path.join(WS, rel), "wb") as f:
            shutil.copyfileobj(resp.raw, f)
    return rel


def flush_media(key):
    item = media_buf.pop(key, None)
    if not item:
        return
    chat, saved, errors = item["chat"], [], []
    for fid, name, size in item["files"]:
        if size and size > 20 * 2**20:
            errors.append(f"{name}: больше 20 МБ — Telegram не отдаёт ботам такие файлы")
            continue
        try:
            saved.append(download(fid, name))
        except Exception as e:
            errors.append(f"{name}: {e}")
    if errors:
        tg("sendMessage", chat_id=chat, text="⚠️ " + "\n".join(errors))
    if not saved:
        return
    caption = item["caption"]
    if caption.startswith("/"):
        caption = handle_command(chat, caption) or ""
    prompt = (caption or "Посмотри приложенные файлы и скажи, что в них.") + \
        "\n\nПриложены файлы (в папке проекта):\n" + "\n".join("- " + s for s in saved) + \
        "\nОткрой их инструментом Read — он умеет показывать изображения и PDF."
    if runs:
        return tg("sendMessage", chat_id=chat, text="📎 Сохранил в " + ", ".join(saved) +
                  ". Задача ещё идёт — потом попроси посмотреть эти файлы.")
    start_task(chat, prompt)


def collect_media(msg, media):
    key = msg.get("media_group_id") or f"m{msg['message_id']}"
    item = media_buf.setdefault(key, {"chat": msg["chat"]["id"], "caption": "", "files": [], "timer": None})
    item["files"].append(media)
    if msg.get("caption") and not item["caption"]:
        item["caption"] = msg["caption"].strip()
    if item["timer"]:
        item["timer"].cancel()
    item["timer"] = threading.Timer(1.5, flush_media, args=(key,))  # альбом приходит пачкой сообщений
    item["timer"].start()


def who_of(msg):
    f = msg.get("from") or {}
    name = (f.get("first_name", "") + " " + f.get("last_name", "")).strip() or msg.get("author_signature") \
        or (msg.get("sender_chat") or {}).get("title") or "?"
    return name + (f" @{f['username']}" if f.get("username") else "") + (" (владелец)" if f.get("id") == OWNER else "")


def addressed(msg, text):
    """Обращение к боту в группе/канале: команда, @упоминание или ответ на сообщение бота."""
    if text.startswith("/"):
        head = text.split()[0]
        return "@" not in head or head.lower().endswith("@" + BOT_USERNAME.lower())
    if BOT_USERNAME and "@" + BOT_USERNAME.lower() in text.lower():
        return True
    return (msg.get("reply_to_message") or {}).get("from", {}).get("id") == BOT_ID


def on_message(msg, channel=False):
    global OWNER
    chat_o = msg["chat"]
    chat, ctype = chat_o["id"], chat_o["type"]
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not OWNER and ctype == "private" and text == "/start":
        OWNER = cfg["owner"] = msg["from"]["id"]
        save_cfg()
        tg("sendMessage", chat_id=OWNER, text="👋 Ты владелец. Теперь добавь меня админом в приватный канал для файлов.")
    from_owner = (msg.get("from") or {}).get("id") == OWNER
    if ctype == "private" and not from_owner:
        return
    ch = C(chat)
    ch["type"], ch["title"] = ctype, chat_o.get("title") or chat_o.get("first_name", "")
    tgctl.log_msg(chat, msg["message_id"], who_of(msg), text or "[медиа]")

    if ctype == "private":
        if not from_owner:
            return
        guest = False
    else:
        if not (addressed(msg, text) or ch["reply"] == "all"):
            return
        if not from_owner and not channel and ch["access"] != "all":
            return
        guest = not from_owner and not ch["trust"]
        if BOT_USERNAME:
            text = re.sub("@" + re.escape(BOT_USERNAME), "", text, flags=re.I).strip()

    if from_owner and msg.get("text") and waiting.pop(OWNER, None) == "add_provider":
        return add_provider(msg)
    media = media_of(msg)
    if media and not guest:
        return collect_media(msg, media)
    if not text:
        return
    if text.startswith("/"):
        if guest and not text.startswith(("/help", "/start")):
            return
        text = handle_command(chat, text)
        if not text:
            return
    start_task(chat, text, guest=guest, who=who_of(msg), mid=msg["message_id"])


# ---------------- Everything Claude Code (affaan-m/ECC) ----------------
ECC_DIR = os.path.join(HERE, "ecc")


def install_ecc(force=False):
    mark = os.path.join(ECC_DIR, ".ok")
    try:
        if force or not os.path.exists(mark):
            print("installing ECC…", flush=True)
            tmp = ECC_DIR + ".tmp"
            shutil.rmtree(tmp, ignore_errors=True)
            with requests.get(ECC_URL, stream=True, timeout=300) as r:
                r.raise_for_status()
                r.raw.decode_content = True
                with tarfile.open(fileobj=r.raw, mode="r|gz") as tf:  # потоком, без распаковки всего репо
                    for m in tf:
                        rel = m.name.split("/", 1)[1] if "/" in m.name else ""
                        if not m.isfile() or not rel.startswith(ECC_PARTS) or ".." in rel.split("/"):
                            continue
                        dst = os.path.join(tmp, rel)
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        with open(dst, "wb") as f:
                            shutil.copyfileobj(tf.extractfile(m), f)
            shutil.rmtree(ECC_DIR, ignore_errors=True)
            os.rename(tmp, ECC_DIR)
            open(mark, "w").close()
        link_ecc()
    except Exception as e:
        print("ECC install error:", e, flush=True)
    load_ecc_cmds()


def _ours(p):
    return os.path.islink(p) and os.readlink(p).startswith(ECC_DIR)


def link_ecc():
    """Ставит ECC в ~/.claude ссылками: команды, агенты, правила; навыки — по переключателю."""
    if not os.path.isdir(ECC_DIR):
        return
    for kind in ("commands", "agents", "skills"):
        dst_dir = os.path.join(CLAUDE_DIR, kind)
        os.makedirs(dst_dir, exist_ok=True)
        for n in os.listdir(dst_dir):
            if _ours(os.path.join(dst_dir, n)):
                os.unlink(os.path.join(dst_dir, n))
        src_dir = os.path.join(ECC_DIR, kind)
        if not os.path.isdir(src_dir) or (kind == "skills" and not cfg.get("ecc_skills")):
            continue
        for n in os.listdir(src_dir):
            dst = os.path.join(dst_dir, n)
            if not os.path.exists(dst):
                os.symlink(os.path.join(src_dir, n), dst)
    rules = os.path.join(CLAUDE_DIR, "rules")
    os.makedirs(rules, exist_ok=True)
    link = os.path.join(rules, "ecc-common")
    if not os.path.lexists(link) and os.path.isdir(os.path.join(ECC_DIR, "rules", "common")):
        os.symlink(os.path.join(ECC_DIR, "rules", "common"), link)


def load_ecc_cmds():
    ecc_cmds.clear()
    d = os.path.join(ECC_DIR, "commands")
    if not os.path.isdir(d):
        return
    for f in sorted(os.listdir(d)):
        if not f.endswith(".md"):
            continue
        desc = ""
        try:
            for line in open(os.path.join(d, f), encoding="utf-8", errors="ignore").read(3000).splitlines():
                if line.startswith("description:"):
                    desc = line.split(":", 1)[1].strip().strip('"\'')
                    break
        except OSError:
            pass
        ecc_cmds[f[:-3]] = desc or f[:-3]


user_cmds = {}  # свои команды из ~/.claude/commands (не ECC)


def load_user_cmds():
    found = {}
    d = os.path.join(CLAUDE_DIR, "commands")
    if os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            p = os.path.join(d, f)
            if not f.endswith(".md") or _ours(p):
                continue
            desc = ""
            try:
                for line in open(p, encoding="utf-8", errors="ignore").read(3000).splitlines():
                    if line.startswith("description:"):
                        desc = line.split(":", 1)[1].strip().strip('"\'')
                        break
            except OSError:
                pass
            found[f[:-3]] = desc or f[:-3]
    return found


def user_cmds_changed():
    new = load_user_cmds()
    if new != user_cmds:
        user_cmds.clear(); user_cmds.update(new)
        return True
    return False


def register_commands(limit=100):
    cmds = [{"command": c, "description": d} for c, d in BUILTIN]
    seen = {c for c, _ in BUILTIN}
    for n, d in sorted(user_cmds.items()):
        c = n.replace("-", "_").lower()
        if c not in seen and re.fullmatch(r"[a-z0-9_]{1,32}", c):
            seen.add(c)
            cmds.append({"command": c, "description": ("⭐ " + d)[:60]})
    for n, d in sorted(ecc_cmds.items()):
        c = n.replace("-", "_").lower()
        if len(cmds) >= limit or c in seen or not re.fullmatch(r"[a-z0-9_]{1,32}", c):
            continue
        seen.add(c)
        cmds.append({"command": c, "description": ("ECC: " + d)[:60]})
    r = tg("setMyCommands", commands=cmds)
    if not r.get("ok"):
        print("setMyCommands:", limit, r.get("description"), flush=True)
        if "TOO_MUCH" in (r.get("description") or "") and limit > len(BUILTIN):
            return register_commands(max(len(BUILTIN), limit - 20))  # остальные команды ECC — в /ecc


def on_my_chat_member(u):
    global CHANNEL
    chat, new = u["chat"], u["new_chat_member"]
    if chat["type"] != "private" and str(chat["id"]) != str(CHANNEL):
        if new["status"] in ("left", "kicked"):
            cfg.get("chats", {}).pop(str(chat["id"]), None); save_cfg()
        elif CHANNEL or chat["type"] != "channel":
            ch = C(chat["id"]); ch["type"], ch["title"] = chat["type"], chat.get("title", "")
            save_cfg()
            if u["from"]["id"] == OWNER:
                tg("sendMessage", chat_id=OWNER, text=f"👥 Меня добавили в «{chat.get('title', '')}». "
                   "Отвечаю там на @упоминания и ответы. Настройки: /chats")
            return
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


def keep_render_awake():
    """Render free засыпает через 15 мин без запросов и теряет /tmp — вместе с сайтами.
    Один лёгкий GET раз в 10 минут держит его бодрым (это ~1 запрос, не нагрузка)."""
    url = os.environ.get("REXEC_URL", "").rstrip("/")
    while url:
        try:
            requests.get(url + "/", timeout=30)
        except Exception:
            pass
        time.sleep(600)


BOT_ID, BOT_USERNAME = 0, ""


def main():
    global BOT_ID, BOT_USERNAME
    me = tg("getMe").get("result") or {}
    BOT_ID, BOT_USERNAME = me.get("id", 0), me.get("username", "")
    if OWNER and ("session" in cfg or "history" in cfg):  # перенос старых сессий в личный чат
        ch = C(OWNER); ch["type"] = "private"
        ch["session"] = ch["session"] or cfg.get("session")
        ch["history"] = ch["history"] or cfg.get("history") or []
    cfg.pop("session", None); cfg.pop("history", None)
    os.chmod(os.path.join(HERE, "tgctl"), 0o755)
    save_cfg()
    ensure_claude()
    if CHANNEL:
        fs.load()
        fs.restore()
    load_ecc_cmds()
    user_cmds_changed()
    register_commands()
    threading.Thread(target=lambda: (install_ecc(), register_commands()), daemon=True).start()
    threading.Thread(target=keep_render_awake, daemon=True).start()
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
                        kill_run(r["proc"])
                elif "my_chat_member" in u:
                    on_my_chat_member(u["my_chat_member"])
                elif "callback_query" in u:
                    on_callback(u["callback_query"])
                elif "channel_post" in u and str(u["channel_post"]["chat"]["id"]) == str(CHANNEL):
                    with fs_lock:
                        fs.on_channel_post(u["channel_post"])
                elif "channel_post" in u:
                    on_message(u["channel_post"], channel=True)
                elif "message" in u:
                    on_message(u["message"])
            except Exception as e:
                print("update error:", e, flush=True)


if __name__ == "__main__":
    main()
