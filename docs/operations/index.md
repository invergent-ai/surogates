# 15. Operations

## Health Checks

Every component exposes two unauthenticated health endpoints: a **liveness** probe and a **readiness** probe.

### Liveness — `GET /health`

Returns `200 {"status": "ok"}` unconditionally if the process is running. No dependency checks. Kubernetes uses this to detect hung or deadlocked processes and restart them.

### Readiness — `GET /health/ready`

Verifies that infrastructure dependencies (PostgreSQL and Redis) are reachable before the pod accepts traffic. Runs a `SELECT 1` against the database and a `PING` against Redis in parallel.

Returns `200` when both checks pass:

```json
{
  "status": "ok",
  "checks": {
    "database": "ok",
    "redis": "ok"
  }
}
```

Returns `503` when any check fails:

```json
{
  "status": "degraded",
  "checks": {
    "database": "ok",
    "redis": "error: ConnectionError(...)"
  }
}
```

### How Each Component Serves Health

| Component | Transport | Port | Implementation |
|---|---|---|---|
| API server | FastAPI routes on its primary HTTP server | `8000` | `surogates/api/routes/health.py` — uses `request.app.state` to access DB session factory and Redis |
| Worker | Side-car `HealthServer` (Starlette/uvicorn) | `healthPort` (default `8080`) | `surogates/health/server.py` — standalone HTTP server started alongside the orchestrator loop |
| Channel adapters | Side-car `HealthServer` (Starlette/uvicorn) | `healthPort` (default `8080`) | Same as worker — started alongside the channel adapter event loop |
| MCP proxy | Primary HTTP server | `8001` | `/health` only (no `/health/ready` yet) |

The worker and channel adapters do not serve HTTP normally, so they spin up a lightweight side-car `HealthServer` on a dedicated port. The API server and MCP proxy serve health endpoints on their primary port.

### Helm Chart Probe Configuration

The Helm chart configures probes for all deployments:

**API server** (`api-deployment.yaml`):

```yaml
readinessProbe:
  httpGet:
    path: /health/ready
    port: http           # 8000
  initialDelaySeconds: 5
  periodSeconds: 10
livenessProbe:
  httpGet:
    path: /health
    port: http           # 8000
  initialDelaySeconds: 10
  periodSeconds: 30
```

**Worker** (`worker-deployment.yaml`):

```yaml
readinessProbe:
  httpGet:
    path: /health/ready
    port: health         # healthPort (default 8080)
  initialDelaySeconds: 5
  periodSeconds: 10
livenessProbe:
  httpGet:
    path: /health
    port: health         # healthPort (default 8080)
  initialDelaySeconds: 10
  periodSeconds: 30
```

**Channel adapters** (e.g. `channel-slack.yaml`):

```yaml
readinessProbe:
  httpGet:
    path: /health/ready
    port: health         # healthPort (default 8080)
  initialDelaySeconds: 5
  periodSeconds: 10
livenessProbe:
  httpGet:
    path: /health
    port: health         # healthPort (default 8080)
  initialDelaySeconds: 10
  periodSeconds: 30
```

**MCP proxy** (`mcp-proxy-deployment.yaml`):

```yaml
readinessProbe:
  httpGet:
    path: /health
    port: http           # 8001
  initialDelaySeconds: 3
  periodSeconds: 10
livenessProbe:
  httpGet:
    path: /health
    port: http           # 8001
  initialDelaySeconds: 5
  periodSeconds: 15
```

### Configuration

The side-car health port is set via the `healthPort` Helm value (default `8080`) and passed to the worker and channel adapter processes as `SUROGATES_HEALTH_PORT`. The API server and MCP proxy ignore this value since they serve health on their primary HTTP port.

### Prometheus Metrics

`GET /metrics` exposes Prometheus-compatible metrics:

| Metric | Type | Description |
|---|---|---|
| `surogates_active_sessions` | gauge | Currently processing sessions per worker |
| `surogates_events_total` | counter | Total events emitted (by type) |
| `surogates_llm_requests_total` | counter | LLM API calls (by provider, model, status) |
| `surogates_llm_latency_seconds` | histogram | LLM response latency |
| `surogates_tool_calls_total` | counter | Tool calls (by name, location, status) |
| `surogates_tool_latency_seconds` | histogram | Tool execution latency |
| `surogates_delivery_pending` | gauge | Pending delivery outbox items |
| `surogates_sandbox_active` | gauge | Active sandbox pods |
| `surogates_session_cost_usd` | histogram | Estimated session cost |

