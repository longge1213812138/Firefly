@echo off
cd /d "%~dp0"
title Firefly - Build EXE
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo ==========================================
echo   Firefly - build runnable EXE (PyInstaller)
echo ==========================================
echo Using Python: %PY%
echo.
"%PY%" build_exe.py %*
if errorlevel 1 (
  echo.
  echo [FAILED] build did not finish successfully. See messages above.
  pause
  exit /b 1
)
echo.
echo [DONE] Result folder: dist
pause
