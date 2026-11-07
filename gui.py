"""Tkinter-интерфейс приложения YouTube Downloader.

Класс-приложение: очередь воркера, буфер обмена, настройки, эфиры, VPN.
Код хождения в сеть живёт в downloader.py, состояние — в config.py.
"""
import datetime
import json
import os
import queue as ui_queue
import subprocess
import threading
import time
from collections import deque

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import config
import downloader
from config import (
    DOWNLOADED_FILE,
    QUALITY_OPTIONS,
    load_queue_file,
    load_settings,
    load_temp_state,
    save_queue_file,
    save_settings,
    save_temp_state,
    set_temp_override,
    temp_folder,
    today_folder_name,
)
from temp_store import move_to_target, prepare_temp_folder
from translators import build_final_title
from utils import clean_filename, desktop_path, extract_all_video_ids, vpn_status

CLIPBOARD_POLL_MS = 1500

# Отложенные эфиры: интервал перепроверки и ожидание за один запуск.
# После исчерпания лимита эфир остаётся в очереди до следующих запусков.
LIVE_RECHECK_SEC = 300
LIVE_SESSION_LIMIT_SEC = 3 * 3600
LIVE_MAX_CHECKS = LIVE_SESSION_LIMIT_SEC // LIVE_RECHECK_SEC


