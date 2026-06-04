#!/bin/bash
# demo_mode_switch.sh - 演示 sv-mode 三种模式秒切效果
set -u

DEMO_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND="$HOME/.sv_demo_store"
MOUNT="$HOME/SecureVaultDemo"
LOG="$DEMO_DIR/sv_demo.log"

cleanup() {
  echo
  echo "==> 清理"
  umount "$MOUNT" 2>/dev/null || diskutil unmount "$MOUNT" 2>/dev/null || true
  [[ -n "${DEMO_PID:-}" ]] && kill "$DEMO_PID" 2>/dev/null || true
}
trap cleanup EXIT

show() {
  echo
  echo "──────────────────────────────────────────"
  echo "▶ $1"
  echo "──────────────────────────────────────────"
}

echo "==> 0. 准备"
rm -rf "$BACKEND" "$MOUNT"
mkdir -p "$BACKEND" "$MOUNT"

echo "==> 1. 启动 demo (白名单: tee, cat)"
python3 "$DEMO_DIR/sv_demo.py" "$BACKEND" "$MOUNT" \
    --allow /usr/bin/tee --allow /bin/cat > "$LOG" 2>&1 &
DEMO_PID=$!
sleep 2
mount | grep -q "$MOUNT" || { echo "挂载失败"; cat "$LOG"; exit 1; }

echo "==> 2. 用白名单 tee 写入一个文件"
echo "secret content for board meeting" | /usr/bin/tee "$MOUNT/secret.txt" > /dev/null
sleep 0.3

show "场景 A: 默认 whitelist 模式"
"$DEMO_DIR/sv-mode"
echo
echo "  $ cat secret.txt   (cat 在白名单)"
/bin/cat "$MOUNT/secret.txt" | sed 's/^/    /'
echo "  $ xxd secret.txt | head -1   (xxd 不在白名单)"
/usr/bin/xxd "$MOUNT/secret.txt" | head -1 | sed 's/^/    /'

show "场景 B: 切到 cipher 模式 -> 连 cat 也只能看到密文"
"$DEMO_DIR/sv-mode" cipher
echo
echo "  $ cat secret.txt"
/bin/cat "$MOUNT/secret.txt" | sed 's/^/    /'
echo "  $ xxd secret.txt | head -1"
/usr/bin/xxd "$MOUNT/secret.txt" | head -1 | sed 's/^/    /'

show "场景 C: 切到 plain 模式 -> 连 xxd 也能看到明文 (慎用!)"
"$DEMO_DIR/sv-mode" plain
echo
echo "  $ xxd secret.txt | head -1"
/usr/bin/xxd "$MOUNT/secret.txt" | head -1 | sed 's/^/    /'
echo "  $ head -c 40 secret.txt"
/usr/bin/head -c 40 "$MOUNT/secret.txt" | sed 's/^/    /'
echo

show "场景 D: 切回 whitelist 模式 -> 恢复正常工作状态"
"$DEMO_DIR/sv-mode" whitelist
echo
echo "  $ cat secret.txt"
/bin/cat "$MOUNT/secret.txt" | sed 's/^/    /'
echo "  $ xxd secret.txt | head -1"
/usr/bin/xxd "$MOUNT/secret.txt" | head -1 | sed 's/^/    /'

echo
echo "==> demo 日志末尾 (看 READ 日志里的 mode= 标记):"
grep -E "READ " "$LOG" | tail -10 | sed 's/^/    /'

echo
echo "==> 演示完毕, 3 秒后清理..."
sleep 3
