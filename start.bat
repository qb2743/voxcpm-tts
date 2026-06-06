@echo off
chcp 65001 >nul
title VoxCPM2 TTS API Server

echo ============================================
echo    VoxCPM2 OpenAI-Compatible TTS API
echo ============================================
echo.

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found. Install Python 3.10+ first.
    echo https://www.python.org/downloads/
    pause
    exit /b 1
)
echo [OK] Python version:
python --version
echo.

cd /d "%dp0"

echo [OK] Installing dependencies...
python -m pip install -r requirements.txt --quiet
if %errorlevel% neq 0 (
    echo [ERROR] Dependency install failed. Check network.
    pause
    exit /b 1
)
echo [OK] Dependencies ready.
echo.

ffmpeg -version >nul 2>&1
if %errorlevel% neq 0 (
    echo [WARN] ffmpeg not found. MP3/Opus/AAC conversion will be unavailable.
    echo        Install: winget install ffmpeg  or  https://ffmpeg.org/
    echo.
)

if not exist voices mkdir voices
dir /b voices\*.wav >nul 2>&1
if %errorlevel% neq 0 (
    echo [WARN] No .wav files in voices/ directory!
    echo        Please add reference audio files and edit config.yaml.
    echo.
)

echo [OK] Starting API server...
echo       Address: http://localhost:7900
echo       Docs:    http://localhost:7900/docs
echo.
echo Press Ctrl+C to stop.
echo ============================================
echo.

python server.py

pause
