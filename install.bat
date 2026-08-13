@echo off
setlocal
cd /d "%~dp0"

echo.
echo ========================================
echo   QuotaDock source installer for Windows
echo ========================================
echo.

where py >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found.
    echo Install Python 3.11 or newer from https://www.python.org/downloads/windows/
    pause
    exit /b 1
)

echo [1/4] Creating an isolated Python environment...
py -3 -m venv .venv
if errorlevel 1 goto :failed

call ".venv\Scripts\activate.bat"

echo [2/4] Installing dependencies...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 goto :failed

echo [3/4] Running tests and building QuotaDock...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_release.ps1"
if errorlevel 1 goto :failed

echo [4/4] Installing the desktop shortcut...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
if errorlevel 1 goto :failed

echo.
echo QuotaDock was installed successfully.
pause
exit /b 0

:failed
echo.
echo [ERROR] Installation failed. Review the messages above for details.
pause
exit /b 1
