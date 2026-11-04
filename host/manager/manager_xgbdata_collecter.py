# manager_xgbdata_collecter.py
# Data collection manager with task queuing and scheduling

from aiohttp import web
from aiohttp.web_request import Request
from aiohttp.web_response import Response
from copy import deepcopy
from manager_objects import ServerManager
import asyncio  
import traceback
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import logging
import math

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class TaskStatus(Enum):
    """Task status enumeration"""
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    SPLIT = "split"


class WorkerStatus(Enum):
    """Worker node status enumeration"""
    IDLE = "idle"
    BUSY = "busy"
    UNAVAILABLE = "unavailable"


@dataclass
class Task:
    """Task data structure"""
    task_id: str
    task_index: str
    hdd_task: Optional[Dict] = None
    mem_task: Optional[Dict] = None
    cpu_task: Optional[Dict] = None
    status: TaskStatus = TaskStatus.QUEUED
    worker_index: Optional[int] = None
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result: Optional[Dict] = None
    error: Optional[str] = None
    parent_task_id: Optional[str] = None  # For split tasks
    split_tasks: List[str] = field(default_factory=list)  # Child task IDs
    allocated_mem_limit: Optional[float] = None  # Allocated worker mem limit
    allocated_hdd_limit: Optional[float] = None  # Allocated worker hdd limit
    allocated_cpu_limit: Optional[float] = None  # Allocated worker cpu limit (percentage)
    split_reason: Optional[str] = None  # 'mem', 'hdd', or None
    target_worker_index: Optional[int] = None  # Pre-assigned worker (for sub-tasks)

    def get_required_resources(self) -> Dict[str, float]:
        """Calculate required resources for this task"""
        mem_mb = 0
        hdd_mb = 0
        
        if self.mem_task:
            mem_mb = self.mem_task.get("size_mb", 0)
        if self.hdd_task:
            hdd_mb = self.hdd_task.get("size_mb", 0)
        
        return {"mem_mb": mem_mb, "hdd_mb": hdd_mb}


