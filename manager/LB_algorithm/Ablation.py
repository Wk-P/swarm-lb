import random
import heapq
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from collections import deque


# =========================================================
# DQN Network
# =========================================================
class DQN(nn.Module):
    def __init__(self, input_dim, output_dim):
        super(DQN, self).__init__()
        self.fc1 = nn.Linear(input_dim, 512)
        self.fc2 = nn.Linear(512, 512)
        self.fc3 = nn.Linear(512, output_dim)

    def forward(self, x):
        x = torch.relu(self.fc1(x))
        x = torch.relu(self.fc2(x))
        return self.fc3(x)


# =========================================================
# DQN Hyperparameters
# =========================================================
gamma = 0.95
epsilon_start = 1
epsilon_end = 0.1
epsilon_decay = 0.995
batch_size = 64
memory_size = 10000
target_update = 100
learning_rate = 0.001



epsilon = epsilon_start

input_dim = 6
output_dim = 3

policy_net = DQN(input_dim, output_dim)
target_net = DQN(input_dim, output_dim)
target_net.load_state_dict(policy_net.state_dict())
target_net.eval()

optimizer = optim.AdamW(policy_net.parameters(), lr=learning_rate, weight_decay=1e-2)

memory = deque(maxlen=memory_size)
losses = []
q_values_list = []


# =========================================================
# Reset DQN if worker number changes
# =========================================================
def reset_dqn_if_needed(num_workers):
    global output_dim, policy_net, target_net, optimizer

    if output_dim != num_workers:
        output_dim = num_workers

        policy_net = DQN(input_dim, output_dim)
        target_net = DQN(input_dim, output_dim)
        target_net.load_state_dict(policy_net.state_dict())
        target_net.eval()

        optimizer = optim.AdamW(policy_net.parameters(), lr=learning_rate, weight_decay=1e-2)


# =========================================================
# Replay Memory
# =========================================================
def store_transition(state, action, reward, next_state, done):
    memory.append((state, action, reward, next_state, done))


def sample_memory(batch_size):
    return random.sample(memory, batch_size)


# =========================================================
# Double DQN Update
# =========================================================
def update_network():
    if len(memory) < batch_size:
        return

    batch = sample_memory(batch_size)

    states, actions, rewards, next_states, dones = zip(*batch)

    states = torch.FloatTensor(np.array(states))
    actions = torch.LongTensor(actions)
    rewards = torch.FloatTensor(rewards)
    next_states = torch.FloatTensor(np.array(next_states))
    dones = torch.FloatTensor(dones)

    q_values = policy_net(states).gather(1, actions.unsqueeze(1)).squeeze(1)

    with torch.no_grad():
        next_actions = policy_net(next_states).argmax(1)
        next_q_values = target_net(next_states).gather(1, next_actions.unsqueeze(1)).squeeze(1)
        target_q_values = rewards + (1 - dones) * gamma * next_q_values

    loss = nn.MSELoss()(q_values, target_q_values)
    losses.append(loss.item())

    optimizer.zero_grad()
    loss.backward()
    optimizer.step()


def update_target_network():
    target_net.load_state_dict(policy_net.state_dict())


# =========================================================
# State Space
# =========================================================
def get_state(task_id, waiting_time, WORKERS, queue_length):
    """
    State Space S_t:
        1. 当前任务等待时间
        2. 当前任务 HDD 需求
        3. 当前任务 MEM 需求
        4. worker 平均 HDD 使用率
        5. worker 平均 MEM 使用率
        6. 当前队列长度
    """

    task_hdd = WORKERS[0]["task_hdd_size_mb"]
    task_mem = WORKERS[0]["task_mem_size_mb"]

    hdd_rates = []
    mem_rates = []

    for worker in WORKERS:
        hdd_limit = worker["hdd_limit_mb"] if worker["hdd_limit_mb"] > 0 else 1.0
        mem_limit = worker["memory_limit_mb"] if worker["memory_limit_mb"] > 0 else 1.0

        hdd_rates.append(worker["worker_hdd_usage"] / hdd_limit)
        mem_rates.append(worker["worker_mem_usage"] / mem_limit)

    state = np.array([waiting_time, task_hdd, task_mem, np.mean(hdd_rates), np.mean(mem_rates), queue_length], dtype=float)

    norm = np.linalg.norm(state)
    if norm > 0:
        state = state / norm

    return state


