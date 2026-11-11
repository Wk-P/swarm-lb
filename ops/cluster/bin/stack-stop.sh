#!/bin/bash
# 【集群内执行·manager】从开发机调用：uv run ops/local/remote.py <环境名> stack-stop.sh
set -e
source "$(dirname "$0")/../../lib.sh"
load_env
require STACK_NAME
sudo docker stack rm "$STACK_NAME"
echo "Stack $STACK_NAME stopped."
