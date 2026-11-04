# import random
# import time
# from datetime import datetime
# import numpy as np
# import torch
# import torch.nn as nn
# import torch.optim as optim
# from collections import deque
# import matplotlib.pyplot as plt
# import gurobipy as gp
# from gurobipy import GRB
#
# '''
# Model
# '''
#
#
# def Optimization_Model1(response_time, WORKERS):
#     num_tasks = len(WORKERS)
#     num_workers = 3
#
#     # weight
#     alpha = 1 / 3
#     beta = 1 / 3
#     gamma = 1 / 3
#
#     # maximum value
#     hdd_max = max(worker.max_hdd_usage for worker in WORKERS)
#     mem_max = max(worker.max_mem_usage for worker in WORKERS)
#     max_response_times = [max(response_time[i]) for i in range(num_tasks)]
#
#     # 创建模型
#     start_modeling = time.perf_counter_ns()
#     model = gp.Model("batch_optimization")
#     # 设置日志输出文件
#     # model.setParam("LogFile", "task_set_gurobi_solver.log")
#
#     # 创建二进制变量 x[i, j]，表示任务 i 是否分配给 worker j
#     x = model.addVars(num_tasks, num_workers, vtype=GRB.BINARY, name="x")
#     b_hdd = model.addVars(1, vtype=GRB.CONTINUOUS, name="b_hdd")
#     b_mem = model.addVars(1, vtype=GRB.CONTINUOUS, name="b_mem")
#
#     # Set objective function：
#     for i in range(num_tasks):
#         model.setObjective(alpha * (b_hdd[0] / hdd_max) + beta * (b_mem[0] / mem_max) + gamma * (
#                     gp.quicksum(response_time[i][j] * x[i, j] for j in range(num_workers)) / max_response_times[i]),
#                            GRB.MINIMIZE)
#
#         # 添加约束
#     for i in range(num_tasks):
#         # 添加约束：每个任务只能分配给一个 worker
#         model.addConstr(gp.quicksum(x[i, j] for j in range(num_workers)) == 1)
#         for j in range(num_workers):
#             # 添加约束：每个 worker 的资源不能超过限制
#             model.addConstr(WORKERS[j].hdd_usage + Tasks[i].hdd_usage * x[i, j] <= WORKERS[j].max_hdd_usage)
#             model.addConstr(WORKERS[j].mem_usage + Tasks[i].mem_usage * x[i, j] <= WORKERS[j].max_mem_usage)
#             # b_hdd
#             model.addConstr(WORKERS[j].hdd_usage + Tasks[i].hdd_usage * x[i, j] <= b_hdd[0])
#             # b_mem
#             model.addConstr(WORKERS[j].mem_usage + Tasks[i].mem_usage * x[i, j] <= b_mem[0])
#     end_modeling = time.perf_counter_ns()
#     modeling_time = end_modeling - start_modeling
#
#     model.setParam(GRB.Param.PoolSearchMode, 2)  # 深度搜索模式，尽量生成多种解
#     model.setParam(GRB.Param.PoolSolutions, 10)  # 设置最多保存 10 个不同的解
#     model.setParam(GRB.Param.PoolGap, 0.2)  # 强制每个解之间有至少 20% 的差异
#
#     # 求解模型
#     start_solving = time.perf_counter_ns()
#     model.optimize()
#     end_solving = time.perf_counter_ns()
#     solver_time = end_solving - start_solving
#
#     # 检查解的数量
#     solution_count = model.SolCount
#     # 初始化变量，用于存储最均匀分布的解
#     best_solution = None
#     best_distribution_diff = float('inf')  # 用于记录最小的任务分布差异
#     best_task_distribution = None
#     for k in range(solution_count):
#         model.setParam(GRB.Param.SolutionNumber, k)
#         result_matrix = np.zeros((num_tasks, num_workers), dtype=int)
#
#         for i in range(num_tasks):
#             for j in range(num_workers):
#                 result_matrix[i, j] = int(x[i, j].Xn)
#
#         # 计算每个 worker 的任务数量
#         task_distribution = result_matrix.sum(axis=0)
#         distribution_diff = max(task_distribution) - min(task_distribution)  # 计算最大最小任务数量的差异
#
#         # 更新最均匀分布的解
#         if distribution_diff < best_distribution_diff:
#             best_distribution_diff = distribution_diff
#             best_solution = result_matrix
#             best_task_distribution = task_distribution
#
#     # 输出最均匀分布的解
#     if best_solution is not None:
#         chosen_workers = []
#         # print(f"Best Balanced Solution (Task Distribution: {best_task_distribution}):")
#         chosen_workers_index = np.argmax(best_solution, axis=1)
#         for i in range(len(chosen_workers_index)):
#             chosen_workers.append(WORKERS[chosen_workers_index[i]])
#     return chosen_workers, (solver_time, modeling_time)
#



