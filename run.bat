@echo off
rem ============================================================
rem  H5 小工具 启动脚本（Windows）
rem  用法：run.bat [--host 0.0.0.0 --port 12787]
rem ============================================================
setlocal
cd /d "%~dp0"

rem 优先用 venv，其次系统 python / py
set "PY="
if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  where py >nul 2>nul
  if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
  echo [run] 未找到 Python。请先执行 install.bat
  pause
  exit /b 1
)

%PY% server.py %*
