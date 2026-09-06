@echo off
cd /d "%~dp0"
title Fairy 本地语音助手
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=C:\Users\91533\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo ==========================================
echo    Fairy 流萤 - 本地语音助手
echo ==========================================
echo 使用的 Python: %PY%
echo.
"%PY%" main.py --diag
echo.
echo 上面三项（ASR / TTS / LLM）全绿即可正常对话。
echo 自检中...
"%PY%" main.py --selftest
if errorlevel 1 (
  echo.
  echo [失败] 自检未通过，请看上面的失败项。
  pause
  exit /b 1
)
echo.
echo [就绪] 进入待命：按空格说话，按 Q 退出。
"%PY%" main.py
echo.
echo 程序已退出。
pause
