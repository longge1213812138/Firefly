@echo off
cd /d "%~dp0"
title Fairy ±æµÿ”Ô“Ù÷˙ ÷
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=C:\Users\91533\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" main.py --mic-test
echo.
pause
