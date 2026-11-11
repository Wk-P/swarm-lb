from manager_objects import ServerManager, ManagerResponse
from tasks.task import task_request

async def cpu_task(headers: dict, data: dict, manager: ServerManager, round_robin_index: int):
    try:
        # before-request
        service_name = manager.worker_urls[round_robin_index]['service_name']
        limits = {}

        for limits_info in manager.worker_resources_limits_list:
            if limits_info['service_name'] == service_name: 
                limits = limits_info['limits'][0]
                break
        

        # response
        response = await task_request(headers, data, manager, round_robin_index)
        
        if 'error' in response:
            raise Exception(f"Upstream error: {response.get('message', 'Unknown error')}")
        
        response['number'] = data.get('number', None)

        print(f"[cpu_task] Worker limits: {limits}")
        print(f"[cpu_task] Comparing limit node_host_ip: {limits.get('node_host_ip')} with response_host: {response.get('response_host')}")
        if limits.get('node_host_ip') == response.get('response_host'):
            response['memory_limit_mb'] = limits.get('memory_limit_mb', None)
            response['cpu_limit_cores'] = limits.get('cpus_limit_cores', None)
            response['hdd_limit_mb'] = limits.get('hdd_limit_mb', None)
            return ManagerResponse(
                task_index=headers.get("task_index", "unknown"),
                task_class="CPU",
                error=0,
                message="success",
                success=True,
                response_data=response,
            )
        else:
            raise Exception("worker node host IP mismatch")
    except Exception as e:
        raise Exception(f"CPU task failed: {str(e)}")