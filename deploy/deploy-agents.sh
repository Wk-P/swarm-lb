#!/usr/bin/env bash
# 把节点监控 agent 部署到所有节点（manager + workers，ssh 别名来自 .env）。
# 在 Windows Git Bash 的仓库根目录执行：
#
#   deploy/deploy-agents.sh                    # 全部节点
#   deploy/deploy-agents.sh <ssh别名>...        # 只部署指定节点
#
# agent 只用标准库、只读，装到 $AGENT_DIR，systemd unit swarm-lb-agent.service，端口 $AGENT_PORT。
set -euo pipefail
source "$(dirname "$0")/lib.sh"
require MANAGER_SSH WORKER1_SSH WORKER2_SSH WORKER3_SSH AGENT_DIR AGENT_PORT
cd "$REPO_ROOT"

if [[ $# -gt 0 ]]; then
  HOSTS=("$@")
else
  read -ra HOSTS <<< "$(all_ssh_hosts)"
fi

UNIT=$(mktemp); trap 'rm -f "$UNIT"' EXIT
render deploy/agent/swarm-lb-agent.service > "$UNIT"

for h in "${HOSTS[@]}"; do
  echo ">>> $h"
  ssh "$h" "mkdir -p $AGENT_DIR"
  scp -q agent/node_agent.py "$h":"$AGENT_DIR/node_agent.py"
  scp -q "$UNIT" "$h":"$AGENT_DIR/swarm-lb-agent.service"
  ssh "$h" "set -e
    u=/etc/systemd/system/swarm-lb-agent.service
    cmp -s $AGENT_DIR/swarm-lb-agent.service \$u || { sudo -n install -m 644 $AGENT_DIR/swarm-lb-agent.service \$u; sudo -n systemctl daemon-reload; }
    sudo -n systemctl enable --quiet swarm-lb-agent
    sudo -n systemctl restart swarm-lb-agent
    for i in \$(seq 20); do curl -s --max-time 1 127.0.0.1:$AGENT_PORT/healthz >/dev/null && break; sleep 0.5; done
    printf '  %s  ' \"\$(systemctl is-active swarm-lb-agent)\"
    curl -s --max-time 2 127.0.0.1:$AGENT_PORT/healthz; echo"
done
