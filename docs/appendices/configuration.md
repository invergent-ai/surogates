# Appendix A: Configuration Reference

Surogates is configured via a YAML file merged with environment variables. Environment variables take precedence over YAML values.

**Config file location:** Set via `SUROGATES_CONFIG` environment variable (default: `/etc/surogates/config.yaml`).

**Environment variable naming:** YAML keys are flattened with underscores and prefixed with `SUROGATES_`. For example, `db.pool_size` becomes `SUROGATES_DB_POOL_SIZE`.

## Database (`db`)

| Key | Env Var | Default | Description |
|---|---|---|---|
| `db.url` | `SUROGATES_DB_URL` | -- | PostgreSQL connection string (`postgresql+asyncpg://...`) |
| `db.pool_size` | `SUROGATES_DB_POOL_SIZE` | `10` | Connection pool size |
| `db.pool_overflow` | `SUROGATES_DB_POOL_OVERFLOW` | `5` | Max overflow connections above pool_size |

## Redis (`redis`)

| Key | Env Var | Default | Description |
|---|---|---|---|
| `redis.url` | `SUROGATES_REDIS_URL` | -- | Redis connection string (`redis://...`) |

## API Server (`api`)

| Key | Env Var | Default | Description |
|---|---|---|---|
| `api.host` | `SUROGATES_API_HOST` | `0.0.0.0` | Bind address |
| `api.port` | `SUROGATES_API_PORT` | `8000` | Bind port |
| `api.workers` | `SUROGATES_API_WORKERS` | `1` | Uvicorn worker count |
| `api.cors_origins` | `SUROGATES_API_CORS_ORIGINS` | `["*"]` | CORS allowed origins |
| `api.web_url` | `SUROGATES_API_WEB_URL` | -- | Public URL for the web UI (used in pairing links) |

## Worker (`worker`)

| Key | Env Var | Default | Description |
|---|---|---|---|
| `worker.concurrency` | `SUROGATES_WORKER_CONCURRENCY` | `50` | Max concurrent sessions per worker |
| `worker.queue_name` | `SUROGATES_WORKER_QUEUE_NAME` | `surogates:work_queue` | Redis queue key |
| `worker.poll_timeout` | `SUROGATES_WORKER_POLL_TIMEOUT` | `5` | Queue poll timeout (seconds) |
| `worker.api_base_url` | `SUROGATES_WORKER_API_BASE_URL` | -- | API server URL for harness tool calls |
| `worker.use_api_for_harness_tools` | `SUROGATES_WORKER_USE_API_FOR_HARNESS_TOOLS` | `false` | Route skill/memory operations through API server |

## LLM Provider (`llm`)

| Key | Env Var | Default | Description |
|---|---|---|---|
| `llm.model` | `SUROGATES_LLM_MODEL` | -- | Model name (e.g., `claude-sonnet-4-20250514`, `gpt-4o`) |
| `llm.base_url` | `SUROGATES_LLM_BASE_URL` | -- | Provider API base URL |
| `llm.api_key` | `SUROGATES_LLM_API_KEY` | -- | Primary API key |
| `llm.temperature` | `SUROGATES_LLM_TEMPERATURE` | `0.7` | Default sampling temperature |
| `llm.provider` | `SUROGATES_LLM_PROVIDER` | auto-detected | Provider name override |
| `llm.credential_pool` | -- | `[]` | Additional API keys for rotation |
| `llm.fallback_providers` | -- | `[]` | Fallback provider chain |

## Sandbox (`sandbox`)

| Key | Env Var | Default | Description |
|---|---|---|---|
| `sandbox.backend` | `SUROGATES_SANDBOX_BACKEND` | `process` | Backend: `process` (dev) or `kubernetes` (prod) |
| `sandbox.default_timeout` | `SUROGATES_SANDBOX_DEFAULT_TIMEOUT` | `300` | Default execution timeout (seconds) |
| `sandbox.k8s_namespace` | `SUROGATES_SANDBOX_K8S_NAMESPACE` | `surogates` | K8s namespace for sandbox pods |
| `sandbox.k8s_image` | `SUROGATES_SANDBOX_K8S_IMAGE` | `ghcr.io/invergent-ai/agent-sandbox:latest` | Sandbox container image |
| `sandbox.k8s_service_account` | `SUROGATES_SANDBOX_K8S_SERVICE_ACCOUNT` | `surogates-sandbox` | ServiceAccount for sandbox pods |
| `sandbox.k8s_s3_endpoint` | `SUROGATES_SANDBOX_K8S_S3_ENDPOINT` | -- | In-cluster S3 endpoint for s3fs sidecar |
| `sandbox.k8s_pod_ready_timeout` | `SUROGATES_SANDBOX_K8S_POD_READY_TIMEOUT` | `60` | Max wait for pod Ready (seconds) |

