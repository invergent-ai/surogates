# 15. Operations

## Health Checks and Metrics

### Health Endpoint

`GET /health` (no auth required) checks all critical dependencies:

```
{
  "status": "healthy",
  "components": {
    "database": "ok",       // PostgreSQL connection pool
    "redis": "ok",          // Redis ping
    "storage": "ok"         // S3/Garage reachable
  }
}
```

Use this for Kubernetes readiness and liveness probes:

```yaml
readinessProbe:
  httpGet:
    path: /health
    port: 8000
  periodSeconds: 10

livenessProbe:
  httpGet:
    path: /health
    port: 8000
  periodSeconds: 30
```

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

