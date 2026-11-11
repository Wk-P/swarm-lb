from aiohttp import web
from functools import wraps
import asyncio
import os
import mmap
from concurrent.futures import ThreadPoolExecutor
from time import perf_counter
from pathlib import Path
import shutil
import copy
from functools import partial
import psutil

# Thread pool for blocking I/O operations
thread_executor = ThreadPoolExecutor(max_workers=3)

# Concurrency control: allow only 1 task executing at a time per worker
MAX_CONCURRENT_TASKS = 1
task_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

def timer(func):
    """Decorator that supports both sync and async functions.

    For coroutine functions it returns an async wrapper that awaits the
    function and measures elapsed time. For regular functions it returns
    a sync wrapper.
    """
    @wraps(func)
    def _sync_wrapper(*args, **kwargs):
        start_time = perf_counter()
        result = func(*args, **kwargs)
        end_time = perf_counter()
        elapsed_time = end_time - start_time
        return result, elapsed_time
    return _sync_wrapper

@timer
def cpu_task(n: int) -> tuple[dict, float]:
    def is_prime(n: int) -> bool:
        if n <= 1:
            return False
        for i in range(2, int(n**0.5) + 1):
            if n % i == 0:
                return False
        return True

    count = 0
    for num in range(2, n + 1):
        if is_prime(num):
            count += 1

    print(f"[CPU] Found {count} prime numbers up to {n}")
    return {"prime_count": count}

@timer
def mem_task(size_mb: int) -> tuple[dict, float]:
    import gc

    size = size_mb * 1024 * 1024
    print(f"[MEM] Allocating {size_mb} MB ...")
    data = bytearray(size)
    print(f"[MEM] Allocation down")
	
    # Access memory to ensure allocation
    for i in range(0, len(data), 4096):
        data[i] = 1
    print(f"[MEM] Accessed memory")

    # Let Python garbage collector handle cleanup
    del data
    gc.collect(generation=2)  # Full GC to ensure memory is freed
    print(f"[MEM] Freed memory")

    return {"allocated_size_mb": size_mb}

# Pre-allocate write buffer once at module load — avoid re-allocation on every task call
# Use anonymous mmap: gives page-aligned (4096B) memory required by O_DIRECT
# 512MB chunks: optimal for NVMe sequential writes
_HDD_CHUNK_SIZE_MB = 512
_HDD_CHUNK_SIZE = _HDD_CHUNK_SIZE_MB * 1024 * 1024
_HDD_MMAP_BUF = mmap.mmap(-1, _HDD_CHUNK_SIZE)  # anonymous mmap, zero-initialized, page-aligned

def hdd_delete_task(path) -> bool:
    _path = Path(path)
    _path.unlink()
    return True 

def hdd_write_task(path, size_mb=1000) -> dict:
    # O_DIRECT: bypass OS page cache entirely → true physical NVMe I/O every call
    # No fdatasync needed: O_DIRECT writes go straight to storage controller
    # Requirements: buffer address + size must be aligned to 4096B (mmap guarantees this)
    chunk_size_mb = _HDD_CHUNK_SIZE_MB
    chunk_size = _HDD_CHUNK_SIZE

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_DIRECT, 0o644)
    try:
        full_chunks = size_mb // chunk_size_mb
        remainder_mb = size_mb % chunk_size_mb

        _HDD_MMAP_BUF.seek(0)
        for _ in range(full_chunks):
            _HDD_MMAP_BUF.seek(0)
            os.write(fd, _HDD_MMAP_BUF)

        if remainder_mb > 0:
            rem_size = remainder_mb * 1024 * 1024  # always a multiple of 1MB = 4096B aligned
            rem_buf = mmap.mmap(-1, rem_size)       # page-aligned anonymous mmap
            try:
                os.write(fd, rem_buf)
            finally:
                rem_buf.close()
    finally:
        os.close(fd)

    return {"written_size": size_mb}

@timer
def hdd_task(path, size_mb=1000) -> tuple[dict, float]:
    _path = Path(path)
    print(f"[HDD] Writing {size_mb} MB to {path} ...")
    written_size = hdd_write_task(path, size_mb)
    file_size_mb = _path.stat().st_size / (1024 * 1024)
    print(f"[HDD] Write down. File size: {file_size_mb} MB")
    print(f"[HDD] Deleting file at {path} ...")
    hdd_delete_task(path)
    print(f"[HDD] File deleted")
    return {"written_size": written_size, "file_size_mb": file_size_mb, "requested_size_mb": size_mb, "deleted": True}


def handle_request_cpu(data: dict) -> web.Response:
    task_index = data.get("task_index", "unknown")
    # Support both "number" and "number" field names
    number = data.get("number")
    number = int(number)  # Ensure integer type
    print(f"[CPU] Received request with task_index={task_index} number={number}")
    
    # calculate the processed time in thread pool
    prime_count, computation_time = cpu_task(number)
    processing_time = computation_time
    return {"result": prime_count, "task_index": task_index, "processing_time": processing_time}


