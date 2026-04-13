# 4. Deployment

Surogates is Kubernetes-native. All components run as K8s workloads. There is no docker-compose or bare-metal deployment mode.

## Namespace

All Surogates resources live in the `surogates` namespace:

```yaml
# k8/base/namespace.yaml
apiVersion: v1
kind: Namespace
metadata:
  name: surogates
```

## Component Overview

```
Namespace: surogates
|
+-- Deployment: api-gateway        (2-3 replicas, FastAPI)
+-- Deployment: worker             (HPA 5-20 replicas)
+-- Pods: sandbox-{session_id}     (ephemeral, created by worker on demand)
+-- Deployment: channel-adapters   (1-2 replicas per channel type)
+-- Deployment: mcp-proxy          (2-3 replicas)
+-- StatefulSet: postgres          (or managed: RDS, CloudSQL, AlloyDB)
+-- Deployment: redis              (or managed: ElastiCache, Memorystore)
+-- StatefulSet: garage            (S3-compatible object storage)
```

## API Server Deployment

The API server serves the REST API, web chat SPA, and SSE event streams.

```yaml
# k8/base/api-deployment.yaml (simplified)
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api-gateway
  namespace: surogates
spec:
  replicas: 2
  template:
    spec:
      containers:
      - name: api
        image: ghcr.io/invergent-ai/surogates:latest
        command: ["uvicorn", "surogates.api.app:create_app", "--factory",
                  "--host", "0.0.0.0", "--port", "8000"]
        ports:
        - containerPort: 8000
        env:
        - name: SUROGATES_CONFIG
          value: /etc/surogates/config.yaml
        volumeMounts:
        - name: config
          mountPath: /etc/surogates/config.yaml
          subPath: config.yaml
        - name: platform-skills
          mountPath: /etc/surogates/skills
          readOnly: true
        readinessProbe:
          httpGet:
            path: /health
            port: 8000
```

The API server needs:
- Database credentials (sessions, tenants, credentials)
- S3 credentials for all tenant and session buckets
- JWT signing secret
- Platform volumes (skills, tools, MCP configs, policies) mounted read-only

## Worker Deployment

Workers run the orchestrator loop, pulling sessions from the Redis queue and executing the agent harness.

```yaml
# k8/base/worker-deployment.yaml (simplified)
apiVersion: apps/v1
kind: Deployment
metadata:
  name: worker
  namespace: surogates
spec:
  replicas: 5
  template:
    spec:
      serviceAccountName: surogates-worker
      containers:
      - name: worker
        image: ghcr.io/invergent-ai/surogates:latest
        command: ["python", "-m", "surogates.orchestrator.worker"]
        env:
        - name: SUROGATES_CONFIG
          value: /etc/surogates/config.yaml
        volumeMounts:
        - name: config
          mountPath: /etc/surogates/config.yaml
          subPath: config.yaml
        - name: platform-skills
          mountPath: /etc/surogates/skills
          readOnly: true
        - name: platform-tools
          mountPath: /etc/surogates/tools
          readOnly: true
        - name: platform-mcp
          mountPath: /etc/surogates/mcp
          readOnly: true
        - name: platform-policies
          mountPath: /etc/surogates/policies
          readOnly: true
```

The worker needs:
- Database credentials (sessions, events, leases)
- Redis access (work queue, nudges)
- API server URL + token (for skills/memory operations)
- K8s API access (ServiceAccount with RBAC to create/delete sandbox pods)
- Platform volumes mounted read-only

### HPA Scaling

Workers scale horizontally based on active sessions:

```yaml
# k8/base/hpa.yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: worker
  namespace: surogates
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: worker
  minReplicas: 5
  maxReplicas: 20
  metrics:
  - type: Pods
    pods:
      metric:
        name: surogates_active_sessions
      target:
        type: AverageValue
        averageValue: "40"
```

Target: average 40 active sessions per worker (out of 50 max concurrency).

## Sandbox Pods

Sandbox pods are created dynamically by workers when a session first needs isolated execution. They are not pre-provisioned.

**Pod structure:**
- **Main container**: Full `surogates` package + `tool-executor` (Python). Receives tool calls via K8s exec API.
- **s3fs sidecar**: FUSE-mounts the session's Garage bucket (`session-{session_id}`) as `/workspace`.
- **Credentials**: Session-scoped S3 credentials injected via K8s Secret. No database, API, or tenant storage access.
- **Safety**: `activeDeadlineSeconds: 3600` -- K8s kills orphan pods after 1 hour.

```
sandbox-{session_id} pod
+---------------------------+
| s3fs sidecar              |  FUSE mount: session-{id} bucket -> /workspace
+---------------------------+
| main container            |  tool-executor: terminal, file ops, code exec
| (surogates package)       |  real Python handlers via ToolRegistry
+---------------------------+
  Session-scoped S3 creds only
  NetworkPolicy: only MCP proxy
```

