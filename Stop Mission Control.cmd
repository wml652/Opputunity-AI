@echo off
title Stop Mission Control
cd /d "%~dp0"
set "PY=backend\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" scripts\launcher.py --stop
ping -n 4 127.0.0.1 >nul
