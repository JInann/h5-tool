#!/usr/bin/env bash
# 启动 H5 小工具后端服务。
# 使用 workbuddy 自带的 python（system python3 缺 CA 证书，且这里统一环境）。
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="/Users/admin/.workbuddy/binaries/python/versions/3.13.12/bin/python3"
[ -x "$PY" ] || PY="python3"
exec "$PY" "$DIR/server.py" "$@"
