#!/bin/bash
# 【集群内执行·manager】从开发机调用：ops/local/remote.sh <环境名> rebuild-swarm.sh
# 重建 swarm（leave → init → workers 重新 join → 部署 stack）。
# 参数来自 $REMOTE_DIR/.env（deploy-host.sh 同步过去）。
set -e
source "$(dirname "$0")/../../lib.sh"
load_env
require MANAGER_IP WORKER1_IP WORKER2_IP WORKER3_IP REMOTE_USER REBUILD_COMPOSE DOCKER_IMAGE
cd ~/swarm-bak
N=$(($(wc -l < stacks.txt)-1))
[ "$N" -eq 1 ] || { echo "stack 数量是 $N，不是 1，停止："; cat stacks.txt; exit 1; }
STACK=$(awk 'NR==2{print $1}' stacks.txt)
echo ">>> stack 名称: $STACK"
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
cd "$(dirname "$REBUILD_COMPOSE")"
docker stack deploy -c "$(basename "$REBUILD_COMPOSE")" "$STACK"
echo ">>> 等待服务启动..."
sleep 30
docker service ls
