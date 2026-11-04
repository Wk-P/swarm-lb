#!/usr/bin/env bash
# 把节点监控 agent 部署到 inventory.toml 里的所有节点（manager + workers）。
# 在 Windows Git Bash 的仓库根目录执行：
#
#   deploy/deploy-agents.sh                    # 全部节点
#   deploy/deploy-agents.sh Worker-Node-1      # 只部署指定 ssh 别名
#
# agent 只用标准库、只读，装到 ~/swarm-lb-agent/，systemd unit swarm-lb-agent.service，端口 9100。
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

if [[ $# -gt 0 ]]; then
  HOSTS=("$@")
else
  mapfile -t HOSTS < <(grep -E '^\s*ssh\s*=' inventory.toml | sed -E 's/.*"(.*)".*/\1/')
fi

for h in "${HOSTS[@]}"; do
  echo ">>> $h"
  ssh "$h" 'mkdir -p ~/swarm-lb-agent'
  scp -q agent/node_agent.py "$h":swarm-lb-agent/node_agent.py
  scp -q deploy/agent/swarm-lb-agent.service "$h":swarm-lb-agent/swarm-lb-agent.service
  ssh "$h" 'set -e
    u=/etc/systemd/system/swarm-lb-agent.service
    cmp -s ~/swarm-lb-agent/swarm-lb-agent.service $u || { sudo -n install -m 644 ~/swarm-lb-agent/swarm-lb-agent.service $u; sudo -n systemctl daemon-reload; }
    sudo -n systemctl enable --quiet swarm-lb-agent
    sudo -n systemctl restart swarm-lb-agent
    for i in $(seq 20); do curl -s --max-time 1 127.0.0.1:9100/healthz >/dev/null && break; sleep 0.5; done
    printf "  %s  " "$(systemctl is-active swarm-lb-agent)"
    curl -s --max-time 2 127.0.0.1:9100/healthz; echo'
done
