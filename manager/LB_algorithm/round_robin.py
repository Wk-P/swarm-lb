import random
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from collections import deque
import matplotlib.pyplot as plt
import gurobipy as gp
from gurobipy import GRB



# MEM_CAPACITY = 500*1_000_000 # 500 MB (use any number)
# HDD_CAPACITY = 1.5*1_000_000_000 # 1.5GB (use any number)
MEM_CAPACITY = 900*1_000_000 # 900 MB (use any number)       ## 서버의 실제값
HDD_CAPACITY = 14.5*1_000_000_000 # 14.5GB (use any number)   ## 서버의 실제값



def round_robin_assignment(index, WORKERS):
    # NUM_WORKERS = len(WORKERS)
    parts = index.split('_')
    wave = int(parts[0].replace('wave', ''))  # 01 → 1
    task = int(parts[2])  # 01 → 1
    task_index = int((wave - 1) * 10 + (task - 1))
    server_index = task_index % 3

    if (WORKERS[server_index]['worker_hdd_usage'] + WORKERS[server_index]['task_hdd_size_mb'] <= WORKERS[server_index]['hdd_limit_mb'] and WORKERS[server_index]['worker_mem_usage'] + WORKERS[server_index]['task_mem_size_mb'] <= WORKERS[server_index]['memory_limit_mb']):
        return server_index








