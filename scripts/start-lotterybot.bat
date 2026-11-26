@echo off
setlocal
cd /d "%~dp0"
title LotteryBot - Launcher

echo ================================================================
echo   LotteryBot - Telegram Lottery ^& Web Control Center
echo ================================================================
echo.

:: Check if LotteryBot is already running
tasklist /FI "IMAGENAME eq LotteryBot.exe" 2>NUL | find /I /N "LotteryBot.exe" >NUL
if "%ERRORLEVEL%"=="0" (
    echo [INFO] LotteryBot is already running.
    echo Opening Web Dashboard in your browser...
    if exist "%~dp0LotteryBot.exe" (
        start "" "%~dp0LotteryBot.exe"
    ) else (
        start "" "http://127.0.0.1:8000"
    )
    timeout /t 2 >nul
    exit /b 0
)

echo Starting LotteryBot service...

if exist "%~dp0LotteryBot.exe" (
    start "LotteryBot Service" "%~dp0LotteryBot.exe" %*
) else if exist "%~dp0desktop_launcher.py" (
    start "LotteryBot Service" /min python "%~dp0desktop_launcher.py" %*
) else (
    echo [ERROR] LotteryBot.exe was not found in %~dp0
    pause
    exit /b 1
)

echo Waiting for LotteryBot Web Dashboard to initialize...
timeout /t 3 /nobreak >nul

echo.
echo ================================================================
echo   LotteryBot service is active!
echo   Your browser will open automatically to the Web Control Center.
echo   To stop the service, double-click "Stop LotteryBot"
echo   or right-click the ticket icon in the Windows System Tray.
echo ================================================================
timeout /t 4 >nul
exit /b 0
