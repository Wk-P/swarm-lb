# worker 镜像历史版本

线上 worker 容器的历史版本存档：从各 worker 上残留的镜像里取出的 `/app`，以及 manager 上 `~/apps/docker`、`~/appv8` 里的源码和 stack 文件（2026-11-11 整理）。**只用来查阅和对照实验数据，不参与部署。**

当前线上运行的是 **v8.7**，部署用 [../stack/collect_data_stack.yml](../stack/collect_data_stack.yml)（按 digest 锁定），源码在 [../worker-app/](../worker-app/)（和 v8.7 的 `app.py` 完全相同）。

## 判断容器用的是哪个版本

看**镜像 digest + stack yaml**。同一个 tag 被反复覆盖过（`collect_data_app:v8` 前后构建了 7 次），只看 tag 无法区分版本。

```bash
# 某个 worker 上，正在跑的容器用的是哪个镜像
sudo docker inspect $(sudo docker ps -q --filter name=collect_data_stack | head -1) --format '{{.Image}}'
```

用输出的镜像 ID 对照下表的「镜像 ID」列。

## 版本表

| 版本 | 镜像 | 镜像 ID（本地） | 构建时间（KST） | 接口 | 主要变化 |
| --- | --- | --- | --- | --- | --- |
| v5 | `soar009/node-service-image:v5`<br>digest `17f502de…` | `23f6645c03b4` | 2025-03-28 | `POST /submit_task`，用 Redis 通知 | 早期版本，依赖 manager 上的 Redis |
| v6 | `soar009/node-service-image:v6`（`v66` 同 digest）<br>digest `ca0f002a…` | 本地已无 | 2025-11-11 | `POST /`，**容器端口 8081** | 去掉 Redis，改用 aiohttp |
| v7 | `soar009/collect_data_app:v7`<br>digest `3b17efcc…` | 本地已无 | 2025-11-18 | `POST /` | 镜像改名为 collect_data_app，极简版 |
| v8.1 | `collect_data_app:v8`（已被覆盖） | `18529c0e3bb3` | 2026-01-29 | `POST /task/` | CPU / MEM / HDD 三类任务，返回 `hdd_free_before`；内存按 cgroup 计算 |
| v8.2 | 同上 | `8b3145a65117` | 2026-02-15 23:02 | 同上 | 内存改用 `psutil` |
| v8.3 | 同上 | `8002ff0f40a9` | 2026-02-15 23:07 | 同上 | MEM 任务也改用 `psutil` |
| v8.4 | 同上 | `61c3c16aeae7` | 2026-02-25 18:56 | 同上 | 和 v8.3 代码相同（只有空行和注释不同），重新构建 |
| v8.5 | 同上 | `67ab8b27d99c` | 2026-02-25 19:55 | 同上 | 加 `MAX_CONCURRENT_TASKS = 1`（同一时间只执行 1 个任务）；计时改用 `perf_counter` |
| v8.6 | 同上 | `299bf45f2f81` | 2026-03-11 21:06 | 同上 | HDD 写入改用 `O_DIRECT`（绕过页缓存），块大小 512MB |
| **v8.7** | `collect_data_app:v8`<br>digest `7eca5baf…` | `be79b0596cb2` | 2026-03-11 22:22 | 同上 | `O_DIRECT` 块大小改为 64MB。**线上运行中** |

- v8.1 到 v8.7 的 Dockerfile 都相同（[v8/Dockerfile](v8/Dockerfile)），都是 `pip install aiohttp psutil`，不锁版本。
- v6、v7 的镜像在 worker 上已经删了，这里只有源码；Docker Hub 上的 tag 还在。
- v5 的 `app.py` 取自镜像，和 manager 上的源码 `app-v5.source.py` 不一样（源码是更早或更晚的另一版），两份都留着。Redis 地址已替换为 `<MANAGER_IP>`。

## stack 文件

| 文件 | 来源 | 说明 |
| --- | --- | --- |
| [v5/stack.yml](v5/stack.yml) | `~/apps/docker/configures/docker-compose.yml` | 3 副本，每节点 1 个，不分 label |
| [v6/stack.yml](v6/stack.yml) | `~/apps/docker/configures/v6-config.yaml` | 端口 8081 |
| [v7/stack.yml](v7/stack.yml) | `~/apps/docker/configures/appv7.yaml` | 3 副本，内存上限 512M |
| [v8/stack-tag.yml](v8/stack-tag.yml) | `~/appv8/stack-config/docker-compose.yaml` | 和现在线上的配置相同，但按 tag 引用镜像，tag 被覆盖后会换镜像 |
| [../stack/collect_data_stack.yml](../stack/collect_data_stack.yml) | 本仓库 | **线上配置**，镜像按 digest 锁定在 v8.7 |
