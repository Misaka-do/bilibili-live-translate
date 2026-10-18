@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [Starting Bilibili live realtime translation Web service]
echo Model cache: %LOCALAPPDATA%\LiveTranslate
echo.
"%~dp0venv\Scripts\python.exe" "%~dp0server\live_web.py" %*
pause
