"""Быстрый режим: ответ сразу потоком от модели провайдера, без запуска Claude Code.

Руки быстрого режима — Render: bash-команды выполняются там (rexec /exec, с интернетом),
страницы читаются напрямую. Если задача требует настоящей работы с проектом (код, файлы, сборка,
долгие задачи), модель сама передаёт её Claude Code на GitHub Actions инструментом claude_code.
"""
import os, re, json, html, time, requests

HIST_N = 24
TOOLS = [
    {"name": "bash", "description": "Выполнить bash-команду на Linux-сервере Render (есть интернет, curl, python3, git): "
     "посчитать, проверить, скачать, узнать. Таймаут 120 с. НЕ для создания или правки файлов проекта — "
     "они на другом сервере и отсюда не сохранятся; для этого claude_code.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "web_fetch", "description": "Прочитать веб-страницу как текст. Для поиска в интернете: "
     "https://html.duckduckgo.com/html/?q=<запрос>",
     "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    {"name": "chart", "description": "Построить график или диаграмму. config — конфиг Chart.js (как в QuickChart): "
     "type (line, bar, pie, doughnut, radar, scatter…), data (labels, datasets), options. Картинка прикрепится к ответу.",
     "input_schema": {"type": "object", "properties": {"config": {"type": "object"}, "caption": {"type": "string"}},
                      "required": ["config"]}},
    {"name": "tell_agent", "description": "Передать уточнение, дополнение или новые данные агенту Claude Code, который СЕЙЧАС "
     "работает над задачей (он получит это прямо во время работы и учтёт). Используй, когда пользователь уточняет, "
     "поправляет или дополняет уже идущую задачу агента.",
     "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "claude_code", "description": "Передать задачу полноценному Claude Code (запуск ~30–60 с): писать или менять "
     "код и файлы проекта, большие многошаговые задачи, агенты, сборки, анализ присланных файлов. "
     "Передай полную формулировку задачи с нужным контекстом из разговора. После вызова больше ничего не пиши.",
     "input_schema": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}},
]


def hist_path(here, chat):
    d = os.path.join(here, "fast_hist")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{chat}.json")


def load_hist(here, chat):
    try:
        return json.load(open(hist_path(here, chat)))
    except Exception:
        return []


def save_hist(here, chat, h):
    json.dump(h[-HIST_N:], open(hist_path(here, chat), "w"), ensure_ascii=False)


def add_note(here, chat, user, assistant):
    """Добавить в историю быстрого режима событие (например, ответ агента), чтобы о нём можно было спросить."""
    h = load_hist(here, chat)
    if h and h[-1]["role"] == "user":
        h.append({"role": "assistant", "content": "…"})
    save_hist(here, chat, h + [{"role": "user", "content": user}, {"role": "assistant", "content": assistant}])


def stash_hist(here, chat, sid):
    """Сохранить историю быстрого режима за сессией sid (при переключении сессий)."""
    src = hist_path(here, chat)
    if os.path.exists(src):
        os.replace(src, os.path.join(os.path.dirname(src), f"{chat}__{sid}.json"))


def unstash_hist(here, chat, sid):
    src = os.path.join(os.path.dirname(hist_path(here, chat)), f"{chat}__{sid}.json")
    if os.path.exists(src):
        os.replace(src, hist_path(here, chat))


def clear_hist(here, chat):
    try:
        os.remove(hist_path(here, chat))
    except OSError:
        pass


def _html_text(s):
    s = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def make_chart(inp):
    r = requests.post("https://quickchart.io/chart/create", timeout=30,
                      json={"chart": inp.get("config") or {}, "width": 900, "height": 520,
                            "backgroundColor": "white", "version": "4"})
    j = r.json()
    if not j.get("success") or not j.get("url"):
        raise RuntimeError(str(j)[:300])
    return j["url"]


def run_tool(name, inp, rexec_url, rexec_token):
    try:
        if name == "bash":
            r = requests.post(rexec_url.rstrip("/") + "/exec", headers={"X-Token": rexec_token}, timeout=180,
                              json={"cmd": inp.get("command", ""), "timeout": 120})
            j = r.json()
            out = (j.get("stdout", "") + ("\n[stderr]\n" + j["stderr"] if j.get("stderr") else ""))
            return f"exit {j.get('code')}\n{out[-12000:]}"
        if name == "web_fetch":
            r = requests.get(inp.get("url", ""), timeout=25, headers={"User-Agent": "Mozilla/5.0"})
            t = r.text
            if "html" in r.headers.get("Content-Type", ""):
                t = _html_text(t)
            return f"HTTP {r.status_code}\n{t[:15000]}"
    except Exception as e:
        return f"ошибка: {e}"
    return "нет такого инструмента"


