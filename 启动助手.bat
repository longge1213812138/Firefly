@echo off
cd /d "%~dp0"
title Firefly ������������
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=C:\Users\91533\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo ==========================================
echo    Firefly ��ө - ������������
echo ==========================================
echo ʹ�õ� Python: %PY%
echo.
"%PY%" main.py --diag
echo.
echo �������ASR / TTS / LLM��ȫ�̼��������Ի���
echo �Լ���...
"%PY%" main.py --selftest
if errorlevel 1 (
  echo.
  echo [ʧ��] �Լ�δͨ�����뿴�����ʧ���
  pause
  exit /b 1
)
echo.
echo [����] ������������ո�˵������ Q �˳���
"%PY%" main.py
echo.
echo �������˳���
pause
