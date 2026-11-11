# manager_sw.py
# Batch scheduling version
# - Receive one tasks list
# - Schedule all tasks only once
# - Forward tasks according to the one-shot assignment
# - Return all results in one response

from aiohttp import web
from aiohttp.web_request import Request
from aiohttp.web_response import Response
from copy import deepcopy

from objects import ServerManager, ManagerResponse
import asyncio
import traceback
import aiohttp
import sys
sys.path.append("/home/pi/appv8/manager")

from pathlib import Path
import joblib
import pandas as pd
import time
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional
from aiohttp.web_response import StreamResponse
import json

from model.preprocess import preprocess_for_prediction_model
from model.predict import predict_execution_time


import LB_algorithm.algor1_set as algor1_set
import LB_algorithm.algor2_set as algor2_set
import LB_algorithm.RL_MOTS as RL_MOTS
import LB_algorithm.Ablation as Ablation





# =========================================================
# Model
# =========================================================
model_path = Path(__file__).parent / "model" / "v1" / "xgb_multioutput_model.joblib"
FEATURE_COLS = [
    'cpu_task_number',   # int
    'cpu_limit',         # float
    'hdd_limit',         # float
    'mem_limit',         # float
    'allocating_hdd',    # float
    'allocating_mem',    # float
    'hdd_rate',          # float
    'mem_rate',          # float
]

if model_path.exists():
    prediction_model = joblib.load(model_path)
else:
    raise FileNotFoundError(f"Model file not found at {model_path}")


# =========================================================
# Logging
# =========================================================
log_dir = Path.cwd() / 'log'
log_dir.mkdir(exist_ok=True)

