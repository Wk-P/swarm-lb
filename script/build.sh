# !/bin/bash
cd /home/pi/appv8/docker

# clear previous images
sudo docker rmi soar009/collect_data_app:v8 -f
sudo docker build -t soar009/collect_data_app:v8 .
sudo docker push soar009/collect_data_app:v8