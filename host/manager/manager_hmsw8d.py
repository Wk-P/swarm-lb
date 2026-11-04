# manager_sw.py 
# single worker node

from aiohttp import web
from aiohttp.web_request import Request
from aiohttp.web_response import Response
from copy import deepcopy
from manager_objects import ServerManager, ManagerResponse
import asyncio
import traceback
import aiohttp

from pathlib import Path
import joblib
import numpy as np
import time
import logging

import LB_algorithm.double_dqn as DQN               
import LB_algorithm.direct as algor1               
import LB_algorithm.sd_min as algor2                
import LB_algorithm.min_min as min_min              
import LB_algorithm.round_robin as round_robin      

model_path = Path(__file__).parent / "model" / "xgb8d.joblib"
if model_path.exists():
    xgb_model = joblib.load(model_path)
else:
    raise FileNotFoundError(f"Model file not found at {model_path}")

# LOG file
log_dir = Path.cwd() / 'log'
log_dir.mkdir(exist_ok=True)  # 确保日志目录存在

logger_names = ['dqn-reward', 'running-tasks', 'manager-events']
for logger_name in logger_names:
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    log_path = log_dir / f'{logger_name}.log'
    handler = logging.FileHandler(log_path, mode='w', encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logger.addHandler(handler)
    logger.propagate = False

    print(f"Logger '{logger_name}' initialized at {log_path}")

manager_logger = logging.getLogger('manager-events')
reward_logger = logging.getLogger('dqn-reward')
running_tasks_logger = logging.getLogger('running-tasks')
manager_logger.info("Manager service started.")


EPISODE = 0
# EPISODE_ADD_CHECK = -1
RUNNING_TASKS = 0
RUNNING_WORKERS = []

lock = asyncio.Lock()

async def choose_url_algorithm(name=None, data=None, response_times=None):
    global EPISODE

    if name == 'algor1':  # 알고리즘 1
        # choose worker
        # worker_index = algor1.Optimization_Model1(response_time, new_task, worker_names_obj, data)
        # return worker_names_obj[worker_index]

        worker_index = algor1.Optimization_Model1(response_times, data)
        return worker_index

    elif name == 'algor2':  # 알고리즘 2  (분배 비율의 표준평차 최소화)
        # choose worker
        worker_index = algor2.Optimization_Model2(response_times, data)
        return worker_index

    elif name == 'dqn':  # DQN 알고리즘
        epsilon = DQN.epsilon_start
        # choose worker
        worker_index = DQN.DQN_Model(response_times, data)[0]
        reward = DQN.DQN_Model(response_times, data)[1]  # 긍정적인 보상 함수 사용
        
        # logging
        if reward_logger:
            reward_logger.info(f'dqn-reward: {reward}')

        # new_task.reward += reward  # 보상 누적

        epsilon = max(DQN.epsilon_end, epsilon * DQN.epsilon_decay)

        async with lock:
            if RUNNING_TASKS == 0:
                EPISODE += 1

                if EPISODE % DQN.target_update == 0:
                    DQN.update_target_network()
                    EPISODE = 0

        return worker_index


    elif name == 'round_robin':  # 알고리즘 1
        worker_index = round_robin.round_robin(response_times, data)
        return worker_index

    elif name == 'min-min':  # 알고리즘 1
        worker_index = min_min.min_min(response_times, data)
        return worker_index
    


async def handle_request(request: Request) -> Response:
    global RUNNING_WORKERS, RUNNING_TASKS
    try:
        data: dict = deepcopy(await request.json())
        task_index = request.headers.get("task_index", "unknown")
        task_class = data.get("task_class", "unknown")
        
        running_tasks_logger.info(f"Received request: task_index={task_index}, task_class={task_class}")

        # Increment RUNNING_TASKS safely
        # sum of running tasks add while received the request from client
        async with lock:
            RUNNING_TASKS += 1

        # Get manager instance
        manager: ServerManager = request.app.get('manager')
        if manager is None:
            async with lock:
                RUNNING_TASKS = 0
            return web.json_response(ManagerResponse(
                task_index=task_index,
                task_class=task_class,
                error=1,
                message="service not ready",
                success=False,
                response_data=None,
            ), status=503)
        
    except Exception as e:
        print(f"Error parsing request: {e}")
        async with lock:
            RUNNING_TASKS -= 1
        return web.json_response(ManagerResponse(
            task_index="unknown",
            task_class=task_class if 'task_class' in locals() else "unknown",
            error=1,
            message=str(e),
            success=False,
            response_data=None,
        ), status=400)

    try:
        # Extract task-specific features based on task_class
        if task_class == "cpu":
            cpu_number = float(data.get('number', float('nan')))
            size_mb = cpu_number  # CPU uses 'number' as size
            mem_size_mb = float('nan')
            hdd_size_mb = float('nan')
        elif task_class == "mem":
            size_mb = float(data.get('size_mb', float('nan')))
            mem_size_mb = size_mb
            hdd_size_mb = float('nan')
        elif task_class == "hdd":
            size_mb = float(data.get('size_mb', float('nan')))
            hdd_size_mb = size_mb
            mem_size_mb = float('nan')
        else:
            # Unknown task_class, set all to NaN
            size_mb = float('nan')
            mem_size_mb = float('nan')
            hdd_size_mb = float('nan')
        
        # ===== Step 1 & 2: Collect features and predict for each worker =====
        predictions = {}  # Store predictions: {"0": prediction_0, "1": prediction_1, ...}
        all_features = {}  # Store features for each worker for debugging
        
        # Use lock to protect WORKERS global variable
        async with lock:
            # Iterate through all workers
            for worker_index in range(len(manager.workers)):
                worker_info = manager.workers[worker_index]
                worker_ssh_client = manager.worker_ssh_clients[worker_index]
            
                # Prepare data dictionary with NaN as default for missing values
                data_xgb = {
                    'task_class': task_class == "cpu" and 1 or (task_class == "mem" and 2 or (task_class == "hdd" and 3 or float('nan'))),
                    'number': float(cpu_number) if task_class == "cpu" else float('nan'),
                    'memory_limit_mb': float(worker_info.get('memory_limit_mb', float('nan'))),
                    'cpu_limit_cores': float(worker_info.get('cpus_limit_cores', float('nan'))),
                    'hdd_limit_mb': float(worker_info.get('hdd_limit_mb', float('nan'))) if worker_info.get('hdd_limit_mb') is not None else float('nan'),
                    'size_mb': size_mb,  # from request data, same for all workers
                    'hdd_free_before': (worker_ssh_client.max_hdd_usage - worker_ssh_client.hdd_usage) / (1024 * 1024) if worker_ssh_client.max_hdd_usage > 0 else float('nan'),  # convert to MB
                    'mem_free_before': (worker_ssh_client.max_mem_usage - worker_ssh_client.mem_usage) / (1024 * 1024) if worker_ssh_client.max_mem_usage > 0 else float('nan'),  # convert to MB
                }

                data_xgb1 = data_xgb.copy()
                data_xgb1.pop('size_mb')

                data_xgb1['mem_size_mb'] = mem_size_mb # if task_class == "mem" else float('nan')
                data_xgb1['hdd_size_mb'] = hdd_size_mb # if task_class == "hdd" else float('nan')

                RUNNING_WORKERS.append(data_xgb1)

                # Build input array for XGBoost model
                X_xgb = np.array([[
                    data_xgb['task_class'],
                    data_xgb['number'],
                    data_xgb['memory_limit_mb'],
                    data_xgb['cpu_limit_cores'],
                    data_xgb['hdd_limit_mb'],
                    data_xgb['size_mb'],
                    data_xgb['hdd_free_before'],
                    data_xgb['mem_free_before']
                ]], dtype=np.float32)
            
                # Get prediction from XGBoost model
                xgb_prediction = xgb_model.predict(X_xgb)[0]  # Get the first prediction result (float)
            
                # Store prediction result
                predictions[str(worker_index)] = float(xgb_prediction)
                all_features[str(worker_index)] = data_xgb
            
                print(f"Worker {worker_index} - XGBoost Prediction: {xgb_prediction}")
                print(f"Worker {worker_index} - Input features: {data_xgb}")
        
        # ===== Step 3: Process all prediction results =====
        print(f"\nTask {task_index} - All predictions: {predictions}")
        manager_logger.info(f"Task {task_index} - All predictions: {predictions}")

        # TODO: Select the best worker based on predictions
        # selected_worker_index = None

        algo_name = ['algor1']
        # algo_name = ['algor2']
        # algo_name = ['dqn']
        # algo_name = ['round-robin']
        # algo_name = ['min-min']
        if algo_name == 'algor1' or algo_name == 'algor2' or algo_name == 'dqn':
            start_time = time.perf_counter_ns()
            selected_worker_index = (await choose_url_algorithm(name=algo_name, data=RUNNING_WORKERS, response_times=predictions))[0]
            end_time = time.perf_counter_ns()
            cpu_time = end_time - start_time
        else:   # round_robin or min_min
            start_time = time.perf_counter_ns()
            selected_worker_index = await choose_url_algorithm(name=algo_name, data=RUNNING_WORKERS, response_times=predictions)
            end_time = time.perf_counter_ns()
            cpu_time = end_time - start_time
        
        # ===== Step 4: Forward task to selected worker =====
        manager_logger.info(f"Task {task_index} assigned to worker {selected_worker_index} using {algo_name} algorithm.")
        if selected_worker_index is not None:
            try:
                # Get selected worker info
                selected_worker = manager.workers[selected_worker_index]
                session: aiohttp.ClientSession = manager.http_clients[selected_worker_index]
                worker_url = manager.worker_urls[selected_worker_index]['url']
                
                # Construct target URL (using root endpoint since we removed task classification)
                target_url = f"{worker_url}/"
                
                print(f"[Forward] Sending request to {target_url} for task {task_index}")
                
                # Forward request asynchronously
                async with session.post(target_url, headers=request.headers, json=data) as resp:
                    resp.raise_for_status()
                    worker_response = await resp.json()
                    worker_response['response_host'] = str(resp.host)
                    
                    if resp.ok:
                        # Enrich response with XGBoost prediction and worker info
                        worker_response['xgb_prediction'] = predictions.get(str(selected_worker_index))
                        worker_response['selected_worker_index'] = selected_worker_index
                        worker_response['worker_info'] = selected_worker
                        worker_response['all_predictions'] = predictions
                        
                        print(f"[Forward] Task {task_index} completed successfully on worker {selected_worker_index}")
                        
                        # Decrement RUNNING_TASKS before returning
                        async with lock:
                            RUNNING_TASKS -= 1
                        
                        # Return successful response
                        return web.json_response(ManagerResponse(
                            task_index=task_index,
                            task_class="",
                            error=0,
                            message="success",
                            success=True,
                            response_data=worker_response,
                        ))
                    else:
                        # Handle worker error response
                        error_msg = worker_response.get('message', f'Worker returned status {resp.status}')
                        print(f"[Forward] Worker {selected_worker_index} returned error: {error_msg}")
                        
                        async with lock:
                            RUNNING_TASKS -= 1
                        
                        return web.json_response(ManagerResponse(
                            task_index=task_index,
                            task_class="",
                            error=1,
                            message=f"Worker error: {error_msg}",
                            success=False,
                            response_data={
                                'xgb_prediction': predictions.get(str(selected_worker_index)),
                                'selected_worker_index': selected_worker_index,
                                'worker_response': worker_response,
                            },
                        ), status=resp.status)
                        
            except aiohttp.ClientError as ce:
                error_msg = f"Failed to connect to worker {selected_worker_index}: {str(ce)}"
                print(f"[Forward] {error_msg}")
                traceback.print_exc()
                
                async with lock:
                    RUNNING_TASKS -= 1
                
                return web.json_response(ManagerResponse(
                    task_index=task_index,
                    task_class="",
                    error=1,
                    message=error_msg,
                    success=False,
                    response_data={
                        'xgb_prediction': predictions.get(str(selected_worker_index)),
                        'selected_worker_index': selected_worker_index,
                        'all_predictions': predictions,
                    },
                ), status=503)
            
            except Exception as fwd_error:
                error_msg = f"Forward task failed: {str(fwd_error)}"
                print(f"[Forward] {error_msg}")
                traceback.print_exc()
                
                async with lock:
                    RUNNING_TASKS -= 1
                
                return web.json_response(ManagerResponse(
                    task_index=task_index,
                    task_class="",
                    error=1,
                    message=error_msg,
                    success=False,
                    response_data={
                        'xgb_prediction': predictions.get(str(selected_worker_index)),
                        'selected_worker_index': selected_worker_index,
                    },
                ), status=500)
        else:
            # No worker selected, return predictions only
            async with lock:
                RUNNING_TASKS -= 1
            return web.json_response(ManagerResponse(
                task_index=task_index,
                task_class="",
                error=0,
                message="Predictions generated, no worker selected",
                success=True,
                response_data={
                    'predictions': predictions,
                    'all_features': all_features,
                    'worker_count': len(manager.workers),
                },
            ))
    except Exception as e:
        print(f"Error handling request: {e}")
        traceback.print_exc()
        async with lock:
            RUNNING_TASKS -= 1
        return web.json_response(ManagerResponse(
            task_index=task_index,
            task_class="",
            error=1,
            message=str(e),
            success=False,
            response_data=None,
        ), status=500)


async def on_startup(app: web.Application):
    try:
            
        # create and attach a shared ClientSession when the event loop is running
        manager  = ServerManager()
        app['manager'] = manager
        monitors = []
        # create monitor tasks for each ssh client if available
        for ssh_client in manager.worker_ssh_clients:
            try:
                monitors.append(asyncio.create_task(ssh_client.mem_usage_handle()))
                monitors.append(asyncio.create_task(ssh_client.hdd_usage_handle()))
            except Exception as exc:
                print(f"Failed to create hm monitor: {exc}")
        app['hm_monitors'] = monitors
    except Exception as e:
        print(f"Error during startup: {e}")
        # ensure keys exist even on error to avoid KeyError in handlers
        app['manager'] = None
        app['hm_monitors'] = []
        raise e

async def on_cleanup(app: web.Application):
    try:
        manager: ServerManager = app.get('manager')
        monitor_tasks: list[asyncio.Task] = app.get('hm_monitors', [])

        if manager is not None:
            for client in manager.worker_ssh_clients:
                try:
                    if hasattr(client, 'ssh_client') and client.ssh_client is not None:
                        await client.ssh_client.close()
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

def init_app():
    try:
        app = web.Application()
        app.on_startup.append(on_startup)
        app.on_cleanup.append(on_cleanup)
        app.router.add_post('/', handle_request)
        return app
    except Exception as e:
        print(f"Error initializing app: {e}")
        raise e

if __name__ == '__main__':
    try:
        app = init_app()
        web.run_app(app, host='0.0.0.0', port=8080)
    except Exception as e:
        print(f"Error running app: {e}")