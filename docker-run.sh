#!/bin/bash

# check if .env file is initialized
[ ! -f ".env1" ] && echo -e "ERROR: .env does not exist.\nUtilize example.env to create a valid .env file.\n"; exit 1;

# if out directory doesn't exist then docker run command will fail
# as it won't be able to mount the volume
[ ! -d "./out" ] && mkdir out

docker run --rm \
    --mount type=volume,dst=/app/out,volume-driver=local,volume-opt=type=none,volume-opt=o=bind,volume-opt=device=$(pwd)/out \
    --env-file .env1 \
    kryptoblack/protexai:latest