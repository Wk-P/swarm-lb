from manager_objects import ServerManager, ManagerResponse
from tasks.task import task_request

async def cpu_task(headers: dict, data: dict, manager: ServerManager, round_robin_index: int):
    try:
        response = await task_request(headers, data, manager, round_robin_index)
        response['number'] = data.get('number', None)
        return ManagerResponse(
            task_index=headers.get("task_index", "unknown"),
            task_class="CPU",
            error=0,
            message="success",
            success=True,
            response_data=response,
        )
        
    except Exception as e:
        raise Exception(f"CPU task failed: {str(e)}")