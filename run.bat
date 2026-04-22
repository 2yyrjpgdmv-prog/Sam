@echo off
REM Run the app using the virtual environment created by install.bat
venv\Scripts\python main.py
if errorlevel 1 (
    echo.
    echo App exited with an error. Did you run install.bat first?
    pause
)
