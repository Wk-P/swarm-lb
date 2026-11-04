#!/usr/bin/env bash
# 把本仓库部署到 Manager-Node:~/swarm-lb，并安装/更新我们的 systemd unit。
# 在 Windows Git Bash 的仓库根目录执行：
#
#   deploy/deploy-manager.sh             # 同步代码 + uv sync + 安装 unit（不重启运行中的采集器）
#   deploy/deploy-manager.sh --restart   # 另外重启当前 active 的我们的服务
#
# 安全约束：
#   - 只安装 deploy/systemd/ 里、且在 inventory.toml 登记为 owner="ours" + managed=true 的 unit
#   - 被替换的旧 unit 先备份到 ~/swarm-bak/systemd/<时间戳>/
#   - 不碰 ~/appv8、~/apps 及其 venv（8081 / 8083 他人服务在用）
#   - 不会 enable collect_8080（开机不自启，需要时在运维界面手动启动）
set -euo pipefail

HOST=${HOST:-Manager-Node}
DEST=/home/pi/swarm-lb
RESTART=0
[[ "${1:-}" == "--restart" ]] && RESTART=1

cd "$(git rev-parse --show-toplevel)"

echo ">>> [1/4] 同步代码到 $HOST:$DEST  ($(git rev-parse --short HEAD)$(git diff --quiet HEAD -- . || echo '+dirty'))"
git ls-files -co --exclude-standard -z \
  | grep -zvE '^(legacy|data|client-windows)/' \
  | tar --null -T - -cf - \
  | ssh "$HOST" "mkdir -p $DEST && tar -xf - -C $DEST && echo '$(git rev-parse HEAD)' > $DEST/.deployed-rev"

echo ">>> [2/4] uv sync"
ssh "$HOST" "cd $DEST && ~/.local/bin/uv sync --frozen 2>&1 | tail -5"

echo ">>> [3/4] 安装 systemd unit"
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
CHANGED=0
for f in deploy/systemd/*.service; do
  u=\$(basename "\$f")
  case " \$ALLOWED " in *" \$u "*) ;; *) echo "  跳过 \$u（inventory 未登记为 ours+managed）"; continue;; esac
  dst=/etc/systemd/system/\$u
  if [ -f "\$dst" ] && cmp -s "\$f" "\$dst"; then echo "  \$u 未变化"; continue; fi
  if [ -f "\$dst" ]; then mkdir -p ~/swarm-bak/systemd/\$TS; cp "\$dst" ~/swarm-bak/systemd/\$TS/; fi
  sudo -n install -m 644 "\$f" "\$dst"; echo "  已安装 \$u"; CHANGED=1
done
[ \$CHANGED = 1 ] && sudo -n systemctl daemon-reload
sudo -n systemctl enable --quiet swarm-lb-ops.service collect_8082_rr.service collect_8084.service
sudo -n systemctl start swarm-lb-ops.service
EOF

if [[ $RESTART == 1 ]]; then
  echo ">>> [4/4] 重启 active 的我们的服务"
  ssh "$HOST" 'for u in collect_8080.service collect_8082_rr.service collect_8084.service swarm-lb-ops.service; do
    if systemctl is-active --quiet $u; then sudo -n systemctl restart $u && echo "  重启 $u"; fi; done'
else
  echo ">>> [4/4] 未重启运行中的服务（加 --restart 生效新代码）"
fi

ssh "$HOST" 'systemctl --no-pager --plain list-units "collect_*" swarm-lb-ops.service log-viewer.service | head -n -5'
echo ">>> 运维界面: http://x.x.x.x:5000/"
