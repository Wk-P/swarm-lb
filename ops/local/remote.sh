#!/usr/bin/env bash
# 【开发机执行】在 manager 上运行集群内运维脚本（ops/cluster/bin/，需先 deploy-host.sh 部署过去）。
#
#   ops/local/remote.sh <环境名> <脚本名> [参数...]
#   ops/local/remote.sh <环境名> --sudo <脚本名> [参数...]     # 以 root 运行（如 setup-udisk.sh）
#
# 例：
#   ops/local/remote.sh lab check-queue.sh 8084
#   ops/local/remote.sh lab stack-start.sh
#   ops/local/remote.sh lab rebuild-swarm.sh          # 交互式，会要求输入各 worker 密码
#   ops/local/remote.sh lab --sudo setup-udisk.sh
#   ops/local/remote.sh lab                           # 列出可用脚本
set -euo pipefail
source "$(dirname "$0")/../lib.sh"
load_env "${1:-}"; shift || true
require MANAGER_SSH REMOTE_DIR

SUDO=""
[[ ${1:-} == --sudo ]] && { SUDO="sudo"; shift; }
if [[ $# == 0 ]]; then
  echo "ops/cluster/bin/ 下可用脚本："; ls "$REPO_ROOT/ops/cluster/bin/" | sed 's/^/  /'; exit 0
fi
SCRIPT=$1; shift
[[ -f "$REPO_ROOT/ops/cluster/bin/$SCRIPT" ]] || die "ops/cluster/bin/$SCRIPT 不存在"

ARGS=$(printf ' %q' "$@")
exec ssh -t "$MANAGER_SSH" "$SUDO bash $REMOTE_DIR/ops/cluster/bin/$SCRIPT$ARGS"
