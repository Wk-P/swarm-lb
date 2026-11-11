# swarm-lb 集群内运维脚本（ops/cluster/bin/，在 manager 上运行）的公共部分。
# 开发机上的脚本是 Python（ops/local/*.py），公共部分在 ops/local/common.py。
#
#   source "<仓库>/ops/lib.sh"
#   load_env [环境名]
#
# 加载顺序：
#   1. 指定了环境名（参数或 $SWARM_ENV）  → envs/<环境名>.env       （开发机）
#   2. 否则仓库根目录有 .env             → .env                    （集群节点上，由 deploy_host.py 同步）
# 都没有就报错退出。

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

die() { echo "错误：$*" >&2; exit 1; }

list_envs() {
  local f names=()
  for f in "$REPO_ROOT"/envs/*.env; do
    [[ -e $f && $(basename "$f") != example.env ]] && names+=("$(basename "$f" .env)")
  done
  echo "${names[*]:-（无，先 cp envs/example.env envs/<环境名>.env）}"
}

load_env() {
  local name=${1:-${SWARM_ENV:-}}
  if [[ -n $name ]]; then
    ENV_NAME=$name
    ENV_FILE="$REPO_ROOT/envs/$name.env"
    [[ -f $ENV_FILE ]] || die "找不到 envs/$name.env。可用环境：$(list_envs)"
  elif [[ -f "$REPO_ROOT/.env" ]]; then
    ENV_NAME=deployed
    ENV_FILE="$REPO_ROOT/.env"
  else
    die "未指定环境。用法：<脚本> <环境名> …    可用环境：$(list_envs)"
  fi
  set -a; source "$ENV_FILE"; set +a
}

# require VAR...  缺任一变量就退出
require() {
  local v miss=()
  for v in "$@"; do [[ -n "${!v:-}" ]] || miss+=("$v"); done
  [[ ${#miss[@]} == 0 ]] || die "$ENV_FILE 缺少：${miss[*]}"
}

# render <模板>  把 @变量名@ 替换成环境中的值，输出到 stdout；缺值则退出
render() {
  local out v
  out=$(cat "$1")
  for v in $(grep -oE '@[A-Z0-9_]+@' "$1" | sort -u | tr -d @); do
    require "$v"
    out=${out//@$v@/${!v}}
  done
  printf '%s\n' "$out"
}

all_ssh_hosts() { echo "$MANAGER_SSH" "$WORKER1_SSH" "$WORKER2_SSH" "$WORKER3_SSH"; }
