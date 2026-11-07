"""Точка входа приложения YouTube Downloader.

Вся логика живёт в модулях: config.py, utils.py, translators.py,
temp_store.py, downloader.py, gui.py. Этот файл оставлен тонким, чтобы
run.bat/run.vbs продолжали запускать одно и то же имя.
"""
from gui import main

if __name__ == "__main__":
    main()
