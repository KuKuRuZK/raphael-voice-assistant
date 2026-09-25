@echo off
chcp 65001 >nul
echo.
echo  ╔══════════════════════════════════╗
echo  ║ Встановлення залежностей Рафаеля ║
echo  ╚══════════════════════════════════╝
echo.

echo [1/2] Встановлення бібліотек з requirements.txt...
pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo.
    echo  Щось не встановилось. Подивись помилку вище.
    pause
    exit /b 1
)

echo.
echo [2/2] Готово!
echo.
echo  Що далі:
echo  1. Скопіюйте secrets.example.json у secrets.json і впишіть ключі
echo  2. Запустіть spotify_auth.bat і gmail_auth.bat (один раз)
echo  3. Запустіть start_hidden.vbs
echo.
pause
