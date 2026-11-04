#!/usr/bin/env bash
# 【开发机执行】把仓库部署到集群宿主机（manager）的 $REMOTE_DIR，并安装/更新我们的 systemd unit。
#
#   ops/local/deploy-host.sh <环境名>              # 同步代码 + .env + uv sync + 安装 unit，重启运维界面
#   ops/local/deploy-host.sh <环境名> --restart    # 另外重启正在运行的我们的采集器
#   ops/local/deploy-host.sh <环境名> --force      # 跳过下面的部署前检查（会提示）
#
# 部署前检查（多人协作，防止互相覆盖）：
#   - 工作区必须干净，HEAD 必须已推送到 origin    —— 远程跑的代码一定能在 git 里找到
#   - manager 上已部署的提交必须是本地 HEAD 的祖先 —— 否则说明别人部署了你没拉取的代码
# 部署记录：$REMOTE_DIR/.deployed-rev（提交）、.deployed-by（谁、何时、从哪台机器）、.deployed-files（文件清单）
#
# 安全约束：
#   - 只安装 ops/cluster/systemd/ 里、且在 inventory.toml 登记为 owner="ours" + managed=true 的 unit
#   - 被替换的旧 unit 先备份到 ~/swarm-bak/systemd/<时间戳>/
#   - 只删除上次由本脚本部署、这次已不存在的文件（按 .deployed-files 清单）；.venv/.env 等不动
#   - 不碰 ~/appv8、~/apps 及其 venv（8081 他人服务、8083 仍在用）
#   - 不改变采集器的开机自启状态（需要时在 manager 上 systemctl enable/disable）
set -euo pipefail
source "$(dirname "$0")/../lib.sh"

ENV_ARG=""; RESTART=0; FORCE=0
for a in "$@"; do
  case $a in
    --restart) RESTART=1 ;;
    --force)   FORCE=1 ;;
    -*)        die "未知参数 $a" ;;
    *)         ENV_ARG=$a ;;
  esac
done
load_env "$ENV_ARG"
require MANAGER_SSH MANAGER_IP REMOTE_DIR OPS_PORT
HOST=$MANAGER_SSH
DEST=$REMOTE_DIR
cd "$REPO_ROOT"

# 部署到集群上的路径（其余：clients/ 在集群外运行，ops/local/ 只在开发机用）
SYNC_EXCLUDE='^(clients|ops/local|envs|data|legacy)/'
UV_GROUPS="--no-default-groups --group host --group algo --group ui"

fail_or_force() { if [[ $FORCE == 1 ]]; then echo "  ⚠️  $*（--force，继续）"; else die "$*   （确认无误可加 --force）"; fi; }

echo ">>> [0/5] 部署前检查  环境=$ENV_NAME  目标=$HOST:$DEST"
REV=$(git rev-parse HEAD)
[[ -z $(git status --porcelain) ]] || fail_or_force "工作区有未提交的改动"
git fetch -q origin || echo "  ⚠️  git fetch 失败，按本地已知的 origin 判断"
[[ -n $(git branch -r --contains "$REV" 2>/dev/null) ]] || fail_or_force "HEAD ${REV:0:7} 还没推送到 origin"
OLD_REV=$(ssh -n "$HOST" "cat $DEST/.deployed-rev 2>/dev/null" || true)
if [[ -n $OLD_REV ]]; then
  OLD_BY=$(ssh -n "$HOST" "cat $DEST/.deployed-by 2>/dev/null" || true)
  echo "  manager 上当前：${OLD_REV:0:7}  ${OLD_BY}"
  if ! git cat-file -e "$OLD_REV^{commit}" 2>/dev/null; then
    fail_or_force "manager 上部署的 ${OLD_REV:0:7} 在本地找不到（别人部署了未推送的代码，或 git 历史被重写过）"
  elif ! git merge-base --is-ancestor "$OLD_REV" "$REV"; then
    fail_or_force "manager 上的 ${OLD_REV:0:7} 不是 HEAD 的祖先（别人部署了你没合并的提交，先 git pull）"
  fi
fi

echo ">>> [1/5] 同步代码 + .env  (${REV:0:7}$([[ -z $(git status --porcelain) ]] || echo '+dirty'))"
FILES=$(git ls-files -co --exclude-standard | grep -vE "$SYNC_EXCLUDE")
printf '%s\n' "$FILES" | tr '\n' '\0' | tar --null -T - -cf - \
  | ssh "$HOST" "mkdir -p $DEST && tar -xf - -C $DEST"