logger_names = ['dqn-reward', 'running-tasks', 'manager-events']
for logger_name in logger_names:
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    log_path = log_dir / f'{logger_name}.log'
    handler = logging.FileHandler(log_path, mode='w', encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False
    print(f"Logger '{logger_name}' initialized at {log_path}")

manager_logger = logging.getLogger('manager-events')
reward_logger = logging.getLogger('dqn-reward')
running_tasks_logger = logging.getLogger('running-tasks')
manager_logger.info("Manager batch service started.")


# =========================================================
# Globals
# =========================================================
EPISODE = 0
RUNNING_TASKS = 0

lock = asyncio.Lock()


# =========================================================
# Utils
# =========================================================
def select_max_prediction_time(predictions: dict):
    """
    Choose one of the longest processing sub-task times in one prediction result.
    """
    ret = max(predictions.values())
    print(f"Choose the longest prediction result for task: {ret} seconds")
    return ret


def normalize_task_payload(raw_task: dict, default_task_index: str) -> dict:
    """
    Normalize incoming batch task.
    Expected format:
    {
        "task_index": "wave01_task_01",
        "cpu_task": {"number": ...},
        "mem_task": {"size_mb": ...},
        "hdd_task": {"size_mb": ...},
        "generated_at": "..."
    }
    """
    task = deepcopy(raw_task) if raw_task else {}

    task.setdefault("task_index", default_task_index)
    task.setdefault("cpu_task", {"number": 0})
    task.setdefault("mem_task", {"size_mb": 0})
    task.setdefault("hdd_task", {"size_mb": 0})
    task.setdefault("generated_at", datetime.now().isoformat())

    # Ensure nested keys exist
    if not isinstance(task.get("cpu_task"), dict):
        task["cpu_task"] = {"number": 0}
    if not isinstance(task.get("mem_task"), dict):
        task["mem_task"] = {"size_mb": 0}
    if not isinstance(task.get("hdd_task"), dict):
        task["hdd_task"] = {"size_mb": 0}

    task["cpu_task"].setdefault("number", 0)
    task["mem_task"].setdefault("size_mb", 0)
    task["hdd_task"].setdefault("size_mb", 0)

    return task


# =========================================================
# Snapshot / Prediction Matrix
# =========================================================
async def build_batch_prediction_matrix(app: web.Application, tasks: List[dict]):
    """
    Build:
    1) prediction_matrix[num_tasks][num_workers]
    2) batch_running_workers[num_tasks][num_workers]
    3) batch_resource_snapshots[num_tasks][num_workers]

    All tasks use one same resource snapshot baseline before scheduling.
    """
    manager: ServerManager = app.get('manager')
    if manager is None:
        raise RuntimeError("service not ready")

    prediction_matrix: List[List[float]] = []
    batch_running_workers: List[List[dict]] = []
    batch_resource_snapshots: List[List[dict]] = []

    async with lock:
        num_workers = len(manager.workers)

        for task in tasks:
            task_predictions = []
            task_running_workers = []
            task_resource_snapshot = []

            allocating_hdd = float(task.get('hdd_task', {}).get('size_mb', 0.0))
            allocating_mem = float(task.get('mem_task', {}).get('size_mb', 0.0))

            for worker_index in range(num_workers):
                worker_info = manager.workers[worker_index]
                worker_ssh_client = manager.worker_ssh_clients[worker_index]

                _input = preprocess_for_prediction_model(
                    cpu_task_number=worker_info.get('cpu_tasks', 0),
                    cpu_limit=worker_info.get('cpus_limit_cores') or float('nan'),
                    hdd_limit=worker_info.get('hdd_limit_mb') or float('nan'),
                    mem_limit=worker_info.get('memory_limit_mb') or float('nan'),
                    allocating_hdd=allocating_hdd,
                    allocating_mem=allocating_mem,
                )

                pred = select_max_prediction_time(
                    predict_execution_time(
                        pd.DataFrame(_input, columns=FEATURE_COLS),
                        prediction_model
                    )
                )
                task_predictions.append(pred)

                current_hdd = worker_ssh_client.hdd_usage / (1024 * 1024)   # MB
                current_mem = worker_ssh_client.mem_usage                   # MB

                data_for_algo = {
                    'number': worker_info.get('cpu_tasks', 0),
                    'cpus_limit_cores': worker_info.get('cpus_limit_cores') or float('nan'),
                    'hdd_limit_mb': worker_info.get('hdd_limit_mb') or float('nan'),
                    'memory_limit_mb': worker_info.get('memory_limit_mb') or float('nan'),
                    'task_hdd_size_mb': allocating_hdd,
                    'task_mem_size_mb': allocating_mem,
                    'worker_hdd_usage': current_hdd,
                    'worker_mem_usage': current_mem,
                }
                task_running_workers.append(data_for_algo)

                task_resource_snapshot.append({
                    'worker_index': worker_index,
                    'worker_name': worker_info.get('name', f'worker_{worker_index}'),
                    'cpu_tasks': worker_info.get('cpu_tasks', 0),
                    'cpus_limit_cores': worker_info.get('cpus_limit_cores'),
                    'memory_limit_mb': worker_info.get('memory_limit_mb'),
                    'hdd_limit_mb': worker_info.get('hdd_limit_mb'),
                    'current_mem_usage_mb': current_mem,
                    'current_hdd_usage_mb': current_hdd,
                })

            prediction_matrix.append(task_predictions)
            batch_running_workers.append(task_running_workers)
            batch_resource_snapshots.append(task_resource_snapshot)

    return prediction_matrix, batch_running_workers, batch_resource_snapshots


# =========================================================
# One-shot Batch Scheduling
# =========================================================
async def choose_batch_algorithm(
    name: Optional[str] = None,
    tasks: Optional[List[dict]] = None,
    batch_running_workers: Optional[List[List[dict]]] = None,
    prediction_matrix: Optional[List[List[float]]] = None,
):
    """
    One-shot scheduling for all tasks.
    Return assignments where:
        assignments[i] = selected worker index for tasks[i]
    """
    global EPISODE

    if tasks is None:
        tasks = []
    if prediction_matrix is None:
        prediction_matrix = []

    num_tasks = len(tasks)
    if num_tasks == 0:
        return []

    num_workers = len(prediction_matrix[0]) if prediction_matrix and prediction_matrix[0] else 0
    if num_workers == 0:
        raise ValueError("No workers available in prediction matrix")

    algo_name = name

    if name == 'algor1_set':  # 알고리즘 1
        manager_logger.info(f"Running choose_url_algorithm with name={name}")
        # choose worker
        # worker_index = algor1.Optimization_Model1(response_time, new_task, worker_names_obj, data)
        # return worker_names_obj[worker_index]

        worker_index = algor1_set.Optimization_Model1(prediction_matrix, batch_running_workers)[0]
        manager_logger.info(f"algor1 selected worker index: {worker_index}")
        return worker_index

    elif name == 'algor2_set':  # 알고리즘 2  (분배 비율의 표준평차 최소화)
        manager_logger.info(f"Running choose_url_algorithm with name={name}")
        # choose worker
        worker_index = algor2_set.Optimization_Model2(prediction_matrix, batch_running_workers)[0]
        manager_logger.info(f"algor2 selected worker index: {worker_index}")
        return worker_index

    elif name == 'RL-MOTS':
        manager_logger.info(f"Running choose_url_algorithm with name={name}")
        # choose worker
        worker_index = RL_MOTS.RL_MOTS_Model(prediction_matrix, batch_running_workers)[0]
        manager_logger.info(f"RL-MOTS selected worker index: {worker_index}")
        return worker_index

    elif name == 'Ablation':
        manager_logger.info(f"Running choose_url_algorithm with name={name}")
        # choose worker
        worker_index = Ablation.Ablation(prediction_matrix, batch_running_workers)[0]
        manager_logger.info(f"Ablation selected worker index: {worker_index}")
        return worker_index


# =========================================================
# Forward Task
# =========================================================
async def forward_task_to_worker(
    manager: ServerManager,
    task: dict,
    task_index: str,
    selected_worker_index: int,
    task_predictions: List[float],
    task_resource_snapshot: List[dict],
    algo_name: str,
    schedule_cpu_time: int,
):
    """
    Forward one already-assigned task to a worker.
    This is execution, not scheduling.
    """
    selected_worker = manager.workers[selected_worker_index]
    session: aiohttp.ClientSession = manager.http_clients[selected_worker_index]
    worker_url = manager.worker_urls[selected_worker_index]['url']
    target_url = f"{worker_url}/task/"

    headers = {
        "task_index": task_index,
        "Content-Type": "application/json"
    }

    async with session.post(target_url, headers=headers, json=task) as resp:
        resp.raise_for_status()
        worker_response = await resp.json()

        worker_response['response_host'] = str(resp.host)
        worker_response['xgb_prediction'] = task_predictions[selected_worker_index]
        worker_response['selected_worker_index'] = selected_worker_index
        worker_response['worker_info'] = selected_worker
        worker_response['all_predictions'] = task_predictions
        worker_response['all_workers_resource_snapshot'] = task_resource_snapshot
        worker_response['cpu_time'] = schedule_cpu_time
        worker_response['algorithm'] = algo_name

        return {
            "task_index": task_index,
            "success": True,
            "error": 0,
            "message": "success",
            "response_data": worker_response,
        }


# =========================================================
# Single endpoint (optional compatibility)
# Still accepts one task, but internally wraps it as batch of size 1
# =========================================================
async def handle_request(request: Request) -> Response:
    """
    Compatibility single-task endpoint.
    Internally converted to one-task batch scheduling.
    """
    try:
        raw_data = deepcopy(await request.json())
        task_index = request.headers.get("task_index", "unknown")
        task = normalize_task_payload(raw_data, task_index)

        body = {
            "batch_id": f"single_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            "tasks": [task]
        }

        fake_request_data = body
        request._read_bytes = None  # harmless reset if aiohttp cached body; not strictly required

        # Reuse batch logic directly
        return await process_batch_body(request.app, fake_request_data)

    except Exception as e:
        traceback.print_exc()
        return web.json_response(
            ManagerResponse(
                task_index="unknown",
                task_class="",
                error=1,
                message=str(e),
                success=False,
                response_data=None,
            ),
            status=400
        )


# =========================================================
# Core batch processor
# =========================================================
# async def process_batch_body(app: web.Application, body: dict) -> Response:
#     """
#     Core one-shot batch scheduling processor.
#     """
#     global RUNNING_TASKS
#
#     batch_start_time = time.time()
#
#     try:
#         batch_id = body.get("batch_id", f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
#         tasks = body.get("tasks", [])
#
#         if not isinstance(tasks, list) or len(tasks) == 0:
#             return web.json_response({
#                 "batch_id": batch_id,
#                 "success": False,
#                 "error": 1,
#                 "message": "'tasks' must be a non-empty list",
#                 "results": []
#             }, status=400)
#
#         normalized_tasks = []
#         for idx, task in enumerate(tasks, start=1):
#             default_task_index = f"task_{idx:04d}"
#             normalized_tasks.append(normalize_task_payload(task, default_task_index))
#
#         manager: ServerManager = app.get('manager')
#         if manager is None:
#             return web.json_response({
#                 "batch_id": batch_id,
#                 "success": False,
#                 "error": 1,
#                 "message": "service not ready",
#                 "results": []
#             }, status=503)
#
#         async with lock:
#             RUNNING_TASKS += len(normalized_tasks)
#
#         running_tasks_logger.info(
#             f"[Batch] Received batch_id={batch_id}, task_count={len(normalized_tasks)}"
#         )
#
#         # -------------------------------------------------
#         # Step 1. Build prediction matrix for all tasks
#         # -------------------------------------------------
#         prediction_matrix, batch_running_workers, batch_resource_snapshots = \
#             await build_batch_prediction_matrix(app, normalized_tasks)
#
#         # Log matrix
#         for i, task in enumerate(normalized_tasks):
#             manager_logger.info(
#                 f"[Batch] Task {task['task_index']} - All predictions: {prediction_matrix[i]}"
#             )
#
#         # -------------------------------------------------
#         # Step 2. One-shot scheduling
#         # -------------------------------------------------
#         algo_name = 'algor1_set'
#
#         schedule_start = time.perf_counter_ns()
#         assignments = await choose_batch_algorithm(
#             name=algo_name,
#             tasks=normalized_tasks,
#             batch_running_workers=batch_running_workers,
#             prediction_matrix=prediction_matrix
#         )
#         schedule_end = time.perf_counter_ns()
#         schedule_cpu_time = schedule_end - schedule_start
#
#         if len(assignments) != len(normalized_tasks):
#             raise RuntimeError("Assignment length mismatch")
#
#         manager_logger.info(
#             f"[Batch] Scheduling completed once using {algo_name}. assignments={assignments}"
#         )
#
#         # -------------------------------------------------
#         # Step 3. Forward tasks according to assignments
#         # This part is execution, not scheduling
#         # -------------------------------------------------
#         results: List[dict] = []
#
#         # Sequential forwarding is safest
#         # If you later want, you can group by worker and do controlled concurrency
#         for task_idx, task in enumerate(normalized_tasks):
#             task_index = task["task_index"]
#             selected_worker_index = assignments[task_idx]
#
#             manager_logger.info(
#                 f"[Batch] Forwarding task {task_index} to worker {selected_worker_index}"
#             )
#
#             try:
#                 result = await forward_task_to_worker(
#                     manager=manager,
#                     task=task,
#                     task_index=task_index,
#                     selected_worker_index=selected_worker_index,
#                     task_predictions=prediction_matrix[task_idx],
#                     task_resource_snapshot=batch_resource_snapshots[task_idx],
#                     algo_name=algo_name,
#                     schedule_cpu_time=schedule_cpu_time
#                 )
#             except aiohttp.ClientError as ce:
#                 traceback.print_exc()
#                 result = {
#                     "task_index": task_index,
#                     "success": False,
#                     "error": 1,
#                     "message": f"Failed to connect to worker {selected_worker_index}: {str(ce)}",
#                     "response_data": {
#                         "selected_worker_index": selected_worker_index,
#                         "all_predictions": prediction_matrix[task_idx],
#                         "all_workers_resource_snapshot": batch_resource_snapshots[task_idx],
#                         "algorithm": algo_name,
#                         "cpu_time": schedule_cpu_time
#                     }
#                 }
#             except Exception as e:
#                 traceback.print_exc()
#                 result = {
#                     "task_index": task_index,
#                     "success": False,
#                     "error": 1,
#                     "message": str(e),
#                     "response_data": {
#                         "selected_worker_index": selected_worker_index,
#                         "all_predictions": prediction_matrix[task_idx],
#                         "all_workers_resource_snapshot": batch_resource_snapshots[task_idx],
#                         "algorithm": algo_name,
#                         "cpu_time": schedule_cpu_time
#                     }
#                 }
#
#             results.append(result)
#
#         success_count = sum(1 for r in results if r.get("success"))
#         failed_count = len(results) - success_count
#
#         batch_response = {
#             "batch_id": batch_id,
#             "success": failed_count == 0,
#             "error": 0 if failed_count == 0 else 1,
#             "message": "success" if failed_count == 0 else "partial_failure",
#             "task_count": len(normalized_tasks),
#             "successful_tasks": success_count,
#             "failed_tasks": failed_count,
#             "schedule_cpu_time": schedule_cpu_time,
#             "batch_elapsed_time": round(time.time() - batch_start_time, 3),
#             "assignments": assignments,
#             "results": results
#         }
#
#         return web.json_response(batch_response, status=200)
#
#     except Exception as e:
#         traceback.print_exc()
#         return web.json_response({
#             "batch_id": body.get("batch_id", "unknown") if isinstance(body, dict) else "unknown",
#             "success": False,
#             "error": 1,
#             "message": str(e),
#             "results": []
#         }, status=500)
#
#     finally:
#         try:
#             task_count = len(body.get("tasks", [])) if isinstance(body, dict) and isinstance(body.get("tasks", []), list) else 0
#             async with lock:
#                 RUNNING_TASKS = max(0, RUNNING_TASKS - task_count)
#         except Exception:
#             pass





async def process_batch_body(request: Request, body: dict) -> Response:
    global RUNNING_TASKS
    app = request.app

    try:
        batch_id = body.get("batch_id", f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        tasks = body.get("tasks", [])

        if not isinstance(tasks, list) or len(tasks) == 0:
            return web.json_response({
                "batch_id": batch_id,
                "success": False,
                "error": 1,
                "message": "'tasks' must be a non-empty list",
                "results": []
            }, status=400)

        normalized_tasks = []
        for idx, task in enumerate(tasks, start=1):
            default_task_index = f"task_{idx:04d}"
            normalized_tasks.append(normalize_task_payload(task, default_task_index))

        manager: ServerManager = app.get('manager')
        if manager is None:
            return web.json_response({"error": "service not ready"}, status=503)

        async with lock:
            RUNNING_TASKS += len(normalized_tasks)

        # ================================
        # Step 1 & 2 不变（预测 + 调度）
        # ================================
        prediction_matrix, batch_running_workers, batch_resource_snapshots = \
            await build_batch_prediction_matrix(app, normalized_tasks)

        algo_name = 'Ablation'

        schedule_start = time.perf_counter_ns()
        assignments = await choose_batch_algorithm(
            name=algo_name,
            tasks=normalized_tasks,
            batch_running_workers=batch_running_workers,
            prediction_matrix=prediction_matrix
        )
        schedule_cpu_time = time.perf_counter_ns() - schedule_start

        # ================================
        # 🔥 Step 3 改成 Streaming
        # ================================
        resp = StreamResponse(
            status=200,
            reason='OK',
            headers={'Content-Type': 'application/x-ndjson'}
        )
        await resp.prepare(request)  # 关键

        # ---- 先发 meta ----
        meta = {
            "type": "meta",
            "batch_id": batch_id,
            "task_count": len(normalized_tasks),
            "schedule_cpu_time": schedule_cpu_time
        }

        await resp.write((json.dumps(meta) + "\n").encode())

        # ---- 并发执行任务 ----
        async def run_one(task_idx, task):
            task_index = task["task_index"]
            selected_worker_index = assignments[task_idx]

            try:
                result = await forward_task_to_worker(
                    manager=manager,
                    task=task,
                    task_index=task_index,
                    selected_worker_index=selected_worker_index,
                    task_predictions=prediction_matrix[task_idx],
                    task_resource_snapshot=batch_resource_snapshots[task_idx],
                    algo_name=algo_name,
                    schedule_cpu_time=schedule_cpu_time
                )
                return result
            except Exception as e:
                return {
                    "task_index": task_index,
                    "success": False,
                    "error": 1,
                    "message": str(e)
                }

        tasks_future = [asyncio.create_task(run_one(i, t)) for i, t in enumerate(normalized_tasks)]

        completed = 0

        # 🔥 核心：谁先完成谁先返回
        for fut in asyncio.as_completed(tasks_future):
            result = await fut
            completed += 1

            await resp.write((json.dumps({
                "type": "task_result",
                **result
            }) + "\n").encode())

        # ---- 结束 ----
        await resp.write((json.dumps({
            "type": "end",
            "batch_id": batch_id,
            "completed": completed
        }) + "\n").encode())

        await resp.write_eof()
        return resp

    finally:
        async with lock:
            RUNNING_TASKS = max(0, RUNNING_TASKS - len(tasks))


# =========================================================
# Batch endpoint
# =========================================================
async def handle_batch_request(request: Request) -> Response:
    """
    POST /batch
    Expected body:
    {
        "batch_id": "batch_001",
        "task_count": 400,
        "tasks": [
            {
                "task_index": "wave01_task_01",
                "cpu_task": {"number": ...},
                "mem_task": {"size_mb": ...},
                "hdd_task": {"size_mb": ...}
            },
            ...
        ]
    }
    """
    try:
        body = deepcopy(await request.json())
        return await process_batch_body(request, body)
    except Exception as e:
        traceback.print_exc()
        return web.json_response({
            "batch_id": "unknown",
            "success": False,
            "error": 1,
            "message": str(e),
            "results": []
        }, status=400)


# =========================================================
# Startup / Cleanup
# =========================================================
async def on_startup(app: web.Application):
    try:
        manager = ServerManager()
        app['manager'] = manager

        monitors = []
        for ssh_client in manager.worker_ssh_clients:
            try:
                monitors.append(asyncio.create_task(ssh_client.mem_usage_handle()))
                monitors.append(asyncio.create_task(ssh_client.hdd_usage_handle()))
            except Exception as exc:
                print(f"Failed to create hm monitor: {exc}")

        app['hm_monitors'] = monitors
        manager_logger.info("ServerManager initialized successfully.")

    except Exception as e:
        print(f"Error during startup: {e}")
        app['manager'] = None
        app['hm_monitors'] = []
        raise e


async def on_cleanup(app: web.Application):
    try:
        manager: ServerManager = app.get('manager')
        monitor_tasks: List[asyncio.Task] = app.get('hm_monitors', [])

        if manager is not None:
            for client in manager.worker_ssh_clients:
                try:
                    if hasattr(client, 'ssh_client') and client.ssh_client is not None:
                        client.ssh_client.close()
                except Exception as exc:
                    print(f"Error closing ssh client: {exc}")

        for task in monitor_tasks:
            try:
                task.cancel()
            except Exception:
                pass

        for task in monitor_tasks:
            try:
                await task
            except asyncio.CancelledError:
                print("HM monitor task cancelled")
            except Exception as exc:
                print(f"Error awaiting monitor task: {exc}")

    except Exception as e:
        print(f"Error during cleanup: {e}")
        traceback.print_exc()


# =========================================================
# App Init
# =========================================================
def init_app():
    try:
        app = web.Application()
        app.on_startup.append(on_startup)
        app.on_cleanup.append(on_cleanup)

        # compatibility single-task endpoint
        # app.router.add_post('/', handle_request)

        # true batch endpoint
        app.router.add_post('/', handle_batch_request)

        return app
    except Exception as e:
        print(f"Error initializing app: {e}")
        raise e


if __name__ == '__main__':
    try:
        app = init_app()
        web.run_app(app, host='0.0.0.0', port=8081)
    except Exception as e:
        print(f"Error running app: {e}")