# =========================================================
# Feasibility / Idle Check
# =========================================================
def get_idle_workers(WORKERS, current_time):
    idle_workers = []

    for i, worker in enumerate(WORKERS):
        if "busy_until" not in worker:
            worker["busy_until"] = 0.0

        if current_time >= worker["busy_until"]:
            idle_workers.append(i)

    return idle_workers


def wait_until_worker_idle(WORKERS, current_time):
    idle_workers = get_idle_workers(WORKERS, current_time)

    if idle_workers:
        return current_time, idle_workers

    next_available_time = min(worker["busy_until"] for worker in WORKERS)
    current_time = next_available_time

    idle_workers = get_idle_workers(WORKERS, current_time)

    return current_time, idle_workers


def is_feasible(worker):
    next_hdd = worker["worker_hdd_usage"] + worker["task_hdd_size_mb"]
    next_mem = worker["worker_mem_usage"] + worker["task_mem_size_mb"]

    return (
        next_hdd <= worker["hdd_limit_mb"]
        and next_mem <= worker["memory_limit_mb"]
    )


# =========================================================
# Reward Function
# =========================================================
def jains_fairness_index(x):
    x = np.array(x, dtype=float)
    denominator = len(x) * np.sum(x ** 2)

    if denominator == 0:
        return 0.0

    return (np.sum(x) ** 2) / denominator


def get_reward(waiting_time, action, WORKERS):
    """
    Reward:
        R = w1 * performance + w2 * memory fairness + w3 * hdd fairness
    """

    reward_time = 1.0 / (waiting_time + 1.0)

    w1 = 1 / 3
    w2 = 1 / 3
    w3 = 1 / 3

    mem_usage = (WORKERS[action]["worker_mem_usage"] + WORKERS[action]["task_mem_size_mb"])
    hdd_usage = (WORKERS[action]["worker_hdd_usage"] + WORKERS[action]["task_hdd_size_mb"])

    other_mem_usages = [WORKERS[i]["worker_mem_usage"] for i in range(len(WORKERS)) if i != action]
    other_hdd_usages = [WORKERS[i]["worker_hdd_usage"] for i in range(len(WORKERS)) if i != action]

    mem_fairness = jains_fairness_index([mem_usage] + other_mem_usages)
    hdd_fairness = jains_fairness_index([hdd_usage] + other_hdd_usages)

    return w1 * reward_time + w2 * mem_fairness + w3 * hdd_fairness


# =========================================================
# DQN Action Selection
# =========================================================
def choose_action(state, epsilon_value, feasible_workers):
    """
    ε-greedy action selection.
    action = worker index
    """

    if random.uniform(0, 1) < epsilon_value:
        return random.choice(feasible_workers)

    with torch.no_grad():
        state_tensor = torch.FloatTensor(state).unsqueeze(0)
        q_values = policy_net(state_tensor)
        q_values_list.append(q_values.mean().item())

        sorted_actions = q_values.argsort(descending=True).squeeze().tolist()

        if isinstance(sorted_actions, int):
            sorted_actions = [sorted_actions]

        for action in sorted_actions:
            if action in feasible_workers:
                return action

    return feasible_workers[0]


# =========================================================
# Dynamic Priority Queue Operations
# =========================================================
def calculate_task_priority(task_id, waiting_time, WORKERS, queue_length):
    """
    使用当前 DQN 输出的最大 Q-value 作为任务动态优先级。
    """

    state = get_state(task_id, waiting_time, WORKERS, queue_length)

    with torch.no_grad():
        q_values = policy_net(torch.FloatTensor(state).unsqueeze(0))
        priority_score = torch.max(q_values).item()

    return priority_score


def RL_Queue_Insert(queue, task_id, waiting_time, WORKERS, queue_length):
    """
    RL_Queue_Insert(Q, task)
    根据当前 Q-value 计算任务优先级，并插入动态优先级队列。
    """

    priority_score = calculate_task_priority(task_id, waiting_time, WORKERS, queue_length)

    heapq.heappush(queue, (-priority_score, task_id))

    return priority_score


def RL_Queue_Extract_Max(queue):
    """
    RL_Queue_Extract_Max(Q)
    取出当前 Q-value 最大的任务。
    """

    if not queue:
        return None

    priority_score, task_id = heapq.heappop(queue)

    return task_id, -priority_score


def RL_Queue_Union(queue1, queue2):
    """
    RL_Queue_Union(Q1, Q2)
    合并两个动态优先级队列。
    """

    new_queue = queue1 + queue2
    heapq.heapify(new_queue)

    return new_queue


