# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
# ↑ 只用标准库：uv run 时不安装项目依赖，也不要求 Python 3.11；没有 uv 时直接 python 运行也可以
"""【开发机执行】把节点监控 agent（ops/cluster/agent/）部署到集群所有节点。

    uv run ops/local/deploy_agents.py <环境名>                  # 全部节点（manager + workers）
    uv run ops/local/deploy_agents.py <环境名> <ssh别名>...      # 只部署指定节点

agent 只用标准库、只读，装到 $AGENT_DIR，systemd unit swarm-lb-agent.service，端口 $AGENT_PORT。
"""
from __future__ import annotations

import sys

from common import REPO_ROOT, load_env, q, ssh, ssh_bash, tar_bytes


def main() -> None:
    args = sys.argv[1:]
    env = load_env(args.pop(0) if args else None)
    env.require("MANAGER_SSH", "WORKER1_SSH", "WORKER2_SSH", "WORKER3_SSH", "AGENT_DIR", "AGENT_PORT", "REMOTE_USER")
    hosts = args or env.ssh_hosts()
    agent_dir, port = env["AGENT_DIR"], env["AGENT_PORT"]

    payload = tar_bytes({
        "node_agent.py": (REPO_ROOT / "ops/cluster/agent/node_agent.py").read_bytes(),
        "swarm-lb-agent.service": env.render(REPO_ROOT / "ops/cluster/systemd/swarm-lb-agent.service").encode("utf-8"),
    })
    for h in hosts:
        print(f">>> {h}")
        ssh(h, f"mkdir -p {q(agent_dir)} && tar -xf - -C {q(agent_dir)}", input=payload)
        ssh_bash(h, INSTALL.replace("@AGENT_DIR@", agent_dir).replace("@AGENT_PORT@", port))


INSTALL = r"""
set -e
u=/etc/systemd/system/swarm-lb-agent.service
cmp -s @AGENT_DIR@/swarm-lb-agent.service $u || { sudo -n install -m 644 @AGENT_DIR@/swarm-lb-agent.service $u; sudo -n systemctl daemon-reload; }
sudo -n systemctl enable --quiet swarm-lb-agent
sudo -n systemctl restart swarm-lb-agent
for i in $(seq 20); do curl -s --max-time 1 127.0.0.1:@AGENT_PORT@/healthz >/dev/null && break; sleep 0.5; done
printf '  %s  ' "$(systemctl is-active swarm-lb-agent)"
curl -s --max-time 2 127.0.0.1:@AGENT_PORT@/healthz; echo
"""

if __name__ == "__main__":
    main()
