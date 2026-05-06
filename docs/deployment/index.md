# 4. Deployment

Surogates is Kubernetes-native. All components run as K8s workloads. There is no docker-compose or bare-metal deployment mode.

Each agent is deployed as an independent Helm release with its own namespace, database, Redis, ingress, and channel adapters. Deploying multiple agents means running multiple `helm install` commands with different values.

## Helm Chart

The Helm chart lives at `helm/surogates/`. Each release creates a fully isolated agent deployment.

```bash
# Deploy a support bot agent
helm install support-bot ./helm/surogates \
  -n support-bot --create-namespace \
  -f helm/surogates/examples/support-bot.yaml

# Deploy a code assistant agent
helm install code-agent ./helm/surogates \
  -n code-agent --create-namespace \
  -f helm/surogates/examples/code-agent.yaml
```

Each agent gets its own subdomain: `{agent.slug}.{agent.domain}` (e.g., `support-bot.k8s.local`, `code-agent.k8s.local`).

### Values Overview

Key sections in `values.yaml`:

| Section | Purpose |
|---------|---------|
| `agent.slug` / `agent.domain` | Agent identity and subdomain |
| `image` | Container image repository and tag |
| `db` | PostgreSQL connection (each agent uses its own database) |
| `redis` | Redis connection |
| `llm` | Model, provider, API key, temperature |
| `api` | API server replicas and resources |
| `worker` | Worker replicas, concurrency, and resources |
| `autoscaling` | Worker HPA configuration |
| `sandbox` | Sandbox backend (`process` or `kubernetes`) |
| `storage` | S3-compatible object storage |
| `mcpProxy` | MCP proxy (enabled/disabled) |
| `channels.slack` | Slack adapter (enabled/disabled, tokens) |
| `ingress` | Ingress class, TLS, annotations |
| `platformSkills` / `platformPolicies` / `platformTools` / `platformMcp` | Platform config mounted as ConfigMaps |

## Component Overview

Each agent namespace contains:

```
Namespace: {agent-slug}
|
+-- Deployment: {agent-slug}-api          (API server, configurable replicas)
+-- Deployment: {agent-slug}-worker       (HPA-scaled workers)
+-- Pods: sandbox-{session_id}            (ephemeral, created by worker on demand)
+-- Deployment: {agent-slug}-channel-slack (conditional, if channels.slack.enabled)
+-- Deployment: {agent-slug}-mcp-proxy    (conditional, if mcpProxy.enabled)
+-- StatefulSet: postgres                 (or managed: RDS, CloudSQL, AlloyDB)
+-- Deployment: redis                     (or managed: ElastiCache, Memorystore)
+-- StatefulSet: garage                   (S3-compatible object storage)
```

## API Server

The API server serves the REST API, web chat SPA, and SSE event streams.

```yaml
# helm/surogates/templates/api-deployment.yaml (simplified)
containers:
  - name: api
    image: ghcr.io/invergent-ai/surogates:latest
    command: ["surogates", "api"]
    ports:
      - containerPort: 8000
    env:
      - name: SUROGATES_DB_URL       # from Secret
      - name: SUROGATES_REDIS_URL    # from Secret
      - name: SUROGATES_JWT_SECRET   # from Secret
      - name: SUROGATES_STORAGE_*    # S3 credentials (if storage.backend=s3)
```

The API server needs:
- Database credentials (sessions, tenants, credentials)
- S3 credentials for all tenant and agent buckets
- JWT signing secret
- Platform volumes (skills) mounted read-only

## Worker

Workers run the orchestrator loop, pulling sessions from the Redis queue and executing the agent harness.

```yaml
# helm/surogates/templates/worker-deployment.yaml (simplified)
containers:
  - name: worker
    image: ghcr.io/invergent-ai/surogates:latest
    command: ["surogates", "worker"]
    env:
      - name: SUROGATES_DB_URL
      - name: SUROGATES_REDIS_URL
      - name: SUROGATES_JWT_SECRET
      - name: SUROGATES_LLM_MODEL        # from values.llm.model
      - name: SUROGATES_LLM_API_KEY      # from values.llm.apiKey
      - name: SUROGATES_WORKER_CONCURRENCY
      - name: SUROGATES_SANDBOX_BACKEND
      - name: SUROGATES_WORKER_API_BASE_URL  # auto-generated: http://{release}-api.{ns}.svc:8000
    volumeMounts:
      - name: platform-policies   # from ConfigMap (if platformPolicies set)
      - name: platform-skills     # from ConfigMap (if platformSkills set)
      - name: platform-tools      # from ConfigMap (if platformTools set)
      - name: platform-mcp        # from ConfigMap (if platformMcp set)
      - name: model-metadata      # from ConfigMap (if modelMetadata set)
```

