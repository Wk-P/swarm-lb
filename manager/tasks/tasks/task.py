from manager_objects import ServerManager
import aiohttp

async def task_request(headers: dict, data: dict, manager: ServerManager, worker_index: int) -> dict:
    session: aiohttp.ClientSession = manager.http_clients[worker_index]
    url = f"{manager.worker_urls[worker_index]}{manager.task_urls.get(headers.get('task_class', 'CPU'), '/cpu/')}"
    print(f"[task_request] Sending request to {url} with task_index={headers.get('task_index', 'unknown')}")
    async with session.post(url, headers=headers, json=data, timeout=0) as resp:
        resp.raise_for_status()
        response_data = await resp.json()
        response_data['response_host'] = resp.host
        if resp.ok:
            return response_data
        else:
            if resp.status == 507:
                return {"error": "not enough resource on worker node"}
            else:
                raise Exception(f"Upstream request failed with status {resp.status}: {response_data}")