#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 一键发布：把本地 backend/ 与 prototype/ 同步到云服务器并重启 systemd 服务
#
# 用法：
#   ./deploy.sh                  # 同步 + 重启
#   ./deploy.sh --tail           # 同步 + 重启 + tail 后端日志（看启动是否正常）
#   ./deploy.sh --status         # 只看远端服务状态 + 健康检查
#   ./deploy.sh --restart-only   # 只重启远端服务，不同步代码
#   ./deploy.sh --dry-run        # 演练（仅打印 rsync 改动，不真改）
#
# 依赖：
#   - 本地：rsync、ssh；可选 sshpass（密码登录时用，brew install hudochenkov/sshpass/sshpass）
#   - 远端：systemd 已配 video-agent.service；nginx 已配 /opt/video-agent
#
# 凭证：
#   推荐用 SSH key（推送上去后免密）。也可设置环境变量 SSHPASS=xxx 走密码登录：
#       export SSHPASS='your-password'
#       ./deploy.sh
#   注意：密码方式需安装 sshpass；不要把密码硬编码进脚本/仓库。
# ---------------------------------------------------------------------------
set -euo pipefail

# ---- 可改配置 -------------------------------------------------------------
SERVER_HOST="${SERVER_HOST:-162.14.76.209}"
SERVER_USER="${SERVER_USER:-root}"
SERVER_DIR="${SERVER_DIR:-/opt/video-agent}"
SYSTEMD_UNIT="${SYSTEMD_UNIT:-video-agent.service}"
HEALTH_URL="${HEALTH_URL:-http://${SERVER_HOST}/api/health}"
# --------------------------------------------------------------------------

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# rsync 排除项：保护云端密钥/虚拟环境/缓存/日志
RSYNC_EXCLUDES=(
  --exclude='.venv'
  --exclude='.cache'
  --exclude='__pycache__'
  --exclude='*.pyc'
  --exclude='.env'                 # 云端 .env 由人工维护，不被本地覆盖
  --exclude='*.log'
  --exclude='data/'
  --exclude='.DS_Store'
)

# SSH 选项
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15)

# 选择 ssh / scp / rsync 包装：优先 SSH key，其次 sshpass+SSHPASS 环境变量
have_sshpass=0
if [[ -n "${SSHPASS:-}" ]]; then
  if command -v sshpass >/dev/null 2>&1; then
    have_sshpass=1
  else
    echo "!! 已设 SSHPASS 但未安装 sshpass；请：brew install hudochenkov/sshpass/sshpass" >&2
    exit 1
  fi
fi

ssh_cmd() {
  if [[ $have_sshpass -eq 1 ]]; then
    sshpass -e ssh "${SSH_OPTS[@]}" "$@"
  else
    ssh "${SSH_OPTS[@]}" "$@"
  fi
}

rsync_cmd() {
  local rsh
  if [[ $have_sshpass -eq 1 ]]; then
    rsh="sshpass -e ssh ${SSH_OPTS[*]}"
  else
    rsh="ssh ${SSH_OPTS[*]}"
  fi
  rsync -e "$rsh" "$@"
}

# ---- 子命令 ---------------------------------------------------------------
do_status() {
  echo "==> 远端服务状态"
  ssh_cmd "${SERVER_USER}@${SERVER_HOST}" "systemctl is-active ${SYSTEMD_UNIT} && echo --- && systemctl status ${SYSTEMD_UNIT} --no-pager | head -12"
  echo ""
  echo "==> 健康检查 ${HEALTH_URL}"
  curl -sS --max-time 8 "$HEALTH_URL" || echo "(健康检查失败)"
  echo ""
}

do_sync() {
  local dry=""
  [[ "${1:-}" == "--dry-run" ]] && dry="--dry-run -v"

  echo "==> 同步 backend → ${SERVER_USER}@${SERVER_HOST}:${SERVER_DIR}/backend/"
  # shellcheck disable=SC2086
  rsync_cmd -az --delete $dry "${RSYNC_EXCLUDES[@]}" \
    "${ROOT}/backend/" "${SERVER_USER}@${SERVER_HOST}:${SERVER_DIR}/backend/"

  echo "==> 同步 prototype → ${SERVER_USER}@${SERVER_HOST}:${SERVER_DIR}/prototype/"
  # shellcheck disable=SC2086
  rsync_cmd -az --delete $dry --exclude='.DS_Store' \
    "${ROOT}/prototype/" "${SERVER_USER}@${SERVER_HOST}:${SERVER_DIR}/prototype/"
}

do_restart() {
  echo "==> 远端重启 ${SYSTEMD_UNIT}"
  ssh_cmd "${SERVER_USER}@${SERVER_HOST}" "systemctl restart ${SYSTEMD_UNIT} && sleep 2 && systemctl is-active ${SYSTEMD_UNIT}"
  echo ""
  echo "==> 健康检查 ${HEALTH_URL}"
  for i in 1 2 3 4 5; do
    if curl -sS --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then
      curl -sS "$HEALTH_URL" | head -c 400
      echo ""
      echo "✓ 服务就绪"
      return 0
    fi
    sleep 1
  done
  echo "!! 健康检查 5 次未通过，请：./deploy.sh --tail" >&2
  return 1
}

do_tail() {
  echo "==> tail 远端日志（Ctrl+C 退出）"
  ssh_cmd -t "${SERVER_USER}@${SERVER_HOST}" "tail -n 50 -f /var/log/video-agent.log"
}

# ---- 入口 -----------------------------------------------------------------
case "${1:-}" in
  --status)
    do_status
    ;;
  --restart-only)
    do_restart
    ;;
  --dry-run)
    do_sync --dry-run
    ;;
  --tail)
    do_sync
    do_restart
    do_tail
    ;;
  ""|--deploy)
    do_sync
    do_restart
    ;;
  -h|--help)
    sed -n '2,18p' "$0"
    ;;
  *)
    echo "未知参数：$1（用 -h 看用法）" >&2
    exit 2
    ;;
esac
