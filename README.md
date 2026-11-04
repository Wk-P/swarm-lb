# swarm-lb

基于树莓派 Docker Swarm 集群（1 manager + 3 worker）的任务负载均衡 / 调度实验项目。
本仓库于 2026-11-04 从各处散落的代码整理而来，**原文件均未删除或修改**。

## 集群

| 节点 | IP | label |
|---|---|---|
| manager | `$MANAGER_IP` | — |
| worker-01 | `$WORKER1_IP` | type=low |
| worker-02 | `$WORKER2_IP` | type=middle |
| worker-03 | `$WORKER3_IP` | type=high |

IP、ssh 别名、账号密码等只保存在本机 `.env`（不进 git）。首次使用：`cp .env.example .env` 后填入真实值，所有部署脚本都从它读取，缺失时直接报错退出。

当前线上：stack `collect_data_stack`，3 个服务 `app_w1/w2/w3`，镜像 `$DOCKER_IMAGE`。

## 目录

| 路径 | 内容 | 来源 |
|---|---|---|
| `manager/` | v8 manager：调度算法 `LB_algorithm/`（RR、min_min、sd_min、algor1/2_set、double_dqn、RL_MOTS、Ablation）、数据采集器、模型 `model/` | Manager-Node `~/appv8` |
| `docker/` | v8 worker 容器 `app.py` + `Dockerfile` | `~/appv8/docker` |
| `client-windows/` | Windows 端发请求的客户端 | `~/appv8/client-windows` |
| `stack-config/` | v8 stack compose | `~/appv8/stack-config` |
| `script/` | v8 构建/启停脚本 | `~/appv8/script` |
| `hdd_config.json` | | `~/appv8` |
| `deploy/` | 部署脚本（读本机 `.env`）、systemd unit 模板、`collect_data_stack.yml`、`rebuild-swarm.sh` | manager `~/` 和 `~/apps/docker/configures` |
| `legacy/`（仅本机，不进 git） | 旧版本代码与数据（v5-v7、cluster_projects、server_codes、daemon-page），含旧明文配置 | 见本机 `legacy/` 内各 `GIT_STATE.txt` |
| `data/` | 实验日志（不进 git） | |

## 注意

- **2026-11-04 网段变更**，主线代码已不再写死 IP（统一读 `.env`）；`legacy/` 只留在本机，不进 git。
- 集群状态快照（`deploy/swarm-bak/`、`services.txt`、`node-labels.txt`）只留在本机，不进 git；manager 上的备份在 `~/swarm-bak`。

- 线上 stack yml 原本放在旧仓库 `~/apps/docker/configures/`，`rebuild-swarm.sh` 也是从那里部署。以本仓库 `deploy/` 为准时，记得同步回节点或修改脚本路径。
- 没有复制的：各处 `.venv`（manager_server 的 1.2G 等）、`__pycache__`、collect_data 的约 5GB 训练数据。
- 相关但独立的仓库，未并入：`D:\github\Routing-Monitor-Docker-Request-`（`Wk-P/...`，早期 demo，已推送）。
- worker 节点用 `swarm_ed25519` 无法免密登录，worker 上的文件未收集。
