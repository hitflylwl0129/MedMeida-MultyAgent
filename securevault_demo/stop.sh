#!/bin/bash
# stop.sh - 停止 SecureVault demo 并清理
# 用法:
#   ./stop.sh           停止 demo, 保留底层数据 (可下次再启动)
#   ./stop.sh --purge   停止 demo 并删除底层数据 (彻底重置)

set -u
DEMO_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND="$HOME/.sv_demo_store"
MOUNT="$HOME/SecureVaultDemo"
PID_FILE="$DEMO_DIR/.sv_demo.pid"

PURGE=0
[[ "${1:-}" == "--purge" ]] && PURGE=1

echo "==> SecureVault demo 停止"

# 1) 卸载挂载点
if mount | grep -q " on $MOUNT "; then
  echo "   卸载 $MOUNT ..."
  umount "$MOUNT" 2>/dev/null \
    || diskutil unmount "$MOUNT" 2>/dev/null \
    || umount -f "$MOUNT" 2>/dev/null \
    || true
fi

# 2) 停止 demo 进程
if [[ -f "$PID_FILE" ]]; then
  PID=$(cat "$PID_FILE")
  if kill -0 "$PID" 2>/dev/null; then
    echo "   停止 demo 进程 PID=$PID ..."
    kill "$PID" 2>/dev/null || true
    sleep 1
    kill -9 "$PID" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
fi

# 兜底: 找其他残留的 sv_demo.py 进程
LEFT=$(pgrep -f "sv_demo.py" || true)
if [[ -n "$LEFT" ]]; then
  echo "   清理残留进程: $LEFT"
  echo "$LEFT" | xargs kill 2>/dev/null || true
fi

# 3) 删除空挂载点目录 (不删数据)
rmdir "$MOUNT" 2>/dev/null || true

# 4) 可选: 清空后端数据
if [[ $PURGE -eq 1 ]]; then
  echo "   --purge 模式: 删除底层数据 $BACKEND"
  rm -rf "$BACKEND"
fi

echo "OK 已停止"
[[ $PURGE -eq 0 ]] && [[ -d "$BACKEND" ]] \
  && echo "   底层数据保留在: $BACKEND  (下次 ./start.sh 仍可用; 彻底清空: ./stop.sh --purge)"
