#!/bin/bash
# 【集群内执行·manager】从开发机调用：ops/local/remote.sh <环境名> build-image.sh <新tag>
# 构建并推送 worker 镜像。参数来自 .env：DOCKER_BUILD_DIR（本仓库 image/worker-app）/ DOCKER_IMAGE
#
# 必须给一个新 tag（如 v9）。已经存在的 tag 拒绝覆盖：同一个 tag 被覆盖后就分不清容器跑的是哪个版本
# （v8 被覆盖过 7 次，见 image/versions/README.md）。推送后打印 digest，填进 image/stack/collect_data_stack.yml。
set -e
source "$(dirname "$0")/../../lib.sh"
load_env
require DOCKER_BUILD_DIR DOCKER_IMAGE
TAG=${1:-}
[ -n "$TAG" ] || { echo "用法：build-image.sh <新tag>，例如 v9"; exit 1; }
REPO=${DOCKER_IMAGE%:*}
IMG="$REPO:$TAG"
if sudo docker manifest inspect "$IMG" >/dev/null 2>&1; then
  echo "$IMG 在镜像仓库里已经存在，拒绝覆盖。换一个新 tag。"; exit 1
fi

cd "$DOCKER_BUILD_DIR"
sudo docker build -t "$IMG" .
sudo docker push "$IMG"
DIGEST=$(sudo docker image inspect "$IMG" --format '{{range .RepoDigests}}{{println .}}{{end}}' | grep "^$REPO@" | head -1)
echo ">>> 已推送 $IMG"
echo ">>> digest: ${DIGEST#*@}"
echo ">>> 下一步：把 image/stack/collect_data_stack.yml 的镜像改成 \${DOCKER_IMAGE}@${DIGEST#*@}"
echo ">>>         （DOCKER_IMAGE 改成 $IMG），提交、部署，再 stack-start.sh；并在 image/versions/ 补记录"
