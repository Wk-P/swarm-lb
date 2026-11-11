# collector_hdd_tier.py —— 8084 训练数据采集 · 按磁盘档位（原 manager_xgbdata_collector_handle_hdd_v2.py）
# 请求带 HDD 档位时，选能容纳该档位的最小 worker；否则轮询，磁盘放不下就改派。
# 资源上限读 swarm/worker_hdd_limits.json（不 SSH）。
# Data collection manager with task queuing and scheduling

from aiohttp import web
from aiohttp.web_request import Request
from aiohttp.web_response import Response
from copy import deepcopy
from worker_manager_static import ServerManager
import asyncio  
import aiohttp
import traceback
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import logging
import math
from collections import deque

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
    min_mem_limit_for_response: Optional[float] = None  # Min limit for split task response
    min_hdd_limit_for_response: Optional[float] = None  # Min limit for split task response

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
        self.task_queue: deque = deque()  # Use deque for priority insertion
        self.queue_event: asyncio.Event = asyncio.Event()  # Event to signal queue changes
        self.tasks: Dict[str, Task] = {}  # task_id -> Task
        self.worker_status: Dict[int, WorkerStatus] = {}  # worker_index -> status
        self.task_counter = 0
        self.running = False
        
        # Initialize worker status
        # Initialize worker status based on worker client availability
        for i in range(len(manager.workers)):
            if i < len(manager.worker_ssh_clients):
                self.worker_status[i] = WorkerStatus.IDLE
            else:
                # Worker exists but no client - mark as UNAVAILABLE
                self.worker_status[i] = WorkerStatus.UNAVAILABLE
                logger.warning(f"[Scheduler] Worker {i} initialized as UNAVAILABLE (no worker client)")
    
    def generate_task_id(self) -> str:
        """Generate unique task ID"""
        self.task_counter += 1
        return f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{self.task_counter}"
    
    async def add_task(self, task: Task, priority: bool = False) -> str:
        """Add task to queue
        
        Args:
            task: Task to add
            priority: If True, add to front of queue (for split sub-tasks)
        """
        if not task.task_id:
            task.task_id = self.generate_task_id()
        
        self.tasks[task.task_id] = task
        
        if priority:
            self.task_queue.appendleft(task.task_id)  # Add to front
            logger.info(f"[Scheduler] Task {task.task_id} added to queue HEAD (priority) (queue size: {len(self.task_queue)})")
        else:
            self.task_queue.append(task.task_id)  # Add to back
            logger.info(f"[Scheduler] Task {task.task_id} added to queue TAIL (queue size: {len(self.task_queue)})")
        
        self.queue_event.set()  # Signal that queue has items
        return task.task_id
    
    def check_worker_resources(self, worker_index: int, required: Dict[str, float]) -> bool:
        """Check if worker has enough resources (100% of config limit)"""
        if worker_index >= len(self.manager.workers):
            return False
        
        # Check if worker client exists
        if worker_index >= len(self.manager.worker_ssh_clients):
            logger.warning(f"[Scheduler] No worker client for worker {worker_index}")
            return False
        
        # Get static limits from config (all in MB)
        worker_ssh_client = self.manager.worker_ssh_clients[worker_index]
        
        # Check if config limits are available
        if worker_ssh_client.max_mem_usage == 0 or worker_ssh_client.max_hdd_usage == 0:
            logger.warning(f"[Scheduler] Worker {worker_index} resource limits not configured")
            return False
        
        worker_mem_limit = worker_ssh_client.max_mem_usage
        worker_hdd_limit = worker_ssh_client.max_hdd_usage
        
        required_mem = required.get("mem_mb", 0)
        required_hdd = required.get("hdd_mb", 0)
        
        mem_ok = required_mem <= worker_mem_limit if required_mem > 0 else True
        hdd_ok = required_hdd <= worker_hdd_limit if required_hdd > 0 else True
        
        return mem_ok and hdd_ok
    
    async def find_available_worker(self, task: Task) -> Optional[int]:
        """Check if task's pre-assigned target worker is available (DEPRECATED)
        
        NOTE: This method is now simplified since all tasks have pre-assigned
        target_worker_index before entering the queue.
        """
        if task.target_worker_index is None:
            logger.error(f"[Scheduler] Task {task.task_id} has no target worker!")
            return None
        
        target_idx = task.target_worker_index
        required = task.get_required_resources()
        
        # Check if worker is IDLE and has resources
        if self.worker_status[target_idx] == WorkerStatus.IDLE:
            if self.check_worker_resources(target_idx, required):
                return target_idx
        
        return None
    
    async def check_worker_health(self, worker_index: int) -> bool:
        """Check if a specific worker is healthy by sending a health check request
        
        Args:
            worker_index: Worker index to check
            
        Returns:
            True if worker is healthy, False otherwise
        """
        try:
            worker = self.manager.workers[worker_index]
            worker_url = worker["url"]
            session = self.manager.http_clients[worker_index]
            
            # Try to connect to worker's root endpoint with short timeout
            timeout = aiohttp.ClientTimeout(total=3, connect=1)
            async with session.get(f"{worker_url}/", timeout=timeout) as resp:
                return resp.status in [200, 404]  # 404 is ok, means service is up but no root handler
                
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning(f"[Scheduler] Worker {worker_index} health check failed: {e}")
            return False
        except Exception as e:
            logger.error(f"[Scheduler] Unexpected error checking worker {worker_index}: {e}")
            return False
    
    async def update_worker_status_from_health(self, worker_index: int, is_healthy: bool):
        """Update worker status based on health check result
        
        Args:
            worker_index: Worker index
            is_healthy: Health check result
        """
        old_status = self.worker_status[worker_index]
        
        if is_healthy:
            # Worker is healthy
            if old_status == WorkerStatus.UNAVAILABLE:
                # Worker recovered from unavailable state
                self.worker_status[worker_index] = WorkerStatus.IDLE
                logger.info(f"[Scheduler] Worker {worker_index} recovered: UNAVAILABLE -> IDLE")
            elif old_status == WorkerStatus.BUSY:
                # Keep BUSY status (task is running)
                pass
        else:
            # Worker is unhealthy
            if old_status != WorkerStatus.UNAVAILABLE:
                self.worker_status[worker_index] = WorkerStatus.UNAVAILABLE
                logger.warning(f"[Scheduler] Worker {worker_index} marked as UNAVAILABLE (was {old_status.value})")
    
    async def health_check_loop(self, check_interval: float = 10.0):
        """Periodic health check loop for all workers
        
        Args:
            check_interval: Seconds between health checks (default 10s)
        """
        logger.info(f"[Scheduler] Health check loop started (interval={check_interval}s)")
        
        while self.running:
            try:
                # Check all workers
                health_tasks = []
                for worker_index in range(len(self.manager.workers)):
                    health_tasks.append(self.check_worker_health(worker_index))
                
                # Run all health checks concurrently
                health_results = await asyncio.gather(*health_tasks, return_exceptions=True)
                
                # Update worker status based on results
                for worker_index, result in enumerate(health_results):
                    if isinstance(result, bool):
                        await self.update_worker_status_from_health(worker_index, result)
                    else:
                        # Exception occurred
                        logger.error(f"[Scheduler] Health check exception for worker {worker_index}: {result}")
                        await self.update_worker_status_from_health(worker_index, False)
                
                # Wait before next check
                await asyncio.sleep(check_interval)
                
            except Exception as e:
                logger.error(f"[Scheduler] Error in health check loop: {e}")
                traceback.print_exc()
                await asyncio.sleep(check_interval)
        
        logger.info("[Scheduler] Health check loop stopped")
    
    async def service_discovery_loop(self, discovery_interval: float = 30.0):
        """Periodic service discovery loop to detect new/removed services
        
        Args:
            discovery_interval: Seconds between discoveries (default 30s)
        """
        logger.info(f"[Scheduler] Service discovery loop started (interval={discovery_interval}s)")
        
        while self.running:
            try:
                await asyncio.sleep(discovery_interval)
                
                # Refresh workers list
                changes = await self.manager.refresh_workers()
                
                # Handle added workers
                if changes['added']:
                    logger.info(f"[Scheduler] Detected {len(changes['added'])} new workers")
                    
                    # Update worker_status dict to include new workers
                    current_worker_count = len(self.worker_status)
                    new_worker_count = len(self.manager.workers)
                    
                    if new_worker_count > current_worker_count:
                        for i in range(current_worker_count, new_worker_count):
                            self.worker_status[i] = WorkerStatus.IDLE
                            logger.info(f"[Scheduler] Added worker {i} with status IDLE")
                        
                        # Workers use static config limits, no monitoring tasks needed
                        for i in range(current_worker_count, len(self.manager.worker_ssh_clients)):
                            logger.info(f"[Scheduler] Worker {i} added with config limits")
                
                # Handle removed workers
                if changes['removed']:
                    logger.warning(f"[Scheduler] Detected {len(changes['removed'])} removed workers")
                    # Note: We keep worker_status entries but mark them as UNAVAILABLE
                    # The health check loop will handle status updates
                
            except Exception as e:
                logger.error(f"[Scheduler] Error in service discovery loop: {e}")
                traceback.print_exc()
                await asyncio.sleep(discovery_interval)
        
        logger.info("[Scheduler] Service discovery loop stopped")
    
    async def split_task_and_assign(self, task: Task, required: Dict[str, float], target_worker_index: int) -> List[Task]:
        """Split task based on TARGET worker's capacity and assign via round-robin (SCHEME 1)
        
        Args:
            task: Task to split
            required: Required resources already calculated
            target_worker_index: Target worker index for capacity calculation
        
        Returns:
            List of sub-tasks with pre-assigned target_worker_index
        """
        split_tasks = []
        
        # Validate worker index
        if target_worker_index >= len(self.manager.worker_ssh_clients):
            logger.error(f"[Scheduler] Invalid worker index {target_worker_index} for split calculation")
            return split_tasks
        
        SAFETY_FACTOR = 0.8  # Safety capacity is 80% of upper limit (100% of safety capacity)
        
        # Get TARGET worker's resource limits from config (all in MB)
        worker_ssh_client = self.manager.worker_ssh_clients[target_worker_index]
        target_mem_limit = worker_ssh_client.max_mem_usage
        target_hdd_limit = worker_ssh_client.max_hdd_usage
        
        # Check if resource data is available
        if target_mem_limit == 0 and target_hdd_limit == 0:
            logger.error(f"[Scheduler] Worker {target_worker_index} resource data not available for split")
            return split_tasks
        
        # Calculate split count based on TARGET worker capacity
        mem_splits = 1
        hdd_splits = 1
        
        if required['mem_mb'] > 0 and target_mem_limit > 0:
            safe_mem_limit = target_mem_limit * SAFETY_FACTOR
            mem_splits = math.ceil(required['mem_mb'] / safe_mem_limit)
        
        if required['hdd_mb'] > 0 and target_hdd_limit > 0:
            safe_hdd_limit = target_hdd_limit * SAFETY_FACTOR
            hdd_splits = math.ceil(required['hdd_mb'] / safe_hdd_limit)
        
        split_count = max(mem_splits, hdd_splits)
        split_count = min(split_count, 20)  # Cap at 20 to prevent excessive splitting
        
        # Store minimum limits for response (will be used in handle_request)
        task.min_mem_limit_for_response = target_mem_limit * SAFETY_FACTOR
        task.min_hdd_limit_for_response = target_hdd_limit * SAFETY_FACTOR
        
        # Determine split reason
        if mem_splits > hdd_splits:
            task.split_reason = 'mem'
        else:
            task.split_reason = 'hdd'
        
        logger.info(f"[Scheduler] Splitting task {task.task_id} into {split_count} sub-tasks (Round-Robin Scheme)")
        logger.info(f"[Scheduler] Target worker-{target_worker_index} limits: mem={target_mem_limit:.2f}MB, hdd={target_hdd_limit:.2f}MB")
        logger.info(f"[Scheduler] Required: mem={required['mem_mb']}MB, hdd={required['hdd_mb']}MB")
        logger.info(f"[Scheduler] Split calculation: mem_splits={mem_splits}, hdd_splits={hdd_splits}, using {split_count} splits")
        logger.info(f"[Scheduler] Split reason: {task.split_reason} (with {int((1-SAFETY_FACTOR)*100)}% safety margin)")
        
        # Create split tasks and assign via round-robin
        num_workers = len(self.manager.workers)
        for i in range(split_count):
            # Get next worker in round-robin
            target_worker = await self.manager.get_and_update_round_robin_index()
            
            split_task = Task(
                task_id=self.generate_task_id(),
                task_index=f"{task.task_index}_split_{i}",
                parent_task_id=task.task_id,
                target_worker_index=target_worker  # Assigned via round-robin
            )
            
            # Store in tasks dict immediately
            self.tasks[split_task.task_id] = split_task
            
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
            
            logger.info(f"[Scheduler] Sub-task {split_task.task_id} assigned to worker-{target_worker}")
        
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
            # Validate worker index
            if worker_index >= len(self.manager.workers):
                task.error = f"Invalid worker index {worker_index}"
                task.status = TaskStatus.FAILED
                task.completed_at = datetime.now()
                self.worker_status[worker_index] = WorkerStatus.IDLE
                logger.error(f"[Scheduler] Task {task.task_id} failed: {task.error}")
                return
            
            if worker_index >= len(self.manager.worker_ssh_clients):
                task.error = f"No worker client for worker {worker_index}"
                task.status = TaskStatus.FAILED
                task.completed_at = datetime.now()
                self.worker_status[worker_index] = WorkerStatus.IDLE
                logger.error(f"[Scheduler] Task {task.task_id} failed: {task.error}")
                return
            
            # Worker already marked as BUSY in scheduler_loop, just update task state
            task.status = TaskStatus.PROCESSING
            task.started_at = datetime.now()
            task.worker_index = worker_index
            
            # Record allocated worker limits (100% of config limits, all in MB)
            worker_ssh_client = self.manager.worker_ssh_clients[worker_index]
            task.allocated_mem_limit = worker_ssh_client.max_mem_usage   # 100% of config limit
            task.allocated_hdd_limit = worker_ssh_client.max_hdd_usage   # 100% of config limit
            task.allocated_cpu_limit = worker_ssh_client.cpu_limit  # CPU limit from config
            
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
                    # No timeout: HDD tasks can take arbitrarily long (e.g. writing 200GB)
                    timeout = aiohttp.ClientTimeout(total=0, connect=300)
                    
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
            # Only reset to IDLE if health-check has NOT already marked this worker UNAVAILABLE.
            # Overwriting UNAVAILABLE → IDLE would cause new tasks to be dispatched to a dead
            # worker, triggering retries and potentially blocking the scheduler.
            if self.worker_status.get(worker_index) == WorkerStatus.UNAVAILABLE:
                logger.warning(
                    f"[Scheduler] Worker {worker_index} is UNAVAILABLE (health-check); "
                    f"keeping UNAVAILABLE instead of resetting to IDLE"
                )
            else:
                self.worker_status[worker_index] = WorkerStatus.IDLE
                logger.info(f"[Scheduler] Worker {worker_index} status: IDLE")
    
    async def scheduler_loop(self):
        """Main scheduler loop - processes tasks from queue based on worker availability"""
        self.running = True
        logger.info("[Scheduler] Scheduler loop started")
        
        while self.running:
            try:
                # Wait for queue to have items
                while not self.task_queue and self.running:
                    self.queue_event.clear()
                    try:
                        await asyncio.wait_for(self.queue_event.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                
                if not self.running:
                    break
                
                if not self.task_queue:
                    continue
                
                # Snapshot queue to avoid "deque mutated during iteration" errors
                queued_task_ids = list(self.task_queue)

                # Pass 1: clean up stale/invalid tasks (no mutation of deque during original iteration)
                to_remove = []
                for task_id in queued_task_ids:
                    task = self.tasks.get(task_id)
                    if not task:
                        logger.warning(f"[Scheduler] Task {task_id} not found in tasks dict, removing from queue")
                        to_remove.append(task_id)
                        continue
                    if task.target_worker_index is None:
                        logger.error(f"[Scheduler] Task {task_id} has no target worker, failing it")
                        task.status = TaskStatus.FAILED
                        task.error = "No target worker assigned"
                        task.completed_at = datetime.now()
                        to_remove.append(task_id)
                        continue
                    # NOTE: No longer auto-fail tasks whose target worker is UNAVAILABLE.
                    # Work-stealing (Pass 2) allows other workers to pick up these tasks
                    # if they have sufficient resources.

                for task_id in to_remove:
                    try:
                        self.task_queue.remove(task_id)
                    except ValueError:
                        pass

                # Pass 2: assign tasks to IDLE workers (pre-assigned + work-stealing)
                assigned_count = 0

                for worker_index, worker_status in self.worker_status.items():
                    if worker_status != WorkerStatus.IDLE:
                        continue  # Skip BUSY or UNAVAILABLE workers

                    # Get this worker's resource limits for work-stealing eligibility check
                    if worker_index >= len(self.manager.worker_ssh_clients):
                        continue
                    worker_client = self.manager.worker_ssh_clients[worker_index]
                    worker_hdd_limit = worker_client.max_hdd_usage
                    worker_mem_limit = worker_client.max_mem_usage

                    task_to_process = None
                    task_id_to_process = None

                    # Priority 1: find task pre-assigned to this worker
                    for task_id in list(self.task_queue):
                        task = self.tasks.get(task_id)
                        if task and task.target_worker_index == worker_index:
                            task_to_process = task
                            task_id_to_process = task_id
                            break

                    # Priority 2 (work-stealing): if no pre-assigned task, steal any
                    # queued task whose resource requirements fit this worker's limits.
                    # This prevents idle workers while others have long queues.
                    if not task_to_process:
                        for task_id in list(self.task_queue):
                            task = self.tasks.get(task_id)
                            if not task:
                                continue
                            required = task.get_required_resources()
                            req_hdd = required.get("hdd_mb", 0)
                            req_mem = required.get("mem_mb", 0)
                            hdd_ok = (req_hdd <= worker_hdd_limit) if req_hdd > 0 else True
                            mem_ok = (req_mem <= worker_mem_limit) if req_mem > 0 else True
                            if hdd_ok and mem_ok:
                                logger.info(
                                    f"[Scheduler] Work-stealing: worker-{worker_index} takes "
                                    f"task {task_id} (originally for worker-{task.target_worker_index})"
                                )
                                task.target_worker_index = worker_index  # Re-assign
                                task_to_process = task
                                task_id_to_process = task_id
                                break

                    if task_to_process and task_id_to_process:
                        try:
                            self.task_queue.remove(task_id_to_process)
                        except ValueError:
                            continue  # Already removed by another coroutine

                        # Mark as BUSY immediately to prevent race condition
                        self.worker_status[worker_index] = WorkerStatus.BUSY
                        logger.info(f"[Scheduler] Assigning task {task_id_to_process} to worker-{worker_index} (IDLE->BUSY)")
                        asyncio.create_task(self.process_task(task_to_process, worker_index))
                        assigned_count += 1

                # If no tasks were assigned (all workers busy or no matching tasks), wait
                if assigned_count == 0:
                    logger.debug(f"[Scheduler] No tasks assigned this cycle (queue size: {len(self.task_queue)})")
                    await asyncio.sleep(0.5)  # Wait before next scan
            
            except Exception as e:
                logger.error(f"[Scheduler] Error in scheduler loop: {e}")
                traceback.print_exc()
        
        logger.info("[Scheduler] Scheduler loop stopped")
    
    async def wait_for_task_completion(self, task_id: str, timeout: float = 1800.0) -> Task:
        """Wait for task and its sub-tasks to complete (non-recursive, default 30min timeout)"""
        task = self.tasks.get(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")
        
        start_time = datetime.now()
        
        # Wait for main task to complete or fail (split is disabled)
        while task.status not in [TaskStatus.COMPLETED, TaskStatus.FAILED]:
            if (datetime.now() - start_time).total_seconds() > timeout:
                raise TimeoutError(f"Task {task_id} timeout after {timeout}s")
            await asyncio.sleep(0.1)
        
        return task
    
    def merge_split_task_results(self, task: Task) -> Dict:
        """Merge results from split tasks.
        
        Worker (app.py) returns:
          { cpu_response, mem_response, hdd_response, total_processing_time }
        Each *_response has: { result: {...}, task_index, processing_time, [mem/hdd_free_before] }
        
        Merge strategy:
          - cpu_response.result   : average  (same computation repeated on each split)
          - mem_response.result   : sum       (total allocated across splits)
          - hdd_response.result   : sum       (total written across splits)
          - total_processing_time : average   (parallel wall-clock time on workers)
        """
        if not task.split_tasks:
            return task.result or {}
        
        merged_result = {
            "cpu_response": None,
            "mem_response": None,
            "hdd_response": None,
            "total_processing_time": None,
        }
        
        subtask_total_processing_time = 0.0
        sub_task_count = 0
        
        for sub_task_id in task.split_tasks:
            sub_task = self.tasks.get(sub_task_id)
            if not sub_task or sub_task.status != TaskStatus.COMPLETED:
                continue
            
            sub_result = sub_task.result or {}
            
            # Merge CPU response (average result values — same task repeated per split)
            cpu_resp = sub_result.get("cpu_response")
            if cpu_resp:
                if merged_result["cpu_response"] is None:
                    merged_result["cpu_response"] = deepcopy(cpu_resp)
                else:
                    for k, v in cpu_resp.get("result", {}).items():
                        if isinstance(v, (int, float)):
                            prev = merged_result["cpu_response"]["result"].get(k, v)
                            merged_result["cpu_response"]["result"][k] = (prev + v) / 2
            
            # Merge MEM response (sum allocated sizes across splits)
            mem_resp = sub_result.get("mem_response")
            if mem_resp:
                if merged_result["mem_response"] is None:
                    merged_result["mem_response"] = deepcopy(mem_resp)
                else:
                    for k, v in mem_resp.get("result", {}).items():
                        if isinstance(v, (int, float)):
                            merged_result["mem_response"]["result"][k] = (
                                merged_result["mem_response"]["result"].get(k, 0) + v
                            )
            
            # Merge HDD response (sum written/file sizes across splits)
            hdd_resp = sub_result.get("hdd_response")
            if hdd_resp:
                if merged_result["hdd_response"] is None:
                    merged_result["hdd_response"] = deepcopy(hdd_resp)
                else:
                    for k, v in hdd_resp.get("result", {}).items():
                        if isinstance(v, bool):  # bool is subclass of int, keep as-is
                            pass
                        elif isinstance(v, (int, float)):
                            merged_result["hdd_response"]["result"][k] = (
                                merged_result["hdd_response"]["result"].get(k, 0) + v
                            )
                        elif isinstance(v, dict):  # e.g. written_size is a nested dict
                            for kk, vv in v.items():
                                if isinstance(vv, (int, float)):
                                    merged_result["hdd_response"]["result"][k][kk] = (
                                        merged_result["hdd_response"]["result"][k].get(kk, 0) + vv
                                    )
            
            # Accumulate total_processing_time from worker
            if "total_processing_time" in sub_result:
                subtask_total_processing_time += sub_result["total_processing_time"]
            sub_task_count += 1
        
        if sub_task_count > 0:
            merged_result["total_processing_time"] = subtask_total_processing_time / sub_task_count
        
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
        
        hdd_tier_header = request.headers.get("X-HDD-Tier", "(none)")
        logger.info(f"[Manager] Received task request: task_index={task_index}, X-HDD-Tier={hdd_tier_header}")
        
        # Create task from request data
        task = Task(
            task_id=scheduler.generate_task_id(),
            task_index=task_index,
            hdd_task=data.get("hdd_task"),
            mem_task=data.get("mem_task"),
            cpu_task=data.get("cpu_task")
        )
        
        # Register task
        scheduler.tasks[task.task_id] = task
        task_id = task.task_id

        # ── Worker selection ─────────────────────────────────────────────────
        # If client sent X-HDD-Tier header, route directly to the matching worker
        # (smallest hdd_limit_mb >= requested tier).  No round-robin redirect needed.
        # If header is absent, fall back to legacy round-robin + overflow redirect.
        hdd_tier_header = request.headers.get("X-HDD-Tier")
        clients = scheduler.manager.worker_ssh_clients

        if hdd_tier_header is not None:
            try:
                requested_tier = int(hdd_tier_header)
            except ValueError:
                requested_tier = 0

            # Sort workers by hdd_limit ascending, find smallest that fits the tier
            sorted_workers = sorted(
                [(i, c.max_hdd_usage) for i, c in enumerate(clients)],
                key=lambda x: x[1]
            )
            target_worker_index = None
            for idx, limit in sorted_workers:
                if limit >= requested_tier:
                    target_worker_index = idx
                    break
            if target_worker_index is None:          # tier exceeds all workers
                target_worker_index = sorted_workers[-1][0]

            # Keep round-robin counter in sync (advance once)
            await scheduler.manager.get_and_update_round_robin_index()

            logger.info(
                f"[Manager] Task {task_id} routed by tier {requested_tier}MB "
                f"→ worker-{target_worker_index} "
                f"(hdd_limit={clients[target_worker_index].max_hdd_usage}MB)"
            )

        else:
            # Legacy path: round-robin + redirect if HDD overflows assigned worker
            required = task.get_required_resources()
            target_worker_index = await scheduler.manager.get_and_update_round_robin_index()
            required_hdd = required.get("hdd_mb", 0)

            if required_hdd > 0 and target_worker_index < len(clients):
                hdd_limit = clients[target_worker_index].max_hdd_usage
                if hdd_limit > 0 and required_hdd > hdd_limit:
                    max_hdd_index = max(
                        range(len(clients)),
                        key=lambda i: clients[i].max_hdd_usage
                    )
                    max_hdd_limit = clients[max_hdd_index].max_hdd_usage
                    if required_hdd > max_hdd_limit:
                        logger.warning(
                            f"[Manager] Task {task_id} HDD {required_hdd}MB exceeds "
                            f"all workers (max={max_hdd_limit}MB), rejecting"
                        )
                        del scheduler.tasks[task_id]
                        return web.json_response({
                            "error": 1,
                            "message": f"HDD request {required_hdd:.1f}MB exceeds all worker limits (max={max_hdd_limit:.1f}MB)",
                            "success": False
                        }, status=400)
                    logger.info(
                        f"[Manager] Task {task_id} HDD {required_hdd}MB exceeds "
                        f"worker-{target_worker_index} ({hdd_limit}MB), "
                        f"redirecting to worker-{max_hdd_index} ({max_hdd_limit}MB)"
                    )
                    target_worker_index = max_hdd_index

        # Task within limits - assign target worker and queue
        task.target_worker_index = target_worker_index
        scheduler.task_queue.append(task.task_id)  # Normal queue to back
        scheduler.queue_event.set()
        logger.info(f"[Manager] Task {task_id} queued to TAIL for worker-{target_worker_index}")
        
        # Wait for task completion (no timeout - tasks may run arbitrarily long)
        try:
            await scheduler.wait_for_task_completion(task_id, timeout=float('inf'))
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
        
        # Build response (split is disabled - always use direct result)
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

        cpu_limit = completed_task.allocated_cpu_limit if completed_task.allocated_cpu_limit else 100
        assigned_worker_index = completed_task.worker_index
        
        # Format response
        response_data = {
            "task_index": completed_task.task_index,
            "worker_task_response": merged_result,
            "cpu_task_number": cpu_task_number,
            "cpu_limit": cpu_limit,
            "hdd_limit": hdd_limit,
            "mem_limit": mem_limit,
            "allocating_hdd": allocating_hdd,
            "allocating_mem": allocating_mem,
            "response_time": response_time,
            "worker_index": assigned_worker_index,
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
            "queue_size": len(scheduler.task_queue),
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
        
        # Start health check loop
        health_check_task = asyncio.create_task(scheduler.health_check_loop(check_interval=10.0))
        app['health_check_task'] = health_check_task
        
        # Start service discovery loop
        service_discovery_task = asyncio.create_task(scheduler.service_discovery_loop(discovery_interval=30.0))
        app['service_discovery_task'] = service_discovery_task
        
        # No resource monitors needed (static config limits used instead)
        app['hm_monitors'] = []
        
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
        
        # Cancel health check task
        health_check_task: asyncio.Task = app.get('health_check_task')
        if health_check_task:
            health_check_task.cancel()
            try:
                await health_check_task
            except asyncio.CancelledError:
                logger.info("[Manager] Health check task cancelled")
        
        # Cancel service discovery task
        service_discovery_task: asyncio.Task = app.get('service_discovery_task')
        if service_discovery_task:
            service_discovery_task.cancel()
            try:
                await service_discovery_task
            except asyncio.CancelledError:
                logger.info("[Manager] Service discovery task cancelled")
        
        manager: ServerManager = app.get('manager')
        monitor_tasks: List[asyncio.Task] = app.get('hm_monitors', [])
        
        if manager is not None:
            # Close HTTP clients
            for client in manager.http_clients:
                try:
                    await client.close()
                except Exception as exc:
                    logger.error(f"[Manager] Error closing HTTP client: {exc}")
        
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
        web.run_app(app, host='0.0.0.0', port=8084)
    except Exception as e:
        logger.error(f"[Manager] Error running app: {e}")