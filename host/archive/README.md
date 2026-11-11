# 已停用的代码（存档）

2026-11-11 从 `host/manager/` 和仓库根目录移过来。这些文件**不再部署**（`deploy-host.sh` 跳过 `host/archive/`），只留着查阅。
它们原来 import 的 `manager_objects` 已改名为 `host/manager/objects.py`，所以不能直接运行；要恢复使用，移回 `host/manager/` 并改 import。

| 文件 | 原位置 | 原用途 | 停用原因 |
| --- | --- | --- | --- |
| `manager/manager_hmsw8d.py` | `host/manager/` | 8080 调度器的 8 维特征版本（模型 `xgb8d.joblib`） | 线上用 `p8080_sched.py`；端口同为 8080，不能同时运行 |
| `manager/manager_hmsw_collect.py` | `host/manager/` | 8080 采集版，使用 `tasks/` | 同上 |
| `manager/tasks/` | `host/manager/tasks/` | 只被 `manager_hmsw_collect.py` 使用；`tasks/tasks/` 是它的重复副本 | 随上一项停用 |
| `manager/manager_xgbdata_collecter.py` | `host/manager/` | 8082 XGB 数据采集（非 RR 版本） | 线上 8082 用 `p8082_collect_rr.py` |
| `manager/manager_xgbdata_collector_handle_hdd.py` | `host/manager/` | 8084 handle_hdd 第 1 版 | 线上 8084 用 `p8084_collect_hdd_rr.py`（v2） |
| `manager/manager_objects_handle_hdd.py` | `host/manager/` | 上一项用的 ServerManager | 线上用 `objects_hdd.py`（v2） |
| `hdd_config.json` | 仓库根目录 | — | 没有代码读取它；实际生效的是 `host/manager/docker_utils/hdd_config.json` |
