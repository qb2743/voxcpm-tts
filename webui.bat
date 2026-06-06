@echo off
chcp 65001 >nul
title VoxCPM2 Voice Studio

cd /d "%~dp0"

echo ============================================
echo    VoxCPM2 Voice Studio
echo ============================================
echo.

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found.
    pause
    exit /b 1
)

echo [OK] Installing dependencies...
python -m pip install -r requirements.txt --quiet 2>nul
echo [OK] Starting...
echo.

echo Opening browser: http://127.0.0.1:5000
echo LAN address will be printed after startup.
echo Press Ctrl+C to stop.
echo ============================================
echo.

python webui.py
pause
