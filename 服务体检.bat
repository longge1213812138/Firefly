@echo off
cd /d "%~dp0"
title Fairy 本地语音助手
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=C:\Users\91533\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo 正在体检云端服务（ASR / TTS / LLM）...
"%PY%" main.py --diag
echo.
pause
