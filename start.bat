@echo off
cd /d "%~dp0"

REM Watchdog: restart Lin if it crashes, but not on intentional exit.
REM .lin_alive appears after 2 minutes of uptime (lin.py). If Lin dies before
REM that 5 times in a row, the install is broken (e.g. missing library):
REM stop restarting and point to crash.log instead of looping forever.
if exist ".lin_stop" del ".lin_stop"
set /a fails=0

:loop
if exist ".lin_alive" del ".lin_alive"
start /wait "" pythonw "%~dp0lin.py"

REM .lin_stop marker means the user quit on purpose - do not restart.
if exist ".lin_stop" goto cleanstop

if exist ".lin_alive" (set /a fails=0) else (set /a fails+=1)
echo [%date% %time%] Lin crashed (early crashes in a row: %fails%) - restarting >> watchdog.log

if %fails% GEQ 5 goto giveup
timeout /t 5 /nobreak >nul
goto loop

:giveup
echo [%date% %time%] Lin keeps crashing on start - watchdog stopped, see crash.log >> watchdog.log
powershell -NoProfile -WindowStyle Hidden -Command "Add-Type -AssemblyName PresentationFramework; [System.Windows.MessageBox]::Show('Raphael crashes on start 5 times in a row. Details: crash.log', 'Raphael') | Out-Null"
goto end

:cleanstop
del ".lin_stop"

:end
