#!/bin/bash
# 在 manager 上执行。参数来自 .env：STACK_NAME / STACK_COMPOSE / DOCKER_IMAGE
set -e
source "$(dirname "$0")/../deploy/lib.sh"
require STACK_NAME STACK_COMPOSE DOCKER_IMAGE
# sudo 会清掉环境变量，compose 里的 ${DOCKER_IMAGE} 需要显式传进去
sudo env DOCKER_IMAGE="$DOCKER_IMAGE" docker stack deploy -c "$STACK_COMPOSE" "$STACK_NAME"
sudo docker stack services "$STACK_NAME"
echo "Stack $STACK_NAME started."
