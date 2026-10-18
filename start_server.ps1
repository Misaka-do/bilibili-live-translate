$ws = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ws
Write-Host "[Starting Bilibili live translate unified server] Model cache: $env:LOCALAPPDATA\LiveTranslate"
& "$ws\venv\Scripts\python.exe" "$ws\server\main.py" @args
