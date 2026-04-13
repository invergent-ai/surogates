#!/bin/bash

docker build -t ghcr.io/invergent-ai/agent-sandbox-s3fs:latest -f Dockerfile .
docker push ghcr.io/invergent-ai/agent-sandbox-s3fs:latest