class TaskScheduler:
    """Task scheduler for managing task queue and worker allocation"""
    
    def __init__(self, manager: ServerManager):
        self.manager = manager
        self.task_queue: asyncio.Queue = asyncio.Queue()
        self.tasks: Dict[str, Task] = {}  # task_id -> Task
        self.worker_status: Dict[int, WorkerStatus] = {}  # worker_index -> status
        self.task_counter = 0
        self.running = False
        
        # Initialize worker status (all start as IDLE)
        for i in range(len(manager.workers)):
            self.worker_status[i] = WorkerStatus.IDLE
    
    def generate_task_id(self) -> str:
        """Generate unique task ID"""
        self.task_counter += 1
        return f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{self.task_counter}"
    
    async def add_task(self, task: Task) -> str:
        """Add task to queue"""
        if not task.task_id:
            task.task_id = self.generate_task_id()
        
        self.tasks[task.task_id] = task
        await self.task_queue.put(task.task_id)
        logger.info(f"[Scheduler] Task {task.task_id} added to queue (queue size: {self.task_queue.qsize()})")
        return task.task_id
    
    def check_worker_resources(self, worker_index: int, required: Dict[str, float]) -> bool:
        """Check if worker has enough resources (with 0.8 safety limit)"""
        if worker_index >= len(self.manager.workers):
            return False
        
        RESOURCE_LIMIT_FACTOR = 0.6  # Only allow tasks using up to 60% of worker limit
        
        # Get real-time limits from SSH monitoring (convert HDD from bytes to MB)
        worker_ssh_client = self.manager.worker_ssh_clients[worker_index]
        worker_mem_limit = worker_ssh_client.max_mem_usage * RESOURCE_LIMIT_FACTOR
        worker_hdd_limit = (worker_ssh_client.max_hdd_usage / (1024 * 1024)) * RESOURCE_LIMIT_FACTOR
        
        required_mem = required.get("mem_mb", 0)
        required_hdd = required.get("hdd_mb", 0)
        
        # Check if required resources exceed 80% of worker limits
        mem_ok = required_mem <= worker_mem_limit if required_mem > 0 else True
        hdd_ok = required_hdd <= worker_hdd_limit if required_hdd > 0 else True
        
        return mem_ok and hdd_ok
    
    async def find_available_worker(self, task: Task) -> Optional[int]:
        """Find available worker for task
        
        - For original tasks: Use round-robin scheduling
        - For sub-tasks: Return inherited target_worker_index (no re-scheduling)
        - CRITICAL: Check worker_status to ensure worker is IDLE before assigning
        """
        required = task.get_required_resources()
        num_workers = len(self.manager.workers)
        
        # Sub-task: directly use inherited target worker (if IDLE)
        if task.target_worker_index is not None:
            target_idx = task.target_worker_index
            # Check if worker is IDLE
            if self.worker_status[target_idx] == WorkerStatus.IDLE:
                if self.check_worker_resources(target_idx, required):
                    logger.debug(f"[Scheduler] Sub-task {task.task_id} using inherited worker {target_idx}")
                    return target_idx
            logger.debug(f"[Scheduler] Sub-task {task.task_id} target worker {target_idx} not available (status={self.worker_status[target_idx].value})")
            return None
        
        # Original task: Use round-robin scheduling
        worker_index = await self.manager.get_and_update_round_robin_index()
        
        # Check if selected worker is IDLE AND has enough resources
        if self.worker_status[worker_index] == WorkerStatus.IDLE:
            if self.check_worker_resources(worker_index, required):
                logger.debug(f"[Scheduler] Task {task.task_id} assigned to worker {worker_index} (round-robin)")
                return worker_index
        
        # If selected worker is busy or lacks resources, try others
        for offset in range(1, num_workers):
            next_index = (worker_index + offset) % num_workers
            # Only consider IDLE workers
            if self.worker_status[next_index] == WorkerStatus.IDLE:
                if self.check_worker_resources(next_index, required):
                    logger.debug(f"[Scheduler] Task {task.task_id} assigned to worker {next_index} (round-robin fallback)")
                    return next_index
        
        logger.debug(f"[Scheduler] No available worker for task {task.task_id} (all busy or insufficient resources)")
        return None
    
    def split_task(self, task: Task, target_worker_index: int) -> List[Task]:
        """Split task into smaller tasks based on TARGET worker's resource constraints
        
        Args:
            task: Task to split
            target_worker_index: Worker index assigned by round-robin (sub-tasks inherit this)
        """
        split_tasks = []
        required = task.get_required_resources()
        
        SAFETY_FACTOR = 0.5  # Use 50% of actual limit for safe task splitting
        
        # Get target worker's REAL-TIME max limits from SSH monitoring (convert HDD from bytes to MB)
        worker_ssh_client = self.manager.worker_ssh_clients[target_worker_index]
        worker_mem_limit = worker_ssh_client.max_mem_usage  # Real-time actual max memory (MB)
        worker_hdd_limit = worker_ssh_client.max_hdd_usage / (1024 * 1024)  # Real-time actual max HDD (bytes -> MB)
        
        # Calculate splits needed for THIS specific worker
        mem_splits = 1
        hdd_splits = 1
        
        if required["mem_mb"] > 0 and worker_mem_limit > 0:
            mem_splits = math.ceil(required["mem_mb"] / (worker_mem_limit * SAFETY_FACTOR))
        
        if required["hdd_mb"] > 0 and worker_hdd_limit > 0:
            hdd_splits = math.ceil(required["hdd_mb"] / (worker_hdd_limit * SAFETY_FACTOR))
        
        # Split count is the max of mem and hdd requirements
        split_count = max(mem_splits, hdd_splits, 2)
        
        # Determine split reason
        if mem_splits > hdd_splits:
            task.split_reason = 'mem'
        else:
            task.split_reason = 'hdd'
        
        logger.info(f"[Scheduler] Splitting task {task.task_id} into {split_count} sub-tasks")
        logger.info(f"[Scheduler] Target worker: worker-{target_worker_index} (real-time max: mem={worker_mem_limit:.2f}MB, hdd={worker_hdd_limit:.2f}MB)")
        logger.info(f"[Scheduler] Required: mem={required['mem_mb']}MB, hdd={required['hdd_mb']}MB")
        logger.info(f"[Scheduler] Split calculation: mem_splits={mem_splits}, hdd_splits={hdd_splits}, using {split_count} splits")
        logger.info(f"[Scheduler] Split reason: {task.split_reason} (with {int((1-SAFETY_FACTOR)*100)}% safety margin)")
        
        # Create split tasks (inherit target worker from parent)
        for i in range(split_count):
            split_task = Task(
                task_id=self.generate_task_id(),
                task_index=f"{task.task_index}_split_{i}",
                parent_task_id=task.task_id,
                target_worker_index=target_worker_index  # Inherit from parent
            )
            
            # Split MEM task
            if task.mem_task:
                original_size = task.mem_task.get("size_mb", 0)
                split_size = original_size // split_count
                if i == split_count - 1:  # Last task takes remainder
                    split_size = original_size - (split_size * (split_count - 1))
                split_task.mem_task = {
                    **task.mem_task,
                    "size_mb": split_size
                }
            
            # Split HDD task
            if task.hdd_task:
                original_size = task.hdd_task.get("size_mb", 0)
                split_size = original_size // split_count
                if i == split_count - 1:  # Last task takes remainder
                    split_size = original_size - (split_size * (split_count - 1))
                split_task.hdd_task = {
                    **task.hdd_task,
                    "size_mb": split_size
                }
            
            # Copy CPU task (no split needed)
            if task.cpu_task:
                split_task.cpu_task = task.cpu_task.copy()
            
            split_tasks.append(split_task)
            task.split_tasks.append(split_task.task_id)
        
        task.status = TaskStatus.SPLIT
        return split_tasks
    
    async def process_task(self, task: Task, worker_index: int, max_retries: int = 3):
        """Process task on assigned worker with retry mechanism
        
        Args:
            task: Task to process
            worker_index: Worker index
            max_retries: Maximum retry attempts (default 3)
        """
        import aiohttp
        
        retry_count = 0
        last_error = None
        
        try:
            # Mark worker as BUSY immediately
            self.worker_status[worker_index] = WorkerStatus.BUSY
            
            task.status = TaskStatus.PROCESSING
            task.started_at = datetime.now()
            task.worker_index = worker_index
            
            # Record allocated worker limits (0.6 * real-time max from SSH monitoring, all in MB)
            RESOURCE_LIMIT_FACTOR = 0.6
            worker_ssh_client = self.manager.worker_ssh_clients[worker_index]
            task.allocated_mem_limit = worker_ssh_client.max_mem_usage * RESOURCE_LIMIT_FACTOR
            task.allocated_hdd_limit = (worker_ssh_client.max_hdd_usage / (1024 * 1024)) * RESOURCE_LIMIT_FACTOR
            task.allocated_cpu_limit = worker_ssh_client.cpu_limit  # CPU limit from Docker
            
            logger.info(f"[Scheduler] Processing task {task.task_id} on worker {worker_index} (status=BUSY)")
            
            # Build request data - embed task_index into each sub-task for consistency
            request_data = {}
            if task.hdd_task:
                request_data["hdd_task"] = {**task.hdd_task, "task_index": task.task_index}
            if task.mem_task:
                request_data["mem_task"] = {**task.mem_task, "task_index": task.task_index}
            if task.cpu_task:
                request_data["cpu_task"] = {**task.cpu_task, "task_index": task.task_index}
            
            # Send request to worker with retry
            worker = self.manager.workers[worker_index]
            worker_url = worker["url"]
            session = self.manager.http_clients[worker_index]
            
            while retry_count <= max_retries:
                try:
                    # Set timeout: 10 minutes total, 2 minutes for connection
                    timeout = aiohttp.ClientTimeout(total=600, connect=120)
                    
                    async with session.post(
                        f"{worker_url}/task/", 
                        json=request_data,
                        timeout=timeout
                    ) as resp:
                        if resp.status == 200:
                            result = await resp.json()
                            task.result = result
                            task.status = TaskStatus.COMPLETED
                            logger.info(f"[Scheduler] Task {task.task_id} completed successfully")
                            return  # Success, exit retry loop
                        else:
                            error_text = await resp.text()
                            last_error = f"Worker returned status {resp.status}: {error_text}"
                            logger.error(f"[Scheduler] Task {task.task_id} failed: {last_error}")
                            break  # Don't retry on non-200 status
                
                except (aiohttp.ServerDisconnectedError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                    retry_count += 1
                    last_error = f"{type(e).__name__}: {str(e)}"
                    
                    if retry_count <= max_retries:
                        wait_time = min(2 ** retry_count, 10)  # Exponential backoff, max 10s
                        logger.warning(
                            f"[Scheduler] Task {task.task_id} attempt {retry_count}/{max_retries} failed: {last_error}. "
                            f"Retrying in {wait_time}s..."
                        )
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(
                            f"[Scheduler] Task {task.task_id} failed after {max_retries} retries: {last_error}"
                        )
                        break
            
            # If we get here, all retries failed
            task.error = last_error or "Unknown error"
            task.status = TaskStatus.FAILED
        
        except Exception as e:
            task.error = str(e)
            task.status = TaskStatus.FAILED
            logger.error(f"[Scheduler] Task {task.task_id} failed with exception: {e}")
            traceback.print_exc()
        
        finally:
            task.completed_at = datetime.now()
            # Mark worker as IDLE
            self.worker_status[worker_index] = WorkerStatus.IDLE
            logger.info(f"[Scheduler] Worker {worker_index} status: IDLE")
    
    async def scheduler_loop(self):
        """Main scheduler loop - processes tasks from queue"""
        self.running = True
        logger.info("[Scheduler] Scheduler loop started")
        
        while self.running:
            try:
                # Get task from queue (with timeout to allow graceful shutdown)
                try:
                    task_id = await asyncio.wait_for(self.task_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                
                task = self.tasks.get(task_id)
                if not task:
                    logger.warning(f"[Scheduler] Task {task_id} not found")
                    continue
                
                logger.info(f"[Scheduler] Processing task {task_id} from queue")
                
                # Find available worker using round-robin
                worker_index = await self.find_available_worker(task)
                
                if worker_index is not None:
                    # Worker available - process task
                    asyncio.create_task(self.process_task(task, worker_index))
                else:
                    # No worker available - check if we need to split task
                    # First, get the worker that would be assigned by round-robin (for splitting)
                    target_worker_index = await self.manager.get_and_update_round_robin_index()
                    
                    # Check if target worker can handle this task (even after split)
                    required = task.get_required_resources()
                    target_worker_can_handle = self.check_worker_resources(target_worker_index, required)
                    
                    if not target_worker_can_handle:
                        # Target worker cannot handle even split tasks - need to split
                        logger.info(f"[Scheduler] Worker {target_worker_index} cannot handle task {task_id}, splitting...")
                        split_tasks = self.split_task(task, target_worker_index)
                        for st in split_tasks:
                            await self.add_task(st)
                    else:
                        # Target worker can handle but is busy - re-queue
                        logger.info(f"[Scheduler] Worker {target_worker_index} busy, re-queuing task {task_id}")
                        await asyncio.sleep(0.5)  # Wait a bit before re-queue
                        await self.task_queue.put(task_id)
            
            except Exception as e:
                logger.error(f"[Scheduler] Error in scheduler loop: {e}")
                traceback.print_exc()
        
        logger.info("[Scheduler] Scheduler loop stopped")
    
    async def wait_for_task_completion(self, task_id: str, timeout: float = 300.0) -> Task:
        """Wait for task and its sub-tasks to complete (non-recursive)"""
        task = self.tasks.get(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")
        
        start_time = datetime.now()
        
        # Wait for main task to complete or fail or split
        while task.status not in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.SPLIT]:
            if (datetime.now() - start_time).total_seconds() > timeout:
                raise TimeoutError(f"Task {task_id} timeout after {timeout}s")
            await asyncio.sleep(0.1)
        
        # If task was split, wait for all sub-tasks (non-recursive)
        if task.status == TaskStatus.SPLIT and task.split_tasks:
            pending_tasks = list(task.split_tasks)
            
            while pending_tasks:
                if (datetime.now() - start_time).total_seconds() > timeout:
                    raise TimeoutError(f"Task {task_id} timeout after {timeout}s")
                
                # Check each pending sub-task
                for sub_task_id in pending_tasks[:]:
                    sub_task = self.tasks.get(sub_task_id)
                    if not sub_task:
                        pending_tasks.remove(sub_task_id)
                        continue
                    
                    # If sub-task completed or failed, remove from pending
                    if sub_task.status in [TaskStatus.COMPLETED, TaskStatus.FAILED]:
                        pending_tasks.remove(sub_task_id)
                    # If sub-task was split, add its children to pending
                    elif sub_task.status == TaskStatus.SPLIT and sub_task.split_tasks:
                        pending_tasks.remove(sub_task_id)
                        pending_tasks.extend(sub_task.split_tasks)
                
                if pending_tasks:
                    await asyncio.sleep(0.1)
        
        return task
    
    def merge_split_task_results(self, task: Task) -> Dict:
        """Merge results from split tasks"""
        if not task.split_tasks:
            return task.result or {}
        
        # Collect all sub-task results
        merged_result = {
            "cpu_result": {},
            "mem_result": {},
            "hdd_result": {}
        }
        
        total_response_time = 0.0
        sub_task_count = 0
        
        for sub_task_id in task.split_tasks:
            sub_task = self.tasks.get(sub_task_id)
            if not sub_task or sub_task.status != TaskStatus.COMPLETED:
                continue
            
            sub_result = sub_task.result or {}
            
            # Merge CPU results
            if "cpu_result" in sub_result:
                for key, value in sub_result["cpu_result"].items():
                    if key not in merged_result["cpu_result"]:
                        merged_result["cpu_result"][key] = value
                    else:
                        # Average CPU values
                        merged_result["cpu_result"][key] = (
                            merged_result["cpu_result"][key] + value
                        ) / 2
            
            # Merge MEM results
            if "mem_result" in sub_result:
                for key, value in sub_result["mem_result"].items():
                    if key == "size_mb":
                        merged_result["mem_result"][key] = merged_result["mem_result"].get(key, 0) + value
                    else:
                        merged_result["mem_result"][key] = value
            
            # Merge HDD results
            if "hdd_result" in sub_result:
                for key, value in sub_result["hdd_result"].items():
                    if key == "size_mb":
                        merged_result["hdd_result"][key] = merged_result["hdd_result"].get(key, 0) + value
                    else:
                        merged_result["hdd_result"][key] = value
            
            # Sum response times
            if "response_time" in sub_result:
                total_response_time += sub_result["response_time"]
            sub_task_count += 1
        
        if sub_task_count > 0:
            merged_result["response_time"] = total_response_time / sub_task_count
        
        return merged_result
    
    async def stop(self):
        """Stop scheduler loop"""
        self.running = False


async def handle_request(request: Request) -> Response:
    """Handle incoming task request"""
    request_start_time = datetime.now()
    
    try:
        data = deepcopy(await request.json())
        task_index = request.headers.get("task_index", "unknown")
        
        scheduler: TaskScheduler = request.app.get('scheduler')
        if scheduler is None:
            return web.json_response({
                "error": 1,
                "message": "Scheduler not ready",
                "success": False
            }, status=503)
        
        logger.info(f"[Manager] Received task request: task_index={task_index}")
        
        # Create task from request data
        task = Task(
            task_id="",  # Will be generated
            task_index=task_index,
            hdd_task=data.get("hdd_task"),
            mem_task=data.get("mem_task"),
            cpu_task=data.get("cpu_task")
        )
        
        # Add to queue
        task_id = await scheduler.add_task(task)
        
        # Wait for task completion
        try:
            await scheduler.wait_for_task_completion(task_id, timeout=600.0)
        except TimeoutError as e:
            return web.json_response({
                "error": 1,
                "message": str(e),
                "success": False
            }, status=408)
        
        # Get completed task
        completed_task = scheduler.tasks.get(task_id)
        if not completed_task:
            return web.json_response({
                "error": 1,
                "message": "Task not found after completion",
                "success": False
            }, status=500)
        
        if completed_task.status == TaskStatus.FAILED:
            return web.json_response({
                "error": 1,
                "message": completed_task.error or "Task failed",
                "success": False
            }, status=500)
        
        # Calculate response time
        response_time = (datetime.now() - request_start_time).total_seconds()
        
        # Build response based on task status
        if completed_task.status == TaskStatus.SPLIT:
            # Task was split - merge results
            merged_result = scheduler.merge_split_task_results(completed_task)
            
            # Use first successful sub-task's limits (all sub-tasks have same limits)
            mem_limit = None
            hdd_limit = None
            for sub_task_id in completed_task.split_tasks:
                sub_task = scheduler.tasks.get(sub_task_id)
                if sub_task and sub_task.status == TaskStatus.COMPLETED:
                    mem_limit = sub_task.allocated_mem_limit
                    hdd_limit = sub_task.allocated_hdd_limit
                    break
        else:
            # Task was not split - use direct result
            merged_result = completed_task.result or {}
            hdd_limit = completed_task.allocated_hdd_limit
            mem_limit = completed_task.allocated_mem_limit
        
        # Extract task parameters
        cpu_task_number = 0
        if completed_task.cpu_task:
            cpu_task_number = completed_task.cpu_task.get("number", 0)
        
        allocating_mem = 0.0
        if completed_task.mem_task:
            allocating_mem = float(completed_task.mem_task.get("size_mb", 0))
        
        allocating_hdd = 0.0
        if completed_task.hdd_task:
            allocating_hdd = float(completed_task.hdd_task.get("size_mb", 0))
        
        # CPU limit: use actual limit from Docker if available
        cpu_limit = completed_task.allocated_cpu_limit if completed_task.allocated_cpu_limit else 100
        
        # Determine assigned worker index
        if completed_task.status == TaskStatus.SPLIT and completed_task.split_tasks:
            # For split tasks, get worker index from first successful sub-task
            assigned_worker_index = None
            for sub_task_id in completed_task.split_tasks:
                sub_task = scheduler.tasks.get(sub_task_id)
                if sub_task and sub_task.worker_index is not None:
                    assigned_worker_index = sub_task.worker_index
                    break
        else:
            # For non-split tasks, use direct worker index
            assigned_worker_index = completed_task.worker_index
        
        # Format response
        response_data = {
            "task_index": completed_task.task_index,
            "cpu_task_number": cpu_task_number,
            "cpu_limit": cpu_limit,
            "hdd_limit": hdd_limit,
            "mem_limit": mem_limit,
            "allocating_hdd": allocating_hdd,
            "allocating_mem": allocating_mem,
            "response_time": response_time,
            "worker_index": assigned_worker_index
        }
        
        return web.json_response(response_data)
    
    except Exception as e:
        logger.error(f"[Manager] Error handling request: {e}")
        traceback.print_exc()
        return web.json_response({
            "error": 1,
            "message": str(e),
            "success": False
        }, status=500)


async def handle_task_status(request: Request) -> Response:
    """Get task status"""
    try:
        task_id = request.match_info.get('task_id')
        scheduler: TaskScheduler = request.app.get('scheduler')
        
        if not scheduler:
            return web.json_response({"error": "Scheduler not ready"}, status=503)
        
        task = scheduler.tasks.get(task_id)
        if not task:
            return web.json_response({"error": "Task not found"}, status=404)
        
        return web.json_response({
            "task_id": task.task_id,
            "task_index": task.task_index,
            "status": task.status.value,
            "worker_index": task.worker_index,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            "result": task.result,
            "error": task.error,
            "split_tasks": task.split_tasks
        })
    
    except Exception as e:
        logger.error(f"[Manager] Error getting task status: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def handle_queue_status(request: Request) -> Response:
    """Get queue status"""
    try:
        scheduler: TaskScheduler = request.app.get('scheduler')
        
        if not scheduler:
            return web.json_response({"error": "Scheduler not ready"}, status=503)
        
        # Count tasks by status
        status_counts = {}
        for task in scheduler.tasks.values():
            status = task.status.value
            status_counts[status] = status_counts.get(status, 0) + 1
        
        return web.json_response({
            "queue_size": scheduler.task_queue.qsize(),
            "total_tasks": len(scheduler.tasks),
            "status_counts": status_counts,
            "worker_status": {idx: status.value for idx, status in scheduler.worker_status.items()}
        })
    
    except Exception as e:
        logger.error(f"[Manager] Error getting queue status: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def on_startup(app: web.Application):
    """Application startup"""
    try:
        logger.info("[Manager] Starting up...")
        
        # Create ServerManager
        manager = ServerManager()
        app['manager'] = manager
        
        # Create TaskScheduler
        scheduler = TaskScheduler(manager)
        app['scheduler'] = scheduler
        
        # Start scheduler loop as background task
        scheduler_task = asyncio.create_task(scheduler.scheduler_loop())
        app['scheduler_task'] = scheduler_task
        
        # Create resource monitors
        monitors = []
        for ssh_client in manager.worker_ssh_clients:
            try:
                monitors.append(asyncio.create_task(ssh_client.mem_usage_handle()))
                monitors.append(asyncio.create_task(ssh_client.hdd_usage_handle()))
            except Exception as exc:
                logger.error(f"[Manager] Failed to create monitor: {exc}")
        app['hm_monitors'] = monitors
        
        logger.info("[Manager] Startup complete")
    
    except Exception as e:
        logger.error(f"[Manager] Error during startup: {e}")
        traceback.print_exc()
        app['manager'] = None
        app['scheduler'] = None
        app['hm_monitors'] = []
        raise e


async def on_cleanup(app: web.Application):
    """Application cleanup"""
    try:
        logger.info("[Manager] Cleaning up...")
        
        scheduler: TaskScheduler = app.get('scheduler')
        if scheduler:
            await scheduler.stop()
        
        scheduler_task: asyncio.Task = app.get('scheduler_task')
        if scheduler_task:
            scheduler_task.cancel()
            try:
                await scheduler_task
            except asyncio.CancelledError:
                logger.info("[Manager] Scheduler task cancelled")
        
        manager: ServerManager = app.get('manager')
        monitor_tasks: List[asyncio.Task] = app.get('hm_monitors', [])
        
        if manager is not None:
            # Close HTTP clients
            for client in manager.http_clients:
                try:
                    await client.close()
                except Exception as exc:
                    logger.error(f"[Manager] Error closing HTTP client: {exc}")
            
            # Close SSH clients
            for client in manager.worker_ssh_clients:
                try:
                    if hasattr(client, 'ssh_client') and client.ssh_client is not None:
                        client.ssh_client.close()
                except Exception as exc:
                    logger.error(f"[Manager] Error closing SSH client: {exc}")
        
        # Cancel monitors
        for task in monitor_tasks:
            try:
                task.cancel()
            except Exception:
                pass
        for task in monitor_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.error(f"[Manager] Error awaiting monitor task: {exc}")
        
        logger.info("[Manager] Cleanup complete")
    
    except Exception as e:
        logger.error(f"[Manager] Error during cleanup: {e}")
        traceback.print_exc()


def init_app():
    """Initialize application"""
    try:
        app = web.Application()
        app.on_startup.append(on_startup)
        app.on_cleanup.append(on_cleanup)
        
        # Routes
        app.router.add_post('/task/', handle_request)
        app.router.add_get('/task/{task_id}', handle_task_status)
        app.router.add_get('/queue/status', handle_queue_status)
        
        return app
    except Exception as e:
        logger.error(f"[Manager] Error initializing app: {e}")
        raise e


if __name__ == '__main__':
    try:
        app = init_app()
        web.run_app(app, host='0.0.0.0', port=8082)
    except Exception as e:
        logger.error(f"[Manager] Error running app: {e}")