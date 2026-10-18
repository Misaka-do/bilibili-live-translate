$ws = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ws
Write-Host "[Starting local ASR + DeepSeek translation server] Model cache: $env:LOCALAPPDATA\LiveTranslate"
& "$ws\venv\Scripts\python.exe" "$ws\server\asr_server.py" @args
