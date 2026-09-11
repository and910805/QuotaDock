@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo.
echo ========================================
echo   Quota PromptDock 原始碼安裝工具
echo ========================================
echo.

where py >nul 2>nul
if errorlevel 1 (
    echo [錯誤] 找不到 Python。
    echo 請安裝 Python 3.12 以上版本： https://www.python.org/downloads/windows/
    pause
    exit /b 1
)

echo [1/4] 建立獨立 Python 環境…
py -3 -m venv .venv
if errorlevel 1 goto :failed

call ".venv\Scripts\activate.bat"

echo [2/4] 安裝依賴套件…
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 goto :failed

echo [3/4] 執行測試並打包…
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_release.ps1"
if errorlevel 1 goto :failed

echo [4/4] 安裝桌面捷徑…
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
if errorlevel 1 goto :failed

echo.
echo Quota PromptDock 已完成安裝。
pause
exit /b 0

:failed
echo.
echo [錯誤] 安裝失敗，請查看上方訊息。
pause
exit /b 1