def stream(base, token, model, system, messages, on_text, stopped, tools=TOOLS, on_think=None):
    """Один запрос /v1/messages со стримингом. Возвращает (content_blocks, stop_reason, usage)."""
    url = (base or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
    hdr = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
    if base:  # свой провайдер — как ANTHROPIC_AUTH_TOKEN у Claude Code
        hdr["Authorization"] = "Bearer " + token
    else:
        hdr["x-api-key"] = token
    body = {"model": model, "max_tokens": 8000, "system": system, "messages": messages, "stream": True}
    if tools:
        body["tools"] = tools
    blocks, stop, usage = [], None, {}
    with requests.post(url, headers=hdr, json=body, stream=True, timeout=(15, 300)) as r:
        if r.status_code != 200:
            raise RuntimeError(f"провайдер: {r.status_code} {r.text[:300]}")
        r.encoding = "utf-8"
        for raw in r.iter_lines(decode_unicode=True):
            if stopped():
                break
            if not raw or not raw.startswith("data:"):
                continue
            try:
                ev = json.loads(raw[5:])
            except ValueError:
                continue
            t = ev.get("type")
            if t == "content_block_start":
                b = dict(ev["content_block"])
                if b.get("type") == "tool_use":
                    b["_json"] = ""
                blocks.append(b)
            elif t == "content_block_delta":
                d, b = ev["delta"], blocks[-1] if blocks else None
                if b is None:
                    continue
                if d.get("type") == "text_delta":
                    b["text"] = b.get("text", "") + d["text"]
                    on_text(d["text"])
                elif d.get("type") == "thinking_delta" and on_think:
                    on_think(d.get("thinking", ""))
                elif d.get("type") == "input_json_delta":
                    b["_json"] += d.get("partial_json", "")
            elif t == "message_delta":
                stop = ev.get("delta", {}).get("stop_reason") or stop
                usage.update(ev.get("usage") or {})
            elif t == "message_start":
                usage.update(ev.get("message", {}).get("usage") or {})
    for b in blocks:
        if b.get("type") == "tool_use":
            try:
                b["input"] = json.loads(b.pop("_json") or "{}")
            except ValueError:
                b["input"] = {}
        b.pop("_json", None)
    return [b for b in blocks if b.get("type") == "tool_use" or (b.get("type") == "text" and b.get("text"))], stop, usage


def answer(here, chat, prompt, base, token, model, system, rexec_url, rexec_token, ui, stopped, guest=False):
    """Цикл агента. ui: dict-колбэки text(delta), step(label), new_text().
    Возвращает {"text", "delegate", "steps", "in", "out"}."""
    hist = load_hist(here, chat)
    msgs = hist + [{"role": "user", "content": prompt}]
    res = {"text": "", "delegate": None, "steps": [], "in": 0, "out": 0, "images": []}
    think = ui.get("think")
    tools = [t for t in TOOLS if t["name"] in ("web_fetch", "chart")] if guest else TOOLS  # гостям — без сервера
    tools = [t for t in tools if t["name"] != "tell_agent" or ui.get("tell_agent")]
    tools = tools + [t for t in ui.get("extra_tools", []) if not guest or t.get("guest")]
    for _ in range(10):
        blocks, stop, usage = stream(base, token, model, system, msgs, ui["text"], stopped, tools, think)
        res["in"] += usage.get("input_tokens", 0)
        res["out"] += usage.get("output_tokens", 0)
        text = "".join(b.get("text", "") for b in blocks if b["type"] == "text")
        calls = [b for b in blocks if b["type"] == "tool_use"]
        if stopped():
            res["text"] = text
            break
        if not calls and not text.strip():  # некоторые модели с tools только «думают» — переспрашиваем без tools
            blocks, stop, usage = stream(base, token, model, system, msgs, ui["text"], stopped, [], think)
            res["out"] += usage.get("output_tokens", 0)
            text = "".join(b.get("text", "") for b in blocks if b["type"] == "text")
        if not calls:
            res["text"] = text
            break
        msgs.append({"role": "assistant", "content": blocks})
        results = []
        for c in calls:
            if c["name"] == "claude_code" and not guest:
                res["delegate"] = c["input"].get("task") or prompt
                res["text"] = text
                break
            if c["name"] == "tell_agent" and ui.get("tell_agent"):
                label = "📨 агенту: " + c["input"].get("text", "")[:70]
                res["steps"].append(label)
                ui["step"](label)
                results.append({"type": "tool_result", "tool_use_id": c["id"], "content": ui["tell_agent"](c["input"].get("text", ""))})
                continue
            if c["name"] in ui.get("handlers", {}):
                label, out, img = ui["handlers"][c["name"]](c["input"])
                res["steps"].append(label)
                ui["step"](label)
                if img:
                    res["images"].append(img)
                results.append({"type": "tool_result", "tool_use_id": c["id"], "content": out})
                continue
            if c["name"] == "chart":
                label = "📊 " + (c["input"].get("caption") or "график")[:80]
                res["steps"].append(label)
                ui["step"](label)
                try:
                    res["images"].append((make_chart(c["input"]), c["input"].get("caption", "")))
                    out = "Готово, график будет приложен к ответу отдельной картинкой. Не вставляй ссылку на него."
                except Exception as e:
                    out = f"ошибка построения графика: {e}"
                results.append({"type": "tool_result", "tool_use_id": c["id"], "content": out})
                continue
            label = ("⚙️ " + c["input"].get("command", "")[:80]) if c["name"] == "bash" else \
                ("🌐 " + c["input"].get("url", "")[:80])
            res["steps"].append(label)
            ui["step"](label)
            results.append({"type": "tool_result", "tool_use_id": c["id"],
                            "content": run_tool(c["name"], c["input"], rexec_url, rexec_token)})
        if res["delegate"]:
            break
        extra = ui["inbox"]() if ui.get("inbox") else []
        for t in extra:  # пользователь дописал, пока шла работа
            results.append({"type": "text", "text": "Пользователь дописал во время работы: " + t})
            res.setdefault("consumed", []).append(t)
        msgs.append({"role": "user", "content": results})
        ui["new_text"]()
    # в историю — только текст, без длинных результатов инструментов
    note = res["text"] or ("[передано Claude Code: " + res["delegate"][:300] + "]" if res["delegate"] else "")
    save_hist(here, chat, hist + [{"role": "user", "content": prompt}, {"role": "assistant", "content": note or "…"}])
    return res
