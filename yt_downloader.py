import os
import re
import json
import datetime
import subprocess
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import ttk, messagebox, filedialog

import yt_dlp

PORTAL_WG_EXE = r"C:\Program Files\PORTAL WG\PORTAL WG.exe"
NODE_PATHS = [
    r"C:\Program Files\nodejs\node.exe",
    r"C:\Program Files (x86)\nodejs\node.exe",
]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADED_FILE = os.path.join(SCRIPT_DIR, "downloaded.json")
QUEUE_FILE = os.path.join(SCRIPT_DIR, "queue.json")


def _today_folder_name(date=None):
    d = date or datetime.date.today()
    return d.strftime("%d.%m.%Y")


def load_queue_file(path=QUEUE_FILE):
    try:
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return []
        data = json.loads(content)
        if isinstance(data, dict):
            data = data.get("items", [])
        if not isinstance(data, list):
            return []
        items = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            vid = (entry.get("video_id") or "").strip()
            url = (entry.get("url") or "").strip()
            if vid and not url:
                url = f"https://www.youtube.com/watch?v={vid}"
            if not vid or not url:
                continue
            try:
                attempts = int(entry.get("attempts") or 0)
            except (TypeError, ValueError):
                attempts = 0
            items.append({
                "video_id": vid,
                "url": url,
                "date_str": (entry.get("date_str") or "").strip() or None,
                "added_at": entry.get("added_at"),
                "attempts": max(0, attempts),
            })
        return items
    except Exception as e:
        print("load_queue error:", e)
        return []


def save_queue_file(items, path=QUEUE_FILE):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp, path)
    except Exception as e:
        print("save_queue error:", e)

CLIPBOARD_POLL_MS = 1500

YT_ID = r"[A-Za-z0-9_-]{11}"
YT_PATTERNS = [
    re.compile(r"(?:youtube\.com|youtube-nocookie\.com)/watch\?(?:[^&\s#]*&)*v=(" + YT_ID + r")"),
    re.compile(r"(?:youtube\.com|youtube-nocookie\.com)/(?:shorts|embed|live|v)/(" + YT_ID + r")"),
    re.compile(r"youtu\.be/(" + YT_ID + r")"),
]

CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")
# Белый список: вырезаем всё, кроме букв, цифр, пробелов и знаков . , … + - ( )
NOT_ALLOWED_RE = re.compile(r"[^\w\s.,…+\-()]", re.UNICODE)


