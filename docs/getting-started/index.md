# 2. Getting Started

## Prerequisites

You need the following installed on your machine:

| Dependency | Purpose |
|---|---|
| **Docker** | Container runtime (required by k3d) |
| **curl, wget, jq, envsubst** | Used by the setup script |
| **libnss3-tools** | Required by mkcert for TLS certificates |

The setup script will automatically install **kubectl**, **helm**, **k3d**, and **mkcert** into `~/.surogates/bin/`.

## Cluster Setup

Surogates ships with a one-command setup script that creates a local k3d cluster with all infrastructure dependencies pre-configured.

```bash
cd k8s/
./setup-cluster.sh
```

This single command:

1. **Installs tooling** -- kubectl, helm, k3d, and mkcert (into `~/.surogates/bin/`, skipped if already present).
2. **Creates a k3d cluster** -- a lightweight K3s cluster running inside Docker containers (1 server + 1 agent node).
3. **Installs Traefik** -- ingress controller with auto-generated TLS certificates for `*.k8s.localhost`.
4. **Installs PostgreSQL** -- session store, event log, tenant data (Bitnami Helm chart).
5. **Installs Redis** -- work queue, delivery nudges, rate limiting (Bitnami Helm chart).
6. **Installs Garage** -- S3-compatible object storage for workspace files and tenant assets.
7. **Deploys Surogates resources** -- namespace, secrets, ConfigMaps, RBAC, ingress routes.
8. **Generates a dev config** -- writes `~/.surogates/config.yaml` pre-configured to connect to the cluster services.

### What the Cluster Looks Like

```
k3d cluster: "surogates"
|
+-- Traefik (ingress, TLS)
|     surogates.k8s.localhost --> api-gateway:8000
|
+-- PostgreSQL (Helm: bitnami/postgresql)
|     NodePort: localhost:32432
|
+-- Redis (Helm: bitnami/redis)
|     NodePort: localhost:32379
|
+-- Garage (Helm: charts-derwitt-dev/garage)
|     NodePort: localhost:30900 (S3 API)
|     Persistent volume: ~/.surogates/garage-data/
|
+-- Namespace: surogates
      Secrets: surogates-db, surogates-redis, surogates-jwt, surogates-s3
      ConfigMaps: surogates config
      RBAC: worker + sandbox ServiceAccounts
      IngressRoute: surogates.k8s.localhost
```

### Exposed Ports

| Service | Port | Protocol |
|---|---|---|
| PostgreSQL | `localhost:32432` | PostgreSQL wire protocol |
| Redis | `localhost:32379` | Redis protocol |
| Garage S3 API | `localhost:30900` | HTTP (S3-compatible) |
| HTTPS (Traefik) | `localhost:443` | HTTPS (TLS via mkcert) |
| K8s API | `localhost:6443` | HTTPS |

## Garage Post-Install Setup

After the cluster is running, Garage needs a one-time layout and key setup. The setup script prints these commands at the end:

```bash
# Find the Garage pod
GARAGE_POD=$(kubectl get pods -l app.kubernetes.io/name=garage -o jsonpath='{.items[0].metadata.name}')

# Assign the node to the cluster layout
kubectl exec -ti $GARAGE_POD -- /garage layout assign -z dc1 -c 1T \
  $(kubectl exec $GARAGE_POD -- /garage node id -q | cut -d@ -f1)

# Apply the layout
kubectl exec -ti $GARAGE_POD -- /garage layout apply --version 1

# Create an access key for Surogates
kubectl exec -ti $GARAGE_POD -- /garage key create surogates-key

# Allow the key to create buckets
kubectl exec -ti $GARAGE_POD -- /garage key allow surogates-key --create-bucket
```

The `key create` command outputs an **access key** and **secret key**. You need these in the next step.

## Configuration

The setup script generates `~/.surogates/config.yaml` with all service endpoints pre-filled. You need to complete two things:

### 1. Paste Garage Credentials

Open `~/.surogates/config.yaml` and fill in the `storage.access_key` and `storage.secret_key` fields with the Garage key output from the previous step.

### 2. Configure an LLM Provider

Add your LLM provider credentials. Surogates works with any OpenAI-compatible endpoint:

```yaml
llm:
  model: "claude-sonnet-4-20250514"
  base_url: "https://api.anthropic.com/v1"
  api_key: "sk-ant-..."
```

Or use OpenRouter, OpenAI, Together, or any other provider:

```yaml
llm:
  model: "gpt-4o"
  base_url: "https://api.openai.com/v1"
  api_key: "sk-..."
```

See [Appendix A: Configuration Reference](../appendices/configuration.md) for all settings.

## Running Surogates

With the cluster running and config complete, start the API server and a worker:

```bash
# Terminal 1: API server
SUROGATES_CONFIG=~/.surogates/config.yaml surogates api

# Terminal 2: Worker
SUROGATES_CONFIG=~/.surogates/config.yaml surogates worker
```

The API server serves the REST API and web chat UI. The worker processes sessions by pulling from the Redis queue and running the agent harness.

### Start the Web Chat UI

```bash
cd web/
npm install
npm run dev
```

The UI runs at `http://localhost:5173`. In production, the API server serves the built SPA at `https://surogates.k8s.localhost`.

## Your First Session

1. Open the web chat UI in your browser.
2. Log in with your credentials.
3. Click **New Session** to create a conversation.
4. Type a message and press Enter.

What happens behind the scenes:

```
Browser: POST /v1/sessions/{id}/messages
    |
    v
API Server: validate JWT -> resolve tenant -> emit user.message event -> enqueue to Redis
    |
    v
Worker: dequeue session_id -> wake(session_id) -> replay events -> LLM loop
    |
    v (events emitted to PostgreSQL as they happen)
    |
Browser: GET /v1/sessions/{id}/events?after=N (SSE stream)
    |
    v
User sees the agent's response, tool calls, and results in real time
```

The browser subscribes to Server-Sent Events (SSE) and renders each event as it arrives -- LLM text, tool invocations, tool results, thinking blocks.

## Re-deploying After Code Changes

If you only need to update the Surogates manifests (namespace, secrets, RBAC) without recreating the cluster:

```bash
cd k8s/
./setup-cluster.sh deploy
```

This applies the Surogates resources to the existing cluster.

## Tearing Down

```bash
~/.surogates/bin/k3d cluster delete surogates
```

Garage data persists in `~/.surogates/garage-data/` across cluster restarts. Delete that directory to start fresh.
