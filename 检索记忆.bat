@echo off
cd /d "%~dp0"
title Fairy 本地语音助手
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=C:\Users\91533\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
set /p KW=请输入要检索的关键词：
"%PY%" main.py --search "%KW%"
echo.
pause
