import time
import asyncio
import traceback
import psutil
import os
import json


# async redis library
import redis.asyncio as aioredis   # type: ignore

import logging

class Task:
    def __init__(self, data_str: str, redis: aioredis.Redis):
        self.data: dict = json.loads(data_str)
        self.data_str = data_str
        self.task_created_time = time.time()
        self.task_id = self.data.get('task_id')
        self.redis_client = redis

    async def notice_task_start_time(self, worker_id):
        notice = json.dumps({
            "task_id": self.task_id,
            "worker_id": worker_id,
            "real_all_waiting_time_0": time.time() - self.data.get('manager_receive_time')
        })
        await self.redis_client.rpush('task_start_notice_queue', notice)

class Result:
    def __init__(self, data: dict):
        self.data = data
        self.task_id = data.get('task_id')
        self.data_str = json.dumps(data)

from concurrent.futures import ThreadPoolExecutor

executor = ThreadPoolExecutor(max_workers=1)

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

logger.debug("This is a debug message")

redis_client = aioredis.from_url('redis://<MANAGER_IP>:6379', decode_responses=True)


# Helper to get CPU times
def get_cpu_times(pid):
    process = psutil.Process(pid)
    cpu_times = process.cpu_times()
    user_times = cpu_times.user
    system_times = cpu_times.system
    return user_times, system_times

# Function to check prime number
def is_prime(n):
    if n < 2:
        return n == 2
    if n % 2 == 0:
        return n == 2
    i = 3
    while i * i <= n:
        if n % i == 0:
            return False
        i += 2
    return True

# Count prime numbers
def count_prime_number(n):
    count = 0
    for i in range(n):
        if is_prime(i):
            count += 1
    return count

# Process task and return performance data
def process(t: Task):
    n = t.data.get('number', 0)
    try:
        start_process_time = time.time()

        # get child pid
        child_pid = os.getpid()

        start_user_times, start_system_times = get_cpu_times(child_pid)

        result = count_prime_number(n)

        # fetch cpu data
        end_user_times, end_system_times = get_cpu_times(child_pid)
        user_times_diff = end_user_times - start_user_times
        system_times_diff = end_system_times - start_system_times

        real_process_time = time.time() - start_process_time
        contention_time = real_process_time - (user_times_diff + system_times_diff)

        cpu_time_spent = user_times_diff + system_times_diff
        cpu_usage_percent = float(cpu_time_spent / real_process_time)

        return Result({
            "task_id": t.data.get('task_id'),
            "request_number": n,
            "result": result,
            "start_process_time": start_process_time,
            "waiting_on_worker": start_process_time - t.task_created_time,
            "real_all_waiting_time": start_process_time - t.data.get('manager_receive_time'),
            "real_process_time": real_process_time,
            "user_cpu_time": user_times_diff,
            "system_cpu_time": system_times_diff,
            "contention_time": contention_time,
            "cpu_spent_usage": cpu_usage_percent,
        })
    except Exception as e:
        return Result({
            "error": traceback.format_exc()
        })


# Function to process tasks asynchronously
async def work_loop():
    global redis_client, executor

    logger.info(f"Work loop started!")
    worker_id = os.getenv('WORKER_ID', "unknown-worker")

    if worker_id == 'unknown-worker':
        # If worker_id is invalid, push an error message to the result queue
        error_msg = {"error": "worker_id is invalid."}
        await redis_client.rpush('result_queue', json.dumps(error_msg))
        return

    try:
        while True:
            # Name of the task queue for this worker
            task_queue_name = f"task_queue_{worker_id}"
            t = await redis_client.blpop(task_queue_name)  # Block until a task is available

            # Parse the task data and extract task_id
            task = Task(t[1], redis_client)  # Assume task data is in JSON format
            task_id = task.data.get("task_id")  # Extract task_id

            if not task_id:
                # Log if task does not contain a valid task_id
                logger.error(f"Task does not contain a valid task_id.")
                continue
            logger.info(f"Processing task {task_id}.")

            await task.notice_task_start_time(worker_id=worker_id)

            # Use the executor to process the task asynchronously
            # result = await asyncio.get_event_loop().run_in_executor(executor, process, task)
            
            result = process(task)


            # Package the result along with the task_id and push it to the result queue
            await redis_client.rpush(f'result_queue_{worker_id}', result.data_str)
            logger.info(f"Task {task_id} processed and result pushed to result_queue.")

    except asyncio.CancelledError:
        logger.info('Worker shutdown.')
    finally:
        # Close the Redis connection when done
        await redis_client.close()
        logger.info('Redis connection closed.')



if __name__ == '__main__':
    asyncio.run(work_loop())
