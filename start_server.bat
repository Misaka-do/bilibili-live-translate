@echo off
cd /d "%~dp0"
echo [Starting Bilibili live translate unified server]
echo   plugin: ws://127.0.0.1:8765  (browser extension)
echo   web   : http://0.0.0.0:8000  (phone subtitle page)
echo Model cache: %LOCALAPPDATA%\LiveTranslate
echo.
"%~dp0venv\Scripts\python.exe" "%~dp0server\main.py" %*
pause
