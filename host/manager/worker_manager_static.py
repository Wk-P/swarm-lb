from typing import TypedDict, Any
import asyncio
import traceback
from aiohttp import ClientTimeout
import aiohttp
from swarm.info_static import get_stack_services, get_stack_services_resources_limits, get_docker_node_list, get_service_port_list
from pathlib import Path

class ManagerResponse(TypedDict):
    task_index: str
    task_class: str
    error: int
    message: str
    success: Any
    response_data: Any


class WorkerSSHClient:
    """Lightweight worker client holding static resource limits from config (no SSH/Docker)."""
    def __init__(self, server_url: str, hostname: str, service_name: str = None,
                 hdd_limit_mb: float = 0, memory_limit_mb: float = 0, cpu_limit_cores: float = 1.0):
        self.server_url = server_url
        self.hostname = hostname
        self.service_name = service_name

        # Static resource limits from config file (all in MB)
        self.max_hdd_usage = hdd_limit_mb or 0       # MB
        self.max_mem_usage = memory_limit_mb or 0    # MB
        self.cpu_limit = (cpu_limit_cores * 100) if cpu_limit_cores else 100  # percentage (100 = 1 core)

        print(f"WorkerClient initialized: {self.hostname} / {self.service_name} | "
              f"HDD={self.max_hdd_usage}MB, MEM={self.max_mem_usage}MB, CPU={self.cpu_limit}%")



