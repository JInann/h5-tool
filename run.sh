#!/usr/bin/env bash
# 启动 H5 小工具后端服务。
# 兼容：macOS / Linux / Windows（Git Bash / MSYS / WSL）
#
# Python 探测优先级：
#   1. 环境变量 PYTHON
#   2. 项目 venv（.venv/bin/python 或 .venv/Scripts/python.exe）
#   3. workbuddy managed python（macOS 用户环境，统一证书/版本）
#   4. 系统 python3 / python / py
# 依赖安装见 install.sh（或 install.bat，Windows）
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for c in \
    ".venv/bin/python" ".venv/Scripts/python.exe" \
    "/Users/admin/.workbuddy/binaries/python/versions/3.13.12/bin/python3" \
    "python3" "python" "py"; do
    if [ -x "$c" ] || command -v "$c" >/dev/null 2>&1; then
      if "$c" --version >/dev/null 2>&1; then
        PY="$c"
        break
      fi
    fi
  done
fi

if [ -z "$PY" ]; then
  echo "[run] 未找到 Python。请先执行 ./install.sh（macOS/Linux）或 install.bat（Windows）" >&2
  exit 1
fi

# 兼容 "py -3" 这类带参数的命令
if [ "${PY#* }" != "$PY" ]; then
  exec sh -c "$PY \"$DIR/server.py\" \"\$@\"" sh "$@"
fi
exec "$PY" "$DIR/server.py" "$@"
