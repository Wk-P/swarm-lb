#!/bin/bash
# 【集群内执行·manager】从开发机调用：uv run ops/local/remote.py <环境名> list-collect-units.sh
sudo systemctl list-units --type=service --all | grep -E "collect*"
