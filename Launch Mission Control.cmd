@echo off
rem One-click launcher for Opportunity Mission Control. Safe to double-click repeatedly.
title Opportunity Mission Control
cd /d "%~dp0"
set "PY=backend\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" scripts\launcher.py %*
if errorlevel 1 (
  echo.
  echo Launch failed. Logs are in "%~dp0.logs".
  pause
  exit /b 1
)
ping -n 4 127.0.0.1 >nul