class YouTubeDownloaderApp:
    def __init__(self, root):
        self.root = root
        root.title("YouTube Downloader")
        root.geometry("700x680")
        root.resizable(True, True)

        self.cond = threading.Condition()
        self._persist_lock = threading.Lock()
        self.pending = deque()
        self.deferred_live = {}
        self.active_ids = set()
        self.failed_ids = set()
        self.failed_info = {}
        self.queue_meta = {}
        self.downloaded_ids = set()
        self.downloaded_records = []
        self.current = None
        self._last_clip = ""
        self._last_pct = -1
        self._settings_job = None
        # Какие видео уже показаны в немодальном окне «Уже скачано»
        self._redownload_prompts = set()
        # Задачи для GUI-потока: Tk нельзя трогать из рабочих потоков
        self._ui_queue = ui_queue.Queue()

        # Сохранённые настройки применяются к дефолтам всех полей
        self.settings = load_settings()
        # Temp-папка может быть переопределена ключом temp_folder в settings.json
        set_temp_override(self.settings.get("temp_folder"))
        # Папка нужна и на главной вкладке (подпись), и в настройках
        self.folder_var = tk.StringVar(value=self.default_folder())

        # Папка temp создаётся сразу; чистка — после восстановления очереди,
        # когда известны настройки качества и список видео в очереди
        try:
            os.makedirs(temp_folder(), exist_ok=True)
        except Exception:
            pass

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

        # Кнопка "Скачать заново" — отдельное окно с выбором качества
        self.redownload_button = ttk.Button(
            main_tab, text="Скачать заново...", command=self.open_redownload_dialog
        )
        self.redownload_button.pack(fill="x", pady=(0, 8))

        # Куда сохраняются видео — видно сразу, можно поменять здесь же
        folder_bar = ttk.Frame(main_tab)
        folder_bar.pack(fill="x", pady=(0, 8))
        ttk.Label(folder_bar, text="Папка:").pack(side="left")
        self.folder_display_var = tk.StringVar()
        self.folder_display_label = ttk.Label(
            folder_bar, textvariable=self.folder_display_var, foreground="#1a5fb4"
        )
        self.folder_display_label.pack(side="left", padx=(4, 6))
        self.folder_change_button = ttk.Button(
            folder_bar, text="Изменить", command=self.choose_folder
        )
        self.folder_change_button.pack(side="right")
        self.update_folder_display()
        self.folder_var.trace_add(
            "write", lambda *a: self.update_folder_display()
        )

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

        self.quality_var = tk.StringVar(value=self.default_quality())
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

        self.proxy_var = tk.StringVar(value=self.settings.get("proxy", ""))
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

        self.ru_audio_var = tk.BooleanVar(value=self.default_flag("ru_audio", True))
        self.ru_audio_check = ttk.Checkbutton(
            settings_tab,
            text="Русская аудиодорожка (если есть)",
            variable=self.ru_audio_var,
        )
        self.ru_audio_check.pack(anchor="w", pady=(0, 4))

        self.translate_var = tk.BooleanVar(value=self.default_flag("translate", True))
        self.translate_check = ttk.Checkbutton(
            settings_tab,
            text="Переводить название на русский",
            variable=self.translate_var,
        )
        self.translate_check.pack(anchor="w", pady=(0, 4))

        self.defer_live_var = tk.BooleanVar(value=self.default_flag("defer_live", True))
        self.defer_live_check = ttk.Checkbutton(
            settings_tab,
            text="Эфиры: откладывать в конец очереди и докачивать после остальных",
            variable=self.defer_live_var,
        )
        self.defer_live_check.pack(anchor="w", pady=(0, 4))

        self.monitor_var = tk.BooleanVar(value=self.default_flag("monitor", True))
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

        default_cookies = self.settings.get("cookies")
        if default_cookies is None:
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

        self.browser_cookies_var = tk.BooleanVar(
            value=self.default_flag("browser_cookies", False)
        )
        self.browser_cookies_check = ttk.Checkbutton(
            settings_tab,
            text="Взять cookies из Chrome (нужно закрыть Chrome!)",
            variable=self.browser_cookies_var,
        )
        self.browser_cookies_check.pack(anchor="w", pady=(0, 8))

        self.folder_label = ttk.Label(settings_tab, text="Папка для сохранения:")
        self.folder_label.pack(anchor="w")

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
        self.watch_settings()
        self.restore_queue()
        self.clean_temp_for_startup()

        self.worker_thread = threading.Thread(target=self.worker_loop, daemon=True)
        self.worker_thread.start()

        self.root.after(300, self.refresh_vpn_status)
        self.root.after(CLIPBOARD_POLL_MS, self.poll_clipboard)
        self.root.after(30, self._drain_ui_queue)
        self.refresh_queue_display()

    # ---------- persistence ----------

    def default_folder(self):
        """Выбранная папка сохранения из прошлого запуска, иначе Рабочий стол."""
        saved = (self.settings.get("folder") or "").strip()
        return saved or desktop_path()

    def default_quality(self):
        saved = self.settings.get("quality")
        if saved in QUALITY_OPTIONS:
            return saved
        return config.DEFAULT_QUALITY

    def default_flag(self, key, fallback):
        saved = self.settings.get(key)
        return fallback if saved is None else bool(saved)

    def collect_settings(self):
        return {
            "folder": self.folder_var.get(),
            "quality": self.quality_var.get(),
            "proxy": self.proxy_var.get(),
            "ru_audio": bool(self.ru_audio_var.get()),
            "translate": bool(self.translate_var.get()),
            "defer_live": bool(self.defer_live_var.get()),
            "monitor": bool(self.monitor_var.get()),
            "cookies": self.cookies_var.get(),
            "browser_cookies": bool(self.browser_cookies_var.get()),
            # Переопределение temp-папки редактируется в самом settings.json
            "temp_folder": temp_folder(),
        }

    def watch_settings(self):
        """Любое изменение настроек сохраняет их (с задержкой, без спама)."""
        for var in (
            self.folder_var, self.quality_var, self.proxy_var, self.ru_audio_var,
            self.translate_var, self.defer_live_var, self.monitor_var,
            self.cookies_var, self.browser_cookies_var,
        ):
            var.trace_add("write", self.schedule_settings_save)

    def schedule_settings_save(self, *_args):
        if self._settings_job is not None:
            try:
                self.root.after_cancel(self._settings_job)
            except Exception:
                pass
        try:
            self._settings_job = self.root.after(400, self.save_settings_now)
        except Exception:
            self._settings_job = None

    def save_settings_now(self):
        self._settings_job = None
        try:
            save_settings(self.collect_settings())
        except Exception as e:
            print("save_settings_now error:", e)

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
                    "date_str": meta.get("date_str") or today_folder_name(),
                    "added_at": meta.get("added_at"),
                    "attempts": meta.get("attempts", 0),
                    "quality": meta.get("quality"),
                })
            for vid, info in self.failed_info.items():
                if vid in seen or vid in self.downloaded_ids:
                    continue
                seen.add(vid)
                meta = self.queue_meta.get(vid, {})
                items.append({
                    "video_id": vid,
                    "url": info.get("url") or f"https://www.youtube.com/watch?v={vid}",
                    "date_str": meta.get("date_str") or today_folder_name(),
                    "added_at": meta.get("added_at"),
                    "attempts": meta.get("attempts", 0),
                    "quality": meta.get("quality"),
                })
            for vid, info in self.deferred_live.items():
                if vid in seen or vid in self.downloaded_ids:
                    continue
                seen.add(vid)
                meta = self.queue_meta.get(vid, {})
                items.append({
                    "video_id": vid,
                    "url": info.get("url") or f"https://www.youtube.com/watch?v={vid}",
                    "date_str": meta.get("date_str") or today_folder_name(),
                    "added_at": meta.get("added_at") or info.get("added_at"),
                    "attempts": meta.get("attempts", 0),
                    "deferred": True,
                    "checks": info.get("checks", 0),
                    "paused": bool(info.get("paused")),
                    "quality": meta.get("quality"),
                })
        with self._persist_lock:
            save_queue_file(items)

    def restore_queue(self):
        stored = load_queue_file()
        if not stored:
            return
        added = 0
        deferred_added = 0
        with self.cond:
            for entry in stored:
                vid = entry.get("video_id")
                url = entry.get("url")
                if not vid or not url:
                    continue
                if vid in self.downloaded_ids or vid in self.active_ids:
                    continue
                if entry.get("deferred"):
                    # Новый запуск — новый цикл ожидания до 3 часов
                    self.active_ids.add(vid)
                    self.deferred_live[vid] = {
                        "url": url,
                        "checks": 0,
                        "paused": False,
                        "added_at": entry.get("added_at"),
                    }
                    self.queue_meta[vid] = {
                        "date_str": entry.get("date_str") or today_folder_name(),
                        "added_at": entry.get("added_at"),
                        "attempts": entry.get("attempts", 0),
                        "quality": entry.get("quality"),
                    }
                    deferred_added += 1
                    continue
                self.active_ids.add(vid)
                self.pending.append((vid, url))
                self.queue_meta[vid] = {
                    "date_str": entry.get("date_str") or today_folder_name(),
                    "added_at": entry.get("added_at"),
                    "attempts": entry.get("attempts", 0),
                    "quality": entry.get("quality"),
                }
                self.failed_ids.discard(vid)
                self.failed_info.pop(vid, None)
                added += 1
            if added:
                self.cond.notify_all()
        if added:
            self.log(f"Восстановлено недокачанных: {added} (докачка продолжится)")
            self.refresh_queue_display()
        if deferred_added:
            self.log(f"Отложенных эфиров: {deferred_added} (докачаются после очереди)")
            self.refresh_queue_display()
        self.persist_queue()

    def on_close(self):
        try:
            self.save_settings_now()
        except Exception:
            pass
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

    def open_redownload_dialog(self):
        """Открывает диалог для повторного скачивания с выбором качества."""
        win = tk.Toplevel(self.root)
        win.title("Скачать заново")
        win.geometry("520x220")
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()

        ttk.Label(win, text="Ссылка на видео:").pack(anchor="w", padx=12, pady=(12, 4))

        url_frame = ttk.Frame(win)
        url_frame.pack(fill="x", padx=12)
        url_entry = ttk.Entry(url_frame)
        url_entry.pack(side="left", fill="x", expand=True)
        url_entry.bind("<Control-v>", lambda e: self.paste_to(url_entry))
        self.setup_context_menu(url_entry)
        ttk.Button(url_frame, text="Вставить", command=lambda: self.paste_to(url_entry)).pack(side="left", padx=(6, 0))

        ttk.Label(win, text="Качество:").pack(anchor="w", padx=12, pady=(12, 4))

        quality_var = tk.StringVar(value=config.DEFAULT_QUALITY)
        quality_combo = ttk.Combobox(
            win,
            textvariable=quality_var,
            values=list(QUALITY_OPTIONS.keys()),
            state="readonly",
            width=40,
        )
        quality_combo.pack(anchor="w", padx=12)

        btn_frame = ttk.Frame(win)
        btn_frame.pack(fill="x", padx=12, pady=(16, 12))

        def do_download():
            url = url_entry.get().strip()
            if not url:
                messagebox.showwarning("Внимание", "Вставьте ссылку на видео", parent=win)
                return
            vids = extract_all_video_ids(url)
            if not vids:
                messagebox.showwarning("Внимание", "Ссылка не распознана как YouTube", parent=win)
                return
            # Качество каждого видео фиксируется per-item; глобальные настройки
            # не трогаем, иначе сломается текущая загрузка и докачка temp
            added = 0
            for vid in vids:
                if self.enqueue(
                    vid, f"https://www.youtube.com/watch?v={vid}",
                    source="повторное", force=True, quality=quality_var.get(),
                ):
                    added += 1
            if added:
                self.log(f"Скачать заново: добавлено {added} видео с качеством {quality_var.get()}")
            win.destroy()

        ttk.Button(btn_frame, text="Скачать", command=do_download).pack(side="right", padx=(6, 0))
        ttk.Button(btn_frame, text="Отмена", command=win.destroy).pack(side="right")

        url_entry.focus_set()

    def enqueue(self, vid, url, source="clipboard", force=False, quality=None):
        with self.cond:
            if not force and vid in self.downloaded_ids:
                self.log(f"Пропуск (уже скачано): {vid}")
                return False
            if vid in self.active_ids:
                return False
            if source == "clipboard" and vid in self.failed_ids:
                return False
            self.active_ids.add(vid)
            self.pending.append((vid, url))
            # Качество фиксируется за каждым видео (per-item): подмена
            # глобальных настроек на лету ломала идущую загрузку
            self.queue_meta[vid] = {
                "date_str": today_folder_name(),
                "added_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "attempts": 0,
                "quality": quality or self.quality_var.get(),
            }
            self.cond.notify()
        self.persist_queue()
        self.log(f"В очередь [{source}]: {vid}")
        self.ui_call(self.refresh_queue_display)
        return True

    def worker_loop(self):
        while True:
            with self.cond:
                while not self.pending and not self.deferred_live:
                    self.cond.wait()
                if self.pending:
                    # Обычные видео всегда раньше отложенных эфиров
                    vid, url = self.pending.popleft()
                    from_deferred = False
                else:
                    nxt = next(
                        (v for v in self.deferred_live if not self.deferred_live[v].get("paused")),
                        None,
                    )
                    if nxt is None:
                        # Остались только эфиры, отложенные до следующих запусков, —
                        # ждём новые видео
                        self.cond.wait()
                        continue
                    vid = nxt
                    url = self.deferred_live[vid]["url"]
                    from_deferred = True
                self.current = (vid, url)
            self.persist_queue()
            self.ui_call(self.refresh_queue_display)
            try:
                self.process(vid, url, from_deferred=from_deferred)
            finally:
                with self.cond:
                    self.current = None
                    if vid not in self.deferred_live:
                        self.active_ids.discard(vid)
                self.persist_queue()
                self.ui_call(self.refresh_queue_display)

    def process(self, vid, url, from_deferred=False):
        self.set_status(f"Обработка {vid}...")
        with self.cond:
            meta = self.queue_meta.get(vid, {})
            date_str = meta.get("date_str") or today_folder_name()
            # Качество конкретного видео, сохранённое при добавлении в очередь
            quality = meta.get("quality")
        snapshot = self._yt_snapshot(quality)
        base_folder = self.folder_var.get()
        date_folder = os.path.join(base_folder, date_str)
        try:
            original_title, is_live, live_status = downloader.extract_meta(url, snapshot)
            if is_live or live_status in ("is_live", "is_upcoming"):
                reason = (
                    "запланированная трансляция ещё не началась"
                    if live_status == "is_upcoming"
                    else "прямой эфир (трансляция идёт)"
                )
                if from_deferred:
                    checks = self.deferred_live.get(vid, {}).get("checks", 0) + 1
                    if checks >= LIVE_MAX_CHECKS:
                        with self.cond:
                            if vid in self.deferred_live:
                                self.deferred_live[vid]["checks"] = 0
                                self.deferred_live[vid]["paused"] = True
                        self.persist_queue()
                        self.log(f"Эфир {vid} не закончился за {LIVE_SESSION_LIMIT_SEC // 3600} ч — останется в очереди, докачается при следующих запусках")
                        self.set_status("Эфир отложен до следующих запусков")
                        return
                    with self.cond:
                        if vid in self.deferred_live:
                            self.deferred_live[vid]["checks"] = checks
                    self.persist_queue()
                    self.log(f"Эфир {vid} ещё идёт (проверка {checks}/{LIVE_MAX_CHECKS}), следующая — через {LIVE_RECHECK_SEC // 60} мин")
                    self.set_status(f"Эфир ещё идёт... проверка {checks}/{LIVE_MAX_CHECKS}")
                    with self.cond:
                        # Новые обычные видео разбудят раньше — они качаются первыми
                        self.cond.wait(timeout=LIVE_RECHECK_SEC)
                    return
                if self.defer_live_var.get() and self.ask_defer_live(original_title, vid):
                    with self.cond:
                        self.deferred_live[vid] = {
                            "url": url,
                            "checks": 0,
                            "added_at": datetime.datetime.now().isoformat(timespec="seconds"),
                        }
                        cur = self.queue_meta.get(vid, {})
                        cur.setdefault("date_str", date_str)
                        self.queue_meta[vid] = cur
                    self.persist_queue()
                    self.log(f"Эфир отложен в конец очереди: {original_title or vid} ({vid}) — докачается после остальных")
                    self.set_status("Эфир отложен (см. журнал)")
                    return
                with self.cond:
                    self.failed_ids.add(vid)
                    self.failed_info[vid] = {"url": url, "reason": "live"}
                    cur = self.queue_meta.get(vid, {})
                    cur["attempts"] = cur.get("attempts", 0) + 1
                    cur.setdefault("date_str", date_str)
                    self.queue_meta[vid] = cur
                self.persist_queue()
                self.log(f"Пропуск {vid}: {reason} — пропущен")
                self.set_status("Пропущен эфир (см. журнал)")
                return
            if from_deferred:
                with self.cond:
                    self.deferred_live.pop(vid, None)
                self.log(f"Эфир {vid} завершился — скачиваю")
            if self.translate_var.get():
                final_title, trans_error = build_final_title(original_title)
                if trans_error:
                    self.log(f"Перевод названия не удался ({trans_error}); сохранён оригинал")
            else:
                final_title = original_title or vid
            fname = clean_filename(final_title)
            # Качаем во временную папку на C:, имя с video_id — чтобы докачка
            # находила свой .part даже если название перевелось иначе
            outtmpl = os.path.join(temp_folder(), f"{vid}.%(ext)s")
            # Запоминаем настройки: если их поменяют, докачка сбросится
            state = load_temp_state()
            state[vid] = self.temp_signature_for(quality)
            save_temp_state(state)
            ok, path, err = downloader.download(url, outtmpl, snapshot, progress_hook=self.progress_hook)
            if ok:
                try:
                    path = move_to_target(path, date_folder, fname)
                except Exception as e:
                    self.log(f"Не удалось перенести файл из temp: {e}")
                # Удаляем всё, что осталось в temp по этому видео
                self._cleanup_temp_files(temp_folder(), vid)
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

    def _yt_snapshot(self, quality=None):
        """Снимок настроек yt-dlp текущего момента (без ссылок на GUI-класс)."""
        cookies_file = self.cookies_var.get().strip()
        if not cookies_file:
            auto = os.path.join(desktop_path(), "cookies.txt")
            if os.path.exists(auto):
                cookies_file = auto
        return {
            "quality": quality if quality in QUALITY_OPTIONS else self.quality_var.get(),
            "ru_audio": bool(self.ru_audio_var.get()),
            "out_folder": self.folder_var.get(),
            "proxy": self.proxy_var.get().strip(),
            "cookies_file": cookies_file or None,
            "browser_cookies": bool(self.browser_cookies_var.get()),
        }

    def temp_signature_for(self, quality):
        """Подпись настроек скачивания конкретного видео: сменились — докачка сбрасывается."""
        return f"{quality}|ru={int(self.ru_audio_var.get())}"

    def temp_signature(self):
        return self.temp_signature_for(self.quality_var.get())

    def clean_temp_for_startup(self):
        """Чистка temp при старте: мусор, сироты, смена настроек, лимит размера."""
        try:
            with self.cond:
                queue_ids = set(self.active_ids) | {
                    v for v, _u in self.pending
                } | set(self.deferred_live)
            removed = prepare_temp_folder(temp_folder(), queue_ids, self.temp_signature())
            if removed:
                self.log(f"Очищено в {temp_folder()}: {removed} файл(ов)")
        except Exception as e:
            print("clean_temp_for_startup error:", e)

    def _cleanup_temp_files(self, folder, vid):
        """Удаляет остатки скачивания по video_id из папки temp."""
        try:
            for entry in os.listdir(folder):
                if entry.startswith(vid):
                    full = os.path.join(folder, entry)
                    try:
                        os.remove(full)
                    except Exception:
                        pass
        except Exception:
            pass
        state = load_temp_state()
        if state.pop(vid, None) is not None:
            save_temp_state(state)

    def ask_defer_live(self, title, vid):
        """Диалог из главного потока: отложить эфир в конец очереди?"""
        result = {}
        done = threading.Event()

        def _do():
            try:
                self.set_status("Требуется решение по эфиру...")
                result["answer"] = messagebox.askyesno(
                    "Прямой эфир",
                    f"«{(title or vid).strip()}» — прямой эфир или "
                    "запланированная трансляция.\n\nОтложить в конец очереди "
                    "и докачать, когда закончится?\n(«Нет» — пропустить это видео)",
                )
            except Exception:
                result["answer"] = False
            finally:
                done.set()

        self.root.after(0, _do)
        done.wait()
        return bool(result.get("answer", False))

    def progress_hook(self, d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            if total:
                percent = downloaded / total * 100
                if int(percent) != self._last_pct:
                    self._last_pct = int(percent)
                    self.ui_call(self.update_progress, percent)
        elif d["status"] == "finished":
            self.ui_call(self.set_status, "Загрузка завершена, финализация...")

    def update_progress(self, percent):
        if threading.current_thread() is not threading.main_thread():
            self.ui_call(self.update_progress, percent)
            return
        self.progress["value"] = percent
        self.status_var.set(f"Скачивание... {percent:.0f}%")

    def set_status(self, text):
        if threading.current_thread() is not threading.main_thread():
            self.ui_call(self.set_status, text)
            return
        self.status_var.set(text)

    def log(self, msg):
        if threading.current_thread() is not threading.main_thread():
            self.ui_call(self.log, msg)
            return

        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, f"[{ts}] {msg}\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def refresh_queue_display(self):
        with self.cond:
            cur = self.current
            pend = list(self.pending)
            deferred = list(self.deferred_live.items())
            done = len(self.downloaded_ids)
        self.queue_listbox.delete(0, tk.END)
        if cur:
            self.queue_listbox.insert(tk.END, f"> {cur[0]}")
        for vid, _url in pend:
            self.queue_listbox.insert(tk.END, f"  {vid}")
        for vid, info in deferred:
            tag = "эфир — продолжится при следующих запусках" if (info or {}).get("paused") else "эфир — ждёт конца"
            self.queue_listbox.insert(tk.END, f"  {vid} ({tag})")
        if not cur and not pend and not deferred:
            self.queue_listbox.insert(tk.END, "  (пусто)")
        self.queue_count_var.set(
            f"В очереди: {len(pend)}  •  Эфиров отложено: {len(deferred)}  •  Скачано: {done}"
        )

    def copy_queue(self):
        with self.cond:
            urls = []
            if self.current:
                urls.append(self.current[1])
            for _vid, url in self.pending:
                urls.append(url)
            for _vid, info in self.deferred_live.items():
                urls.append(info["url"])
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
        # Сначала планируем следующий опрос: любое исключение ниже не должно
        # навсегда останавливать монитор буфера обмена
        self.root.after(CLIPBOARD_POLL_MS, self.poll_clipboard)
        if not self.monitor_var.get():
            return
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            return
        try:
            if not text or text == self._last_clip:
                return
            self._last_clip = text
            self.handle_clipboard_links(extract_all_video_ids(text))
        except Exception as e:
            self.log(f"Ошибка монитора буфера: {e}")

    def handle_clipboard_links(self, vids):
        """Новые видео добавляем сразу, по уже скачанным показываем немодальное окно."""
        if not vids:
            return
        with self.cond:
            fresh = [v for v in vids if v not in self.downloaded_ids]
            existing = [v for v in vids if v in self.downloaded_ids]
        for vid in fresh:
            self.enqueue(vid, f"https://www.youtube.com/watch?v={vid}", source="clipboard")
        if not existing:
            return
        # Вопрос не должен останавливать ни интерфейс, ни загрузку очереди
        self.prompt_redownload_async(existing)

    def prompt_redownload_async(self, vids):
        """Немодальное окно: скачать уже сохранённые видео заново или пропустить.

        Очередь и монитор буфера при этом продолжают работать.
        """
        pending = [v for v in vids if v not in self._redownload_prompts]
        if not pending:
            return
        self._redownload_prompts.update(pending)

        win = tk.Toplevel(self.root)
        win.title("Уже скачано")
        win.resizable(False, False)
        try:
            win.transient(self.root)
        except Exception:
            pass
        try:
            win.attributes("-topmost", True)
            win.after(1500, lambda: win.attributes("-topmost", False))
        except Exception:
            pass
        # Намеренно без grab_set(): окно не блокирует приложение

        ttk.Label(
            win,
            text="Эти видео уже скачаны. Скачать их заново вместе с очередью?",
            wraplength=430,
            justify="left",
        ).pack(anchor="w", padx=14, pady=(14, 6))

        list_frame = ttk.Frame(win)
        list_frame.pack(fill="both", expand=True, padx=14)
        titles = "\n".join(
            f"• {self.downloaded_title(v)}" for v in pending[:12]
        )
        if len(pending) > 12:
            titles += f"\n… и ещё {len(pending) - 12}"
        ttk.Label(
            list_frame, text=titles, wraplength=430, justify="left"
        ).pack(anchor="w")

        hint = ttk.Label(
            win,
            text="Можно не отвечать — загрузка остальных видео не останавливается.",
            foreground="gray",
            wraplength=430,
            justify="left",
        )
        hint.pack(anchor="w", padx=14, pady=(8, 0))

        buttons = ttk.Frame(win)
        buttons.pack(fill="x", padx=14, pady=(12, 14))

        def close(*_args):
            self._redownload_prompts.difference_update(pending)
            try:
                win.destroy()
            except Exception:
                pass

        def do_redownload():
            for vid in pending:
                self.enqueue(
                    vid,
                    f"https://www.youtube.com/watch?v={vid}",
                    source="буфер (повтор)",
                    force=True,
                )
            close()

        ttk.Button(buttons, text="Скачать заново", command=do_redownload).pack(
            side="right", padx=(6, 0)
        )
        ttk.Button(buttons, text="Пропустить", command=close).pack(side="right")
        win.protocol("WM_DELETE_WINDOW", close)

    def downloaded_title(self, vid):
        with self.cond:
            for rec in self.downloaded_records:
                if rec.get("video_id") == vid:
                    return rec.get("final_title") or rec.get("original_title") or vid
        return vid

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
                    [config.PORTAL_WG_EXE],
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except Exception:
                pass
            self.ui_call(self.refresh_vpn_status)

        self.status_var.set("Переподключение VPN... (5 секунд)")
        threading.Thread(target=worker, daemon=True).start()

    def refresh_vpn_status(self):
        def check():
            self.ui_call(self._set_vpn_status, *vpn_status())

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

    def ui_call(self, fn, *args):
        """Выполнить действие в GUI-потоке. Безопасно из любого потока."""
        try:
            self._ui_queue.put_nowait((fn, args))
        except Exception:
            pass

    def _drain_ui_queue(self):
        """Забирает задачи из рабочих потоков и выполняет их в GUI-потоке."""
        try:
            while True:
                try:
                    fn, args = self._ui_queue.get_nowait()
                except ui_queue.Empty:
                    break
                try:
                    fn(*args)
                except Exception as e:
                    print("ui task error:", e)
        except Exception as e:
            print("drain ui queue error:", e)
        try:
            self.root.after(50, self._drain_ui_queue)
        except Exception:
            pass

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
        try:
            current = self.folder_var.get()
            if current and not os.path.isdir(current):
                current = os.path.dirname(current) or desktop_path()
        except Exception:
            current = desktop_path()
        folder = filedialog.askdirectory(initialdir=current)
        if folder:
            self.folder_var.set(folder)

    def update_folder_display(self):
        """Короткая подпись папки на главном экране (конец пути важнее начала)."""
        try:
            path = (self.folder_var.get() or "").strip()
        except Exception:
            return
        if not path:
            self.folder_display_var.set("(не выбрана)")
            return
        display = path
        if len(display) > 48:
            display = "…" + display[-47:]
        self.folder_display_var.set(display)
        try:
            if os.path.isdir(path):
                self.folder_display_label.config(foreground="#1a5fb4")
            else:
                self.folder_display_label.config(foreground="#c62828")
        except Exception:
            pass

    def choose_cookies_file(self):
        file = filedialog.askopenfilename(
            initialdir=desktop_path(),
            title="Выберите файл cookies.txt",
            filetypes=[("Netscape cookies", "*.txt"), ("Все файлы", "*.*")],
        )
        if file:
            self.cookies_var.set(file)


def main():
    root = tk.Tk()
    app = YouTubeDownloaderApp(root)
    root.mainloop()