The worker needs:
- Database credentials (sessions, events, leases)
- Redis access (work queue, nudges)
- LLM provider credentials (API key)
- API server URL (auto-resolved to the release's own API service)
- K8s API access (ServiceAccount with RBAC to create/delete sandbox pods)
- Platform volumes mounted read-only

### HPA Scaling

Workers scale horizontally based on CPU utilization:

```yaml
# helm/surogates/templates/hpa.yaml (conditional on autoscaling.enabled)
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: {release}-worker
  minReplicas: 5     # values.autoscaling.minReplicas
  maxReplicas: 20    # values.autoscaling.maxReplicas
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70  # values.autoscaling.targetCPU
```

## Sandbox Pods

Sandbox pods are created dynamically by workers when a session first needs isolated execution. They are not pre-provisioned.

**Pod structure:**
- **Main container**: Full `surogates` package + `tool-executor` (Python). Receives tool calls via K8s exec API.
- **s3fs sidecar**: FUSE-mounts the session's Garage path (`{agent_bucket}:/sessions/{session_id}`) as `/workspace`.
- **Credentials**: Session-scoped S3 credentials injected via K8s Secret. No database, API, or tenant storage access.
- **Safety**: `activeDeadlineSeconds: 3600` -- K8s kills orphan pods after 1 hour.

```
sandbox-{session_id} pod
+---------------------------+
| s3fs sidecar              |  FUSE mount: agent bucket session path -> /workspace
+---------------------------+
| main container            |  tool-executor: terminal, file ops, code exec
| (surogates package)       |  real Python handlers via ToolRegistry
+---------------------------+
  Session-scoped S3 creds only
  NetworkPolicy: only MCP proxy + Garage S3
```

**Lifecycle:**
1. Session starts -- no sandbox exists.
2. LLM requests a sandbox tool (e.g., `terminal`) -- worker provisions a pod via `K8sSandbox`.
3. Subsequent sandbox tool calls reuse the same pod.
4. Session ends or times out -- worker destroys the pod.

## Channel Adapters

Each messaging channel runs as a separate deployment, conditional on its `enabled` flag in values.

```yaml
# helm/surogates/templates/channel-slack.yaml (conditional on channels.slack.enabled)
containers:
  - name: channel
    image: ghcr.io/invergent-ai/surogates:latest
    command: ["surogates", "channel", "slack"]
    env:
      - name: SUROGATES_SLACK_BOT_TOKEN   # from Secret
      - name: SUROGATES_SLACK_APP_TOKEN   # from Secret
      - name: SUROGATES_SLACK_REQUIRE_MENTION
      - name: SUROGATES_SLACK_REPLY_IN_THREAD
```

Channel adapters need:
- Platform credentials (Slack tokens, from Secret)
- Database access (for identity resolution and delivery outbox)
- Redis access (for delivery nudges)

## MCP Proxy

The MCP proxy handles credential injection for external MCP tool calls. The sandbox calls MCP tools through this proxy -- it never sees the credentials. Conditional on `mcpProxy.enabled`.

```yaml
# helm/surogates/templates/mcp-proxy-deployment.yaml (conditional)
containers:
  - name: mcp-proxy
    image: ghcr.io/invergent-ai/surogates:latest
    command: ["surogates", "mcp-proxy"]
    ports:
      - containerPort: 8001
```

## Infrastructure Dependencies

### PostgreSQL

Stores all relational data: sessions, events, tenants, credentials, leases, delivery outbox. Use a managed service (RDS, CloudSQL, AlloyDB) or a StatefulSet.

Each agent deployment should use its own database (or schema) for full isolation.

Key tables: `orgs`, `users`, `channel_identities`, `sessions`, `events`, `session_leases`, `session_cursors`, `delivery_outbox`, `delivery_cursors`, `credentials`, `skills`, `mcp_servers`.

See [Appendix C: Database Schema](../appendices/database-schema.md) for full DDL.

### Redis

Used for the work queue (`BZPOPMIN` on sorted set), wake nudges (pub/sub), rate limiting (sliding window), and short-lived caches. Redis is an accelerator, not a source of truth -- all durable state is in PostgreSQL.

Each agent deployment uses its own Redis instance (or database number).

### Garage

Lightweight, S3-compatible object storage. Two bucket types:
- `tenant-{org_id}` -- skills, memory, MCP configs (persistent)
- configured agent bucket -- session workspace files under `sessions/{session_id}/`

Garage ports: 3900 (S3 API), 3903 (admin API).

## Platform Volumes

Platform-level resources are provisioned as ConfigMaps and mounted read-only into worker and API server pods. Each is conditional -- only created and mounted when the corresponding values section is non-empty.

| Value Key | Mount Path | Contents |
|---|---|---|
| `platformSkills` | `/etc/surogates/skills/` | Common skill definitions (`SKILL.md` files) |
| `platformTools` | `/etc/surogates/tools/` | Tool enablement and configuration |
| `platformMcp` | `/etc/surogates/mcp/` | MCP server definitions |
| `platformPolicies` | `/etc/surogates/policies/` | AGT policy definitions |
| `modelMetadata` | `/etc/surogates/model-metadata.json` | Model catalog (context windows, pricing, capabilities) |

These volumes are managed by the platform operator, not by tenants. They form the bottom layer of the 3-layer resource loading (platform > org > user).

## Ingress

Each agent gets its own Ingress resource with a subdomain derived from `agent.slug` and `agent.domain`:

```yaml
# helm/surogates/templates/ingress.yaml (conditional on ingress.enabled)
spec:
  ingressClassName: nginx
  tls:
    - hosts:
        - support-bot.k8s.local     # {agent.slug}.{agent.domain}
      secretName: support-bot-tls
  rules:
    - host: support-bot.k8s.local
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: support-bot-api
                port:
                  name: http
```

Path routing:
- `/` -- web chat SPA (static files)
- `/v1/*` -- REST API
- `/health` -- health check

For multiple agents, use a wildcard TLS certificate on `*.k8s.local` and let each Helm release create its own Ingress rule.

## RBAC

Two ServiceAccounts per agent namespace with different privilege levels:

**`{release}-worker`** -- used by worker pods:
- Create/delete pods in the namespace (for sandbox management)
- Execute commands in pods (K8s exec API for sandbox tool calls)
- Create/delete/get secrets (for session-scoped S3 credentials)

**`{release}-sandbox`** -- used by sandbox pods:
- Minimal -- no K8s API access at all.

## Network Policies

Sandbox pods are locked down via NetworkPolicy:

```yaml
# helm/surogates/templates/sandbox-networkpolicy.yaml
spec:
  podSelector:
    matchLabels:
      agent: {agent-slug}
      component: sandbox
  policyTypes:
    - Egress
    - Ingress
  ingress:
    - from:
        - podSelector:
            matchLabels:
              component: worker    # K8s exec from worker only
  egress:
    - to: [kube-dns]               # DNS resolution
    - to: [mcp-proxy]              # External tool calls (if enabled)
    - to: [garage]                 # Workspace file I/O via s3fs
```

Sandbox pods can only reach:
- The MCP proxy (for external tool calls)
- Garage S3 API (for workspace file I/O via s3fs)
- DNS (kube-dns for name resolution)
- Nothing else -- no internet, no database, no Redis, no API server.

## Multi-Agent Deployment

To deploy multiple agents, run separate `helm install` commands with different values:

```bash
# Support bot: Slack-connected, lightweight sandbox
helm install support-bot ./helm/surogates \
  -n support-bot --create-namespace \
  -f helm/surogates/examples/support-bot.yaml

# Code agent: web-only, full K8s sandbox with SRT
helm install code-agent ./helm/surogates \
  -n code-agent --create-namespace \
  -f helm/surogates/examples/code-agent.yaml
```

Each agent is fully isolated:
- Own namespace, own database, own Redis
- Own ingress at `{slug}.{domain}`
- Own LLM credentials and model selection
- Own channel adapters (Slack, etc.)
- Own sandbox configuration
- Independent scaling (separate HPA per agent)

Shared identity across agents is managed externally via LDAP.
