#!/bin/bash
stack_name=collect_data_stack
sudo docker stack rm $stack_name
echo "Stack $stack_name stopped."