def handle_request_mem(data: dict) -> web.Response:
    def free_memory() -> int:
        """Return available memory (MB)"""
        # In modern Docker + Pi 5 environments, psutil.virtual_memory().available
        # automatically takes the minimum of (container quota remaining).
        return psutil.virtual_memory().available // (1024 * 1024)

    task_index = data.get("task_index", "unknown")
    size_mb = int(data.get("size_mb", 100))  # Ensure integer type
    print(f"[MEM] Received request with task_index={task_index} size_mb={size_mb}")

    # Record mem_free before allocation (no pre-check, Manager already validated)
    mem_free_before = free_memory()
    
    # Execute task directly - Manager already checked resources
    allocated_size, computation_time = mem_task(size_mb)
    processing_time = computation_time
    return {"result": allocated_size, "task_index": task_index, "processing_time": processing_time, "mem_free_before": mem_free_before}


def handle_request_hdd(data: dict) -> web.Response:
    task_index = data.get("task_index", "unknown")
    import time
    task_index = data.get("task_index", "unknown")
    size_mb = int(data.get("size_mb", 100))  # Ensure integer type
    print(f"[HDD] Received request with task_index={task_index} size_mb={size_mb}")

    # Use unique file per task to enable true concurrent HDD operations
    timestamp = int(time.time() * 1000000)  # microsecond precision
    path = f"/app/data/hdd_test_{task_index}_{timestamp}"
    # Record hdd_free before writing (no pre-check, Manager already validated)
    hdd_free_before = shutil.disk_usage(Path(path).parent.as_posix()).free // (1024 * 1024)
    
    # Execute task directly - Manager already checked resources
    written_size, computation_time = hdd_task(path, size_mb)
    processing_time = computation_time
    return {"result": written_size, "task_index": task_index, "processing_time": processing_time, "hdd_free_before": hdd_free_before}


async def handle_request(request: web.Request) -> web.Response:
    # Concurrency control: only MAX_CONCURRENT_TASKS can execute simultaneously
    async with task_semaphore:
        # get task infor from data body, without task class, just call different handlers
        request_data: dict = copy.deepcopy(await request.json())
        hdd_task_data = request_data.get("hdd_task")
        mem_task_data = request_data.get("mem_task")
        cpu_task_data = request_data.get("cpu_task")

        # Submit tasks to thread pool concurrently
        loop = asyncio.get_event_loop()
        tasks = {
            "hdd_response": None,
            "mem_response": None,
            "cpu_response": None,
        }

        if hdd_task_data is not None:
            # hdd has type for [ read, write, delete ]
            tasks["hdd_response"] = loop.run_in_executor(executor=thread_executor, func=partial(handle_request_hdd, hdd_task_data))
        if mem_task_data is not None:
            tasks["mem_response"] = loop.run_in_executor(executor=thread_executor, func=partial(handle_request_mem, mem_task_data))
        if cpu_task_data is not None:
            tasks["cpu_response"] = loop.run_in_executor(executor=thread_executor, func=partial(handle_request_cpu, cpu_task_data))
        
        # Collect responses
        combined_response = {
            "cpu_response": None,
            "mem_response": None,
            "hdd_response": None,
        }
        for key, fut in tasks.items():
            if fut is not None:
                resp_data = await fut
                combined_response[key] = resp_data
        
        # Calculate total_processing_time as max of individual processing times (pure processing, no lock wait)
        processing_times = []
        for resp_key in ["cpu_response", "mem_response", "hdd_response"]:
            if combined_response[resp_key] is not None:
                pt = combined_response[resp_key].get("processing_time", 0)
                if pt > 0:
                    processing_times.append(pt)
        
        combined_response["total_processing_time"] = max(processing_times) if processing_times else 0
        
        return web.json_response(combined_response)

def init_app():
    app = web.Application()
    app.router.add_post('/task/', handle_request)
    return app

def cleanup_hdd_data_folder():
    """Clean up HDD data folder on startup to avoid interference from previous tests"""
    data_folder = Path("/app/data")
    if data_folder.exists():
        print(f"[HDD] Cleaning up data folder: {data_folder}")
        for file in data_folder.glob("*"):
            try:
                if file.is_file():
                    file.unlink()
                    print(f"[HDD] Deleted file: {file}")
                elif file.is_dir():
                    shutil.rmtree(file)
                    print(f"[HDD] Deleted directory: {file}")
            except Exception as e:
                print(f"[HDD] Error deleting {file}: {e}")
        print(f"[HDD] Cleanup complete")
    else:
        print(f"[HDD] Data folder does not exist, creating: {data_folder}")
        data_folder.mkdir(parents=True, exist_ok=True)

if __name__ == '__main__':
    # Clean up HDD data folder on startup
    cleanup_hdd_data_folder()
    
    free_memory_mb = psutil.virtual_memory().available // (1024 * 1024)
    total_memory_mb = psutil.virtual_memory().total // (1024 * 1024)
    
    print(f"[MEM] System Total: {total_memory_mb} MB")
    print(f"[MEM] Initial Available: {free_memory_mb} MB")
    
    app = init_app()
    web.run_app(app, host='0.0.0.0', port=8080)