## HPA Configuration and Scaling

Workers scale horizontally based on active sessions per pod:

```
Target: average 40 active sessions per worker pod
Min replicas: 5
Max replicas: 20

Scale up when:  avg sessions/pod > 40
Scale down when: avg sessions/pod < 20 (for 5+ minutes)
```

The HPA uses a custom metric (`surogates_active_sessions`) exposed via the Prometheus adapter or metrics-server.

### Scaling Considerations

- **Workers are stateless**: Any worker can process any session. Scale freely.
- **API servers are stateless**: Scale based on HTTP request rate.
- **Sandbox pods are ephemeral**: Created on demand, destroyed when sessions end. No dedicated scaling -- the worker manages them.
- **Redis**: Single instance is sufficient for most deployments. Use Redis Cluster for >10,000 concurrent sessions.
- **PostgreSQL**: Connection pool is the bottleneck. Size `pool_size` proportionally to worker count.

## Monitoring Active Sessions

### Key Queries

**Active sessions by status:**
```
surogates_active_sessions{status="processing"}
```

**Lease health** (sessions with expired leases indicate crashed workers):
```
SELECT count(*) FROM session_leases WHERE expires_at < now();
```

**Delivery backlog** (growing backlog indicates adapter issues):
```
surogates_delivery_pending{channel="slack"}
```

**LLM error rate:**
```
rate(surogates_llm_requests_total{status="error"}[5m])
  / rate(surogates_llm_requests_total[5m])
```

### Alerting Recommendations

| Alert | Condition | Severity |
|---|---|---|
| High LLM error rate | > 5% errors over 5 minutes | Warning |
| Delivery backlog growing | > 100 pending items for 10 minutes | Warning |
| Expired leases | > 0 expired leases for 2 minutes | Critical |
| Worker pod restarts | > 3 restarts in 10 minutes | Critical |
| Sandbox provisioning failures | > 5 failures in 5 minutes | Warning |
| Storage unreachable | Health check fails for storage | Critical |

## Troubleshooting

### Crash Recovery

**Symptom:** Session appears stuck, no new events.

**Diagnosis:**
1. Check if the session has an active lease: look for the session in `session_leases`.
2. If the lease is expired, the worker crashed. The orchestrator should re-enqueue.
3. If the lease is active but no events are flowing, the worker may be stuck on an LLM call (check stale stream timeout).

**Resolution:**
- Normally, crash recovery is automatic: lease expires -> re-enqueue -> new worker picks up.
- If recovery doesn't happen, manually enqueue the session via Redis or restart the worker deployment.

### Lease Expiry

**Symptom:** Session is processed by two workers simultaneously (duplicate events).

**Cause:** Lease TTL too short relative to LLM call latency.

**Resolution:**
- Increase `_LEASE_TTL_SECONDS` (default: 60s).
- Ensure lease renewal interval (every 3 iterations) is frequent enough.
- The lease token prevents actual conflicts -- the second worker will fail to renew and stop.

### Delivery Retries

**Symptom:** Messages delivered multiple times to Slack.

**Cause:** Adapter crash after sending but before marking as delivered.

**Resolution:**
- The delivery outbox uses deduplication keys to prevent true duplicates at the platform level.
- If duplicates appear, check that only one adapter instance is running per channel.

### Sandbox Provisioning Failures

**Symptom:** Tool calls fail with "sandbox provisioning timeout".

**Diagnosis:**
1. Check K8s events: `kubectl get events -n surogates --field-selector reason=FailedCreate`
2. Check pod status: `kubectl get pods -n surogates -l role=sandbox`
3. Common causes: resource limits (CPU/memory), image pull failures, s3fs mount failures.

**Resolution:**
- Increase pod resource limits if OOM-killed.
- Verify Garage credentials for s3fs mount.
- Check NetworkPolicy allows s3fs to reach Garage.

## Database Schema and Migrations

Surogates uses Alembic for database migrations. The schema is currently created via SQLAlchemy ORM metadata.

```bash
# Run pending migrations
uv run alembic upgrade head

# Generate a new migration after model changes
uv run alembic revision --autogenerate -m "description"

# Check current migration status
uv run alembic current
```

