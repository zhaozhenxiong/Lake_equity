@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run py -3.12 install.py first.
  exit /b 1
)
if "%~1"=="" (
  ".venv\Scripts\python.exe" run.py serve
) else (
  ".venv\Scripts\python.exe" run.py %*
)
