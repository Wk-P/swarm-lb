import random
import time
from datetime import datetime
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from collections import deque
import matplotlib.pyplot as plt
import gurobipy as gp
from gurobipy import GRB


def Optimization_Model2(prediction_matrix, batch_running_workers):
    num_tasks = len(prediction_matrix)
    num_workers = len(prediction_matrix[0])

    # weight (sum=1, random)
    alpha = 1 / 3
    beta = 1 / 3
    gamma = 1 / 3

    # Maximum value measured by 20000 tests
    hdd_usage_std_max = 0.166666667
    mem_usage_std_max = 0.166666667

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

    # Create model
    start_modeling = time.perf_counter_ns()
    model = gp.Model("server_optimization")

    # Create variables
    x = model.addVars(num_tasks, num_workers, vtype=GRB.BINARY, name="x")

    # Set objective function：
    for i in range(num_tasks):
        hdd_usage_std = gp.quicksum(((task_hdd_demands[i] * x[i, j] + worker_hdd_usages[j]) - (
                    task_hdd_demands[i] + worker_hdd_usages[j]) / num_workers) ** 2 for j in range(num_workers))
        mem_usage_std = gp.quicksum(((task_mem_demands[i] * x[i, j] + worker_mem_usages[j]) - (
                    task_mem_demands[i] + worker_mem_usages[j]) / num_workers) ** 2 for j in range(num_workers))

        model.setObjective(
            alpha / hdd_usage_std_max * hdd_usage_std + beta / mem_usage_std_max * mem_usage_std + gamma /
            max_response_times[i] * gp.quicksum(prediction_matrix[i][j] * x[i, j] for j in range(num_workers)),
            GRB.MINIMIZE)

    # Add constraint
    for i in range(num_tasks):
        # 添加约束：每个任务只能分配给一个 worker
        model.addConstr(gp.quicksum(x[i, j] for j in range(num_workers)) == 1)
        for j in range(num_workers):
            # real capacity constraints
            model.addConstr(worker_hdd_usages[j] + task_hdd_demands[i] * x[i, j] <= worker_hdd_limits[j], name=f"hdd_capacity_worker_{j}")
            model.addConstr(worker_mem_usages[j] + task_mem_demands[i] * x[i, j] <= worker_mem_limits[j], name=f"mem_capacity_worker_{j}")

    end_modeling = time.perf_counter_ns()
    modeling_time = end_modeling - start_modeling

    model.setParam(GRB.Param.PoolSearchMode, 2)  # 深度搜索模式，尽量生成多种解
    model.setParam(GRB.Param.PoolSolutions, 10)  # 设置最多保存 10 个不同的解
    model.setParam(GRB.Param.PoolGap, 0.2)  # 强制每个解之间有至少 20% 的差异

    # Optimize modeal
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