ssh "$HOST" "umask 077 && cat > $DEST/.env" < "$ENV_FILE"

echo ">>> [2/5] 清理上次部署、这次已不存在的文件"
printf '%s\n' "$FILES" | ssh "$HOST" "cat > $DEST/.deployed-files.new"
ssh "$HOST" "bash -s" <<EOF
set -euo pipefail
cd $DEST
if [ -f .deployed-files ]; then
  n=0
  while IFS= read -r f; do
    case "\$f" in ""|/*|*..*) continue;; esac
    [ -f "\$f" ] && { rm -f -- "\$f"; n=\$((n+1)); }
  done < <(comm -23 <(sort .deployed-files) <(sort .deployed-files.new))
  echo "  删除 \$n 个旧文件"
  find . -path ./.venv -prune -o -type d -empty -print -delete | sed 's/^/  删除空目录 /'
else
  echo "  首次记录文件清单，不删除任何文件"
fi
mv .deployed-files.new .deployed-files
EOF

# 本机渲染 unit 模板（agent 的 unit 由 deploy-agents.sh 负责）
RENDER_DIR=$(mktemp -d); trap 'rm -rf "$RENDER_DIR"' EXIT
for f in ops/cluster/systemd/*.service; do
  [[ $(basename "$f") == swarm-lb-agent.service ]] && continue
  render "$f" > "$RENDER_DIR/$(basename "$f")"
done
tar -C "$RENDER_DIR" -cf - . | ssh "$HOST" "rm -rf $DEST/.rendered/systemd && mkdir -p $DEST/.rendered/systemd && tar -xf - -C $DEST/.rendered/systemd"

echo ">>> [3/5] uv sync ($UV_GROUPS)"
ssh -n "$HOST" "cd $DEST && ~/.local/bin/uv sync --frozen $UV_GROUPS 2>&1 | tail -3"

echo ">>> [4/5] 安装 systemd unit"
ssh "$HOST" "bash -s" <<EOF
set -euo pipefail
cd $DEST
ALLOWED=\$(.venv/bin/python - <<'PY'
import tomllib
inv = tomllib.load(open("inventory.toml", "rb"))
print(" ".join(s["unit"] for s in inv["service"] if s.get("owner") == "ours" and s.get("managed")))
PY
)
TS=\$(date +%Y%m%d-%H%M%S)
CHANGED=""
for f in .rendered/systemd/*.service; do
  u=\$(basename "\$f")
  case " \$ALLOWED " in *" \$u "*) ;; *) echo "  跳过 \$u（inventory 未登记为 ours+managed）"; continue;; esac
  dst=/etc/systemd/system/\$u
  if [ -f "\$dst" ] && cmp -s "\$f" "\$dst"; then echo "  \$u 未变化"; continue; fi
  if [ -f "\$dst" ]; then mkdir -p ~/swarm-bak/systemd/\$TS; cp "\$dst" ~/swarm-bak/systemd/\$TS/; fi
  sudo -n install -m 644 "\$f" "\$dst"; echo "  已安装 \$u"; CHANGED="\$CHANGED \$u"
done
[ -n "\$CHANGED" ] && sudo -n systemctl daemon-reload
for u in \$CHANGED; do
  [ "\$u" != swarm-lb-ops.service ] && systemctl is-active --quiet \$u && [ $RESTART = 0 ] \
    && echo "  ⚠️  \$u 正在运行且 unit 已变化，加 --restart 才会生效"
done
sudo -n systemctl enable --quiet swarm-lb-ops.service
sudo -n systemctl restart swarm-lb-ops.service && echo "  重启 swarm-lb-ops.service"
if [ $RESTART = 1 ]; then
  for u in \$ALLOWED; do
    [ "\$u" = swarm-lb-ops.service ] && continue
    if systemctl is-active --quiet \$u; then sudo -n systemctl restart \$u && echo "  重启 \$u"; fi
  done
fi
EOF

echo ">>> [5/5] 记录部署"
BY="$(git config user.name || echo unknown) $(date '+%F %T') from $(hostname)"
ssh -n "$HOST" "echo '$REV' > $DEST/.deployed-rev && echo '$BY' > $DEST/.deployed-by"
echo "  ${REV:0:7}  $BY"

ssh -n "$HOST" 'systemctl --no-pager --plain list-units "collect_*" swarm-lb-ops.service | head -n -5'
echo ">>> 运维界面: http://$MANAGER_IP:$OPS_PORT/"
