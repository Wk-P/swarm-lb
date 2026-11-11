from aiohttp import web
import psutil  # type: ignore
import os
import time
import traceback
import asyncio

# 使用进程池做到串行
from concurrent.futures import ProcessPoolExecutor

executor = ProcessPoolExecutor(max_workers=1)

def get_cpu_times(pid):
    process = psutil.Process(pid)
    cpu_times = process.cpu_times()
    user_times = cpu_times.user
    system_times = cpu_times.system
    
    return user_times, system_times

def is_prime(num):
    if num <= 2:
        return num == 2  # 只有2是质数
    if num % 2 == 0:
        return num == 2
    i = 3
    while i * i <= num:
        if num % i == 0:
            return False
        i += 2
    return True


def prime_count(num, task_index):
    print("PID", os.getpid(), "start", time.time(), "num", num)

    process_start_time = time.perf_counter()

    # get child pid
    child_pid = os.getpid()

    start_user_times, start_system_times = get_cpu_times(child_pid)

    sum = 0
    for i in range(2, num):
        if is_prime(i):
            sum += 1

    # fetch cpu data
    end_user_times, end_system_times = get_cpu_times(child_pid)
    user_times_diff = end_user_times - start_user_times
    system_times_diff = end_system_times - start_system_times

    real_process_time = time.perf_counter() - process_start_time
    contention_time = real_process_time - (user_times_diff + system_times_diff)

    return {
        "task_index": task_index,
        "request_num": num,
        "return_result": sum,
        "user_cpu_time": user_times_diff,
        "system_cpu_time": system_times_diff,
        "real_process_time": real_process_time,
        "contention_time": max(0, contention_time),
        "process_start_time": process_start_time,
        "finish_time": time.perf_counter()
    }

async def handle(request: web.Request):
    try:
        # 请求到达时间（使用 time.time() 以便跨进程对比）
        arrival_timestamp = time.time()
        arrival_time = time.perf_counter()

        data = await request.json()

        received_time = time.perf_counter()
        received_timestamp = time.time()
        json_parse_time = received_time - arrival_time

        # 使用进程池做到单任务串行处理
        loop = asyncio.get_event_loop()
        
        submit_to_executor_time = time.perf_counter()
        submit_timestamp = time.time()

        result = await loop.run_in_executor(
            executor,
            prime_count,
            data['number'], data.get('task_index')
        )

        executor_complete_time = time.perf_counter()
        complete_timestamp = time.time()
        
        # 注意：process_start_time 是在子进程中用 perf_counter() 记录的
        # 不能直接用它减去主进程的时间，因为 perf_counter 在不同进程中基准点不同
        # 正确的计算方式：
        real_process_time = result['real_process_time']
        total_executor_time = executor_complete_time - submit_to_executor_time
        queue_waiting_time = total_executor_time - real_process_time

        # 真实处理开始时间
        result['actual_start_time'] = submit_to_executor_time + queue_waiting_time
        result['queue_waiting_time'] = queue_waiting_time
        result['worker_arrival_time'] = arrival_time
        result['worker_received_time'] = received_time
        result['worker_json_parse_time'] = json_parse_time
        result['worker_submit_to_executor_time'] = submit_to_executor_time
        result['worker_executor_complete_time'] = executor_complete_time
        result['worker_total_executor_time'] = total_executor_time
        
        # 添加 time.time() 时间戳，用于跨进程对比和 Manager 端日志
        result['worker_arrival_timestamp'] = arrival_timestamp
        result['worker_received_timestamp'] = received_timestamp
        result['worker_submit_timestamp'] = submit_timestamp
        result['worker_complete_timestamp'] = complete_timestamp

        return web.json_response(result)
    except Exception as e:
        error_message = traceback.format_exc()
        return web.json_response({"error": error_message}, status=500)
    

def create_app():
    app = web.Application()
    
    # 添加路由
    app.router.add_post('', handle)

    async def cleanup(app):
        executor.shutdown(wait=True)
    app.on_cleanup.append(cleanup)

    return app

if __name__ == '__main__':
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=8081)
