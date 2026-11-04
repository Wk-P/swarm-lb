import random
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from collections import deque
import matplotlib.pyplot as plt
import gurobipy as gp
from gurobipy import GRB




# def min_min(response_time, Task, WORKERS):
#     server_index = int(np.argmin(response_time))

#     if (WORKERS[server_index].hdd_usage + Task.hdd_usage <= WORKERS[server_index].max_hdd_usage and WORKERS[server_index].mem_usage + Task.mem_usage <= WORKERS[server_index].max_mem_usage):
#         return server_index




def min_min(response_time, WORKERS):
    server_index = int(np.argmin(response_time))

    worker_index = 0
    if (WORKERS[server_index]['worker_hdd_usage'] + WORKERS[server_index]['task_hdd_size_mb'] <= WORKERS[server_index]['hdd_limit_mb'] and WORKERS[server_index]['worker_mem_usage'] + WORKERS[server_index]['task_mem_size_mb'] <= WORKERS[server_index]['memory_limit_mb']):
        worker_index = server_index

        return worker_index



