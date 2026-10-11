@echo off
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    start "" ".venv\Scripts\python.exe" "server.py"
    exit /b 0
)

where python >nul 2>nul
if not errorlevel 1 (
    start "" python "server.py"
    exit /b 0
)

echo Python 3.10+ is required to run this website.
pause
exit /b 1
