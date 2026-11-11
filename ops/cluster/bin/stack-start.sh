#!/bin/bash
# 【集群内执行·manager】从开发机调用：uv run ops/local/remote.py <环境名> stack-start.sh
# 参数来自 .env：STACK_NAME / STACK_COMPOSE / DOCKER_IMAGE
set -e
source "$(dirname "$0")/../../lib.sh"
load_env
require STACK_NAME STACK_COMPOSE DOCKER_IMAGE
# sudo 会清掉环境变量，compose 里的 ${DOCKER_IMAGE} 需要显式传进去
sudo env DOCKER_IMAGE="$DOCKER_IMAGE" docker stack deploy -c "$STACK_COMPOSE" "$STACK_NAME"
sudo docker stack services "$STACK_NAME"
echo "Stack $STACK_NAME started."
