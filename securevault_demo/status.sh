#!/bin/bash
# status.sh - 查看 SecureVault demo 运行状态
set -u
DEMO_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND="$HOME/.sv_demo_store"
MOUNT="$HOME/SecureVaultDemo"
LOG="$DEMO_DIR/sv_demo.log"
PID_FILE="$DEMO_DIR/.sv_demo.pid"
CONTROL="$BACKEND/.sv_control"

echo "==> SecureVault demo 状态"
echo

# 进程
if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "  进程     : RUNNING (PID=$(cat "$PID_FILE"))"
else
  echo "  进程     : STOPPED"
fi

# 挂载
if mount | grep -q " on $MOUNT "; then
  echo "  挂载     : MOUNTED $MOUNT"
else
  echo "  挂载     : NOT MOUNTED"
fi

# 模式
if [[ -f "$CONTROL" ]]; then
  echo "  当前模式 : $(cat "$CONTROL")"
else
  echo "  当前模式 : (控制文件不存在)"
fi

# 数据
if [[ -d "$BACKEND" ]]; then
  N=$(find "$BACKEND" -type f ! -name ".sv_control" | wc -l | tr -d ' ')
  echo "  后端数据 : $BACKEND  ($N 个文件)"
fi

# 日志路径
echo "  日志     : $LOG"

# 挂载点文件列表
if mount | grep -q " on $MOUNT "; then
  echo
  echo "  挂载点内容:"
  ls -la "$MOUNT" 2>/dev/null | sed 's/^/    /'
fi
