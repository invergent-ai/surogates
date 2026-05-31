# Platform-level scheduled work + cleanup manifests

Text-only Kubernetes manifest templates that the operations team
applies to deploy the platform-level scheduled-work ticker and the
consolidated cleanup + idle-reset CronJobs.

## Files

- `scheduled-work-ticker.yaml.template` -- the Deployment that runs N
  replicas of `python -m surogates.scheduled.platform_ticker`.  Only
  one replica fires at a time via the Redis leader lock
  (`RedisLeaderLock`).
- `cleanup-cronjob.yaml.template` -- the CronJob that runs
  `python -m surogates.jobs.platform_cleanup` every 6 hours (default
  schedule).
- `idle-reset-cronjob.yaml.template` -- the CronJob that runs
  `python -m surogates.jobs.platform_idle_reset` every 5 minutes
  (default schedule).

## Placeholders

Each template uses `{{ name }}`-style placeholders that the operator
fills in.  Use `envsubst`, `yq`, `kustomize`, or any other tool of
choice:

| Placeholder | Description |
|-------------|-------------|
| `{{ namespace }}` | K8s namespace (e.g. `surogates-platform`) |
| `{{ image_registry }}` | Container registry (e.g. `ghcr.io/invergent-ai`) |
| `{{ image_tag }}` | Surogates wheel image tag (e.g. `v3.1.0`) |
| `{{ replica_count }}` | Number of ticker replicas (e.g. `3` for HA) |
| `{{ db_secret }}` | K8s Secret name holding `SUROGATES_DB__URL` |
| `{{ redis_secret }}` | K8s Secret name holding `SUROGATES_REDIS__URL` |
| `{{ ops_api_url }}` | URL of the surogate-ops API (e.g. `https://ops.example.com`) |
| `{{ ops_api_token_secret }}` | K8s Secret holding a runtime-scope API token |
| `{{ schedule }}` | Cron expression for the CronJob (defaults: cleanup `"0 */6 * * *"`, idle-reset `"*/5 * * * *"`) |

## Rollout

1. **Deploy the ticker first** with `replica_count=1` to a staging
   cluster.  Watch `kubectl logs -l app.kubernetes.io/name=surogates-scheduled-work-ticker -f`
   for the leader-lock acquisition message.
2. **Verify no double-fires** by tailing the
   `surogates:work_queue` ZSET in Redis with `redis-cli MONITOR` for
   a few minutes.  Each scheduled session should appear exactly once.
3. **Scale to N replicas** (`kubectl scale deployment surogates-scheduled-work-ticker --replicas=3`).
   Watch the lock log -- only one replica should report acquisition;
   the others should sit on `acquire returned False`.
4. **Kill the leader** (`kubectl delete pod <leader-pod>`).  Within
   the TTL window (default 10s) the next replica acquires.  No
   missed ticks; the SIGTERM grace period
   (`terminationGracePeriodSeconds: 15`) ensures the released
   pod cleanly drops the lock before exit.

## Operations follow-up

The Python code provides the substrate (lock + ticker + cleanup
scripts) but does NOT wire the production defaults for
`platform_cleanup._default_agent_iter` /
`_default_cleanup_for_agent` /
`platform_idle_reset._default_*`.  Those functions raise
`NotImplementedError` today.  The follow-up wires the surogate-ops
API client + the per-agent cleanup body.

The CI smoke test against a kind cluster (apply manifests, run the
ticker for 60s, verify exactly-once delivery) is also a follow-up
because it needs a real cluster.