def RL_Queue_Rebuild(queue, waiting_time, WORKERS, batch_running_workers):
    """
    每次任务执行后，重新计算等待任务的 Q-value，并重建优先级队列。
    """

    waiting_tasks = []

    while queue:
        _, task_id = heapq.heappop(queue)
        waiting_tasks.append(task_id)

    new_queue = []

    for task_id in waiting_tasks:
        set_task_to_workers(task_id, batch_running_workers, WORKERS)
        RL_Queue_Insert(new_queue, task_id, waiting_time, WORKERS, queue_length=len(new_queue))

    return new_queue


# =========================================================
# Worker Resource Update
# =========================================================
def set_task_to_workers(task_id, batch_running_workers, WORKERS):
    """
    将当前 task 的资源需求写入每个 worker。
    """

    for j in range(len(WORKERS)):
        WORKERS[j]["task_hdd_size_mb"] = batch_running_workers[task_id][j]["task_hdd_size_mb"]
        WORKERS[j]["task_mem_size_mb"] = batch_running_workers[task_id][j]["task_mem_size_mb"]


def release_finished_workers(WORKERS, current_time):
    """
    释放已经完成任务的 worker。
    这里采用 base usage 恢复方式。
    """

    for worker in WORKERS:
        if "busy_until" not in worker:
            worker["busy_until"] = 0.0

        if current_time >= worker["busy_until"]:
            worker["worker_hdd_usage"] = worker.get("base_hdd_usage", worker["worker_hdd_usage"])
            worker["worker_mem_usage"] = worker.get("base_mem_usage", worker["worker_mem_usage"])