def find_node():
    for path in NODE_PATHS:
        if os.path.exists(path):
            return path
    try:
        out = subprocess.run(
            ["where", "node"], capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        line = (out.stdout or "").strip().splitlines()
        if line:
            return line[0]
    except Exception:
        pass
    return None


def desktop_path():
    return os.path.join(os.path.expanduser("~"), "Desktop")


def vpn_is_up():
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-NetAdapter | Where-Object { $_.InterfaceDescription -match 'WireGuard|Tunnel' } | Select-Object -ExpandProperty Status"],
            capture_output=True, text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return "Up" in (out.stdout or "")
    except Exception:
        return False


def vpn_process_running():
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process | Where-Object { $_.Name -match 'PORTAL|wireguard|amnezia|awg' } | Select-Object -ExpandProperty ProcessName"],
            capture_output=True, text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return bool((out.stdout or "").strip())
    except Exception:
        return False


def extract_video_id(text):
    if not text:
        return None
    for rx in YT_PATTERNS:
        m = rx.search(text)
        if m:
            return m.group(1)
    return None


def extract_all_video_ids(text):
    if not text:
        return []
    found = []
    seen = set()
    for rx in YT_PATTERNS:
        for m in rx.finditer(text):
            vid = m.group(1)
            if vid not in seen:
                seen.add(vid)
                found.append((m.start(), vid))
    found.sort(key=lambda x: x[0])
    return [vid for _pos, vid in found]


def has_cyrillic(text):
    return bool(CYRILLIC_RE.search(text or ""))


def clean_filename(name):
    name = NOT_ALLOWED_RE.sub("", name or "")
    name = name.replace("_", "")
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    if len(name) > 150:
        name = name[:150].rstrip()
    return name or "video"


def _try_translator(engine, text):
    if engine == "google":
        from deep_translator import GoogleTranslator
        result = GoogleTranslator(source="auto", target="ru").translate(text)
    elif engine == "mymemory":
        # MyMemory не понимает source="auto" и короткие коды: только явный
        # язык полными именами (english -> russian).
        from deep_translator import MyMemoryTranslator
        result = MyMemoryTranslator(source="english", target="russian").translate(text)
    else:
        raise ValueError(f"Неизвестный переводчик: {engine}")
    return (result or "").strip()


def translate_to_ru(text):
    """Переводит текст на русский.

    Возвращает (перевод, ошибка): при отсутствии необходимости перевода
    или при успехе ошибка равна None; при неудаче перевод равен исходному
    тексту, а ошибка содержит причину.
    """
    if not text or has_cyrillic(text):
        return text, None
    errors = []
    for engine in ("google", "mymemory"):
        try:
            result = _try_translator(engine, text)
            if result and result.lower() != text.strip().lower():
                return result, None
        except Exception as e:
            errors.append(f"{engine}: {e}")
    return text, ("; ".join(errors) if errors else "переводчики недоступны")


def build_final_title(original):
    """Возвращает (итоговое название, ошибка перевода или None)."""
    original = (original or "").strip()
    if not original:
        return "video", None
    if has_cyrillic(original):
        return original, None
    translated, error = translate_to_ru(original)
    if not translated or translated.strip().lower() == original.strip().lower():
        return original, error
    return f"{translated} ({original})", None


QUALITY_OPTIONS = {
    "1080p (по умолчанию)": "1080",
    "720p": "720",
    "480p": "480",
    "360p": "360",
    "Лучшее": "best",
}


class LogCapture:
    def __init__(self):
        self.lines = []

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        self.lines.append("WARNING: " + msg)

    def error(self, msg):
        self.lines.append("ERROR: " + msg)


class YouTubeDownloaderApp:
    def __init__(self, root):
        self.root = root
        root.title("YouTube Downloader")
        root.geometry("700x680")
        root.resizable(True, True)

        self.cond = threading.Condition()
        self._persist_lock = threading.Lock()
        self.pending = deque()
        self.active_ids = set()
        self.failed_ids = set()
        self.failed_info = {}
        self.queue_meta = {}
        self.downloaded_ids = set()
        self.downloaded_records = []
        self.current = None
        self._last_clip = ""
        self._last_pct = -1

        self.load_downloaded()

        self.frame = ttk.Frame(root, padding=12)
        self.frame.pack(fill="both", expand=True)

        self.notebook = ttk.Notebook(self.frame)
        self.notebook.pack(fill="both", expand=True)

        main_tab = ttk.Frame(self.notebook, padding=12)
        settings_tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(main_tab, text="Главная")
        self.notebook.add(settings_tab, text="Настройки")

        # ---------------- Главная ----------------
        self.url_label = ttk.Label(
            main_tab, text="Ссылки на видео (можно несколько — по одной в строке):"
        )
        self.url_label.pack(anchor="w")

        url_frame = ttk.Frame(main_tab)
        url_frame.pack(fill="x", pady=(4, 4))
        self.url_scroll = ttk.Scrollbar(url_frame)
        self.url_scroll.pack(side="right", fill="y")
        self.url_text = tk.Text(
            url_frame, height=4, wrap="word", relief="solid", borderwidth=1,
            yscrollcommand=self.url_scroll.set,
        )
        self.url_text.pack(side="left", fill="both", expand=True)
        self.url_scroll.config(command=self.url_text.yview)
        self.url_text.bind("<Control-Return>", lambda e: self.start_download())
        self.url_text.bind("<Control-v>", lambda e: self.paste_to(self.url_text))

        self.paste_button = ttk.Button(
            main_tab, text="Вставить", command=lambda: self.paste_to(self.url_text)
        )
        self.paste_button.pack(anchor="e", pady=(0, 8))

        self.setup_context_menu(self.url_text)
        self.url_text.focus_set()

        self.download_button = ttk.Button(
            main_tab, text="Добавить в очередь", command=self.start_download
        )
        self.download_button.pack(fill="x", pady=(4, 8))

        queue_frame = ttk.LabelFrame(main_tab, text="Очередь загрузок", padding=6)
        queue_frame.pack(fill="both", expand=True, pady=(0, 6))

        qhead = ttk.Frame(queue_frame)
        qhead.pack(fill="x")

        self.queue_count_var = tk.StringVar(value="В очереди: 0  •  Скачано: 0")
        self.queue_count_label = ttk.Label(qhead, textvariable=self.queue_count_var)
        self.queue_count_label.pack(side="left")

        self.copy_queue_button = ttk.Button(
            qhead, text="Копировать", command=self.copy_queue
        )
        self.copy_queue_button.pack(side="right")

        list_frame = ttk.Frame(queue_frame)
        list_frame.pack(fill="both", expand=True, pady=(4, 0))

        self.queue_scroll = ttk.Scrollbar(list_frame)
        self.queue_scroll.pack(side="right", fill="y")

        self.queue_listbox = tk.Listbox(
            list_frame,
            height=8,
            yscrollcommand=self.queue_scroll.set,
            relief="solid",
            borderwidth=1,
        )
        self.queue_listbox.pack(side="left", fill="both", expand=True)
        self.queue_scroll.config(command=self.queue_listbox.yview)
        self.queue_listbox.bind("<Control-c>", lambda e: self.copy_queue())

        self.progress = ttk.Progressbar(main_tab, mode="determinate")
        self.progress.pack(fill="x", pady=(0, 6))

        self.status_var = tk.StringVar(value="Готов к работе")
        self.status_label = ttk.Label(
            main_tab, textvariable=self.status_var, anchor="center"
        )
        self.status_label.pack(fill="x")

        self.vpn_var = tk.StringVar(value="Проверка VPN...")
        self.vpn_label = ttk.Label(
            main_tab, textvariable=self.vpn_var, anchor="center"
        )
        self.vpn_label.pack(fill="x", pady=(4, 0))

        vpn_buttons = ttk.Frame(main_tab)
        vpn_buttons.pack(fill="x", pady=(6, 0))

        self.refresh_vpn_button = ttk.Button(
            vpn_buttons, text="Проверить VPN", command=self.refresh_vpn_status
        )
        self.refresh_vpn_button.pack(side="left", fill="x", expand=True, padx=(0, 3))

        self.reconnect_vpn_button = ttk.Button(
            vpn_buttons,
            text="Переподключить VPN (сменить IP)",
            command=self.reconnect_vpn,
        )
        self.reconnect_vpn_button.pack(side="left", fill="x", expand=True, padx=(3, 0))

        # ---------------- Настройки ----------------
        self.quality_label = ttk.Label(settings_tab, text="Качество:")
        self.quality_label.pack(anchor="w")

        self.quality_var = tk.StringVar(value="1080p (по умолчанию)")
        self.quality_combo = ttk.Combobox(
            settings_tab,
            textvariable=self.quality_var,
            values=list(QUALITY_OPTIONS.keys()),
            state="readonly",
            width=40,
        )
        self.quality_combo.pack(anchor="w", pady=(4, 8))

        self.proxy_label = ttk.Label(settings_tab, text="Прокси (необязательно):")
        self.proxy_label.pack(anchor="w")

        self.proxy_var = tk.StringVar(value="")
        self.proxy_entry = ttk.Entry(settings_tab, textvariable=self.proxy_var)
        self.proxy_entry.pack(fill="x", pady=(4, 4))
        self.proxy_entry.bind("<Control-v>", lambda e: self.paste_to(self.proxy_entry))
        self.setup_context_menu(self.proxy_entry)

        self.proxy_hint = ttk.Label(
            settings_tab,
            text="Пример: http://127.0.0.1:8080 или socks5://127.0.0.1:1080",
            foreground="gray",
        )
        self.proxy_hint.pack(anchor="w", pady=(0, 8))

        self.ru_audio_var = tk.BooleanVar(value=True)
        self.ru_audio_check = ttk.Checkbutton(
            settings_tab,
            text="Русская аудиодорожка (если есть)",
            variable=self.ru_audio_var,
        )
        self.ru_audio_check.pack(anchor="w", pady=(0, 4))

        self.translate_var = tk.BooleanVar(value=True)
        self.translate_check = ttk.Checkbutton(
            settings_tab,
            text="Переводить название на русский",
            variable=self.translate_var,
        )
        self.translate_check.pack(anchor="w", pady=(0, 4))

        self.monitor_var = tk.BooleanVar(value=True)
        self.monitor_check = ttk.Checkbutton(
            settings_tab,
            text="Следить за буфером обмена (автоматически добавлять ссылки)",
            variable=self.monitor_var,
        )
        self.monitor_check.pack(anchor="w", pady=(0, 8))

        self.cookies_label = ttk.Label(
            settings_tab,
            text="cookies.txt (пусто = авто с рабочего стола):",
        )
        self.cookies_label.pack(anchor="w")

        default_cookies = os.path.join(desktop_path(), "cookies.txt")
        if not os.path.exists(default_cookies):
            default_cookies = ""
        self.cookies_var = tk.StringVar(value=default_cookies)
        self.cookies_entry = ttk.Entry(settings_tab, textvariable=self.cookies_var)
        self.cookies_entry.pack(fill="x", pady=(4, 4))

        self.cookies_button = ttk.Button(
            settings_tab,
            text="Выбрать файл...",
            command=self.choose_cookies_file,
        )
        self.cookies_button.pack(anchor="e", pady=(0, 4))

        self.browser_cookies_var = tk.BooleanVar(value=False)
        self.browser_cookies_check = ttk.Checkbutton(
            settings_tab,
            text="Взять cookies из Chrome (нужно закрыть Chrome!)",
            variable=self.browser_cookies_var,
        )
        self.browser_cookies_check.pack(anchor="w", pady=(0, 8))

        self.folder_label = ttk.Label(settings_tab, text="Папка для сохранения:")
        self.folder_label.pack(anchor="w")

        self.folder_var = tk.StringVar(value=desktop_path())
        self.folder_entry = ttk.Entry(settings_tab, textvariable=self.folder_var)
        self.folder_entry.pack(fill="x", pady=(4, 4))

        self.folder_button = ttk.Button(
            settings_tab, text="Выбрать...", command=self.choose_folder
        )
        self.folder_button.pack(anchor="e", pady=(0, 8))

        log_frame = ttk.LabelFrame(settings_tab, text="Журнал", padding=6)
        log_frame.pack(fill="both", expand=True, pady=(6, 6))

        self.log_text = tk.Text(log_frame, height=10, wrap="word", state="disabled",
                                relief="solid", borderwidth=1)
        self.log_text.pack(fill="both", expand=True)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.restore_queue()

        self.worker_thread = threading.Thread(target=self.worker_loop, daemon=True)
        self.worker_thread.start()

        self.root.after(300, self.refresh_vpn_status)
        self.root.after(CLIPBOARD_POLL_MS, self.poll_clipboard)
        self.refresh_queue_display()

    # ---------- persistence ----------

    def load_downloaded(self):
        self.downloaded_ids = set()
        self.downloaded_records = []
        if not os.path.exists(DOWNLOADED_FILE):
            return
        try:
            with open(DOWNLOADED_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if not content:
                return
            records = []
            try:
                for line in content.splitlines():
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))
            except json.JSONDecodeError:
                data = json.loads(content)
                records = data if isinstance(data, list) else [data]
            for r in records:
                vid = r.get("video_id")
                if vid:
                    self.downloaded_ids.add(vid)
                self.downloaded_records.append(r)
        except Exception as e:
            print("load_downloaded error:", e)

    def append_record(self, record):
        try:
            with open(DOWNLOADED_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
        except Exception as e:
            self.log(f"Не удалось записать downloaded.json: {e}")

    # ---------- unfinished queue persistence ----------

    def persist_queue(self):
        with self.cond:
            ordered = []
            seen = set()
            if self.current:
                ordered.append(self.current)
            ordered.extend(list(self.pending))
            items = []
            for vid, url in ordered:
                if vid in seen or vid in self.downloaded_ids:
                    continue
                seen.add(vid)
                meta = self.queue_meta.get(vid, {})
                items.append({
                    "video_id": vid,
                    "url": url,
                    "date_str": meta.get("date_str") or _today_folder_name(),
                    "added_at": meta.get("added_at"),
                    "attempts": meta.get("attempts", 0),
                })
            for vid, info in self.failed_info.items():
                if vid in seen or vid in self.downloaded_ids:
                    continue
                seen.add(vid)
                meta = self.queue_meta.get(vid, {})
                items.append({
                    "video_id": vid,
                    "url": info.get("url") or f"https://www.youtube.com/watch?v={vid}",
                    "date_str": meta.get("date_str") or _today_folder_name(),
                    "added_at": meta.get("added_at"),
                    "attempts": meta.get("attempts", 0),
                })
        with self._persist_lock:
            save_queue_file(items)

    def restore_queue(self):
        stored = load_queue_file()
        if not stored:
            return
        added = 0
        with self.cond:
            for entry in stored:
                vid = entry.get("video_id")
                url = entry.get("url")
                if not vid or not url:
                    continue
                if vid in self.downloaded_ids or vid in self.active_ids:
                    continue
                self.active_ids.add(vid)
                self.pending.append((vid, url))
                self.queue_meta[vid] = {
                    "date_str": entry.get("date_str") or _today_folder_name(),
                    "added_at": entry.get("added_at"),
                    "attempts": entry.get("attempts", 0),
                }
                self.failed_ids.discard(vid)
                self.failed_info.pop(vid, None)
                added += 1
            if added:
                self.cond.notify_all()
        if added:
            self.log(f"Восстановлено недокачанных: {added} (докачка продолжится)")
            self.refresh_queue_display()
        self.persist_queue()

    def on_close(self):
        try:
            self.persist_queue()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    # ---------- queue / worker ----------

    def start_download(self):
        text = self.url_text.get("1.0", tk.END).strip()
        if not text:
            messagebox.showwarning("Внимание", "Вставьте ссылку или список ссылок")
            return
        ids = extract_all_video_ids(text)
        if ids:
            added = 0
            for vid in ids:
                if self.enqueue(vid, f"https://www.youtube.com/watch?v={vid}", source="вручную"):
                    added += 1
            self.log(f"Добавлено в очередь: {added} из {len(ids)}")
        else:
            self.enqueue(text, text, source="вручную")
        self.url_text.delete("1.0", tk.END)

    def enqueue(self, vid, url, source="clipboard"):
        with self.cond:
            if vid in self.downloaded_ids:
                self.log(f"Пропуск (уже скачано): {vid}")
                return False
            if vid in self.active_ids:
                return False
            if source == "clipboard" and vid in self.failed_ids:
                return False
            self.active_ids.add(vid)
            self.pending.append((vid, url))
            self.queue_meta[vid] = {
                "date_str": _today_folder_name(),
                "added_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "attempts": 0,
            }
            self.cond.notify()
        self.persist_queue()
        self.log(f"В очередь [{source}]: {vid}")
        self.root.after(0, self.refresh_queue_display)
        return True

    def worker_loop(self):
        while True:
            with self.cond:
                while not self.pending:
                    self.cond.wait()
                vid, url = self.pending.popleft()
                self.current = (vid, url)
            self.persist_queue()
            self.root.after(0, self.refresh_queue_display)
            try:
                self.process(vid, url)
            finally:
                with self.cond:
                    self.current = None
                    self.active_ids.discard(vid)
                self.persist_queue()
                self.root.after(0, self.refresh_queue_display)

    def process(self, vid, url):
        self.set_status(f"Обработка {vid}...")
        with self.cond:
            meta = self.queue_meta.get(vid, {})
            date_str = meta.get("date_str") or _today_folder_name()
        base_folder = self.folder_var.get()
        date_folder = os.path.join(base_folder, date_str)
        try:
            original_title, is_live, live_status = self.extract_meta(url)
            if is_live or live_status in ("is_live", "is_upcoming"):
                reason = (
                    "запланированная трансляция ещё не началась"
                    if live_status == "is_upcoming"
                    else "прямой эфир (трансляция идёт)"
                )
                with self.cond:
                    self.failed_ids.add(vid)
                    self.failed_info[vid] = {"url": url, "reason": "live"}
                    cur = self.queue_meta.get(vid, {})
                    cur["attempts"] = cur.get("attempts", 0) + 1
                    cur.setdefault("date_str", date_str)
                    self.queue_meta[vid] = cur
                self.persist_queue()
                self.log(f"Пропуск {vid}: {reason} — качание эфиров не поддерживается")
                self.set_status("Пропущен эфир (см. журнал)")
                return
            if self.translate_var.get():
                final_title, trans_error = build_final_title(original_title)
                if trans_error:
                    self.log(f"Перевод названия не удался ({trans_error}); сохранён оригинал")
            else:
                final_title = original_title or vid
            fname = clean_filename(final_title)
            outtmpl = os.path.join(date_folder, fname + ".%(ext)s")
            ok, path, err = self.download_one(url, outtmpl)
            if ok:
                record = {
                    "video_id": vid,
                    "url": url,
                    "original_title": original_title,
                    "final_title": final_title,
                    "file_path": path,
                    "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
                }
                with self.cond:
                    self.downloaded_ids.add(vid)
                    self.downloaded_records.append(record)
                    self.queue_meta.pop(vid, None)
                    self.failed_ids.discard(vid)
                    self.failed_info.pop(vid, None)
                self.append_record(record)
                self.persist_queue()
                self.log(f"Скачано: {final_title}")
                self.set_status("Готово!")
            else:
                with self.cond:
                    self.failed_ids.add(vid)
                    self.failed_info[vid] = {"url": url}
                    cur = self.queue_meta.get(vid, {})
                    cur["attempts"] = cur.get("attempts", 0) + 1
                    cur.setdefault("date_str", date_str)
                    self.queue_meta[vid] = cur
                self.persist_queue()
                self.log(f"Ошибка {vid}: {err[:300]}")
                self.set_status("Ошибка (см. журнал)")
        except Exception as e:
            with self.cond:
                self.failed_ids.add(vid)
                self.failed_info[vid] = {"url": url}
                cur = self.queue_meta.get(vid, {})
                cur["attempts"] = cur.get("attempts", 0) + 1
                cur.setdefault("date_str", date_str)
                self.queue_meta[vid] = cur
            self.persist_queue()
            self.log(f"Сбой {vid}: {e}")
            self.set_status("Ошибка (см. журнал)")

    def extract_meta(self, url):
        for client in [None, ["web_embedded"], ["android_vr"], ["android"]]:
            try:
                opts = self.build_opts(client, None, None, skip=True)
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    if not info:
                        continue
                    return (
                        info.get("title") or "",
                        bool(info.get("is_live")),
                        info.get("live_status"),
                    )
            except Exception:
                continue
        return "", False, None

    def download_one(self, url, outtmpl):
        quality = QUALITY_OPTIONS[self.quality_var.get()]
        ru_audio = "[language=ru]" if self.ru_audio_var.get() else ""
        if quality == "best":
            fmt = f"bestvideo+bestaudio{ru_audio}/bestvideo+bestaudio/best"
        else:
            fmt = (
                f"bestvideo[height<={quality}]+bestaudio{ru_audio}"
                f"/bestvideo[height<={quality}]+bestaudio"
                f"/best[height<={quality}]"
            )
        # Клиенты по умолчанию — первыми: только они отдают даб-дорожки
        # (включая автодубляж). web_embedded/android — запасные варианты.
        attempts = [
            (fmt, None),
            (fmt, ["web_embedded"]),
            ("18", ["android"]),
        ]
        last_error = ""
        opts = None
        for _round in range(2):
            for f, client in attempts:
                opts = None
                try:
                    os.makedirs(os.path.dirname(outtmpl), exist_ok=True)
                    opts = self.build_opts(client, f, outtmpl)
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(url, download=True)
                        path = info.get("filepath")
                        if not path:
                            rd = info.get("requested_downloads") or []
                            if rd:
                                path = rd[0].get("filepath")
                        return True, path, ""
                except Exception as e:
                    detail = str(e).strip()
                    if not detail and opts is not None:
                        detail = "\n".join(opts["logger"].lines[-50:])
                    if not detail:
                        detail = str(e) or repr(e)
                    last_error = detail
                    # Недоступность основного формата не должна отменять
                    # следующий вариант: для некоторых видео web_embedded
                    # отдаёт только storyboard, а android всё ещё доступен.
                    if self._is_retryable(detail):
                        time.sleep(2)
        return False, None, last_error

    @staticmethod
    def _is_retryable(detail):
        d = (detail or "").lower()
        markers = (
            "http error 403", "unable to download", "getaddrinfo", "failed to resolve",
            "temporary failure", "timed out", "timeout", "connection", "reset by peer",
            "remote end closed", "http error 5", "eof occurred", "ssl", "network",
            "too slow", "throttl",
        )
        return any(m in d for m in markers)

    def build_opts(self, client, fmt, outtmpl, skip=False):
        opts = {
            "outtmpl": outtmpl or os.path.join(self.folder_var.get(), "%(title)s.%(ext)s"),
            "noplaylist": True,
            "continuedl": True,
            "retries": 5,
            "fragment_retries": 5,
            "socket_timeout": 15,
            "force_ipv4": True,
            "throttled_rate": 100000,
            "concurrent_fragment_downloads": 4,
            "logger": LogCapture(),
            "merge_output_format": "mp4",
        }
        if os.path.exists(os.path.join(SCRIPT_DIR, "ffmpeg.exe")):
            opts["ffmpeg_location"] = SCRIPT_DIR
        if fmt:
            opts["format"] = fmt
        if skip:
            opts["skip_download"] = True
        else:
            opts["progress_hooks"] = [self.progress_hook]

        node_path = find_node()
        if node_path:
            opts["js_runtimes"] = {"node": {"path": node_path}}

        proxy = self.proxy_var.get().strip()
        if proxy:
            opts["proxy"] = proxy
        if client:
            opts["extractor_args"] = {"youtube": {"player_client": client}}

        cookies_file = self.cookies_var.get().strip()
        if not cookies_file:
            auto = os.path.join(desktop_path(), "cookies.txt")
            if os.path.exists(auto):
                cookies_file = auto
        if cookies_file:
            opts["cookiefile"] = cookies_file
        elif self.browser_cookies_var.get():
            opts["cookiesfrombrowser"] = ("chrome",)
        return opts

    def progress_hook(self, d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            if total:
                percent = downloaded / total * 100
                if int(percent) != self._last_pct:
                    self._last_pct = int(percent)
                    self.root.after(0, self.update_progress, percent)
        elif d["status"] == "finished":
            self.root.after(0, self.set_status, "Загрузка завершена, финализация...")

    def update_progress(self, percent):
        self.progress["value"] = percent
        self.status_var.set(f"Скачивание... {percent:.0f}%")

    def set_status(self, text):
        self.status_var.set(text)

    def log(self, msg):
        def _do():
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            self.log_text.configure(state="normal")
            self.log_text.insert(tk.END, f"[{ts}] {msg}\n")
            self.log_text.see(tk.END)
            self.log_text.configure(state="disabled")
        self.root.after(0, _do)

    def refresh_queue_display(self):
        with self.cond:
            cur = self.current
            pend = list(self.pending)
            done = len(self.downloaded_ids)
        self.queue_listbox.delete(0, tk.END)
        if cur:
            self.queue_listbox.insert(tk.END, f"> {cur[0]}")
        for vid, _url in pend:
            self.queue_listbox.insert(tk.END, f"  {vid}")
        if not cur and not pend:
            self.queue_listbox.insert(tk.END, "  (пусто)")
        self.queue_count_var.set(f"В очереди: {len(pend)}  •  Скачано: {done}")

    def copy_queue(self):
        with self.cond:
            urls = []
            if self.current:
                urls.append(self.current[1])
            for _vid, url in self.pending:
                urls.append(url)
        if not urls:
            self.log("Очередь пуста — копировать нечего")
            return
        text = "\n".join(urls)
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update()
            self.log(f"Скопировано в буфер: {len(urls)} ссылок")
        except tk.TclError as e:
            self.log(f"Не удалось скопировать: {e}")

    # ---------- clipboard monitor ----------

    def poll_clipboard(self):
        if self.monitor_var.get():
            try:
                text = self.root.clipboard_get()
            except tk.TclError:
                text = ""
            if text and text != self._last_clip:
                self._last_clip = text
                for vid in extract_all_video_ids(text):
                    self.enqueue(vid, f"https://www.youtube.com/watch?v={vid}", source="clipboard")
        self.root.after(CLIPBOARD_POLL_MS, self.poll_clipboard)

    # ---------- VPN ----------

    def reconnect_vpn(self):
        def worker():
            try:
                subprocess.run(
                    ["taskkill", "/F", "/IM", "PORTAL WG.exe"],
                    capture_output=True, text=True, timeout=15,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except Exception:
                pass
            time.sleep(5)
            try:
                subprocess.Popen(
                    [PORTAL_WG_EXE],
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except Exception:
                pass
            self.root.after(0, self.refresh_vpn_status)

        self.status_var.set("Переподключение VPN... (5 секунд)")
        threading.Thread(target=worker, daemon=True).start()

    def refresh_vpn_status(self):
        def check():
            up = vpn_is_up()
            proc = vpn_process_running()
            self.root.after(0, self._set_vpn_status, up, proc)

        threading.Thread(target=check, daemon=True).start()

    def _set_vpn_status(self, up, proc):
        if up:
            text = "VPN: подключен (WireGuard-туннель активен)"
            fg = "#1a7f37"
        elif proc:
            text = "VPN: программа запущена, но туннель не поднят"
            fg = "#b06000"
        else:
            text = "VPN: не подключен (рекомендуется для стабильного скачивания)"
            fg = "#c62828"
        self.vpn_var.set(text)
        self.vpn_label.config(foreground=fg)

    # ---------- misc UI helpers ----------

    def setup_context_menu(self, widget):
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Вырезать", command=lambda: widget.event_generate("<<Cut>>"))
        menu.add_command(label="Копировать", command=lambda: widget.event_generate("<<Copy>>"))
        menu.add_command(label="Вставить", command=lambda: widget.event_generate("<<Paste>>"))
        widget.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))
        widget.bind("<Control-x>", lambda e: widget.event_generate("<<Cut>>"))
        widget.bind("<Control-c>", lambda e: widget.event_generate("<<Copy>>"))

    def paste_to(self, widget, event=None):
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            return "break"
        if isinstance(widget, tk.Text):
            widget.insert(tk.INSERT, text)
        else:
            widget.delete(0, tk.END)
            widget.insert(0, text)
        return "break"

    def choose_folder(self):
        folder = filedialog.askdirectory(initialdir=self.folder_var.get())
        if folder:
            self.folder_var.set(folder)

    def choose_cookies_file(self):
        file = filedialog.askopenfilename(
            initialdir=desktop_path(),
            title="Выберите файл cookies.txt",
            filetypes=[("Netscape cookies", "*.txt"), ("Все файлы", "*.*")],
        )
        if file:
            self.cookies_var.set(file)


if __name__ == "__main__":
    root = tk.Tk()
    app = YouTubeDownloaderApp(root)
    root.mainloop()
