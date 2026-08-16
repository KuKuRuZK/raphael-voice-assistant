@echo off
cd /d "%~dp0"

REM Watchdog: restart Lin if it crashes, but not on intentional exit.
if exist ".lin_stop" del ".lin_stop"

:loop
start /wait "" pythonw "%~dp0lin.py"

REM .lin_stop marker means the user quit on purpose - do not restart.
if exist ".lin_stop" goto cleanstop

echo [%date% %time%] Lin crashed - restarting >> watchdog.log
timeout /t 5 /nobreak >nul
goto loop

:cleanstop
del ".lin_stop"

:end
