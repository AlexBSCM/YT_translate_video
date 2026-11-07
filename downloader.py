"""Обёртка yt-dlp: выбор клиентов, формата, ретраи и meta-запрос.

Функции принимают «снимок настроек» (dict) вместо прямого доступа к
GUI-переменным:
    quality           — метка качества из QUALITY_OPTIONS
    ru_audio          — предпочитать русскую аудиодорожку
    out_folder        — папка сохранения по умолчанию (для %(title)s)
    proxy             — прокси или пустая строка
    cookies_file      — путь к cookies.txt или None
    browser_cookies   — брать cookies из Chrome (только если cookies_file None)
"""
import os
import time

import yt_dlp

import config
from utils import find_node


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


def _is_retryable(detail):
    d = (detail or "").lower()
    markers = (
        "http error 403", "unable to download", "getaddrinfo", "failed to resolve",
        "temporary failure", "timed out", "timeout", "connection", "reset by peer",
        "remote end closed", "http error 5", "eof occurred", "ssl", "network",
        "too slow", "throttl",
    )
    return any(m in d for m in markers)


def build_opts(client=None, fmt=None, outtmpl=None, skip=False,
               progress_hook=None, snapshot=None):
    """Собирает опции yt_dlp.YoutubeDL из снимка настроек."""
    snap = snapshot or {}
    opts = {
        "outtmpl": outtmpl or os.path.join(
            snap.get("out_folder") or config.SCRIPT_DIR, "%(title)s.%(ext)s"
        ),
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
    if os.path.exists(os.path.join(config.SCRIPT_DIR, "ffmpeg.exe")):
        opts["ffmpeg_location"] = config.SCRIPT_DIR
    if fmt:
        opts["format"] = fmt
    if skip:
        opts["skip_download"] = True
    else:
        opts["progress_hooks"] = [progress_hook or (lambda _d: None)]

    node_path = find_node()
    if node_path:
        opts["js_runtimes"] = {"node": {"path": node_path}}

    proxy = (snap.get("proxy") or "").strip()
    if proxy:
        opts["proxy"] = proxy
    if client:
        opts["extractor_args"] = {"youtube": {"player_client": client}}

    cookies_file = (snap.get("cookies_file") or "").strip()
    if cookies_file:
        opts["cookiefile"] = cookies_file
    elif snap.get("browser_cookies"):
        opts["cookiesfrombrowser"] = ("chrome",)
    return opts


def _format_for(quality_label, ru_audio):
    quality = config.QUALITY_OPTIONS.get(
        quality_label, config.QUALITY_OPTIONS[config.DEFAULT_QUALITY]
    )
    if quality == "best":
        return f"bestvideo+bestaudio{ru_audio}/bestvideo+bestaudio/best"
    return (
        f"bestvideo[height<={quality}]+bestaudio{ru_audio}"
        f"/bestvideo[height<={quality}]+bestaudio"
        f"/best[height<={quality}]"
    )


def download(url, outtmpl, snapshot, progress_hook=None):
    """Скачивает одно видео. Возвращает (ok, путь файла, текст ошибки).

    Клиенты по умолчанию — первыми: только они отдают даб-дорожки
    (включая автодубляж). web_embedded/android — запасные варианты.
    """
    fmt = _format_for(snapshot.get("quality") or "", "[language=ru]" if snapshot.get("ru_audio") else "")
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
                opts = build_opts(client, f, outtmpl, progress_hook=progress_hook, snapshot=snapshot)
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
                if _is_retryable(detail):
                    # Пауза растёт с каждым кругом попыток (2с, 4с) —
                    # меньше лишнего шума, когда сеть лежит надолго
                    time.sleep(2 * (_round + 1))
    return False, None, last_error


def extract_meta(url, snapshot):
    """Метаданные без скачивания: (название, is_live, live_status)."""
    for client in [None, ["web_embedded"], ["android_vr"], ["android"]]:
        try:
            opts = build_opts(client, None, None, skip=True, snapshot=snapshot)
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
