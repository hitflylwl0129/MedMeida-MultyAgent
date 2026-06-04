#!/bin/bash
# start.sh - 后台启动 SecureVault demo
set -u
DEMO_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND="$HOME/.sv_demo_store"
MOUNT="$HOME/SecureVaultDemo"
LOG="$DEMO_DIR/sv_demo.log"
PID_FILE="$DEMO_DIR/.sv_demo.pid"

# 父进程链向上查多少级 (加上此值后, 白名单 App 派生的所有子进程都自动通过)
ANCESTOR_DEPTH=6

# 白名单进程
# Electron App 的实际工作进程是各种 Helper.app, 4 个 helper 都要加白
ALLOWLIST=(
  /usr/bin/tee
  /bin/cat
  "/Applications/CodeBuddy CN.app/Contents/MacOS/Electron"
  "/Applications/CodeBuddy CN.app/Contents/Frameworks/CodeBuddy CN Helper.app/Contents/MacOS/CodeBuddy CN Helper"
  "/Applications/CodeBuddy CN.app/Contents/Frameworks/CodeBuddy CN Helper (Renderer).app/Contents/MacOS/CodeBuddy CN Helper (Renderer)"
  "/Applications/CodeBuddy CN.app/Contents/Frameworks/CodeBuddy CN Helper (Plugin).app/Contents/MacOS/CodeBuddy CN Helper (Plugin)"
  "/Applications/WorkBuddy.app/Contents/MacOS/Electron"
  "/Applications/WorkBuddy.app/Contents/Frameworks/WorkBuddy Helper.app/Contents/MacOS/WorkBuddy Helper"
  "/Applications/WorkBuddy.app/Contents/Frameworks/WorkBuddy Helper (Renderer).app/Contents/MacOS/WorkBuddy Helper (Renderer)"
  "/Applications/WorkBuddy.app/Contents/Frameworks/WorkBuddy Helper (Plugin).app/Contents/MacOS/WorkBuddy Helper (Plugin)"
)

echo "==> SecureVault demo 启动"

# 1) 检查是否已经在跑
if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "!! demo 已经在运行 (PID=$(cat "$PID_FILE"))"
  echo "   如需重启, 请先: ./stop.sh"
  exit 1
fi

if mount | grep -q " on $MOUNT "; then
  echo "!! 挂载点已存在: $MOUNT"
  echo "   请先: ./stop.sh   再重试"
  exit 1
fi

# 2) 准备目录
mkdir -p "$BACKEND" "$MOUNT"
echo "   后端目录: $BACKEND"
echo "   挂载点  : $MOUNT"

# 3) 拼接 --allow 参数
ALLOW_ARGS=()
for app in "${ALLOWLIST[@]}"; do
  ALLOW_ARGS+=(--allow "$app")
done

# 4) 启动
nohup python3 "$DEMO_DIR/sv_demo.py" "$BACKEND" "$MOUNT" \
    "${ALLOW_ARGS[@]}" \
    --ancestor-depth "$ANCESTOR_DEPTH" \
    > "$LOG" 2>&1 &

DEMO_PID=$!
echo "$DEMO_PID" > "$PID_FILE"

# 5) 等挂载就绪 (最多 5 秒)
for i in 1 2 3 4 5; do
  sleep 1
  if mount | grep -q " on $MOUNT "; then
    break
  fi
done

if ! mount | grep -q " on $MOUNT "; then
  echo "!! 启动失败, 看一下日志:"
  echo "   $LOG"
  echo "----- 日志末尾 -----"
  tail -15 "$LOG"
  rm -f "$PID_FILE"
  exit 1
fi

echo
echo "OK demo 已后台运行"
echo "   PID            : $DEMO_PID"
echo "   日志           : $LOG  (tail -f 查看)"
echo "   挂载点         : $MOUNT"
echo "   白名单进程数   : ${#ALLOWLIST[@]}"
echo "   父进程链深度   : $ANCESTOR_DEPTH (CodeBuddy/WorkBuddy 派生的所有子进程自动通过)"
echo
echo "试试看 (复制下面任一行执行):"
echo "   echo \"机密内容\" | /usr/bin/tee ~/SecureVaultDemo/note.txt"
echo "   cat  ~/SecureVaultDemo/note.txt           # 白名单 -> 明文"
echo "   xxd  ~/SecureVaultDemo/note.txt           # 非白名单 -> 密文"
echo "   ./sv-mode cipher / plain / whitelist     # 三模式开关"
echo "   ./status.sh                               # 看运行状态"
echo "   ./stop.sh                                 # 停止并清理"

