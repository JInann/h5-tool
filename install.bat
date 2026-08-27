@echo off
rem ============================================================
rem  H5 小工具 一键安装脚本（Windows）
rem  流程：找 Python → 校验版本 → 找 Node(可选) → 校验版本
rem        → 找 adb → 安装 Python 依赖 → 校验 devtools 产物
rem  用法：install.bat [--skip-node]
rem ============================================================
setlocal enabledelayedexpansion
chcp 65001 >nul 2>nul
cd /d "%~dp0"

set "SKIP_NODE=0"
for %%a in (%*) do if /i "%%a"=="--skip-node" set "SKIP_NODE=1"

echo [install] 平台: Windows

rem ---------- 1. 查找 Python ----------
echo [install] 步骤 1/6：查找 Python...
set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo [FAIL] 未找到 Python。请安装 Python 3.8+（https://www.python.org/downloads/），安装时勾选 Add to PATH
  exit /b 1
)

for /f "delims=" %%v in ('%PY% --version 2^>^&1') do set "PY_VER=%%v"
echo [OK] Python: !PY_VER!

rem ---------- 2. 校验 Python 版本 ----------
echo [install] 步骤 2/6：校验 Python 版本（要求 ^>= 3.8）...
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)" >nul 2>nul
if errorlevel 1 (
  echo [FAIL] Python 版本过低：!PY_VER!，需要 ^>= 3.8
  exit /b 1
)
echo [OK] Python 版本通过

rem ---------- 3. 查找 Node（可选） ----------
if "%SKIP_NODE%"=="0" (
  echo [install] 步骤 3/6：查找 Node.js（可选，devtools-frontend 构建需要）...
  set "NODE="
  where node >nul 2>nul
  if not errorlevel 1 set "NODE=node"
  if not defined NODE (
    echo [WARN] 未找到 Node.js —— 不影响服务运行，仅重建 devtools 面板产物时需要
  ) else (
    for /f "delims=" %%v in ('node --version 2^>^&1') do set "NODE_VER=%%v"
    node -e "const [m]=process.versions.node.split('.').map(Number); process.exit(m>=18?0:1)" >nul 2>nul
    if errorlevel 1 (
      echo [WARN] Node.js 版本过低：!NODE_VER!（devtools 构建建议 ^>= 18）；服务运行不受影响
    ) else (
      echo [OK] Node.js: !NODE_VER!
    )
  )
) else (
  echo [install] 步骤 3/6：跳过 Node 检查（--skip-node）
)

rem ---------- 4. 查找 adb ----------
echo [install] 步骤 4/6：查找 adb...
where adb >nul 2>nul
if errorlevel 1 (
  echo [WARN] 未找到 adb —— 手机控制/镜像/调试功能不可用。请安装 Android Platform Tools 并加入 PATH
) else (
  for /f "delims=" %%v in ('adb version 2^>^&1 ^| findstr /i "version"') do set "ADB_VER=%%v"
  echo [OK] adb: !ADB_VER!
)

rem ---------- 5. 安装 Python 依赖 ----------
echo [install] 步骤 5/6：安装 Python 依赖...
if exist requirements.txt (
  echo [install]   发现 requirements.txt，执行安装...
  %PY% -m pip install -r requirements.txt --quiet
  if errorlevel 1 (
    echo [WARN] pip 安装失败，请手动执行：%PY% -m pip install -r requirements.txt
  ) else (
    echo [OK] Python 依赖安装完成
  )
) else (
  echo [install]   无 requirements.txt（本项目纯标准库实现，无需第三方依赖）
)

rem ---------- 6. 校验 devtools 产物与服务 ----------
echo [install] 步骤 6/6：校验 devtools 产物与服务...
if exist "devtools-local\front_end\inspector.html" (
  echo [OK] devtools-frontend 产物存在（WebView 调试面板可用）
) else (
  echo [WARN] devtools 产物缺失 —— 「WebView 调试」面板不可用，其余功能正常。
  echo [WARN]   获取方式：见 README「更新 devtools-frontend 产物」或设置 DEVTOOLS_DIR 环境变量
)

%PY% -m py_compile server.py cdp.py scr_stream.py >nul 2>nul
if errorlevel 1 (
  echo [WARN] 服务代码语法校验失败，请检查 server.py / cdp.py / scr_stream.py
) else (
  echo [OK] 服务代码语法校验通过
)

echo.
echo [OK] 安装完成！启动方式：
echo      run.bat
echo      （或 python server.py --host 127.0.0.1 --port 12787）
echo     启动后浏览器打开 http://127.0.0.1:12787/
pause
