# swarm-lb

基于树莓派 Docker Swarm 集群（1 manager + 3 worker）的任务负载均衡 / 调度实验项目。

## 运行环境

```
开发机 / 他人机器 ──(ssh、git)──▶  集群（本地 192.x，模拟远程）
  ├ clients/   发请求              ├ manager（宿主机）：host/manager 调度与采集器、ops/cluster/ui 运维界面
  └ ops/local/ 部署、远程运维      ├ 每个节点：ops/cluster/agent 监控 agent
                                   └ worker 容器：image/worker-app（docker 镜像，swarm 调度）
```

| 节点 | IP | label |
|---|---|---|
| manager | `$MANAGER_IP` | — |
| worker-01 | `$WORKER1_IP` | type=low |
| worker-02 | `$WORKER2_IP` | type=middle |
| worker-03 | `$WORKER3_IP` | type=high |

当前线上：stack `collect_data_stack`，3 个服务 `app_w1/w2/w3`，镜像 `$DOCKER_IMAGE`。端口归属见 [inventory.toml](inventory.toml)。

## 目录（按"代码在哪运行"划分）

| 路径 | 在哪运行 | 内容 |
|---|---|---|
| `host/manager/` | 集群宿主机 manager（systemd） | 调度算法 `LB_algorithm/`、8080/8082/8084 采集器、模型 `model/` |
| `image/worker-app/` | docker 镜像（worker 上的容器） | worker `app.py` + `Dockerfile`，独立的 `pyproject.toml` |
| `image/stack/` | swarm stack 定义 | `docker-compose.yaml`、`collect_data_stack.yml` |
| `clients/load-client/` | 集群外（本机或他人机器） | 发请求的客户端 |
| `ops/cluster/ui/` | 集群内运维 · manager :5000 | 运维管理界面 |
| `ops/cluster/agent/` | 集群内运维 · 每个节点 :9100 | 节点监控 agent（只用标准库） |
| `ops/cluster/systemd/` | 集群内运维 | 所有 systemd unit 模板（`@变量名@` 部署时渲染） |
| `ops/cluster/bin/` | 集群内运维 · manager | 重建 swarm、stack 启停、构建镜像、查询队列等 |
| `ops/local/` | 集群外运维 · 开发机 | 部署脚本、远程执行入口；`snapshots/` 是集群状态快照（不进 git） |
| `ops/lib.sh` | 两边共用 | 加载环境参数、模板渲染 |
| `envs/` | 开发机 | 各环境参数，只有 `example.env` 进 git |
| `inventory.toml` | 两边共用 | 节点、服务、端口归属登记 |
| `data/` | | 实验日志（不进 git） |
| `legacy/` | | 旧版本代码与数据，只留本机，不进 git |

**规则：除 `ops/local/`、`clients/`、`envs/` 外，其余目录都会被部署到集群上。**

## 环境参数

IP、ssh 别名、账号密码等只放在本机 `envs/<环境名>.env`，不进 git。

```bash
cp envs/example.env envs/lab.env    # lab = 本地 192.x 集群；以后接入真正的远程集群再加 prod.env
```

所有运维脚本的第一个参数都是环境名，也可以设置 `SWARM_ENV=lab` 后省略。部署时这份文件会同步到 manager 的 `$REMOTE_DIR/.env`（权限 600），集群内的服务和脚本都从那里读取。

## 常用命令（开发机，Git Bash，仓库根目录）

| 做什么 | 命令 |
|---|---|
| 部署宿主机代码、运维界面、unit | `ops/local/deploy-host.sh lab` |
| 同上，并重启正在运行的采集器 | `ops/local/deploy-host.sh lab --restart` |
| 部署所有节点的监控 agent | `ops/local/deploy-agents.sh lab [ssh别名...]` |
| 在 manager 上运行运维脚本 | `ops/local/remote.sh lab <脚本名>`，不带脚本名会列出全部 |
| 例：查询 8084 队列 | `ops/local/remote.sh lab check-queue.sh 8084` |
| 例：重建 swarm | `ops/local/remote.sh lab rebuild-swarm.sh` |
| 运行客户端 | `SWARM_ENV=lab uv run python clients/load-client/client.py` |

只改前端时同样执行 `deploy-host.sh`。每次部署都会重启运维界面，几秒钟就能恢复。

## 多人协作

- **远程代码只能通过 git 修改，再用 `deploy-host.sh` 部署**，不要直接在 manager 上改。
- 部署前会检查：工作区干净、HEAD 已推送到 origin，并且 manager 上已部署的提交是 HEAD 的祖先。最后一条不满足，说明别人部署了你没拉取的代码，要先 `git pull`。确认无误时可以加 `--force` 跳过检查。
- manager 上的部署记录：`.deployed-rev`（提交）、`.deployed-by`（谁、什么时候、从哪台机器）。运维界面页头会显示当前部署的提交，鼠标悬停可以看到部署人。
- 每人一份自己的 `envs/<环境名>.env`。

## 依赖

只有一份 `uv.lock`，按运行位置分组安装：

| 位置 | 命令 |
|---|---|
| manager | `uv sync --no-default-groups --group host --group algo --group ui`（`deploy-host.sh` 自动执行） |
| 客户端机器 | `uv sync --no-default-groups --group client` |
| 开发机 | `uv sync`（host + ui + client），需要算法依赖时用 `uv sync --all-groups` |

docker 镜像有自己的 `image/worker-app/pyproject.toml`。agent 只用标准库，不需要安装依赖。

## 注意

- 8081（他人）和 8083（我们的，待迁入）的代码还在 manager 的 `~/appv8`，使用 `~/apps/manager_server/.venv`，不归本仓库部署管理。
- `ops/cluster/bin/build-image.sh`、`stack-start.sh` 和 `rebuild-swarm.sh` 读取的是 env 里的 `DOCKER_BUILD_DIR`、`STACK_COMPOSE` 和 `REBUILD_COMPOSE`，目前仍指向 `~/appv8` 和 `~/apps` 下的旧文件。要改用本仓库的 `image/`，修改 env 即可。
- worker 节点用 `swarm_ed25519` 无法免密登录，worker 上的文件没有收集。
