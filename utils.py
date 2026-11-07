"""Отдельные утилиты без привязки к GUI или yt-dlp.

Парсинг ссылок YouTube, чистка имён файлов, поиск Node.js и статус VPN.
"""
import os
import re
import subprocess

import config

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
    for path in config.NODE_PATHS:
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


def vpn_status():
    """Статус VPN одной командой PowerShell: (туннель поднят, процесс запущен).

    Раньше было два отдельных вызова PowerShell на каждую проверку.
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-NetAdapter | Where-Object { $_.InterfaceDescription -match 'WireGuard|Tunnel' } "
             "| Select-Object -ExpandProperty Status; "
             "'---SEP---'; "
             "Get-Process | Where-Object { $_.Name -match 'PORTAL|wireguard|amnezia|awg' } "
             "| Select-Object -ExpandProperty ProcessName"],
            capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        text = (out.stdout or "")
        parts = text.split("---SEP---")
        up = "Up" in parts[0]
        proc_running = len(parts) > 1 and bool(parts[1].strip())
        return up, proc_running
    except Exception:
        return False, False


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
