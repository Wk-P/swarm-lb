from tracemalloc import start
from aiohttp import web
import asyncio
from concurrent.futures import ThreadPoolExecutor
from time import perf_counter

thread_executor = ThreadPoolExecutor(max_workers=1)

def is_prime(n: int) -> bool:
    if n <= 1:
        return False
    for i in range(2, int(n**0.5) + 1):
        if n % i == 0:
            return False
    return True

def compute_primes_up_to(n: int) -> list[int]:
    start_time = perf_counter()
    count = 0
    for num in range(2, n + 1):
        if is_prime(num):
            count += 1
    return count, perf_counter() - start_time


async def handle_request(request: web.Request) -> web.Response:
    data = await request.json()

    number = data.get("number", 0)

    # get loop from asyncio
    loop = asyncio.get_event_loop()
    
    # calculate the processed time in thread pool
    prime_count, computation_time = await loop.run_in_executor(thread_executor, compute_primes_up_to, number)
    processing_time = computation_time

    return web.json_response({"result": prime_count, "task_index": data["task_index"], "processing_time": processing_time})

def init_app():
    app = web.Application()
    app.router.add_post('/', handle_request)
    return app
if __name__ == '__main__':

    app = init_app()
    web.run_app(app, host='0.0.0.0', port=8080)