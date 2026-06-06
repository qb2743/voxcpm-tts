@echo off
chcp 65001 >nul
title Build EXE

cd /d "%~dp0"

echo ============================================
echo    PyInstaller Build Script
echo ============================================
echo.

python -m pip install pyinstaller --quiet

echo Building server.exe...
pyinstaller --onefile --name voxcpm-server --add-data "config.yaml;." --add-data "voices;voices" server.py

echo Building webui.exe...
pyinstaller --onefile --name voxcpm-webui --add-data "config.yaml;." --add-data "voices;voices" webui.py

echo.
echo Done! Check the dist/ folder.
pause