# 部署脚本公共部分：加载仓库根目录的 .env（本机私有，不进 git），并提供模板渲染。
# 用法（在脚本里）：  source "$(dirname "$0")/lib.sh"   （script/ 下用 ../deploy/lib.sh）

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ENV_FILE="$REPO_ROOT/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "缺少 $ENV_FILE —— 部署参数只保存在本机。先执行：cp .env.example .env 并填入真实值" >&2
  exit 1
fi
set -a; source "$ENV_FILE"; set +a

# require VAR...  缺任一变量就退出
require() {
  local v miss=0
  for v in "$@"; do [[ -n "${!v:-}" ]] || { echo ".env 缺少 $v" >&2; miss=1; }; done
  [[ $miss == 0 ]] || exit 1
}

# render <模板>  把模板里的 @VAR@ 替换成 .env 中的值，输出到 stdout；有未替换的占位符则报错
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
