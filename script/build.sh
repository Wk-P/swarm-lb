#!/bin/bash
# 构建并推送 worker 镜像。参数来自 .env：DOCKER_BUILD_DIR / DOCKER_IMAGE
set -e
source "$(dirname "$0")/../deploy/lib.sh"
require DOCKER_BUILD_DIR DOCKER_IMAGE
cd "$DOCKER_BUILD_DIR"

# clear previous images
sudo docker rmi "$DOCKER_IMAGE" -f
sudo docker build -t "$DOCKER_IMAGE" .
sudo docker push "$DOCKER_IMAGE"
