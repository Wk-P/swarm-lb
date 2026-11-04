#!/bin/bash
set -e
cd ~/swarm-bak
N=$(($(wc -l < stacks.txt)-1))
[ "$N" -eq 1 ] || { echo "stack 数量是 $N，不是 1，停止："; cat stacks.txt; exit 1; }
STACK=$(awk 'NR==2{print $1}' stacks.txt)
echo ">>> stack 名称: $STACK"
sudo docker swarm leave --force
sudo docker swarm init --advertise-addr x.x.x.x
ADDR=$(docker node inspect self --format '{{.ManagerStatus.Addr}}')
[ "$ADDR" = "x.x.x.x:2377" ] || { echo "地址不对: $ADDR，停止"; exit 1; }
TOKEN=$(docker swarm join-token -q worker)
for ip in 150 151 152; do
  echo ">>> x.x.x.$ip  请输入 pi 的密码"
  ssh -t -o StrictHostKeyChecking=accept-new pi@x.x.x.$ip \
    "sudo docker swarm leave --force; sudo docker swarm join --token $TOKEN x.x.x.x:2377"
done
sleep 10
docker node ls
cd /home/pi/apps/docker/configures
docker stack deploy -c docker-compose.yml "$STACK"
echo ">>> 等待服务启动..."
sleep 30
docker service ls
