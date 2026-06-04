#!/usr/bin/env bash
# 短视频制作 Agent 后端启动脚本
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
VENV=".venv"

WARMUP=0
for arg in "$@"; do
  case "$arg" in
    --warmup) WARMUP=1 ;;
    -h|--help)
      echo "用法: ./run.sh [--warmup]"
      echo "  --warmup   启动前预热形象库（6 张图直传 VOD 换 FileId 入缓存）"
      exit 0
      ;;
    *) echo "未知参数: $arg（可用 --warmup / --help）" >&2; exit 2 ;;
  esac
done

if [ ! -d "$VENV" ]; then
  echo "==> 创建虚拟环境 $VENV"
  "$PY" -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

echo "==> 安装依赖"
pip install -q --upgrade pip
pip install -q -r requirements.txt

if [ ! -f ".env" ]; then
  echo "==> 未发现 .env，从 .env.example 复制（记得填密钥）"
  cp .env.example .env
fi

if [ "$WARMUP" -eq 1 ]; then
  echo "==> 预热形象库（6 张图直传 VOD 换 FileId 入缓存）"
  if ! python warmup.py; then
    echo "!! 预热存在失败项，请检查上方日志（常见：密钥无效/VOD 未开通）" >&2
    exit 1
  fi
fi

HOST="$(grep -E '^APP_HOST=' .env | cut -d= -f2 || echo 127.0.0.1)"
PORT="$(grep -E '^APP_PORT=' .env | cut -d= -f2 || echo 8000)"
echo "==> 启动 http://${HOST:-127.0.0.1}:${PORT:-8000}  (Ctrl+C 退出)"
exec uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"
