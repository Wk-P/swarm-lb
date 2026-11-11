#!/bin/bash
# 【集群内执行·manager】从开发机调用：uv run ops/local/remote.py <环境名> rebuild-swarm.sh
# 重建 swarm（leave → init → workers 重新 join → 恢复节点 label → 部署 stack）。交互式：要输入各 worker 的密码。
# 参数来自 $REMOTE_DIR/.env（deploy_host.py 同步过去）。
#   stack：STACK_NAME + STACK_COMPOSE（本仓库 image/stack/collect_data_stack.yml，镜像按 digest 锁定）
#   label：按 inventory.toml 的 [[node]] 恢复 type=low/middle/high，否则服务的放置约束无法满足，会一直 pending
set -e
source "$(dirname "$0")/../../lib.sh"
load_env
require MANAGER_IP WORKER1_IP WORKER2_IP WORKER3_IP REMOTE_USER STACK_NAME STACK_COMPOSE DOCKER_IMAGE
[ -f "$STACK_COMPOSE" ] || { echo "找不到 $STACK_COMPOSE，停止"; exit 1; }

# 先读出要恢复的 label（每行 "IP label"），读不到就不往下做
LABELS=$(REPO_ROOT="$REPO_ROOT" "$REPO_ROOT/.venv/bin/python" -c '
import os, tomllib
inv = tomllib.load(open(os.environ["REPO_ROOT"] + "/inventory.toml", "rb"))
for n in inv["node"]:
    if n.get("label"):
        print(os.path.expandvars(n["ip"]), n["label"])
')
[ -n "$LABELS" ] || { echo "inventory.toml 里读不到节点 label，停止"; exit 1; }
echo ">>> stack: $STACK_NAME  compose: $STACK_COMPOSE"
echo "$LABELS" | sed 's/^/>>> 将恢复 label: /'
read -r -p ">>> 重建会中断所有 worker 容器和正在跑的实验。确认继续？输入 yes：" ans
[ "$ans" = yes ] || { echo "已取消"; exit 1; }

sudo docker swarm leave --force
sudo docker swarm init --advertise-addr "$MANAGER_IP"
ADDR=$(docker node inspect self --format '{{.ManagerStatus.Addr}}')
[ "$ADDR" = "$MANAGER_IP:2377" ] || { echo "地址不对: $ADDR，停止"; exit 1; }
TOKEN=$(docker swarm join-token -q worker)
for ip in "$WORKER1_IP" "$WORKER2_IP" "$WORKER3_IP"; do
  echo ">>> $ip  请输入 $REMOTE_USER 的密码"
  ssh -t -o StrictHostKeyChecking=accept-new "$REMOTE_USER@$ip" \
    "sudo docker swarm leave --force; sudo docker swarm join --token $TOKEN $MANAGER_IP:2377"
done
sleep 10
docker node ls

# 恢复 label：按节点地址对应 inventory 里的 IP
for id in $(docker node ls -q); do
  addr=$(docker node inspect "$id" --format '{{.Status.Addr}}')
  label=$(echo "$LABELS" | awk -v a="$addr" '$1==a{print $2}')
  if [ -n "$label" ]; then
    docker node update --label-add "type=$label" "$id" >/dev/null
    echo ">>> $addr type=$label"
  fi
done

docker stack deploy -c "$STACK_COMPOSE" "$STACK_NAME"
echo ">>> 等待服务启动..."
sleep 30
docker service ls
