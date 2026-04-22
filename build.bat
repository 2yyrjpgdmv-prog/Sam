@echo off
REM ─────────────────────────────────────────────────────────────────────────────
REM  build.bat  —  Package into a standalone .exe  (run install.bat first)
REM  NOTE: The .exe will be large (~250MB) because it bundles Chromium.
REM        For most users, run.bat is simpler.
REM ─────────────────────────────────────────────────────────────────────────────

echo Installing PyInstaller...
venv\Scripts\pip install pyinstaller

echo Building FBGroupScanner.exe...
venv\Scripts\pyinstaller ^
    --onedir ^
    --windowed ^
    --name "FBGroupScanner" ^
    --collect-all playwright ^
    main.py

echo.
if exist dist\FBGroupScanner\FBGroupScanner.exe (
    echo SUCCESS — dist\FBGroupScanner\FBGroupScanner.exe
) else (
    echo Build failed — check output above.
)
pause
