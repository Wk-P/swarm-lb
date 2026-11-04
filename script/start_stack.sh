#!/bin/bash
stack_name=collect_data_stack
stack_config_path=~/appv8/stack-config/docker-compose.yaml
sudo docker stack deploy -c $stack_config_path  $stack_name
sudo docker stack services $stack_name
echo "Stack $stack_name started."
