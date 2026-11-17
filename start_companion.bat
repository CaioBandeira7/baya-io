@echo off
rem Starts Baya.io Companion on this PC. Open the printed address on the iPad (same Wi-Fi).
cd /d "%~dp0"
set "PY=python"
if exist "%USERPROFILE%\.venv\Scripts\python.exe" set "PY=%USERPROFILE%\.venv\Scripts\python.exe"
"%PY%" -m companion %*
pause
