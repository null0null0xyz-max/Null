"""Telegram-бот для Claude Code на Wispbyte.

- стриминг ответа в черновик (sendRichMessageDraft) + блок <tg-thinking> со статусами шагов
- нативная кнопка Stop (can_stop) и событие stopped_message_generation
- выбор модели, провайдеры (добавить/убрать/переключить), сессии с продолжением
- файлы проекта живут в Telegram-канале (tgfs.py), команды выполняются на Render (rexec)

Env: BOT_TOKEN, REXEC_URL, REXEC_TOKEN (OWNER_ID, CHANNEL_ID — необязательно, определяются сами)
     необязательно: CLAUDE_BIN (по умолчанию "claude"), ANTHROPIC_API_KEY
"""
from dotenv_min import load_env; load_env()
import os, re, json, time, html, shutil, tarfile, mimetypes, threading, subprocess, itertools, requests
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
    "effort": "", "mode": "bypass", "history": [], "ecc_skills": True,
}
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
MODES = {"bypass": "🚀 Всё разрешено", "plan": "📋 План (только чтение)", "acceptEdits": "✏️ Правки без Bash"}
CLAUDE_DIR = os.path.expanduser("~/.claude")
ECC_URL = "https://codeload.github.com/affaan-m/ECC/tar.gz/refs/heads/main"
ECC_PARTS = ("commands/", "agents/", "skills/", "rules/common/")
# команды Claude Code, которые бот передаёт в Claude как есть (работают в -p)
PASS = {"compact": "/compact", "context": "/context", "init": "/init",
        "review": "/review", "security_review": "/security-review"}
BUILTIN = [("menu", "Модель, провайдеры, effort, режим"), ("new", "Новая сессия (= /clear)"),
           ("clear", "Новая сессия"), ("resume", "Вернуться к прошлой сессии"),
           ("effort", "Effort: low/medium/high/xhigh/max"), ("model", "Сменить модель"),
           ("mode", "Режим разрешений (план / всё)"), ("plan", "Включить/выключить режим плана"),
           ("compact", "Сжать контекст сессии"), ("context", "Сколько занято контекста"),
           ("init", "Создать CLAUDE.md для проекта"),
           ("review", "Ревью кода"), ("security_review", "Проверка безопасности"),
           ("stop", "Остановить задачу"), ("status", "Состояние бота и сервера"),
           ("files", "Файлы проекта"), ("usage", "Кредиты, токены, остаток"),
           ("agents", "Чаты агентов последней задачи"), ("skills", "Навыки"),
           ("repair", "Восстановить оглавление и index в канале"),
           ("ecc", "Команды Everything Claude Code"),
           ("help", "Справка")]
SYSTEM_HINT = (
    "Рабочая папка проекта — текущая директория. Файлы читай и правь здесь. "
    "Любые тяжёлые команды (установка пакетов, запуск, тесты, сборка) выполняй через "
    'Bash: rexec "команда" — она выполнится на удалённой машине с копией папки, '
    "изменённые файлы вернутся сюда. Отвечай по-русски. "
    "Навыки лежат в ~/.claude/skills. Если пользователь просит или задача повторяется — можешь сам создать "
    "новый навык: ~/.claude/skills/<имя>/SKILL.md (frontmatter name и description, дальше инструкция)."
)
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


