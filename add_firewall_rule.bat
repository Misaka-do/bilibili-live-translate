@echo off
chcp 65001 >nul
:: Check for admin rights
net session >nul 2>&1
if %errorlevel% NEQ 0 (
    echo Admin rights required. Requesting elevation...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo Adding firewall inbound rule: TCP 8000 ...
netsh advfirewall firewall delete rule name="LiveTranslate Web Service (TCP 8000)" >nul 2>&1
netsh advfirewall firewall add rule name="LiveTranslate Web Service (TCP 8000)" dir=in action=allow protocol=TCP localport=8000 >nul
if %errorlevel% EQU 0 (
    echo.
    echo [OK] TCP 8000 opened. Phone can now access http://PC-IP:8000
) else (
    echo.
    echo [Failed] Auto-add failed. Manual steps:
    echo   1. Open "Windows Security" - "Firewall & network protection" - "Advanced settings"
    echo   2. "Inbound Rules" - "New Rule" - Port - TCP 8000 - Allow
)
echo.
pause
