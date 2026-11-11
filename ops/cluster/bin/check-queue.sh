#!/bin/bash
# 【集群内执行·manager】从开发机调用：uv run ops/local/remote.py <环境名> check-queue.sh [端口，默认 8084]
source "$(dirname "$0")/../../lib.sh"
load_env
require MANAGER_IP
curl -s "http://$MANAGER_IP:${1:-8084}/queue/status"; echo
