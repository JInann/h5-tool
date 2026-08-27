#!/usr/bin/env bash
# H5 小工具 一键安装脚本
# 兼容：macOS / Linux / Windows（Git Bash 或 WSL 下运行）
#
# 流程：找 Python → 校验版本 → 找 Node（可选）→ 校验版本 → 找 adb
#       → 安装 Python 依赖（若有 requirements.txt）→ 校验 devtools 产物 → 验证服务可启动
#
# 用法：
#   ./install.sh            # 全量检测与安装
#   ./install.sh --skip-node  # 跳过 Node 检查（不用 devtools 构建时）
#   ./install.sh --verbose    # 输出每条检测明细

set -e

INFO='\033[1;36m'
OK='\033[1;32m'
WARN='\033[1;33m'
FAIL='\033[1;31m'
NC='\033[0m'

info() { printf "${INFO}[install]${NC} %s\n" "$*"; }
ok()   { printf "${OK}[OK]${NC} %s\n" "$*"; }
warn() { printf "${WARN}[WARN]${NC} %s\n" "$*"; }
fail() { printf "${FAIL}[FAIL]${NC} %s\n" "$*"; exit 1; }

SKIP_NODE=0
VERBOSE=0
for a in "$@"; do
  case "$a" in
    --skip-node) SKIP_NODE=1 ;;
    --verbose)   VERBOSE=1 ;;
    *) warn "忽略未知参数: $a" ;;
  esac
done

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# ---------- 0. 平台识别 ----------
OS="$(uname -s 2>/dev/null || echo Windows)"
case "$OS" in
  Darwin)  OS_NAME="macOS" ;;
  Linux)   OS_NAME="Linux" ;;
  MINGW*|MSYS*|CYGWIN*) OS_NAME="Windows (Git Bash/MSYS)" ;;
  *)       OS_NAME="$OS" ;;
esac
info "平台: $OS_NAME"

# ---------- 1. 查找 Python ----------
find_python() {
  # 按优先级尝试：项目 venv → 环境变量 PYTHON → workbuddy managed python（mac）→ 系统命令
  local candidates=(
    ".venv/bin/python" ".venv/Scripts/python.exe"
    "$PYTHON"
    "/Users/admin/.workbuddy/binaries/python/versions/3.13.12/bin/python3"
    "python3" "python" "py"
  )
  local c
  for c in "${candidates[@]}"; do
    [ -z "$c" ] && continue
    if [ -x "$c" ] || command -v "$c" >/dev/null 2>&1; then
      if "$c" --version >/dev/null 2>&1; then
        echo "$c"
        return 0
      fi
    fi
  done
  return 1
}

info "步骤 1/6：查找 Python…"
PY="$(find_python)" || fail "未找到 Python。请安装 Python 3.8+（https://www.python.org/downloads/），Windows 安装时勾选 Add to PATH"
[ "$VERBOSE" = "1" ] && info "  使用: $PY"
PY_VER="$("$PY" --version 2>&1 | sed 's/Python //')"
ok "Python: $PY_VER ($PY)"

# ---------- 2. 校验 Python 版本 ----------
info "步骤 2/6：校验 Python 版本（要求 >= 3.8）…"
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
  fail "Python 版本过低: $PY_VER，需要 >= 3.8"
fi
ok "Python 版本通过"

# ---------- 3. 查找 Node（可选，仅 devtools 构建需要） ----------
NODE=""
if [ "$SKIP_NODE" = "0" ]; then
  info "步骤 3/6：查找 Node.js（可选，devtools-frontend 构建需要）…"
  for c in node node.exe; do
    if command -v "$c" >/dev/null 2>&1; then NODE="$c"; break; fi
  done
  if [ -z "$NODE" ]; then
    warn "未找到 Node.js —— 不影响服务运行，仅在做「WebView 调试面板」重建 devtools 产物时需要（npm run build）"
  else
    NODE_VER="$("$NODE" --version 2>&1 | sed 's/^v//')"
    if "$NODE" -e 'const [m]=process.versions.node.split(".").map(Number); process.exit(m>=18?0:1)' 2>/dev/null; then
      ok "Node.js: v$NODE_VER"
    else
      warn "Node.js 版本过低: v$NODE_VER（devtools 构建建议 >= 18）；服务运行不受影响"
    fi
  fi
else
  info "步骤 3/6：跳过 Node 检查（--skip-node）"
fi

# ---------- 4. 查找 adb ----------
info "步骤 4/6：查找 adb（Android 调试桥）…"
if command -v adb >/dev/null 2>&1 || command -v adb.exe >/dev/null 2>&1; then
  ADB_VER="$(adb version 2>&1 | head -1)"
  ok "adb: $ADB_VER"
else
  warn "未找到 adb —— 手机控制/镜像/调试功能不可用。请安装 Android Platform Tools（https://developer.android.com/tools/releases/platform-tools）并加入 PATH"
fi

# ---------- 5. 安装 Python 依赖 ----------
info "步骤 5/6：安装 Python 依赖…"
if [ -f "requirements.txt" ]; then
  info "  发现 requirements.txt，执行安装…"
  "$PY" -m pip install -r requirements.txt --quiet || warn "pip 安装失败，请手动执行: $PY -m pip install -r requirements.txt"
  ok "Python 依赖安装完成"
else
  info "  无 requirements.txt（本项目纯标准库实现，无需第三方依赖）"
fi

# ---------- 6. 校验 devtools 产物 + 服务可启动 ----------
info "步骤 6/6：校验 devtools 产物与服务…"
DEVTOOLS_MARKER="devtools-local/front_end/inspector.html"
if [ -f "$DEVTOOLS_MARKER" ]; then
  ok "devtools-frontend 产物存在（WebView 调试面板可用）"
else
  warn "devtools 产物缺失 —— 「WebView 调试」面板不可用，其余功能正常。"
  warn "  获取方式：见 README「更新 devtools-frontend 产物」或设置 DEVTOOLS_DIR 指向已有构建目录"
fi

if "$PY" -m py_compile server.py cdp.py scr_stream.py 2>/dev/null; then
  ok "服务代码语法校验通过"
else
  warn "服务代码语法校验失败，请检查 server.py / cdp.py / scr_stream.py"
fi

echo
ok "安装完成！启动方式："
echo "    ./run.sh           # macOS / Linux / Git Bash"
echo "    run.bat            # Windows cmd / PowerShell"
echo "启动后浏览器打开 http://127.0.0.1:12787/"
