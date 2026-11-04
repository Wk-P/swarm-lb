"""
XGBoost Data Collection Client v1
Client for sending test tasks to Manager server and collecting performance data
"""

import random
import time
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, List
import logging
import asyncio
import aiohttp
import requests

# logging configuration
log_file = Path(__file__).parent / "log" / "xgboost_client_vt1.log"
log_file.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler()  # also log to console
    ]
)

class XGBoostDataCollector:
    """XGBoost data collection client class"""
    
    def __init__(self, manager_url: str):
        """
        Initialize the client
        
        Args:
            manager_url: Manager server URL
        """
        self.manager_url = manager_url
        self.task_url = f"{manager_url}/"
        self.queue_status_url = f"{manager_url}/queue/status"
        
        # Load worker resource limits from config file
        config_path = Path(__file__).parent / "hdd_config.json"
        with open(config_path, 'r', encoding='utf-8') as f:
            _worker_configs = json.load(f)
        
        # Calculate limits for task generation
        self.min_mem_limit = min(cfg["mem_limit_mb"] for cfg in _worker_configs)
        self.max_mem_limit = max(cfg["mem_limit_mb"] for cfg in _worker_configs)
        self.min_hdd_limit = min(cfg["hdd_limit_mb"] for cfg in _worker_configs)
        self.max_hdd_limit = max(cfg["hdd_limit_mb"] for cfg in _worker_configs)
        
        # Task parameter ranges
        # CPU: 0~500000 loops
        self.cpu_range = (0, 500000)
        
        # MEM: 0.3~0.8 times the minimum limit (min=512MB)
        # Reasoning:
        # - Lower bound 0.3 (~154MB): Test small memory scenario
        # - Upper bound 0.8 (~410MB): Approaches w2/w3 limit (512MB), triggers split with safety factor
        # - w1 (1024MB limit) will rarely split on MEM; w2/w3 (512MB) may split
        self.mem_multiplier_range = (0.3, 0.8)
        
        # HDD: 0.3~0.8 times the minimum limit (min=20480MB)
        # Reasoning:
        # - Lower bound 0.3 (~6144MB): Test moderate file scenario
        # - Upper bound 0.8 (~16384MB): Exceeds w1 safe limit (20480*0.5=10240MB), triggers split
        # - w2 (51200MB) and w3 (204800MB) safe limits are much larger, rarely split
        self.hdd_multiplier_range = (0.3, 0.8)
        
        logging.info(f"[Client] Initialized with manager_url={manager_url}")
        logging.info(f"[Client] Worker limits: min_mem={self.min_mem_limit}MB, max_mem={self.max_mem_limit}MB")
        logging.info(f"[Client] Worker limits: min_hdd={self.min_hdd_limit}MB, max_hdd={self.max_hdd_limit}MB")
        logging.info(f"[Client] MEM multiplier range: {self.mem_multiplier_range[0]}~{self.mem_multiplier_range[1]}")
        logging.info(f"[Client] HDD multiplier range: {self.hdd_multiplier_range[0]}~{self.hdd_multiplier_range[1]}")
        
        # Collect all responses for this session
        self.collected_responses = []
        self.session_start_time = datetime.now()
        self.session: Optional[aiohttp.ClientSession] = None
    
    def generate_random_task(self, task_index: str) -> Dict:
        """
        Generate random task parameters
        
        Args:
            task_index: Task index identifier
            
        Returns:
            Dictionary containing task parameters
        """
        # Random CPU task
        cpu_task_number = random.randint(self.cpu_range[0], self.cpu_range[1])
        
        # Random MEM task (based on multiplier of minimum limit)
        mem_multiplier = random.uniform(self.mem_multiplier_range[0], self.mem_multiplier_range[1])
        mem_size_mb = int(round(self.min_mem_limit * mem_multiplier))
        
        # Random HDD task (based on multiplier of minimum limit)
        hdd_multiplier = random.uniform(self.hdd_multiplier_range[0], self.hdd_multiplier_range[1])
        hdd_size_mb = int(round(self.min_hdd_limit * hdd_multiplier))
        
        task = {
            "task_index": task_index,
            "cpu_task": {
                "number": cpu_task_number
            },
            "mem_task": {
                "size_mb": mem_size_mb
            },
            "hdd_task": {
                "size_mb": hdd_size_mb
            },
            "generated_at": datetime.now().isoformat()
        }
        
        logging.info(f"[Client] Generated task {task_index}: "
                   f"CPU={cpu_task_number}, MEM={mem_size_mb}MB, HDD={hdd_size_mb}MB")
        
        return task
    
    async def send_task_async(self, task: Dict, timeout: int = 1800) -> Optional[Dict]:
        """
        Send task to Manager server (async version)
        
        Args:
            task: Task parameter dictionary
            timeout: Request timeout (seconds, default 30min)
            
        Returns:
            Response data dictionary, None if failed
        """
        task_index = task["task_index"]
        
        # Prepare request headers
        headers = {
            "task_index": task_index,
            "Content-Type": "application/json"
        }
        
        # Prepare request body
        request_body = {
            "cpu_task": task["cpu_task"],
            "mem_task": task["mem_task"],
            "hdd_task": task["hdd_task"]
        }
        
        try:
            logging.info(f"[Client] Sending task {task_index} to {self.task_url}")
            start_time = time.time()
            
            # Create session if not exists
            if self.session is None:
                self.session = aiohttp.ClientSession()
            
            async with self.session.post(
                self.task_url,
                headers=headers,
                json=request_body,
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as response:
                elapsed_time = time.time() - start_time
                
                if response.status == 200:
                    result = await response.json()
                    logging.info(f"[Client] Task {task_index} completed successfully "
                               f"(client_elapsed={elapsed_time:.2f}s, "
                               f"server_response_time={result.get('response_time', 'N/A')}s)")
                    
                    # Add client-measured elapsed time
                    result["client_elapsed_time"] = round(elapsed_time, 3)
                    
                    # Collect response for session summary
                    self.collected_responses.append(result)
                    
                    return result
                else:
                    response_text = await response.text()
                    logging.error(f"[Client] Task {task_index} failed with status {response.status}")
                    logging.error(f"[Client] Response: {response_text}")
                    return None
                    
        except asyncio.TimeoutError:
            logging.error(f"[Client] Task {task_index} timeout after {timeout}s")
            return None
        except aiohttp.ClientError as e:
            logging.error(f"[Client] Task {task_index} request error: {e}")
            return None
        except Exception as e:
            logging.error(f"[Client] Task {task_index} unexpected error: {e}")
            return None
    
    def get_queue_status(self) -> Optional[Dict]:
        """
        Get queue status
        
        Returns:
            Queue status dictionary, None if failed
        """
        try:
            response = requests.get(self.queue_status_url, timeout=5)
            if response.status_code == 200:
                return response.json()
            else:
                logging.error(f"[Client] Failed to get queue status: {response.status_code}")
                return None
        except Exception as e:
            logging.error(f"[Client] Error getting queue status: {e}")
            return None
    
    def save_result(self, result: Dict, output_dir: Path):
        """
        Save single task result to file
        
        Args:
            result: Task result dictionary
            output_dir: Output directory
        """
        if not result:
            return
        
        # Create output directory
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # Generate filename
        task_index = result.get("task_index", "unknown")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"result_{task_index}_{timestamp}.json"
        
        # Save to file
        filepath = output_dir / filename
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        
        logging.info(f"[Client] Saved result to {filepath}")
    
    def save_collected_responses(self, output_dir: Optional[Path] = None):
        """
        Save all collected responses to a date-named v1.json file
        
        Args:
            output_dir: Output directory, defaults to data_xgb/
        """
        if not self.collected_responses:
            logging.warning("[Client] No responses collected, skipping save")
            return
        
        # Set default output directory
        if output_dir is None:
            output_dir = Path(__file__).parent / "data_xgb"
        
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # Generate filename with date + v1.json format
        date_str = self.session_start_time.strftime("%Y%m%d")
        filename = f"{date_str}_v1.json"
        filepath = output_dir / filename
        
        # Prepare summary data
        summary_data = {
            "session_info": {
                "start_time": self.session_start_time.isoformat(),
                "end_time": datetime.now().isoformat(),
                "total_responses": len(self.collected_responses)
            },
            "responses": self.collected_responses
        }
        
        # Save to file
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(summary_data, f, indent=2, ensure_ascii=False)
        
        logging.info(f"[Client] Saved {len(self.collected_responses)} responses to {filepath}")
    
    async def run_wave_test(self,
                           num_waves: int,
                           tasks_per_wave_min: int = 3,
                           tasks_per_wave_max: int = 10,
                           wave_interval: Optional[float] = None,
                           output_dir: Optional[Path] = None) -> List[Dict]:
        """
        Run wave test - send n tasks per wave, wait m seconds (1~3) between waves
        
        Args:
            num_waves: Number of waves to send
            tasks_per_wave_min: Minimum tasks per wave (default 3)
            tasks_per_wave_max: Maximum tasks per wave (default 10)
            wave_interval: Seconds between waves (None for random 1~3)
            output_dir: Output directory for results
            
        Returns:
            List of all successful task results
        """
        logging.info(f"[Client] Starting wave test with {num_waves} waves")
        
        # Set default output directory
        if output_dir is None:
            timestamp = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
            output_dir = Path(__file__).parent / "data_xgb" / f"wave_{timestamp}"
        
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # Create session if not exists
        if self.session is None:
            self.session = aiohttp.ClientSession()
        
        all_results = []
        wave_start_time = time.time()
        
        for wave_num in range(1, num_waves + 1):
            # Random tasks per wave within specified range
            n = random.randint(tasks_per_wave_min, tasks_per_wave_max)
            
            logging.info(f"[Client] Wave {wave_num}/{num_waves}: Sending {n} tasks concurrently")
            
            # Generate tasks for this wave
            wave_tasks = []
            for i in range(n):
                task_index = f"wave{wave_num:02d}_task_{i+1:02d}"
                task = self.generate_random_task(task_index)
                wave_tasks.append(task)
            
            # Send all tasks in this wave concurrently
            send_tasks = [self.send_task_async(task) for task in wave_tasks]
            results = await asyncio.gather(*send_tasks, return_exceptions=True)
            
            # Collect successful results
            wave_success = 0
            for result in results:
                if isinstance(result, dict) and result is not None:
                    all_results.append(result)
                    self.save_result(result, output_dir)
                    wave_success += 1
            
            logging.info(f"[Client] Wave {wave_num} completed: {wave_success}/{n} successful")
            
            # Wait before next wave (except for last wave)
            if wave_num < num_waves:
                m = wave_interval if wave_interval else random.uniform(1.0, 3.0)
                logging.info(f"[Client] Waiting {m:.1f}s before next wave...")
                await asyncio.sleep(m)
        
        total_elapsed = time.time() - wave_start_time
        
        # Save summary
        summary = {
            "wave_config": {
                "num_waves": num_waves,
                "tasks_per_wave_range": f"{tasks_per_wave_min}~{tasks_per_wave_max}",
                "wave_interval_range": "1~3s" if not wave_interval else f"{wave_interval}s",
                "total_elapsed_time": round(total_elapsed, 3)
            },
            "total_tasks": len(all_results),
            "successful_tasks": len(all_results),
            "test_timestamp": datetime.now().isoformat(),
            "results": all_results
        }
        
        summary_file = output_dir / "summary.json"
        with open(summary_file, 'w', encoding='utf-8') as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        
        logging.info(f"[Client] Wave test completed: {len(all_results)} tasks in {num_waves} waves")
        logging.info(f"[Client] Total time: {total_elapsed:.1f}s, Results saved to {output_dir}")
        
        # Save all collected responses to date_v1.json
        self.save_collected_responses()
        
        return all_results
    
    async def cleanup(self):
        """Close aiohttp session"""
        if self.session:
            await self.session.close()
            self.session = None


def _manager_ip() -> str:
    """manager IP，按顺序查找：
    1. 环境变量 MANAGER_IP
    2. 环境变量 SWARM_ENV 指定的 <仓库>/envs/<名>.env（开发机）
    3. 本目录下的 .env（他人机器只拷了 clients/ 时用，写一行 MANAGER_IP=...）
    """
    import os
    if os.environ.get("MANAGER_IP"):
        return os.environ["MANAGER_IP"]
    here = Path(__file__).resolve().parent
    cands = [here / ".env"]
    if os.environ.get("SWARM_ENV"):
        cands.insert(0, here.parents[1] / "envs" / f"{os.environ['SWARM_ENV']}.env")
    for env in cands:
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("MANAGER_IP="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    raise SystemExit("未找到 MANAGER_IP：设置环境变量 MANAGER_IP，或 SWARM_ENV=<环境名>（读 envs/<环境名>.env），"
                     "或在本目录放一个 .env 写 MANAGER_IP=...")


async def main_async(num_waves: int, tasks_min: int = 3, tasks_max: int = 10):
    """Main async function - example usage
    
    Args:
        num_waves: Number of waves to send (default 1)
        tasks_min: Minimum tasks per wave (default 3)
        tasks_max: Maximum tasks per wave (default 10)
    """
    
    # Create client instance
    client = XGBoostDataCollector(manager_url=f"http://{_manager_ip()}:8081")
    
    try:
        # Check queue status
        queue_status = client.get_queue_status()
        if queue_status:
            logging.info(f"[Main] Current queue status: {queue_status}")
        
        # Wave test: Send n tasks per wave, wait m seconds (1~3) between waves
        logging.info(f"\n=== Running wave test ({num_waves} waves, {tasks_min}~{tasks_max} tasks/wave, 1~3s interval) ===")
        results = await client.run_wave_test(num_waves=num_waves, tasks_per_wave_min=tasks_min, tasks_per_wave_max=tasks_max)
        logging.info(f"[Main] Wave test completed. Total successful: {len(results)}")
        
    finally:
        # Cleanup
        await client.cleanup()


if __name__ == "__main__":
    import sys
    
    logging.info("[Main] Running in ASYNC mode")
    logging.info("[Main] Usage: python client_v1.py [num_waves] [tasks_min] [tasks_max]")
    logging.info("[Main] Example: python client_v1.py 20 3 5  (send 20 waves, 3~5 tasks per wave)")
    
    # Get parameters from command line
    num_waves = 1
    tasks_min = 4
    tasks_max = 5
    
    if len(sys.argv) > 1:
        try:
            num_waves = int(sys.argv[1])
            logging.info(f"[Main] Using num_waves={num_waves} from command line")
        except ValueError:
            logging.warning(f"[Main] Invalid num_waves '{sys.argv[1]}', using default 1")
    
    if len(sys.argv) > 2:
        try:
            tasks_min = int(sys.argv[2])
            logging.info(f"[Main] Using tasks_min={tasks_min} from command line")
        except ValueError:
            logging.warning(f"[Main] Invalid tasks_min '{sys.argv[2]}', using default 4")
    
    if len(sys.argv) > 3:
        try:
            tasks_max = int(sys.argv[3])
            logging.info(f"[Main] Using tasks_max={tasks_max} from command line")
        except ValueError:
            logging.warning(f"[Main] Invalid tasks_max '{sys.argv[3]}', using default 5")
    
    # Validate range
    if tasks_min > tasks_max:
        logging.warning(f"[Main] tasks_min ({tasks_min}) > tasks_max ({tasks_max}), swapping values")
        tasks_min, tasks_max = tasks_max, tasks_min
    
    asyncio.run(main_async(num_waves, tasks_min, tasks_max))
