@echo off
set "DIR=%~dp0"
cd /d "%DIR%"
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PATH=%DIR%;%PATH%"
title Hyperliquid Order Terminal

echo Hyperliquid order terminal
echo.
if not exist "%DIR%.venv\Scripts\python.exe" (
  echo First run setup-windows.cmd in this folder.
  echo.
)
echo Commands:
echo   query
echo   order BTC buy --dry-run
echo   markets BTC
echo.
cmd /k
