# manager_sw.py 
# single worker node

from aiohttp import web
from aiohttp.web_request import Request
from aiohttp.web_response import Response
from copy import deepcopy
from manager_objects import ServerManager, ManagerResponse
import asyncio
import traceback
from tasks.cpu import cpu_task
from tasks.mem import mem_task
from tasks.hdd import hdd_task

from pathlib import Path
import joblib


model_path = Path(__file__).parent / "model" / "xgb.joblib"
if model_path.exists():
    xgb_model = joblib.load(model_path)
else:
    raise FileNotFoundError(f"Model file not found at {model_path}")

def task_class_to_handler(task_class: str):
    try:
        if task_class == "CPU":
            return cpu_task
        elif task_class == "MEM":
            return mem_task
        elif task_class == "HDD":
            return hdd_task
        # add other task classes and their handlers here
        raise ValueError(f"Unknown task class: {task_class}")
    except ValueError as e:
        raise e
    except Exception as e:
        raise e

async def handle_request(request: Request) -> Response:
    try:
        data = deepcopy(await request.json())

        task_class = request.headers.get("task_class", "CPU")
        task_index = request.headers.get("task_index", "unknown")
        # round-robin select server
        manager: ServerManager = request.app.get('manager')
        if manager is None:
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
        return web.json_response(ManagerResponse(
            task_index=task_index,
            task_class=task_class,
            error=1,
            message=str(e),
            success=False,
            response_data=None,
        ), status=400)

    try:
        # find handler for task_class (may raise ValueError for unknown class)
        try:
            handler = task_class_to_handler(task_class)
        except ValueError as ve:
            print(f"Unknown task class requested: {task_class}")
            return web.json_response(ManagerResponse(
                task_index=task_index,
                task_class=task_class,
                error=1,
                message=str(ve),
                success=False,
                response_data=None,
            ), status=400)

        # call the handler and return its JSON-able response
        round_robin_index = await manager.get_and_update_round_robin_index()
        response = await handler(request.headers, data, manager, round_robin_index)
        return web.json_response(response)
    except Exception as e:
        print(f"Error handling request: {e}")
        return web.json_response(ManagerResponse(
            task_index=task_index,
            task_class=task_class,
            error=1,
            message=str(e),
            success=False,
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