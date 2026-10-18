@echo off
cd /d "%~dp0"
echo [Starting Bilibili live translate - Web only (phone)]
echo   web   : http://0.0.0.0:8000  (phone subtitle page)
echo Model cache: %LOCALAPPDATA%\LiveTranslate
echo.
"%~dp0venv\Scripts\python.exe" "%~dp0server\main.py" --no-plugin %*
pause
