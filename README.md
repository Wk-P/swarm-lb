# swarm-lb

基于树莓派 Docker Swarm 集群（1 manager + 3 worker）的任务负载均衡 / 调度实验项目。
本仓库于 2026-11-04 从各处散落的代码整理而来，**原文件均未删除或修改**。

## 集群

| 节点 | IP | label |
|---|---|---|
| manager | x.x.x.x | — |
| worker-01 | x.x.x.x | type=low |
| worker-02 | x.x.x.x | type=middle |
| worker-03 | x.x.x.x | type=high |

当前线上：stack `collect_data_stack`，3 个服务 `app_w1/w2/w3`，镜像 `soar009/collect_data_app:v8`。

## 目录

| 路径 | 内容 | 来源 |
|---|---|---|
| `manager/` | v8 manager：调度算法 `LB_algorithm/`（RR、min_min、sd_min、algor1/2_set、double_dqn、RL_MOTS、Ablation）、数据采集器、模型 `model/` | Manager-Node `~/appv8` |
| `docker/` | v8 worker 容器 `app.py` + `Dockerfile` | `~/appv8/docker` |
| `client-windows/` | Windows 端发请求的客户端 | `~/appv8/client-windows` |
| `stack-config/` | v8 stack compose | `~/appv8/stack-config` |
| `script/` | v8 构建/启停脚本 | `~/appv8/script` |
| `hdd_config.json` | | `~/appv8` |
| `deploy/` | **当前线上在用**的 `collect_data_stack.yml`、`rebuild-swarm.sh`、`swarm-bak/`（2026-11-04 swarm 状态备份）、services/node-labels 快照 | Manager-Node `~/` 和 `~/apps/docker/configures` |
| `legacy/v5-v7/` | 旧版本 manager_server、v5/v6/v7 app、scripts | Manager-Node `~/apps`（git `zhoyang1106/apps`，含未提交改动，见 `GIT_STATE.txt`） |
| `legacy/cluster_projects/` | v6 app、server_8100、clients；`clients/collect_data/` 是 v8 的早期快照 | `D:\github\cluster_projects`（collect_data 为 git `Wk-P/ExampleCode-trainXGBoost`，只复制了代码，见其 `GIT_STATE.txt`） |
| `legacy/server_codes/` | 最早的 manager_node / worker_node | `D:\github\server_codes` |
| `legacy/daemon-page/` | 日志查看小页面 | Manager-Node `~/daemon-page` |
| `data/` | 实验日志（不进 git） | |

## 注意

- 线上 stack yml 原本放在旧仓库 `~/apps/docker/configures/`，`rebuild-swarm.sh` 也是从那里部署。以本仓库 `deploy/` 为准时，记得同步回节点或修改脚本路径。
- 没有复制的：各处 `.venv`（manager_server 的 1.2G 等）、`__pycache__`、collect_data 的约 5GB 训练数据。
- 相关但独立的仓库，未并入：`D:\github\Routing-Monitor-Docker-Request-`（`Wk-P/...`，早期 demo，已推送）。
- worker 节点用 `swarm_ed25519` 无法免密登录，worker 上的文件未收集。
