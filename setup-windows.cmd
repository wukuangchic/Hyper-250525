@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"

echo Creating local Python environment...
py -3 -m venv .venv
if errorlevel 1 goto error

echo Updating pip...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto error

if exist "hyperliquid-python-sdk\pyproject.toml" (
  echo Installing dependencies from local SDK copy...
  ".venv\Scripts\python.exe" -m pip install eth-account==0.13.7 ".\hyperliquid-python-sdk"
) else (
  echo Installing dependencies from requirements.txt...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)
if errorlevel 1 goto error

echo.
echo Setup complete. You can now run order-terminal-windows.cmd.
pause
exit /b 0

:error
echo.
echo Setup failed. Check the message above.
pause
exit /b 1
