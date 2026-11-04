import asyncio
from functools import wraps
from math import e, exp
import aiohttp
from aiohttp import ClientSession, ClientTimeout
from typing import Any, Dict
from datetime import datetime
import json
import random
from pathlib import Path
from uuid import uuid4
import time

SUM_FAILURE_REQUESTS = 0
SUM_SUCCESS_REQUESTS = 0

# set client class for data collection
class DataClient:
    def __init__(self, base_url: str):
        self.target_url = base_url
        self.session = ClientSession(timeout=ClientTimeout(total=0), connector=aiohttp.TCPConnector(limit=5000))

    # send request
    async def fetch_data(self, headers, data) -> Dict[str, Any]:
        global SUM_FAILURE_REQUESTS, SUM_SUCCESS_REQUESTS
        async with self.session.post(self.target_url, headers=headers, json=data) as response:
            response_data = dict(await response.json())
            print(f"[function] (fetch_data) response_data: {response_data}")
            # check key word whether has all keywords
            if response_data.get("response_data") is None:
                SUM_FAILURE_REQUESTS += 1
                print(f"[fetch_data] response_data: {response_data}")
                raise Exception("Invalid response: missing 'response_data' field")

            if response_data.get("error") == 0:
                SUM_SUCCESS_REQUESTS += 1
                return response_data
            else:
                SUM_FAILURE_REQUESTS += 1
                # check error message
                raise Exception(response_data.get("message"))
        
    async def close(self):
        await self.session.close()


def generate_task_data(task_class: str, **kwargs):
    if task_class not in ['hdd', 'mem', 'cpu']:
        raise ValueError(f"Invalid task_class: {task_class}. Must be one of 'HDD', 'MEM', 'CPU'.")

    # Map task_class string to integer
    try:
        size_mb = kwargs.get('size_mb', None)
        number = kwargs.get('number', None)
        task_data = {
            "size_mb": size_mb,
            "task_class": task_class,
            'number': number,
        }
    except Exception:
        raise ValueError("Invalid parameters for task data generation.")
    
    task_index = uuid4().hex+str(int(time.time()*10000))
    data = {
        "task_index": task_index,
        **task_data,
    }
    headers = {
        "task_index": task_index,
        "Content-Type": "application/json",
    }
    # debug: show generated task
    print(f"[generate_task_data] task_index={data['task_index']} task_class={task_class} size_mb={data.get('size_mb', 'N/A')} number={data.get('number', 'N/A')}")
    return headers, data


