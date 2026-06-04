#!/bin/bash
# SecureVault Demo 自动化测试脚本
# 演示: 同一文件, cat (白名单) 读到明文, xxd (非白名单) 读到密文
set -u

DEMO_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND="$HOME/.sv_demo_store"
MOUNT="$HOME/SecureVaultDemo"
LOG="$DEMO_DIR/sv_demo.log"

cleanup() {
  echo
  echo "==> 清理: 卸载并停掉 demo 进程"
  umount "$MOUNT" 2>/dev/null || diskutil unmount "$MOUNT" 2>/dev/null || true
  if [[ -n "${DEMO_PID:-}" ]]; then
    kill "$DEMO_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

echo "==> 0. 准备目录"
rm -rf "$BACKEND" "$MOUNT"
mkdir -p "$BACKEND" "$MOUNT"

echo "==> 1. 启动 SecureVault demo (后台)"
echo "    白名单: /bin/cat /usr/bin/tee  (tee 用来代表'写入'的白名单进程)"
echo "    日志: $LOG"
python3 "$DEMO_DIR/sv_demo.py" "$BACKEND" "$MOUNT" \
    --allow /bin/cat \
    --allow /usr/bin/tee \
    > "$LOG" 2>&1 &
DEMO_PID=$!
sleep 2

if ! mount | grep -q "$MOUNT"; then
  echo "!! 挂载失败, 查看日志:"
  cat "$LOG"
  exit 1
fi
echo "    挂载成功:"
mount | grep "$MOUNT" | sed 's/^/      /'

echo
echo "==> 2. 用白名单进程 (tee) 写入文件 'hello world from securevault'"
echo "    重要: 用 'tee' 而不是 'cat >' 因为 shell 重定向时 create 是 bash 做的, "
echo "          tee 自己 open 文件, create 的进程就是 tee 本身, 能命中白名单"
echo "hello world from securevault" | /usr/bin/tee "$MOUNT/notes.txt" > /dev/null
sleep 0.5

echo
echo "==> 3. 看一眼底层 backend 里实际存的字节 (应该是'密文', 即按位取反后的乱码)"
echo "    底层文件: $BACKEND/notes.txt"
echo "    hex 视图:"
xxd "$BACKEND/notes.txt" | sed 's/^/      /'

echo
echo "==> 4. [关键演示] 白名单 cat 读 -> 应该看到明文"
echo "    $ cat $MOUNT/notes.txt"
echo "    输出:"
/bin/cat "$MOUNT/notes.txt" | sed 's/^/      /'

echo
echo "==> 5. [关键演示] 非白名单 xxd 读 -> 应该看到密文(乱码)"
echo "    $ xxd $MOUNT/notes.txt"
echo "    输出:"
/usr/bin/xxd "$MOUNT/notes.txt" | sed 's/^/      /'

echo
echo "==> 6. [关键演示] 非白名单 head 读 -> 应该看到密文"
echo "    $ head -c 50 $MOUNT/notes.txt | od -c | head -2"
/usr/bin/head -c 50 "$MOUNT/notes.txt" | /usr/bin/od -c | head -2 | sed 's/^/      /'

echo
echo "==> 7. [关键演示] 非白名单 echo+重定向 写 -> 应该被拒绝 (Permission denied)"
echo "    $ echo 'malicious' > $MOUNT/evil.txt"
if echo "malicious" > "$MOUNT/evil.txt" 2>&1; then
  echo "    !! 居然写成功了, 策略未生效!"
else
  echo "    符合预期: 写操作被拒绝"
fi

echo
echo "==> 8. demo 运行日志摘要 (最后 15 行):"
tail -15 "$LOG" | sed 's/^/      /'

echo
echo "==> 测试完成。按 Ctrl-C 或等 3 秒自动清理..."
sleep 3
