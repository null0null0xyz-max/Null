"""Файловая система поверх Telegram-канала.

Каждый файл = документ в канале с подписью-путём ("app.py" и "/app.py" -> корень,
"projects/app.py" -> папка projects). Закреплённое сообщение = корень с навигацией.
У каждой папки своё сообщение-оглавление. Карта path -> message_id/file_id хранится
в index.json (тоже документ в канале); его file_id спрятан в ссылке ⚙️ закрепа,
поэтому после перезапуска бот восстанавливает всё через getChat.
"""
import os, io, json, html, hashlib, re, requests

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".cache", ".claude"}
MAX_FILE = 45 * 1024 * 1024  # бот загружает до 50 МБ, скачивает до 20 МБ
MAX_DOWNLOAD = 20 * 1024 * 1024


def norm(path):
    p = path.strip().replace("\\", "/").lstrip("/")
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if not parts or ".." in parts:
        raise ValueError(f"плохой путь: {path!r}")
    return "/".join(parts)


def sha(data):
    return hashlib.sha256(data).hexdigest()


class TgFS:
    def __init__(self, token, channel_id, workspace, cache_path="index.json"):
        self.api = f"https://api.telegram.org/bot{token}/"
        self.fapi = f"https://api.telegram.org/file/bot{token}/"
        self.cid = int(channel_id)
        self.ws = os.path.abspath(workspace)
        self.cache = cache_path
        self.idx = {"files": {}, "dirs": {}, "dir_text": {}, "index_mid": None}
        os.makedirs(self.ws, exist_ok=True)

    # ---------- Telegram helpers ----------
    def call(self, method, files=None, **data):
        if files:
            data = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in data.items()}
            r = requests.post(self.api + method, data=data, files=files, timeout=120)
        else:
            r = requests.post(self.api + method, json=data, timeout=60)
        j = r.json()
        if not j.get("ok") and "not modified" not in j.get("description", ""):
            raise RuntimeError(f"{method}: {j.get('description')}")
        return j.get("result")

    def link(self, mid):
        return f"https://t.me/c/{str(self.cid).removeprefix('-100')}/{mid}"

    def download(self, file_id):
        f = self.call("getFile", file_id=file_id)
        return requests.get(self.fapi + f["file_path"], timeout=120).content

    # ---------- index ----------
    def load(self):
        """Локальный кэш -> иначе index.json из закрепа -> иначе пустой канал."""
        if os.path.exists(self.cache):
            self.idx = json.load(open(self.cache))
            return
        chat = self.call("getChat", chat_id=self.cid)
        pin = chat.get("pinned_message") or {}
        for e in pin.get("entities", []):
            m = re.search(r"[?&]idx=([\w-]+)", e.get("url", ""))
            if m:
                self.idx = json.loads(self.download(m.group(1)))
                self._save_cache()
                return

    def _save_cache(self):
        json.dump(self.idx, open(self.cache, "w"))

    # ---------- workspace <-> channel ----------
    def restore(self):
        """Скачать все файлы из канала в рабочую папку (после перезапуска)."""
        for path, meta in self.idx["files"].items():
            dst = os.path.join(self.ws, path)
            if os.path.exists(dst) and sha(open(dst, "rb").read()) == meta["sha"]:
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            open(dst, "wb").write(self.download(meta["fid"]))

    def scan(self):
        out = {}
        for root, dirs, files in os.walk(self.ws):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in files:
                full = os.path.join(root, name)
                if os.path.getsize(full) > MAX_FILE:
                    continue
                out[norm(os.path.relpath(full, self.ws))] = full
        return out

    def put(self, path, data):
        path = norm(path)
        name = path.split("/")[-1]
        meta = self.idx["files"].get(path)
        if meta and meta["sha"] == sha(data):
            return
        if meta:  # editMessageMedia сохраняет message_id -> ссылки не ломаются
            msg = self.call("editMessageMedia", chat_id=self.cid, message_id=meta["mid"],
                            media={"type": "document", "media": "attach://f", "caption": path},
                            files={"f": (name, data)})
        else:
            msg = self.call("sendDocument", chat_id=self.cid, caption=path,
                            disable_notification=True, files={"document": (name, data)})
        self.idx["files"][path] = {"mid": msg["message_id"], "fid": msg["document"]["file_id"], "sha": sha(data)}

    def rm(self, path):
        meta = self.idx["files"].pop(norm(path), None)
        if meta:
            try:
                self.call("deleteMessage", chat_id=self.cid, message_id=meta["mid"])
            except RuntimeError:
                pass

    def sync(self):
        """Залить изменения из рабочей папки в канал. Возвращает (+добавлено/изменено, -удалено)."""
        local = self.scan()
        changed = 0
        for path, full in local.items():
            data = open(full, "rb").read()
            before = self.idx["files"].get(path, {}).get("sha")
            self.put(path, data)
            changed += before != self.idx["files"][path]["sha"]
        gone = [p for p in self.idx["files"] if p not in local]
        for p in gone:
            self.rm(p)
        if changed or gone or not self.idx["dirs"].get(""):
            self.rebuild_nav()
        return changed, len(gone)

    def on_channel_post(self, post):
        """Ты сам кинул документ в канал с подписью-путём -> добавить в ФС."""
        doc, cap = post.get("document"), (post.get("caption") or "").strip()
        if not doc or not cap or doc.get("file_size", 0) > MAX_DOWNLOAD:
            return False
        path = norm(cap)
        old = self.idx["files"].get(path)
        data = self.download(doc["file_id"])
        dst = os.path.join(self.ws, path)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        open(dst, "wb").write(data)
        self.idx["files"][path] = {"mid": post["message_id"], "fid": doc["file_id"], "sha": sha(data)}
        if old and old["mid"] != post["message_id"]:
            try:
                self.call("deleteMessage", chat_id=self.cid, message_id=old["mid"])
            except RuntimeError:
                pass
        self.rebuild_nav()
        return True

    # ---------- навигация ----------
    def tree(self):
        dirs = {"": ([], [])}
        for path in sorted(self.idx["files"]):
            parts = path.split("/")
            for i in range(len(parts) - 1):
                parent, d = "/".join(parts[:i]), "/".join(parts[:i + 1])
                dirs.setdefault(d, ([], []))
                if d not in dirs[parent][0]:
                    dirs[parent][0].append(d)
            dirs["/".join(parts[:-1])][1].append(path)
        return dirs

    def render_dir(self, d, subdirs, files, index_fid=None):
        title = "/" + d if d else "/"
        lines = [f"📂 <b>{html.escape(title)}</b>"]
        if d:
            parent = d.rsplit("/", 1)[0] if "/" in d else ""
            lines.append(f'<a href="{self.link(self.idx["dirs"][parent])}">⬆️ ..</a>')
        for s in subdirs:
            lines.append(f'📁 <a href="{self.link(self.idx["dirs"][s])}">{html.escape(s.split("/")[-1])}/</a>')
        for f in files:
            lines.append(f'📄 <a href="{self.link(self.idx["files"][f]["mid"])}">{html.escape(f.split("/")[-1])}</a>')
        if len(lines) == 1:
            lines.append("<i>пусто</i>")
        if index_fid:
            lines.append(f'\n<a href="https://t.me/?idx={index_fid}">⚙️</a> файлов: {len(self.idx["files"])}')
        text = "\n".join(lines)
        return text if len(text) < 4000 else text[:3990] + "\n…"

    def rebuild_nav(self):
        dirs = self.tree()
        for d in sorted(dirs, key=lambda x: (x.count("/"), x)):  # создать недостающие оглавления
            if not self.idx["dirs"].get(d):
                m = self.call("sendMessage", chat_id=self.cid, text="📂 …", disable_notification=True)
                self.idx["dirs"][d] = m["message_id"]
                self.idx["dir_text"][d] = ""
                if d == "":
                    self.call("pinChatMessage", chat_id=self.cid, message_id=m["message_id"],
                              disable_notification=True)
        for d in [x for x in self.idx["dirs"] if x not in dirs]:  # удалить пустые папки
            try:
                self.call("deleteMessage", chat_id=self.cid, message_id=self.idx["dirs"].pop(d))
            except RuntimeError:
                pass
            self.idx["dir_text"].pop(d, None)
        for d, (subdirs, files) in dirs.items():
            if d == "":
                continue
            self._edit_dir(d, self.render_dir(d, subdirs, files))
        index_fid = self._upload_index()
        self._edit_dir("", self.render_dir("", *dirs[""], index_fid=index_fid))
        self._save_cache()

    def _edit_dir(self, d, text):
        if self.idx["dir_text"].get(d) == text:
            return
        self.call("editMessageText", chat_id=self.cid, message_id=self.idx["dirs"][d], text=text,
                  parse_mode="HTML", link_preview_options={"is_disabled": True})
        self.idx["dir_text"][d] = text

    def _upload_index(self):
        data = json.dumps(self.idx).encode()
        if self.idx.get("index_mid"):
            msg = self.call("editMessageMedia", chat_id=self.cid, message_id=self.idx["index_mid"],
                            media={"type": "document", "media": "attach://f", "caption": "⚙️ index"},
                            files={"f": ("index.json", data)})
        else:
            msg = self.call("sendDocument", chat_id=self.cid, caption="⚙️ index", disable_notification=True,
                            files={"document": ("index.json", data)})
            self.idx["index_mid"] = msg["message_id"]
        return msg["document"]["file_id"]