def run_claude(chat_id, prompt):
    did = next(draft_ids)
    started = time.time()
    st = {"steps": [], "text": "", "thinking": "", "result": "", "error": "", "dirty": True, "res": {}}
    agents = {}  # tool_use_id агента -> {"name", "log": [], "last": ""}
    bal0 = provider_balance()
    cmd = [CLAUDE, "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--include-partial-messages", "--forward-subagent-text",
           "--model", cfg["model"], "--append-system-prompt", SYSTEM_HINT]
    mode = cfg.get("mode", "bypass")
    cmd += ["--dangerously-skip-permissions"] if mode == "bypass" else ["--permission-mode", mode]
    if cfg.get("effort"):
        cmd += ["--effort", cfg["effort"]]
    new_session = not cfg.get("session")
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
        steps = "\n".join(html.escape(x) for x in st["steps"][-5:])
        status = f"Думаю · {sec} с · шагов {len(st['steps'])}" + (f"\n{steps}" if steps else "")
        for a in list(agents.values())[-3:]:
            status += f"\n🧩 {html.escape(a['name'])}: {html.escape(a['last'][:70] or 'работает…')}"
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
        t, pid = ev.get("type"), ev.get("parent_tool_use_id")
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
                cfg["session"] = ev["session_id"]
                if new_session:
                    title = re.sub(r"\s+", " ", prompt.split("\n\nПриложены файлы")[0])[:40]
                    cfg["history"] = ([{"id": ev["session_id"], "title": title, "ts": int(time.time())}]
                                      + [h for h in cfg["history"] if h["id"] != ev["session_id"]])[:10]
                save_cfg()
            if ev.get("is_error"):
                st["error"] = ev.get("subtype", "error")
    proc.wait()
    err = "".join(errlines)[-1500:]
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
    info = [f"⏱ {sec} с", f"🔁 {res.get('num_turns', 0)} ходов", f"🔤 {fmt_k(tin)} → {fmt_k(tout)}"]
    if cost is not None:
        info.append(f"💰 ${cost:.4f}" + ("" if spent is not None else " (оценка)"))
    if bal1:
        info.append(f"остаток ${bal1['left']:.2f}")
    if added or removed:
        info.append(f"📁 +{added} −{removed}")
    footer = "\n\n<i>" + " · ".join(info) + "</i>"

    # ---- ход работы и агенты — сворачиваемыми блоками
    details = ""
    if st["steps"]:
        details += "Шаги:\n" + "\n".join(f"{i + 1}. {x}" for i, x in enumerate(st["steps"]))
    if st["thinking"].strip():
        details += ("\n\n" if details else "") + "Размышления:\n" + st["thinking"].strip()
    if details:
        send_rich(chat_id, bq(f"🧠 <b>Ход работы</b> · {len(st['steps'])} шагов", details))
    for a in agents.values():
        if a["log"]:
            send_rich(chat_id, bq(f"🧩 <b>{html.escape(a['name'])}</b>", "\n\n".join(a["log"])))

    if stopped:
        tg("sendMessage", chat_id=chat_id, text="⏹ Остановлено." + footer, parse_mode="HTML")
        return
    text = st["result"] or (f"Ошибка:\n```\n{err}\n```" if err else "(пустой ответ)")
    parts = list(chunks(text))
    for i, part in enumerate(parts):
        send_rich(chat_id, md_to_html(part) + (footer if i == len(parts) - 1 else ""))


def send_rich(chat_id, body):
    r = tg("sendRichMessage", chat_id=chat_id, rich_message={"html": body})
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
                [("🧩 ECC", "ecc"), ("📁 Синхронизировать", "sync")]]))


def menu_effort():
    cur = cfg.get("effort") or ""
    rows = [[(("✅ " if e == cur else "") + e, "e:" + e) for e in EFFORTS[:3]],
            [(("✅ " if e == cur else "") + e, "e:" + e) for e in EFFORTS[3:]] + [(("✅ " if not cur else "") + "авто", "e:")],
            [("⬅️ Назад", "main")]]
    return "Effort — сколько модель думает. У сторонних провайдеров может не поддерживаться.", kb(rows)


def menu_mode():
    rows = [[(("✅ " if k == cfg["mode"] else "") + v, "mo:" + k)] for k, v in MODES.items()]
    return "Режим разрешений:", kb(rows + [[("⬅️ Назад", "main")]])


