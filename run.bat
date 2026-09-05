@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PATH=%cd%;%PATH%"
python yt_downloader.py
if errorlevel 1 pause