def save_results_to_file(data_list: list[Dict[str, Any]], results: Dict[str, Any], task_class: str, folder_arg: dict) -> Path:
    # save results to file 
    # style:
    # check task_index in data and results
    #   request number from data
    #   response number from results
    # add request number to result['request_number']
    # add response number to result['response_number']

    filename = f"results_{task_class}.json"

    # count startwith "data" folder sum and add index to "data_t3"
    parent_path: Path = Path(__file__).parent / 'data_t3' / folder_arg.get("folder")
    parent_path.mkdir(parents=True, exist_ok=True)

    filepath = parent_path / filename

    # prepare results
    output = dict()

    exclude_key_names = {"success", "error", "message", "task_index", "result", "task_class"}

    for req in data_list:
        task_index = req.get("task_index")
        matching_result: dict = results.get(task_index)
        if matching_result:
            # judge task_class to get task_data
            # task data for all k, v but whatever it is
            tasks_data = { k: v for k, v in matching_result.items() if k not in exclude_key_names }
            print(f"tasks_data: {tasks_data}")
            print(f"[save_results_to_file] tasks_data: {tasks_data}")
            # add to output
            for k, v in tasks_data.items():
                if output.get(k):
                    output[k].append(v)
                else:
                    output[k] = [v]

    # If file exists, try to merge by extending lists for matching keys
    final = output
    if filepath.exists():
        try:
            with open(filepath, 'r', encoding='utf-8') as rf:
                existing = json.load(rf)
            if isinstance(existing, dict):
                for k, v in output.items():
                    if isinstance(v, list):
                        if k in existing and isinstance(existing[k], list):
                            existing[k].extend(v)
                        else:
                            existing[k] = v
                final = existing
        except Exception as e:
            # If reading fails, overwrite with current output (but report)
            print(f"[save_results_to_file] Failed to read existing file, will overwrite: {e}")
            final = output

    # Atomic write: write to temp file then replace
    tmp = filepath.with_suffix(filepath.suffix + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as wf:
        json.dump(final, wf, indent=4)
    try:
        tmp.replace(filepath)
    except Exception:
        # fallback to non-atomic replace
        with open(filepath, 'w', encoding='utf-8') as wf:
            json.dump(final, wf, indent=4)

    print(f"Saved results to {filepath}")
    return filepath


def tasks(task_func):
    @wraps(task_func)
    async def wrapper(client: DataClient, *args, **kwargs):
        tasks = []
        data_list = []
        return await task_func(client, data_list, tasks, *args, **kwargs)
    return wrapper


@tasks
async def hdd_tasks(client: DataClient, data_list: list[Dict[str, Any]], tasks: list[asyncio.Task], folder_arg: dict = None, task_count: int = 5, loop: int = 1):
    for _ in range(loop):
        for i in range(task_count):
            size_mb=random.randint(10, 500)
            headers, data = generate_task_data(task_class="hdd", size_mb=size_mb)
            task = asyncio.create_task(client.fetch_data(headers, data))    
            tasks.append(task)
            print(f"[hdd_task] enqueued task {i+1}/{task_count} id={data['task_index']} size_mb={data['size_mb']}")
            data_list.append(data)

        # gather results
        results: list[dict[str, Any]] = await asyncio.gather(*tasks, return_exceptions=True)
        return_results= dict()

        # collect results
        for result in results:
            if not isinstance(result, dict):
                print(f"[hdd_task] encountered exception: {result}")
                continue

            task_index = result.get("task_index")
            if task_index:
                print(f"[hdd_task] result: {result}")
                inner_response = result.pop("response_data")
                print(f"[hdd_task] inner_response: {inner_response}")
                return_results[task_index] = {**result, **inner_response}
            print(f"[hdd_task] received result for task_index={result['task_index']}")
        
        # save results to file
        path = save_results_to_file(data_list=data_list, results=return_results, task_class="hdd", folder_arg=folder_arg)
        # print log
        print(f"Sent {len(data_list)} requests at once with speed fast")
        print(f"Success save file to path: {path}")
        # clear lists so next loop doesn't re-process already completed tasks
        tasks.clear()
        data_list.clear()


@tasks
async def mem_tasks(client: DataClient, data_list: list[Dict[str, Any]], tasks: list[asyncio.Task], folder_arg: dict = None, task_count: int = 10, loop: int = 1):
    for _ in range(loop):
        for i in range(task_count):
            headers, data = generate_task_data(task_class="mem", size_mb=random.randint(200, 1000))
            task = asyncio.create_task(client.fetch_data(headers, data))
            tasks.append(task)
            print(f"[mem_task] enqueued task {i+1}/{task_count} id={data['task_index']} size_mb={data['size_mb']}")
            data_list.append(data)

        # gather results
        results: list[dict] = await asyncio.gather(*tasks, return_exceptions=True)
        return_results = dict()

        # collect results
        for result in results:
            if isinstance(result, Exception):
                print(f"[mem_task] encountered exception: {result}")
                continue
            task_index = result.get("task_index")
            if task_index:
                inner_response = result.pop("response_data")
                print(f"[mem_task] inner_response: {inner_response}")
                return_results[task_index] = {**result, **inner_response}
            print(f"[mem_task] received result for task_index={result['task_index']}")

        path = save_results_to_file(data_list=data_list, results=return_results, task_class="mem", folder_arg=folder_arg)
        # print log
        print(f"Sent {len(data_list)} requests at once with speed fast")
        print(f"Success save file to path: {path}")

        # clear lists to avoid re-processing completed tasks in following loops
        tasks.clear()
        data_list.clear()


@tasks
async def cpu_tasks(client: DataClient, data_list: list[Dict[str, Any]], tasks: list[asyncio.Task], folder_arg: dict = None, task_count: int = 5, loop: int = 1):
    for _ in range(loop):
        # batch_size = random.randint(10, 20)
        batch_size = task_count
        for i in range(batch_size):  # send batch_size requests at once
            number = random.randint(10000, 500_000)
            headers, data = generate_task_data(task_class="cpu", number=number)
            data_list.append(data)
            task = asyncio.create_task(client.fetch_data(headers, data))
            tasks.append(task)
            print(f"[fast] enqueued task {i+1}/{batch_size} id={data['task_index']} size_mb={data.get('size_mb', 'N/A')}")

        # gather results
        results: list[dict] = await asyncio.gather(*tasks, return_exceptions=True)
        return_results = dict()

        # collect results
        for result in results:
            if isinstance(result, Exception):
                print(f"[cpu_task] encountered exception: {result}")
                continue
            task_index = result.get("task_index")
            if task_index:
                inner_response = result.pop("response_data")
                print(f"[cpu_task] inner_response: {inner_response}")
                return_results[task_index] = {**result, **inner_response}
            print(f"[cpu_task] received result for task_index={result['task_index']}")
        path = save_results_to_file(data_list=data_list, results=return_results, task_class="cpu", folder_arg=folder_arg)
        # print log
        print(f"Sent {len(data_list)} requests at once with speed fast")
        print(f"Success save file to path: {path}")

        # clear lists so subsequent loops start fresh
        tasks.clear()
        data_list.clear()



async def main():
    # create client instance
    url = "http://x.x.x.x:8080/"
    client = DataClient(url)
    folder = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")

    is_fixed = False
    

    # single task type parameters
    if not is_fixed:
        task_count = 200
        loop_count = 100

        await hdd_tasks(client, folder_arg={"folder": folder}, task_count=task_count, loop=loop_count)
        await mem_tasks(client, folder_arg={"folder": folder}, task_count=task_count, loop=loop_count)
        await cpu_tasks(client, folder_arg={"folder": folder}, task_count=task_count, loop=loop_count)


    # fixed task type parameters
    else:
        task_funcs = [
            hdd_tasks,
            mem_tasks,
            cpu_tasks,
        ]

        tasks_sum = 10

        # randomly select task functions to execute, once for one task
        for _ in range(tasks_sum):
            func = random.choice(task_funcs)
            await func(client, folder_arg={"folder": folder}, task_count=1, loop=1)

    # close client session
    await client.close()

    # finish
    print("Data collection completed.")

if __name__ == "__main__":
    print("Starting data collection...")
    start_time = datetime.now()
    print(f"Start Datetime: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    asyncio.run(main())
    end_time = datetime.now()
    print(f"End Datetime: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("Data collection finished.")
    print(f"Total successful requests: {SUM_SUCCESS_REQUESTS}")
    print(f"Total failed requests: {SUM_FAILURE_REQUESTS}")

    # calculate and print accurate run time
    run_seconds = (end_time - start_time).total_seconds()
    run_hours = run_seconds / 3600
    run_minutes = run_seconds / 60
    if run_hours < 1:
        if run_minutes < 1:
            print(f"Run time: {run_seconds:.2f} seconds")
        else:
            print(f"Run time: {run_minutes:.2f} minutes ({run_seconds:.2f} seconds)")
    else:  
        print(f"Run time: {run_hours:.2f} hours ({run_minutes:.2f} minutes, {run_seconds:.2f} seconds)")