def menu_sessions():
    rows = []
    for h in cfg["history"]:
        mark = "✅ " if h["id"] == cfg.get("session") else ""
        when = time.strftime("%d.%m %H:%M", time.localtime(h["ts"]))
        rows.append([(f"{mark}{when} · {h['title'] or '…'}", "r:" + h["id"])])
    text = "Прошлые сессии — нажми, чтобы продолжить:" if rows else "Сессий пока нет."
    return text, kb(rows + [[("⬅️ Назад", "main")]])


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
        show(chat, mid, menu_sessions())
    elif d.startswith("r:"):
        cfg["session"] = d[2:]; save_cfg(); note = "Продолжаю эту сессию"
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
/new или /clear — новая сессия · /resume — прошлые сессии
/effort high — уровень размышлений · /model имя — модель
/plan — режим плана · /mode — режим разрешений
/compact /context /cost /init /review /security_review — команды Claude Code
/stop — остановить · /status — состояние · /files — файлы
/usage — кредиты и остаток · /agents — чаты агентов · /skills — навыки · /repair — починить канал
/ecc — все команды Everything Claude Code (например /code_review, /plan_prd, /tdd)
Любую команду ECC можно писать и через дефис: /code-review"""


def start_task(chat, prompt):
    if not CHANNEL:
        return tg("sendMessage", chat_id=chat, text="Сначала добавь меня админом в канал для файлов.")
    if runs:
        return tg("sendMessage", chat_id=chat, text="Ещё работаю над прошлой задачей — нажми Stop или /stop.")
    threading.Thread(target=run_claude, args=(chat, prompt), daemon=True).start()


def send_long(chat, text, **kw):
    for part in chunks(text):
        tg("sendMessage", chat_id=chat, text=part, **kw)


def status_text():
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
    s = cfg.get("session")
    return (f"🤖 Claude Code: <code>{html.escape(ver or '?')}</code>\n"
            f"Провайдер: <code>{html.escape(cfg['provider'])}</code> · модель <code>{html.escape(cfg['model'])}</code>\n"
            f"Effort: {cfg.get('effort') or 'авто'} · режим: {MODES.get(cfg['mode'])}\n"
            f"Сессия: <code>{s[:8] if s else 'новая'}</code> · задача: {'идёт' if runs else 'нет'}\n"
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
    elif key in ("new", "clear"):
        cfg["session"] = None; save_cfg()
        tg("sendMessage", chat_id=chat, text="🆕 Новая сессия")
    elif key == "resume":
        t, m = menu_sessions()
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
            r["proc"].terminate()
        tg("sendMessage", chat_id=chat, text="⏹ Останавливаю." if runs else "Сейчас ничего не выполняется.")
    elif key == "status":
        tg("sendMessage", chat_id=chat, text=status_text(), parse_mode="HTML")
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
        for a in last_agents.values():
            send_rich(chat, bq(f"🧩 <b>{html.escape(a['name'])}</b>", "\n\n".join(a["log"]) or "—"))
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
        real = next((n for n in ecc_cmds if n.replace("-", "_") == key), name)
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


def on_message(msg):
    global OWNER
    if not OWNER and msg.get("chat", {}).get("type") == "private" and msg.get("text") == "/start":
        OWNER = cfg["owner"] = msg["from"]["id"]
        save_cfg()
        tg("sendMessage", chat_id=OWNER, text="👋 Ты владелец. Теперь добавь меня админом в приватный канал для файлов.")
    if msg.get("from", {}).get("id") != OWNER:
        return
    chat = msg["chat"]["id"]
    if msg.get("text") and waiting.pop(OWNER, None) == "add_provider":
        return add_provider(msg)
    media = media_of(msg)
    if media:
        return collect_media(msg, media)
    text = (msg.get("text") or "").strip()
    if not text:
        return
    if text.startswith("/"):
        text = handle_command(chat, text)
        if not text:
            return
    start_task(chat, text)


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


def register_commands(limit=100):
    cmds = [{"command": c, "description": d} for c, d in BUILTIN]
    seen = {c for c, _ in BUILTIN}
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
    load_ecc_cmds()
    register_commands()
    threading.Thread(target=lambda: (install_ecc(), register_commands()), daemon=True).start()
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
