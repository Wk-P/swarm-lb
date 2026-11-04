#!/bin/bash
set -e
source "$(dirname "$0")/../deploy/lib.sh"
require STACK_NAME
sudo docker stack rm "$STACK_NAME"
echo "Stack $STACK_NAME stopped."
