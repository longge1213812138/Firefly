@echo off
cd /d "%~dp0"
title Á÷Ó© Firefly - ×À³è
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=D://Python313//python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" main.py --pet