**Lifecycle:**
1. Session starts -- no sandbox exists.
2. LLM requests a sandbox tool (e.g., `terminal`) -- worker provisions a pod via `K8sSandbox`.
3. Subsequent sandbox tool calls reuse the same pod.
4. Session ends or times out -- worker destroys the pod.

## Channel Adapter Deployments

Each messaging channel runs as a separate deployment:

```yaml
# k8/base/channel-slack-deployment.yaml (simplified)
apiVersion: apps/v1
kind: Deployment
metadata:
  name: channel-slack
  namespace: surogates
spec:
  replicas: 1
  template:
    spec:
      containers:
      - name: slack
        image: ghcr.io/invergent-ai/surogates:latest
        command: ["python", "-m", "surogates.channels.slack"]
        env:
        - name: SLACK_APP_TOKEN
          valueFrom:
            secretKeyRef:
              name: slack-credentials
              key: app-token
        - name: SLACK_BOT_TOKEN
          valueFrom:
            secretKeyRef:
              name: slack-credentials
              key: bot-token
```

Channel adapters need:
- Platform credentials (Slack tokens)
- API server URL (to forward normalized messages)
- Redis access (for delivery nudges)
- Database access (for delivery outbox claiming)

## MCP Proxy Deployment

The MCP proxy handles credential injection for external MCP tool calls. The sandbox calls MCP tools through this proxy -- it never sees the credentials.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mcp-proxy
  namespace: surogates
spec:
  replicas: 2
  template:
    spec:
      containers:
      - name: mcp-proxy
        image: ghcr.io/invergent-ai/surogates:latest
        command: ["python", "-m", "surogates.mcp_proxy"]
```

## Infrastructure Dependencies

### PostgreSQL

Stores all relational data: sessions, events, tenants, credentials, leases, delivery outbox. Use a managed service (RDS, CloudSQL, AlloyDB) or a StatefulSet.

Key tables: `orgs`, `users`, `channel_identities`, `sessions`, `events`, `session_leases`, `session_cursors`, `delivery_outbox`, `delivery_cursors`, `credentials`, `skills`, `mcp_servers`.

See [Appendix C: Database Schema](../appendices/database-schema.md) for full DDL.

### Redis

Used for the work queue (`BZPOPMIN` on sorted set), wake nudges (pub/sub), rate limiting (sliding window), and short-lived caches. Redis is an accelerator, not a source of truth -- all durable state is in PostgreSQL.

### Garage

Lightweight, S3-compatible object storage. Two bucket types:
- `tenant-{org_id}` -- skills, memory, MCP configs (persistent)
- `session-{session_id}` -- workspace files (ephemeral, lifecycle-managed)

Garage ports: 3900 (S3 API), 3903 (admin API).

## Platform Volumes

Platform-level resources are provisioned as read-only volumes mounted into worker and API server pods:

| Volume | Mount Path | Contents |
|---|---|---|
| `platform-skills` | `/etc/surogates/skills/` | Common skill definitions (`SKILL.md` files) |
| `platform-tools` | `/etc/surogates/tools/` | Tool enablement and configuration |
| `platform-mcp` | `/etc/surogates/mcp/` | MCP server definitions |
| `platform-policies` | `/etc/surogates/policies/` | AGT policy definitions |
| `model-metadata` | mounted in workers | Model catalog (context windows, pricing, capabilities) |

These volumes are managed by the platform operator, not by tenants. They form the bottom layer of the 3-layer resource loading (platform > org > user).

## Ingress

```yaml
# k8/base/ingress.yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: surogates
  namespace: surogates
spec:
  rules:
  - host: surogates.example.com
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: api-gateway
            port:
              number: 8000
```

Path routing:
- `/` -- web chat SPA (static files)
- `/v1/*` -- REST API
- `/health` -- health check

## RBAC

Two ServiceAccounts with different privilege levels:

**`surogates-worker`** -- used by worker pods:
- Create/delete pods in the `surogates` namespace (for sandbox management)
- Create/delete secrets (for session-scoped S3 credentials)
- Read secrets (for LLM credentials)

**`surogates-sandbox`** -- used by sandbox pods:
- Minimal -- no K8s API access at all.

## Network Policies

```yaml
# k8/base/sandbox-networkpolicy.yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: sandbox-isolation
  namespace: surogates
spec:
  podSelector:
    matchLabels:
      role: sandbox
  policyTypes:
  - Egress
  egress:
  - to:
    - podSelector:
        matchLabels:
          app: mcp-proxy
  - to:
    - podSelector:
        matchLabels:
          app: garage
    ports:
    - port: 3900
```

Sandbox pods can only reach:
- The MCP proxy (for external tool calls)
- Garage S3 API (for workspace file I/O via s3fs)
- Nothing else -- no internet, no database, no Redis, no API server.
