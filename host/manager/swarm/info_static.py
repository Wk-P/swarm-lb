import traceback
import docker       # type: ignore
from docker.models.services import Service      # type: ignore
from docker.models.nodes import Node      # type: ignore
from pathlib import Path
import pandas as pd

def get_docker_node_list():
    docker_client = docker.from_env()
    try:
        nodes: list[Node] = docker_client.nodes.list(filters={"role": "worker"})
        node_list = []
        for node in nodes:
            node_info = {
                "id": node.id,
                "addr": node.attrs.get("Status", {}).get("Addr", ""),
                "hostname": node.attrs.get("Description", {}).get("Hostname", ""),
                "status": node.attrs.get("Status", {}),
                "spec": node.attrs.get("Spec", {}),
            }
            node_list.append(node_info)
        return node_list
    except Exception as e:
        print(f"Error retrieving Docker nodes: {e}")
        traceback.print_exc()
        return []


def get_stack_services(stack_name: str) -> list[dict]:
    docker_client = docker.from_env()
    try:
        services: list[Service] = docker_client.services.list(filters={"label": f"com.docker.stack.namespace={stack_name}"})
        service_list = []
        for service in services:
            service_info = {
                "id": service.id,
                "name": service.name,
                "labels": service.attrs.get("Spec", {}).get("Labels", {}),
            }
            service_list.append(service_info)
        return service_list
    except Exception as e:
        print(f"Error retrieving services for stack {stack_name}: {e}")
        traceback.print_exc()
        return []


def get_hdd_limit(service_name: str, hostname:str, port: int, node_label: str) -> dict:
    """
    Get the HDD limit for a worker via config file and worker labels
    1. find worker label 
    2. find hdd limit from file with worker label
    3. data: {
        "label": 
    }
    Returns:
        intege number: The HDD limit in MB for the worker, or None if not found.
    """
    limit = {}
    path = Path(__file__).parent / 'worker_hdd_limits.json'
    try:
        hdd_data = pd.read_json(path)
        # find the hdd limit for the service
        for _, data in hdd_data.iterrows():
            data_dict = data.to_dict()
            if data_dict.get('label') == node_label:
                return int(data_dict.get('hdd_limit_mb'))
        if limit is None:
            print(f"No HDD limit found for service {service_name} in config file. Using default HDD limit.")
            limit = None  # or set to a default value if desired
        else:
            print(f"HDD limit found for service {service_name} in config file: {limit} MB")
    except Exception as e:
        print(f"Error retrieving HDD limit for service {service_name} on {hostname}:{port} - {e}")
        traceback.print_exc()
    
    return limit

def get_resources_limit(service_name: str) -> dict:
    """
    Get the resource limits for workers with ssh

    Returns:
        dict: A dictionary containing resource limits for workers.
    """

    # use docker client and service name
    limits = {}
    try:
        docker_client = docker.from_env() # requires daemon listening
        service: Service = docker_client.services.get(service_name)

        spec: dict = service.attrs.get("Spec", {})
        task_template: dict = spec.get("TaskTemplate", {})
        resources: dict = task_template.get("Resources", {})
        limits_spec: dict = resources.get("Limits", {})

        # get tasks for get node host ips
        tasks: list[dict] = service.tasks(filters={"desired-state": "running"})
        limits = []

        for task in tasks:
            task_node_id = task.get("NodeID", "")
            try:
                node: Node = docker_client.nodes.get(task_node_id)
                node_status: dict = node.attrs.get("Status", {})
                addr = node_status.get("Addr", "")
                hdd_limit = get_hdd_limit(service.name, addr, 22, node.attrs.get("Spec", {}).get("Labels", {}).get("type", ""))          # real ssh credentials in private deployment
                limit = {
                    'node_id': task_node_id,
                    'node_hostname': node.attrs.get("Description", {}).get("Hostname", ""),
                    'node_host_ip': addr,
                    'service_name': service_name,
                    'cpus_limit_cores': limits_spec.get("NanoCPUs", 0) / 1e9,  # convert from nanocpus to cpus
                    'memory_limit_mb': limits_spec.get("MemoryBytes", 0) / (1024 * 1024),  # convert from bytes to MB
                    'hdd_limit_mb': hdd_limit,
                }
                
            except Exception as e:
                print(f"Error retrieving node host IP for node {task_node_id}: {e}")
                traceback.print_exc()
                limit = {
                    'node_id': task_node_id,
                    'node_hostname': "",
                    'node_host_ip': "",
                    'service_name': service_name,
                    'cpus_limit_cores': limits_spec.get("NanoCPUs", 0) / 1e9,  # convert from nanocpus to cpus
                    'memory_limit_mb': limits_spec.get("MemoryBytes", 0) / (1024 * 1024),  # convert from bytes to MB
                }

            limits.append(limit)

    except Exception as e:
        print(f"Error retrieving resource limits for service {service_name} on: {e}")
        traceback.print_exc()
    
    return limits

def get_stack_services_resources_limits(stack_name: str) -> list[dict]:
    """
    Get the resource limits for all services in a Docker stack.

    Returns:
        dict: A dictionary mapping service names to their resource limits.
    """
    resources_limits = []
    try:
        services = get_stack_services(stack_name)
        for service in services:
            service_name = service['name']
            limits = get_resources_limit(service_name)
            resources_limits.append({
                "service_name": service_name, 
                "limits": limits
            })
    except Exception as e:
        print(f"Error retrieving resource limits for stack {stack_name}: {e}")
        traceback.print_exc()
    
    return resources_limits

def get_service_port_list(service_name: str) -> list[dict]:
    docker_client = docker.from_env()
    port_list = []
    try:
        services: list[Service] = docker_client.services.list(filters={"name": service_name})
        for service in services:
            endpoint: dict = service.attrs.get("Endpoint", {})
            ports: list[dict] = endpoint.get("Ports", [])
            for port_mapping in ports:
                if port_mapping.get("Protocol") == "tcp":
                    port_list.append({
                        "PublishedPort": port_mapping.get("PublishedPort"),
                        "ServiceName": service_name
                    })
    except Exception as e:
        print(f"Error retrieving ports for service {service_name}: {e}")
        traceback.print_exc()
    
    return port_list



if __name__ == "__main__":
    """ test hdd config file """
    limit  = get_hdd_limit("collect_data_stack_app_w1", "worker-01", 22, "low")
    print(limit)