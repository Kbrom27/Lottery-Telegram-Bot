@echo off
setlocal
title Stopping LotteryBot...

echo ================================================================
echo   Stopping LotteryBot Service...
echo ================================================================
echo.

tasklist /FI "IMAGENAME eq LotteryBot.exe" 2>NUL | find /I /N "LotteryBot.exe" >NUL
if "%ERRORLEVEL%"=="0" (
    taskkill /F /IM "LotteryBot.exe" /T >nul 2>&1
    echo [SUCCESS] LotteryBot has been successfully stopped.
) else (
    echo [INFO] LotteryBot service is not currently running.
)

echo.
timeout /t 3 >nul
exit /b 0