# =========================================================
# RL-MOTS Scheduling Algorithm
# =========================================================
def Ablation(prediction_matrix, batch_running_workers):
    """
    RL-MOTS scheduling algorithm.

    Returns
    -------
    chosen_workers_index : list[int]
        每个任务最终选择的 worker 编号。

    rewards : list[float]
        每个任务对应的调度奖励值。
    """

    global epsilon
    arriva_time = 0

    if not prediction_matrix:
        return [], []

    if not batch_running_workers:
        return [], []

    num_tasks = len(prediction_matrix)
    num_workers = len(prediction_matrix[0])

    reset_dqn_if_needed(num_workers)

    # -------------------------------------------------
    # Initialize Resource Pool
    # -------------------------------------------------
    WORKERS = []

    for j in range(num_workers):
        worker = batch_running_workers[0][j].copy()
        worker["busy_until"] = 0.0
        worker["base_hdd_usage"] = worker["worker_hdd_usage"]
        worker["base_mem_usage"] = worker["worker_mem_usage"]
        WORKERS.append(worker)

    # -------------------------------------------------
    # Initialize Dynamic Priority Queue
    # -------------------------------------------------
    dynamic_priority_queue = []

    # -------------------------------------------------
    # Initialize Virtual FIFO Queue
    # -------------------------------------------------
    virtual_fifo_queue = deque()

    chosen_workers_index = [-1] * num_tasks
    rewards = [0.0] * num_tasks

    current_time = 0.0
    step_count = 0
    max_steps = num_tasks * 200

    # -------------------------------------------------
    # Receive New Task(s)
    # RL_Queue_Insert
    # -------------------------------------------------
    for task_id in range(num_tasks):
        set_task_to_workers(task_id, batch_running_workers, WORKERS)
        waiting_time = current_time - arriva_time
        RL_Queue_Insert(dynamic_priority_queue, task_id, waiting_time, WORKERS, queue_length=len(dynamic_priority_queue))

    # =================================================
    # Main Scheduling Loop
    # =================================================
    while dynamic_priority_queue or virtual_fifo_queue:

        step_count += 1
        if step_count > max_steps:
            raise RuntimeError("RL-MOTS scheduling exceeded max steps.")

        release_finished_workers(WORKERS, current_time)

        # -------------------------------------------------
        # FIFO → Priority Queue
        # 等待队列中的任务重新计算 Q-value 后再进入优先级队列
        # -------------------------------------------------
        while virtual_fifo_queue:
            fifo_task_id = virtual_fifo_queue.popleft()
            set_task_to_workers(fifo_task_id, batch_running_workers, WORKERS)
            waiting_time = current_time - arriva_time
            RL_Queue_Insert(dynamic_priority_queue, fifo_task_id, waiting_time, WORKERS, queue_length=len(dynamic_priority_queue))

        # -------------------------------------------------
        # 重新计算等待任务的 Q-value 并重建队列
        # -------------------------------------------------
        waiting_time = current_time - arriva_time
        dynamic_priority_queue = RL_Queue_Rebuild(dynamic_priority_queue, waiting_time, WORKERS, batch_running_workers)

        # -------------------------------------------------
        # RL_Queue_Extract_Max
        # -------------------------------------------------
        extracted = RL_Queue_Extract_Max(dynamic_priority_queue)

        if extracted is None:
            continue

        task_id, priority_score = extracted
        set_task_to_workers(task_id, batch_running_workers, WORKERS)

        # -------------------------------------------------
        # 等待空闲 worker
        # -------------------------------------------------
        current_time, idle_workers = wait_until_worker_idle(WORKERS, current_time)
        release_finished_workers(WORKERS, current_time)
        feasible_workers = [j for j in idle_workers if is_feasible(WORKERS[j])]

        # -------------------------------------------------
        # Preemption / Virtual FIFO Queue
        # 如果没有空闲且可行的 worker，任务进入 FIFO
        # -------------------------------------------------
        if not feasible_workers:
            virtual_fifo_queue.append(task_id)
            current_time += 1.0
            continue

        # -------------------------------------------------
        # Observe current state
        # -------------------------------------------------
        waiting_time = current_time - arriva_time
        state = get_state(task_id, waiting_time, WORKERS, queue_length=len(dynamic_priority_queue) + len(virtual_fifo_queue))

        # -------------------------------------------------
        # Select action using ε-greedy policy
        # -------------------------------------------------
        action = choose_action(state, epsilon, feasible_workers)

        # -------------------------------------------------
        # Execute action
        # -------------------------------------------------
        waiting_time = current_time - arriva_time
        reward = get_reward(waiting_time, action, WORKERS)
        old_state = state.copy()

        WORKERS[action]["worker_hdd_usage"] += WORKERS[action]["task_hdd_size_mb"]
        WORKERS[action]["worker_mem_usage"] += WORKERS[action]["task_mem_size_mb"]

        tasks = [10891, 265986, 418779, 61657, 13836, 409899, 119237, 279037, 409002, 181428, 367231, 177115, 237450, 125953, 25567, 202568, 106288, 278697, 255764, 182266, 492445, 343464, 200699, 356689, 205281, 391336, 350434, 216189, 141539, 333491, 495645, 235467, 349153, 412267, 350795, 328796, 96890, 446445, 2978, 195490, 237903, 154275, 344871, 213688, 410052, 221904, 437156, 324717, 41881, 416667, 356739, 165381, 362832, 285773, 27581, 305435, 321288, 156591, 7309, 336806, 71517, 231890, 71710, 127197, 467103, 146050, 154855, 490890, 241827, 125318, 132285, 123590, 248427, 329182, 44142, 411551, 134784, 247177, 373503, 329318, 375411, 469442, 71362, 217810, 326736, 132513, 148818, 253457, 105274, 312034, 283680, 485405, 36647, 430622, 255831, 250679, 473010, 350643, 314521, 73518, 454038, 76341, 42735, 239516, 237308, 357883, 162074, 442295, 368750, 299398]
        if action == 0:
            execution_time = tasks[task_id] / 1661.49148834135
        elif action == 1:
            execution_time = tasks[task_id] / 1606.28824501093
        else:
            execution_time = tasks[task_id] / 1611.64246801655

        if execution_time <= 0:
            execution_time = 1e-6

        WORKERS[action]["busy_until"] = current_time + execution_time

        chosen_workers_index[task_id] = action
        rewards[task_id] = reward

        # -------------------------------------------------
        # Observe next state
        # -------------------------------------------------
        waiting_time = current_time - arriva_time
        next_state = get_state(task_id, waiting_time, WORKERS, queue_length=len(dynamic_priority_queue) + len(virtual_fifo_queue))
        done = (len(dynamic_priority_queue) == 0 and len(virtual_fifo_queue) == 0)

        # -------------------------------------------------
        # Store transition and update DQN
        # -------------------------------------------------
        store_transition(old_state, action, reward, next_state, done)
        update_network()
        epsilon = max(epsilon_end, epsilon * epsilon_decay)

        if step_count % target_update == 0:
            update_target_network()

        # -------------------------------------------------
        # 每次执行后重建优先级队列
        # 对应论文中的：
        # priority queue is reconstituted after extract-max
        # -------------------------------------------------
        waiting_time = current_time - arriva_time
        dynamic_priority_queue = RL_Queue_Rebuild(dynamic_priority_queue, waiting_time, WORKERS, batch_running_workers)

    return chosen_workers_index, rewards


