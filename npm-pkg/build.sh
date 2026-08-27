#!/bin/bash
# npm-pkg 构建脚本：从主目录同步源码 → 生成 npm 包产物（tgz）
#
# 源码单一来源 = 主目录（server.py / cdp.py / scr_stream.py / scrcpy-server / web/），
# 这里只是构建时拷贝的产物，已 gitignore。改代码永远只改主目录一处。
#
# 用法：
#   ./build.sh            # 同步源码 + 生成 fkjs-h5-tool-<version>.tgz
#   ./build.sh --sync     # 只同步源码，不 pack
set -e
cd "$(dirname "$0")"

ROOT="$(cd .. && pwd)"   # 主目录（npm-pkg/ 的上一级）
SYNC_FILES=(server.py cdp.py scr_stream.py scrcpy-server)
SYNC_DIRS=(web)

echo "[build] 从 $ROOT 同步源码到 npm-pkg/ ..."
for f in "${SYNC_FILES[@]}"; do
  cp "$ROOT/$f" "$f"
  echo "  ✓ $f"
done
for d in "${SYNC_DIRS[@]}"; do
  rm -rf "$d" && cp -r "$ROOT/$d" "$d"
  echo "  ✓ $d/"
done

if [ "$1" = "--sync" ]; then
  echo "[build] 仅同步完成"
  exit 0
fi

echo "[build] npm pack ..."
npm pack
echo "[build] 完成：$(ls -t fkjs-h5-tool-*.tgz | head -1)"
