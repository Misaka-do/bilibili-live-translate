@echo off
cd /d "%~dp0"
echo [Starting local ASR + DeepSeek translation server]
echo Model cache: %LOCALAPPDATA%\LiveTranslate
"%~dp0venv\Scripts\python.exe" "%~dp0server\asr_server.py" %*
pause
