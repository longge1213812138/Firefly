@echo off
cd /d "%~dp0"
title 流萤 Fairy - 控制台
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=D://Python313//python.exe"
if not exist "%PY%" set "PY=python"
echo 正在打开流萤控制台（对话 / 记忆 / 配置 / 状态）...
"%PY%" gui.py
if errorlevel 1 pause