## Storage (`storage`)

| Key | Env Var | Default | Description |
|---|---|---|---|
| `storage.backend` | `SUROGATES_STORAGE_BACKEND` | `local` | Backend: `local` (dev) or `s3` (prod) |
| `storage.base_path` | `SUROGATES_STORAGE_BASE_PATH` | `/tmp/surogates/tenant-assets` | Local backend base directory |
| `storage.endpoint` | `SUROGATES_STORAGE_ENDPOINT` | -- | S3 endpoint URL |
| `storage.region` | `SUROGATES_STORAGE_REGION` | `garage` | S3 region |
| `storage.access_key` | `SUROGATES_STORAGE_ACCESS_KEY` | -- | S3 access key |
| `storage.secret_key` | `SUROGATES_STORAGE_SECRET_KEY` | -- | S3 secret key |

## Slack Channel (`slack`)

| Key | Env Var | Default | Description |
|---|---|---|---|
| `slack.app_token` | `SUROGATES_SLACK_APP_TOKEN` | -- | Socket Mode app token (`xapp-...`) |
| `slack.bot_token` | `SUROGATES_SLACK_BOT_TOKEN` | -- | Bot OAuth token (`xoxb-...`) |
| `slack.require_mention` | `SUROGATES_SLACK_REQUIRE_MENTION` | `true` | Only respond when @mentioned in channels |
| `slack.free_response_channels` | `SUROGATES_SLACK_FREE_RESPONSE_CHANNELS` | `""` | Comma-separated channel IDs where mention is not required |
| `slack.allow_bots` | `SUROGATES_SLACK_ALLOW_BOTS` | `none` | Bot message handling: `none`, `mentions`, `all` |
| `slack.reply_in_thread` | `SUROGATES_SLACK_REPLY_IN_THREAD` | `true` | Reply in threads |
| `slack.reply_broadcast` | `SUROGATES_SLACK_REPLY_BROADCAST` | `false` | Broadcast thread replies to channel |

## Governance (`governance`)

| Key | Env Var | Default | Description |
|---|---|---|---|
| `governance.enabled` | `SUROGATES_GOVERNANCE_ENABLED` | `true` | Enable policy enforcement |
| `governance.transparency.enabled` | -- | `false` | Enable EU AI Act transparency endpoints |
| `governance.transparency.level` | -- | `full` | Transparency level |
| `governance.transparency.require_confirmation` | -- | `true` | Require user confirmation for AI-generated content |
| `governance.transparency.emotion_recognition` | -- | `false` | Flag emotion recognition usage |

## Saga (`saga`)

| Key | Env Var | Default | Description |
|---|---|---|---|
| `saga.enabled` | `SUROGATES_SAGA_ENABLED` | `false` | Enable multi-step tool chain tracking with automatic rollback |
| `saga.default_step_timeout` | `SUROGATES_SAGA_DEFAULT_STEP_TIMEOUT` | `300` | Max seconds per tool call step |
| `saga.default_max_retries` | `SUROGATES_SAGA_DEFAULT_MAX_RETRIES` | `2` | Retries per step before marking as failed |
| `saga.retry_delay` | `SUROGATES_SAGA_RETRY_DELAY` | `1.0` | Initial retry delay in seconds (exponential backoff) |

## Tenant Defaults

| Key | Env Var | Default | Description |
|---|---|---|---|
| `org_id` | `SUROGATES_ORG_ID` | -- | Default org ID (for single-tenant dev setups) |
| `jwt_secret` | `SUROGATES_JWT_SECRET` | -- | JWT signing secret (HS256) |
| `log_level` | `SUROGATES_LOG_LEVEL` | `INFO` | Logging level |

## Example: Full Development Config

```yaml
db:
  url: "postgresql+asyncpg://surogates:surogates@localhost:5432/surogates"
  pool_size: 5
  pool_overflow: 5

redis:
  url: "redis://localhost:6379/0"

api:
  host: "0.0.0.0"
  port: 8000
  workers: 1

llm:
  model: "claude-sonnet-4-20250514"
  base_url: "https://api.anthropic.com/v1"
  api_key: "sk-ant-..."
  temperature: 0.7

worker:
  concurrency: 10
  queue_name: "surogates:work_queue"
  poll_timeout: 5
  api_base_url: "http://localhost:8000"

sandbox:
  backend: "process"
  default_timeout: 300

storage:
  backend: "local"
  base_path: "/tmp/surogates/tenant-assets"

governance:
  enabled: true

org_id: "c5ea6808-7545-4e30-9340-a96386378030"
jwt_secret: "dev-secret-do-not-use-in-production"
log_level: "DEBUG"
```
