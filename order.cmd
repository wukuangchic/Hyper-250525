@echo off
setlocal
set "DIR=%~dp0"
cd /d "%DIR%"
set "PYTHONUTF8=1"

if exist "%DIR%.venv\Scripts\python.exe" (
  "%DIR%.venv\Scripts\python.exe" "%DIR%hl_order.py" %*
) else (
  py -3 "%DIR%hl_order.py" %*
)
exit /b %ERRORLEVEL%
