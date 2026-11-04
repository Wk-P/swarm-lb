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

'''
Model
'''


def Optimization_Model1(response_time, WORKERS):
    NUM_WORKERS = len(WORKERS)

    # weight
    alpha = 1 / 3
    beta = 1 / 3
    gamma = 1 / 3

    hdd_max = max(worker['hdd_limit_mb'] for worker in WORKERS)
    mem_max = max(worker['memory_limit_mb'] for worker in WORKERS)
    response_time_max = max(response_time)

    # Create model
    start_modeling = time.perf_counter_ns()
    model = gp.Model("server_optimization")

    # 设置日志输出文件
    # model.setParam("LogFile", "single_task_gurobi_solver.log")

    # Create variables
    x = model.addVars(NUM_WORKERS, vtype=GRB.BINARY, name="x")  # x = 0 or 1  이진법
    b_hdd = model.addVars(1, vtype=GRB.CONTINUOUS, name="b_hdd")
    b_mem = model.addVars(1, vtype=GRB.CONTINUOUS, name="b_mem")

    # Set objective function：
    model.setObjective((alpha * (b_hdd[0] / hdd_max)) + (beta * (b_mem[0] / mem_max)) + gamma * (
                gp.quicksum(response_time[j] * x[j] for j in range(NUM_WORKERS)) / response_time_max), GRB.MINIMIZE)

    # Add constraint
    model.addConstr(gp.quicksum(x[j] for j in range(NUM_WORKERS)) == 1, "c1")
    # start_time_2 = time.perf_counter_ns()
    for j in range(NUM_WORKERS):
        # maximum value
        model.addConstr(WORKERS[j]['worker_hdd_usage'] + WORKERS[j]['task_hdd_size_mb'] * x[j] <= WORKERS[j]['hdd_limit_mb'])
        model.addConstr(WORKERS[j]['worker_mem_usage'] + WORKERS[j]['task_mem_size_mb'] * x[j] <= WORKERS[j]['memory_limit_mb'])
        # b_hdd
        model.addConstr(WORKERS[j]['worker_hdd_usage'] + WORKERS[j]['task_hdd_size_mb'] * x[j] <= b_hdd[0])
        # b_mem
        model.addConstr(WORKERS[j]['worker_mem_usage'] + WORKERS[j]['task_mem_size_mb'] * x[j] <= b_mem[0])
    end_modeling = time.perf_counter_ns()
    modeling_time = end_modeling - start_modeling

    # Optimize modeal
    start_solving = time.perf_counter_ns()
    model.optimize()
    end_solving = time.perf_counter_ns()
    solver_time = end_solving - start_solving

    if model.status == GRB.OPTIMAL:
        # print("algor1_CPU time:", end_time - start_time)
        # 获取所有 x[worker.id].X 的值
        # x_values = [x[j].X for j in range(NUM_WORKERS)]
        worker_index = [j for j in range(NUM_WORKERS) if x[j].X == 1.0]
        return worker_index, (solver_time, modeling_time)

    return 0, (solver_time, modeling_time)





