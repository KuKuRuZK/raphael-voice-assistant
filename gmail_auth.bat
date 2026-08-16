@echo off
REM Gmail re-auth for Lin. Double-click for primary inbox, or pass 2 for second.
cd /d "%~dp0"
python gmail_auth.py %1
pause