# algor1_set.py
# batch scheduling optimization model for Manager /batch

import time
import numpy as np
import gurobipy as gp
from gurobipy import GRB


def Optimization_Model1(prediction_matrix, batch_running_workers):
    """
    Batch scheduling optimization model.

    Parameters
    ----------
    prediction_matrix : list[list[float]]
        shape = [num_tasks][num_workers]
        prediction_matrix[i][j] means:
        predicted execution / response time of task i on worker j

    batch_running_workers : list[list[dict]]
        shape = [num_tasks][num_workers]
        batch_running_workers[i][j] contains resource / state info of
        assigning task i to worker j

        expected dict keys:
            - number
            - cpus_limit_cores
            - hdd_limit_mb
            - memory_limit_mb
            - task_hdd_size_mb
            - task_mem_size_mb
            - worker_hdd_usage
            - worker_mem_usage

    Returns
    -------
    chosen_workers_index : list[int]
        chosen_workers_index[i] = assigned worker index for task i

    timing_info : tuple[int, int]
        (solver_time_ns, modeling_time_ns)
    """

    # -------------------------------------------------
    # basic checks
    # -------------------------------------------------
    if not prediction_matrix:
        return [], (0, 0)

    if not batch_running_workers:
        return [], (0, 0)

    num_tasks = len(prediction_matrix)
    num_workers = len(prediction_matrix[0])

    if num_tasks == 0 or num_workers == 0:
        return [], (0, 0)

    if len(batch_running_workers) != num_tasks:
        raise ValueError(
            f"batch_running_workers length mismatch: "
            f"{len(batch_running_workers)} != {num_tasks}"
        )

    for i in range(num_tasks):
        if len(prediction_matrix[i]) != num_workers:
            raise ValueError(
                f"prediction_matrix row {i} length mismatch: "
                f"{len(prediction_matrix[i])} != {num_workers}"
            )
        if len(batch_running_workers[i]) != num_workers:
            raise ValueError(
                f"batch_running_workers row {i} length mismatch: "
                f"{len(batch_running_workers[i])} != {num_workers}"
            )

    # -------------------------------------------------
    # weights
    # -------------------------------------------------
    alpha = 1.0 / 3.0   # HDD peak usage
    beta = 1.0 / 3.0    # MEM peak usage
    gamma = 1.0 / 3.0   # response time

    # -------------------------------------------------
    # extract worker static/current info
    # capacities are same across tasks for same worker,
    # so use the first task row
    # -------------------------------------------------
    first_row = batch_running_workers[0]

    worker_hdd_limits = []
    worker_mem_limits = []
    worker_hdd_usages = []
    worker_mem_usages = []

    for j in range(num_workers):
        info = first_row[j]

        hdd_limit = float(info.get("hdd_limit_mb", 0.0) or 0.0)
        mem_limit = float(info.get("memory_limit_mb", 0.0) or 0.0)
        hdd_usage = float(info.get("worker_hdd_usage", 0.0) or 0.0)
        mem_usage = float(info.get("worker_mem_usage", 0.0) or 0.0)

        worker_hdd_limits.append(hdd_limit)
        worker_mem_limits.append(mem_limit)
        worker_hdd_usages.append(hdd_usage)
        worker_mem_usages.append(mem_usage)

    hdd_max = max(worker_hdd_limits) if worker_hdd_limits else 1.0
    mem_max = max(worker_mem_limits) if worker_mem_limits else 1.0

    if hdd_max <= 0:
        hdd_max = 1.0
    if mem_max <= 0:
        mem_max = 1.0

    # -------------------------------------------------
    # extract task demand info
    # each task has same task demand across workers,
    # so use [i][0]
    # -------------------------------------------------
    task_hdd_demands = []
    task_mem_demands = []

    for i in range(num_tasks):
        info = batch_running_workers[i][0]

        task_hdd = float(info.get("task_hdd_size_mb", 0.0) or 0.0)
        task_mem = float(info.get("task_mem_size_mb", 0.0) or 0.0)

        task_hdd_demands.append(task_hdd)
        task_mem_demands.append(task_mem)

    # -------------------------------------------------
    # normalize prediction scale row by row
    # -------------------------------------------------
    max_response_times = []
    for i in range(num_tasks):
        row_max = max(float(x) for x in prediction_matrix[i]) if prediction_matrix[i] else 1.0
        if row_max <= 0:
            row_max = 1.0
        max_response_times.append(row_max)

    # -------------------------------------------------
    # build model
    # -------------------------------------------------
    start_modeling = time.perf_counter_ns()

    model = gp.Model("batch_optimization")
    model.setParam("OutputFlag", 0)

    # x[i,j] = 1 if task i assigned to worker j
    x = model.addVars(num_tasks, num_workers, vtype=GRB.BINARY, name="x")

    # peak usage among workers after assignment
    b_hdd = model.addVar(1, vtype=GRB.CONTINUOUS, name="b_hdd")
    b_mem = model.addVar(1, vtype=GRB.CONTINUOUS, name="b_mem")

    # -------------------------------------------------
    # objective
    # IMPORTANT: set objective only once
    # -------------------------------------------------
    for i in range(num_tasks):
        model.setObjective(alpha * (b_hdd / hdd_max) + beta * (b_mem / mem_max) + gamma * (gp.quicksum(prediction_matrix[i][j] * x[i, j] for j in range(num_workers)) / max_response_times[i]), GRB.MINIMIZE)

    # -------------------------------------------------
    # each task must be assigned to exactly one worker
    # -------------------------------------------------
    for i in range(num_tasks):
        model.addConstr(gp.quicksum(x[i, j] for j in range(num_workers)) == 1, name=f"assign_task_{i}")

    # -------------------------------------------------
    # capacity constraints + peak constraints
    # -------------------------------------------------
        for j in range(num_workers):
            # real capacity constraints
            model.addConstr(worker_hdd_usages[j] + task_hdd_demands[i] * x[i, j] <= worker_hdd_limits[j], name=f"hdd_capacity_worker_{j}")
            model.addConstr(worker_mem_usages[j] + task_mem_demands[i] * x[i, j] <= worker_mem_limits[j], name=f"mem_capacity_worker_{j}")

            # peak usage helper constraints
            model.addConstr(worker_hdd_usages[j] + task_hdd_demands[i] * x[i, j] <= b_hdd, name=f"peak_hdd_worker_{j}")
            model.addConstr(worker_mem_usages[j] + task_mem_demands[i] * x[i, j] <= b_mem, name=f"peak_mem_worker_{j}")

    end_modeling = time.perf_counter_ns()
    modeling_time = end_modeling - start_modeling

    # -------------------------------------------------
    # optional solution pool settings
    # -------------------------------------------------
    model.setParam(GRB.Param.PoolSearchMode, 2)
    model.setParam(GRB.Param.PoolSolutions, 10)
    model.setParam(GRB.Param.PoolGap, 0.2)

    # -------------------------------------------------
    # solve
    # -------------------------------------------------
    start_solving = time.perf_counter_ns()
    model.optimize()
    end_solving = time.perf_counter_ns()
    solver_time = end_solving - start_solving

    # -------------------------------------------------
    # check status
    # -------------------------------------------------
    if model.Status not in [GRB.OPTIMAL, GRB.SUBOPTIMAL]:
        raise RuntimeError(
            f"Gurobi failed to find feasible solution. Status={model.Status}"
        )

    # -------------------------------------------------
    # choose the most balanced solution in solution pool
    # -------------------------------------------------
    solution_count = model.SolCount
    best_solution = None
    best_distribution_diff = float("inf")

    for k in range(solution_count):
        model.setParam(GRB.Param.SolutionNumber, k)

        result_matrix = np.zeros((num_tasks, num_workers), dtype=int)

        for i in range(num_tasks):
            for j in range(num_workers):
                result_matrix[i, j] = int(round(x[i, j].Xn))

        task_distribution = result_matrix.sum(axis=0)
        distribution_diff = int(task_distribution.max() - task_distribution.min())

        if distribution_diff < best_distribution_diff:
            best_distribution_diff = distribution_diff
            best_solution = result_matrix.copy()

    if best_solution is None:
        raise RuntimeError("No valid solution found in solution pool.")

    chosen_workers_index = np.argmax(best_solution, axis=1).tolist()

    return chosen_workers_index, (solver_time, modeling_time)
