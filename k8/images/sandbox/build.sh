#!/bin/bash

docker build -t ghcr.io/invergent-ai/agent-sandbox:latest -f Dockerfile .
docker push ghcr.io/invergent-ai/agent-sandbox:latest