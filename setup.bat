@echo off
chcp 65001 >nul
echo.
echo  ╔══════════════════════════════════╗
echo  ║   Встановлення залежностей Лін   ║
echo  ╚══════════════════════════════════╝
echo.

echo [1/3] Встановлення основних бібліотек...
pip install edge-tts pygame SpeechRecognition psutil pyautogui pystray pillow groq spotipy

echo.
echo [2/3] Встановлення PyAudio (потрібно для мікрофону)...
pip install pyaudio

echo.
echo [3/3] Готово!
echo.
echo  Що далі:
echo  1. Запустіть start.bat
echo  2. При першому запуску відкриється браузер для авторизації Spotify
echo  3. Натисніть Allow — і все готово!
echo.
pause
