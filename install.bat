@echo off
REM ─────────────────────────────────────────────────────────────────────────────
REM  install.bat  —  Run this ONCE before using the app
REM  Requires: Python 3.9+ installed and on PATH
REM ─────────────────────────────────────────────────────────────────────────────

echo === Facebook Group Activity Scanner — First-time Setup ===
echo.

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Download it from https://www.python.org/downloads/
    pause & exit /b 1
)

echo [1/3] Creating virtual environment...
python -m venv venv
if errorlevel 1 ( echo Failed to create venv & pause & exit /b 1 )

echo [2/3] Installing Python packages...
venv\Scripts\pip install --upgrade pip >nul
venv\Scripts\pip install -r requirements.txt
if errorlevel 1 ( echo Failed to install packages & pause & exit /b 1 )

echo [3/3] Downloading Chromium browser (one-time, ~200MB)...
venv\Scripts\playwright install chromium
if errorlevel 1 ( echo Failed to download browser & pause & exit /b 1 )

echo.
echo =====================================================
echo  Setup complete!  Run the app with:  run.bat
echo =====================================================
pause
