@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Запускаю авторизацію Spotify...
python spotify_auth.py
