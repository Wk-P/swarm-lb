#!/bin/bash
# 【集群内执行·manager】从开发机调用：ops/local/remote.sh <环境名> list-collect-units.sh
sudo systemctl list-units --type=service --all | grep -E "collect*"
