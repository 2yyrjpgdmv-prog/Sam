@echo off
REM ─────────────────────────────────────────────────────────────────────────────
REM  Build script — produces a single .exe in the dist\ folder
REM  Requirements: Python 3.9+ on PATH
REM ─────────────────────────────────────────────────────────────────────────────

echo Installing dependencies...
pip install -r requirements.txt

echo.
echo Building FBGroupScanner.exe ...
pyinstaller ^
    --onefile ^
    --windowed ^
    --name "FBGroupScanner" ^
    --add-data "." ^
    main.py

echo.
if exist dist\FBGroupScanner.exe (
    echo SUCCESS: dist\FBGroupScanner.exe is ready.
) else (
    echo ERROR: Build failed. Check the output above.
)
pause
