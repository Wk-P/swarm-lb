from manager_objects import ServerManager, ManagerResponse
from tasks.task import task_request

async def hdd_task(headers: dict, data: dict, manager: ServerManager, round_robin_index: int):
    # check the resource of worker node 
    # if resource is enough, forward the request to worker node
    # else return error response
    
    if manager.check_worker_resources(round_robin_index, "HDD") is False:
        raise Exception("worker node HDD resource not enough")
    
    try:
        # get service node information
        # select the limit information for the worker node first
        # use service name and node host IP to verify the response
        # find the limit info for the service name
        service_name = manager.worker_urls[round_robin_index]['service_name']
        limit = {}
        for limits_info in manager.worker_resources_limits_list:
            if limits_info['service_name'] == service_name: 
                limit = limits_info['limits'][0]
                break

        response = await task_request(headers, data, manager, round_robin_index)
        
        # Check if the response contains an error
        if 'error' in response:
            raise Exception(f"Upstream error: {response.get('message', 'Unknown error')}")
        
        response['size_mb'] = data.get('size_mb', None)
        # get memory limit for the worker node
        print(f"[hdd_task] Worker limits: {limit}")
        # just one worker for service, use index 0 to get limit information
        print(f"[hdd_task] Comparing limit node_host_ip: {limit.get('node_host_ip')} with response_host: {response.get('response_host')}")
        if limit.get('node_host_ip') == response.get('response_host'):
            response['memory_limit_mb'] = limit.get('memory_limit_mb')
            response['cpu_limit_cores'] = limit.get('cpus_limit_cores')
            response['hdd_limit_mb'] = limit.get('hdd_limit_mb', None)
        
            return ManagerResponse(
                task_index=headers.get("task_index", "unknown"),
                task_class="HDD",
                error=0,
                message="success",
                success=True,
                response_data=response,
            )
        else:
            raise Exception("worker node host IP mismatch")
        
    except Exception as e:
        raise Exception(f"HDD task failed: {str(e)}")