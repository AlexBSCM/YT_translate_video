"""Пути, константы, настройки и файлы состояния приложения.

Только данные и чтение/запись на диск — без сетей, yt-dlp и GUI.
"""
import datetime
import json
import os

PORTAL_WG_EXE = r"C:\Program Files\PORTAL WG\PORTAL WG.exe"

NODE_PATHS = [
    r"C:\Program Files\nodejs\node.exe",
    r"C:\Program Files (x86)\nodejs\node.exe",
]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADED_FILE = os.path.join(SCRIPT_DIR, "downloaded.json")
QUEUE_FILE = os.path.join(SCRIPT_DIR, "queue.json")

# Всё качается сюда, готовый файл переносится в целевую папку.
# .part сохраняются — это незавершённые загрузки для докачки.
TEMP_FOLDER_DEFAULT = r"C:\yt_temp"
TEMP_KEEP_SUFFIX = ".part"
# Если temp разросся больше — самые старые .part удаляются
TEMP_MAX_BYTES = 20 * 1024 * 1024 * 1024
# Какие настройки скачивания действовали для каждого .part
TEMP_STATE_FILE = os.path.join(SCRIPT_DIR, "temp_state.json")
# Настройки вкладки «Настройки», включая выбранную папку сохранения
SETTINGS_FILE = os.path.join(SCRIPT_DIR, "settings.json")

QUALITY_OPTIONS = {
    "1080p (по умолчанию)": "1080",
    "720p": "720",
    "480p": "480",
    "360p": "360",
    "Лучшее": "best",
}
DEFAULT_QUALITY = "1080p (по умолчанию)"

# Переопределение temp-папки ключом "temp_folder" в settings.json
_temp_override = None


def temp_folder():
    """Действующая temp-папка: переопределение из настроек или дефолт."""
    return _temp_override or TEMP_FOLDER_DEFAULT


def set_temp_override(path):
    """Задаёт temp-папку из settings.json (пусто/нет ключа — дефолт)."""
    global _temp_override
    path = (path or "").strip()
    _temp_override = path or None


def load_settings(path=SETTINGS_FILE):
    """Читает сохранённые настройки. При отсутствии/битом файле — пустой dict."""
    try:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return {}
        data = json.loads(content)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print("load_settings error:", e)
        return {}


def save_settings(data, path=SETTINGS_FILE):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp, path)
        return True
    except Exception as e:
        print("save_settings error:", e)
        return False


def load_temp_state(path=TEMP_STATE_FILE):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def save_temp_state(state, path=TEMP_STATE_FILE):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


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
            try:
                checks = int(entry.get("checks") or 0)
            except (TypeError, ValueError):
                checks = 0
            quality = (entry.get("quality") or "").strip()
            if quality not in QUALITY_OPTIONS:
                quality = None
            items.append({
                "video_id": vid,
                "url": url,
                "date_str": (entry.get("date_str") or "").strip() or None,
                "added_at": entry.get("added_at"),
                "attempts": max(0, attempts),
                "deferred": bool(entry.get("deferred")),
                "checks": max(0, checks),
                "paused": bool(entry.get("paused")),
                "quality": quality,
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


def today_folder_name(date=None):
    d = date or datetime.date.today()
    return d.strftime("%d.%m.%Y")