# Round-robin server manager using itertools.cycle (no async lock needed)
class ServerManager:
    def __init__(self, **kwargs):
        # get worker resource limits
        # return (cpus_limit_cores, memory_limit_mb)
        if kwargs.get("stack_name") is None:
            with open(Path(__file__).parent / 'stack_name', 'r', encoding='utf-8') as f:
                self.stack_name = f.read().strip()
        else:
            self.stack_name = kwargs.get("stack_name")


        print(f"Load Stack Name: {self.stack_name}")

        self.worker_resources_limits_list = get_stack_services_resources_limits(stack_name=self.stack_name)
        self.node_list = get_docker_node_list()

        print(f"Worker Resources Limits for Workers...")
        print(f"Worker Resources Limits: {[res for res in self.worker_resources_limits_list]}")

        print(f"Get Docker Nodes...")
        print(f"Docker Nodes: {[node for node in self.node_list]}")

        self.post_list = []
        self.services_list = get_stack_services(stack_name=self.stack_name)
        print(f"Stack Services: {[service for service in self.services_list]}")

        for service in self.services_list:
            self.post_list.append(get_service_port_list(service_name=service['name']))

        print(f"Get Services Ports...")
        print(f"Services Ports: {self.post_list}")

        # Build a unified workers list from discovered resource limits and ports.
        # Each entry contains node/service info and the request URL to use.
        self.workers: list[dict] = []

        # map service_name -> published port (first seen)
        port_map: dict[str, int] = {}
        for ports in self.post_list:
            if ports and isinstance(ports, list) and ports[0].get('ServiceName'):
                svc = ports[0].get('ServiceName')
                port_map.setdefault(svc, ports[0].get('PublishedPort'))

        # prefer using worker_resources_limits_list as authoritative mapping of service -> node
        for limits_info in self.worker_resources_limits_list:
            svc_name = limits_info.get('service_name')
            for limit in limits_info.get('limits', []):
                node_ip = limit.get('node_host_ip', '')
                pub_port = port_map.get(svc_name)
                if pub_port:
                    url = f"http://{node_ip}:{pub_port}"
                else:
                    # if no published port known, fallback to bare node ip
                    url = f"http://{node_ip}"

                self.workers.append({
                    'service_name': svc_name,
                    'node_id': limit.get('node_id'),
                    'node_hostname': limit.get('node_hostname'),
                    'node_host_ip': node_ip,
                    'published_port': pub_port,
                    'url': url,
                    'cpus_limit_cores': limit.get('cpus_limit_cores'),
                    'memory_limit_mb': limit.get('memory_limit_mb'),
                    'hdd_limit_mb': limit.get('hdd_limit_mb'),
                })

        # If no workers discovered from limits, fallback to zipping node_list and post_list
        if not self.workers:
            for node, port in zip(self.node_list, self.post_list):
                if not port:
                    continue
                svc = port[0].get('ServiceName')
                pub = port[0].get('PublishedPort')
                node_addr = node.get('addr')
                self.workers.append({
                    'service_name': svc,
                    'node_id': node.get('id'),
                    'node_hostname': node.get('hostname'),
                    'node_host_ip': node_addr,
                    'published_port': pub,
                    'url': f"http://{node_addr}:{pub}",
                    'cpus_limit_cores': 0,
                    'memory_limit_mb': 0,
                    'hdd_limit_mb': None,
                })

        # Build backward-compatible structures used elsewhere
        self.worker_urls = [{ 'url': w['url'], 'service_name': w['service_name'] } for w in self.workers]

        print(f"Initialized ServerManager with {len(self.worker_urls)} worker nodes.")
        print(f"Worker URLs: {self.worker_urls}")

        self.http_clients = [
            aiohttp.ClientSession(
                timeout=ClientTimeout(total=0),
                connector=aiohttp.TCPConnector(limit=5000),
            ) for _ in self.worker_urls
        ]
        self.task_urls = {
            "HDD": "/hdd/",
            "MEM": "/mem/",
            "CPU": "/cpu/",
            "cpu": "/cpu/",
            "mem": "/mem/",
            "hdd": "/hdd/",
        }

        
        print(f"Load Stack Name: {self.stack_name}")
        print(f"Get Services Resources Limits for Workers...")
        print(f"Worker Services Names: {[res['service_name'] for res in self.worker_resources_limits_list]}")

        # cpu task round-robin index
        self.round_robin_index = 0
        self.round_robin_index_lock = asyncio.Lock()
        

        # create WorkerClient instances based on unified workers list (config limits, no SSH)
        self.worker_ssh_clients: list[WorkerSSHClient] = []
        for w in self.workers:
            hostname = w.get('node_host_ip') or self.find_hostip_by_servicename(w.get('service_name'))
            try:
                worker_client = WorkerSSHClient(
                    server_url=w.get('url'),
                    hostname=hostname,
                    service_name=w.get('service_name'),
                    hdd_limit_mb=w.get('hdd_limit_mb') or 0,
                    memory_limit_mb=w.get('memory_limit_mb') or 0,
                    cpu_limit_cores=w.get('cpus_limit_cores') or 1.0,
                )
                self.worker_ssh_clients.append(worker_client)
                print(f"Successfully added worker client for {w.get('service_name')}")
            except Exception as exc:
                print(f"Failed to create WorkerClient for {w.get('service_name')}@{hostname}: {exc}")
                traceback.print_exc()

    def find_hostip_by_servicename(self, service_name: str) -> str:
        # First try unified workers list
        if hasattr(self, 'workers'):
            for w in self.workers:
                if w.get('service_name') == service_name:
                    ip = w.get('node_host_ip')
                    if ip:
                        print(f"[find_hostip_by_servicename] Found host IP from workers: {ip} for service: {service_name}")
                        return ip

        # Fallback to original resource limits list
        for limits in self.worker_resources_limits_list:
            if limits['service_name'] == service_name:
                limits_inner = limits.get('limits', [])
                if limits_inner:
                    print(f"[find_hostip_by_servicename] Found host IP from limits: {limits_inner[0].get('node_host_ip')} for service: {service_name}")
                    return limits_inner[0].get('node_host_ip')

        # Final fallback to first node address
        if self.node_list:
            return self.node_list[0].get('addr', '')
        return ''

    async def get_and_update_round_robin_index(self) -> int:
        async with self.round_robin_index_lock:
            index = self.round_robin_index
            self.round_robin_index = (self.round_robin_index + 1) % len(self.worker_urls)
        return index
    
    async def refresh_workers(self) -> dict:
        """Refresh workers list by re-discovering services and containers
        
        Returns:
            dict with 'added', 'removed', 'updated' lists of worker info
        """
        print("[ServerManager] Refreshing workers list...")
        
        changes = {
            'added': [],
            'removed': [],
            'updated': []
        }
        
        try:
            # Re-fetch service and node information
            new_worker_resources_limits_list = get_stack_services_resources_limits(stack_name=self.stack_name)
            new_node_list = get_docker_node_list()
            new_services_list = get_stack_services(stack_name=self.stack_name)
            
            # Build new workers list
            new_workers = []
            new_post_list = []
            
            for service in new_services_list:
                new_post_list.append(get_service_port_list(service_name=service['name']))
            
            # Map service_name -> published port
            port_map = {}
            for ports in new_post_list:
                if ports and isinstance(ports, list) and ports[0].get('ServiceName'):
                    svc = ports[0].get('ServiceName')
                    port_map.setdefault(svc, ports[0].get('PublishedPort'))
            
            # Build new workers list from limits
            for limits_info in new_worker_resources_limits_list:
                svc_name = limits_info.get('service_name')
                for limit in limits_info.get('limits', []):
                    node_ip = limit.get('node_host_ip', '')
                    pub_port = port_map.get(svc_name)
                    if pub_port:
                        url = f"http://{node_ip}:{pub_port}"
                    else:
                        url = f"http://{node_ip}"
                    
                    new_workers.append({
                        'service_name': svc_name,
                        'node_id': limit.get('node_id'),
                        'node_hostname': limit.get('node_hostname'),
                        'node_host_ip': node_ip,
                        'published_port': pub_port,
                        'url': url,
                        'cpus_limit_cores': limit.get('cpus_limit_cores'),
                        'memory_limit_mb': limit.get('memory_limit_mb'),
                        'hdd_limit_mb': limit.get('hdd_limit_mb'),
                    })
            
            # Detect changes
            old_worker_keys = {(w['service_name'], w['node_hostname']) for w in self.workers}
            new_worker_keys = {(w['service_name'], w['node_hostname']) for w in new_workers}
            
            # Find added and removed workers
            added_keys = new_worker_keys - old_worker_keys
            removed_keys = old_worker_keys - new_worker_keys
            
            for key in added_keys:
                worker = next(w for w in new_workers if (w['service_name'], w['node_hostname']) == key)
                changes['added'].append(worker)
                print(f"[ServerManager] New worker detected: {worker['service_name']}@{worker['node_hostname']}")
            
            for key in removed_keys:
                worker = next(w for w in self.workers if (w['service_name'], w['node_hostname']) == key)
                changes['removed'].append(worker)
                print(f"[ServerManager] Worker removed: {worker['service_name']}@{worker['node_hostname']}")
            
            # Update internal state
            self.workers = new_workers
            self.worker_urls = [{'url': w['url'], 'service_name': w['service_name']} for w in self.workers]
            self.worker_resources_limits_list = new_worker_resources_limits_list
            self.node_list = new_node_list
            self.services_list = new_services_list
            self.post_list = new_post_list
            
            # Update HTTP clients (add new, keep existing)
            current_client_count = len(self.http_clients)
            new_client_count = len(self.worker_urls)
            
            if new_client_count > current_client_count:
                # Add new clients
                for _ in range(new_client_count - current_client_count):
                    self.http_clients.append(
                        aiohttp.ClientSession(
                            timeout=ClientTimeout(total=0),
                            connector=aiohttp.TCPConnector(limit=5000),
                        )
                    )
                print(f"[ServerManager] Added {new_client_count - current_client_count} new HTTP clients")
            elif new_client_count < current_client_count:
                # Close excess clients
                for i in range(new_client_count, current_client_count):
                    try:
                        await self.http_clients[i].close()
                    except Exception as e:
                        print(f"[ServerManager] Error closing HTTP client {i}: {e}")
                self.http_clients = self.http_clients[:new_client_count]
                print(f"[ServerManager] Removed {current_client_count - new_client_count} HTTP clients")
            
            # Update worker clients (add new workers with config limits)
            for worker in changes['added']:
                hostname = worker.get('node_host_ip')
                try:
                    new_worker_client = WorkerSSHClient(
                        server_url=worker.get('url'),
                        hostname=hostname,
                        service_name=worker.get('service_name'),
                        hdd_limit_mb=worker.get('hdd_limit_mb') or 0,
                        memory_limit_mb=worker.get('memory_limit_mb') or 0,
                        cpu_limit_cores=worker.get('cpus_limit_cores') or 1.0,
                    )
                    self.worker_ssh_clients.append(new_worker_client)
                    changes['updated'].append(f"Added worker client for {worker['service_name']}")
                except Exception as exc:
                    print(f"[ServerManager] Failed to create WorkerClient for {worker['service_name']}: {exc}")
                    traceback.print_exc()
            
            print(f"[ServerManager] Refresh complete: {len(changes['added'])} added, {len(changes['removed'])} removed")
            return changes
            
        except Exception as e:
            print(f"[ServerManager] Error refreshing workers: {e}")
            traceback.print_exc()
            return changes
    

    def check_worker_resources(self, worker_index: int, task_class: str) -> bool:
        """Stub: real-time usage tracking removed; resource gating is handled by TaskScheduler."""
        return True
    
if __name__ == "__main__":
    # test code
    manager = ServerManager()
    for i, client in enumerate(manager.worker_ssh_clients):
        print(f"Worker {i}: {client.hostname} | service={client.service_name} "
              f"| Max MEM={client.max_mem_usage}MB, Max HDD={client.max_hdd_usage}MB, CPU={client.cpu_limit}%")