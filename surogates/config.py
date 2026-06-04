"""Application settings — YAML config file + environment variable overrides.

Settings are loaded from two sources, merged in this order:

1. Config file at ``$SUROGATES_CONFIG`` (default: ``/etc/surogates/config.yaml``)
2. Environment variables (``SUROGATES_*``)

Environment variables **always take precedence** over config file values.

In Kubernetes, the config file is typically mounted from a ConfigMap.
All other paths (tenant assets, skills, MCP, policies) are
individually configurable — there is no single "home directory".
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings


# ---------------------------------------------------------------------------
# Config file path
# ---------------------------------------------------------------------------

# Default config file location (K8s ConfigMap mount point).
DEFAULT_CONFIG_PATH = "/etc/surogates/config.yaml"


def get_config_path() -> Path:
    """Return the path to the config file.

    Reads ``SUROGATES_CONFIG`` env var, falls back to
    ``/etc/surogates/config.yaml``.
    """
    return Path(os.getenv("SUROGATES_CONFIG", DEFAULT_CONFIG_PATH))


# ---------------------------------------------------------------------------
# YAML config loader
# ---------------------------------------------------------------------------


def _load_yaml_config() -> dict[str, Any]:
    """Read the YAML config file.

    Returns the parsed dict, or ``{}`` if the file doesn't exist or
    can't be parsed.
    """
    config_path = get_config_path()
    if not config_path.is_file():
        return {}
    try:
        with open(config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        return {}


def _flatten_yaml(data: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Flatten a nested YAML dict into ``SUROGATES_*`` env-var-style keys.

    Example::

        {"db": {"url": "postgres://..."}}
        → {"SUROGATES_DB_URL": "postgres://..."}

    Only leaf string/int/float/bool values are included.
    """
    result: dict[str, str] = {}
    for key, value in data.items():
        env_key = f"{prefix}{key}".upper()
        if isinstance(value, dict):
            child_prefix = f"SUROGATES_{key}_" if not prefix else f"{prefix}{key}_"
            result.update(_flatten_yaml(value, child_prefix))
        elif isinstance(value, (str, int, float, bool)):
            full_key = f"SUROGATES_{env_key}" if not prefix else env_key
            result[full_key] = str(value)
    return result


# ---------------------------------------------------------------------------
# Settings classes
# ---------------------------------------------------------------------------


class DatabaseSettings(BaseSettings):
    model_config = {"env_prefix": "SUROGATES_DB_"}

    url: str = "postgresql+asyncpg://surogates:surogates@localhost:5432/surogates"
    pool_size: int = 10
    pool_overflow: int = 20


class OpsDatabaseSettings(BaseSettings):
    """Read-only connection to the surogate-ops database.

    Used by the kb_list_pages / kb_read_page tools to look up which
    knowledge bases are attached to this agent and to read the wiki
    page tree. Empty ``url`` disables the KB tools entirely.
    """
    model_config = {"env_prefix": "SUROGATES_OPS_DB_"}

    url: str = ""
    pool_size: int = 2
    pool_overflow: int = 2


class KBHubSettings(BaseSettings):
    """Connection to surogate-hub for fetching wiki page content.

    The wiki tree (paths, titles, types) lives in the ops DB; the
    actual markdown content lives in Hub repos. This struct holds the
    Hub credentials the worker uses to fetch that content. Empty
    ``endpoint_url`` disables the KB tools entirely.
    """
    model_config = {"env_prefix": "SUROGATES_KB_HUB_"}

    endpoint_url: str = ""
    access_key_id: str = ""
    secret_access_key: str = ""


class RedisSettings(BaseSettings):
    model_config = {"env_prefix": "SUROGATES_REDIS_"}

    url: str = "redis://localhost:6379/0"


class APISettings(BaseSettings):
    model_config = {"env_prefix": "SUROGATES_API_"}

    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 1
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    web_url: str = "https://surogates.k8s.localhost"  # Public URL for the web UI (used in pairing links, emails, etc.)
    rate_limit_rpm: int = 300


class ToolOutputSettings(BaseSettings):
    """Limits for model-visible tool output."""

    model_config = {"env_prefix": "SUROGATES_TOOL_OUTPUT_"}

    max_bytes: int = 50_000
    max_lines: int = 2000
    max_line_length: int = 2000


# shared work queue.  Every enqueue lands here;
# the dispatcher decodes the (org_id, agent_id, session_id)
# tuple to know which tenant's TurnConcurrencyGate slot to acquire
# before handing the session to a worker.
SHARED_WORK_QUEUE_KEY: str = "surogates:work_queue"


def encode_queue_member(
    *, org_id: str, agent_id: str, session_id: str,
) -> str:
    """Encode the tenant tuple as a pipe-delimited queue member.

    The dispatcher decodes this in
    :func:`parse_queue_member` to know which tenant's
    :class:`~surogates.runtime.TurnConcurrencyGate` slot to acquire
    before handing the session to a worker — no DB round-trip per
    dequeue.

    Rejects identifiers containing ``'|'`` (the delimiter) so a bad
    row never lands in the queue."""
    for part_name, part in (
        ("org_id", org_id), ("agent_id", agent_id),
        ("session_id", session_id),
    ):
        if "|" in part:
            raise ValueError(
                f"{part_name}={part!r} contains '|' which is the "
                f"queue-member delimiter; reject at enqueue so a "
                f"bad row never lands in the queue",
            )
    return f"{org_id}|{agent_id}|{session_id}"


def parse_queue_member(member: str) -> tuple[str, str, str]:
    """Decode a queue member; raises ``ValueError`` on malformed input."""
    parts = member.split("|")
    if len(parts) != 3:
        raise ValueError(
            f"malformed queue member {member!r}; expected "
            f"<org_id>|<agent_id>|<session_id>",
        )
    return parts[0], parts[1], parts[2]


async def enqueue_session(
    redis: Any,
    *,
    org_id: str,
    agent_id: str,
    session_id: Any,
    priority: float = 0,
) -> None:
    """Enqueue a session on the shared work queue.

    Single entry point used by every component
    that wakes a session — the API, channel adapters, coordinator/
    delegate tools, and worker-notify helpers.  Members encode the
    ``(org_id, agent_id, session_id)`` tuple so the dispatcher can
    extract the tenant for the per-tenant concurrency-gate check
    without a DB round-trip per dequeue.  Lower *priority* values
    are popped first.
    """
    member = encode_queue_member(
        org_id=str(org_id), agent_id=str(agent_id),
        session_id=str(session_id),
    )
    await redis.zadd(SHARED_WORK_QUEUE_KEY, {member: priority})


# Default Redis channel prefix for session interrupts.
INTERRUPT_CHANNEL_PREFIX: str = "surogates:interrupt"


class WorkerSettings(BaseSettings):
    """Worker process configuration."""

    model_config = {"env_prefix": "SUROGATES_WORKER_"}

    concurrency: int = 50
    poll_timeout: int = 5
    api_base_url: str = "http://localhost:8000"
    use_api_for_harness_tools: bool = True
    # Emit iteration.summary / turn.summary events from the harness so
    # the Simple chat view can render one-line iteration summaries and
    # per-turn artifact recaps. Off disables the summarizer entirely;
    # older SDK versions ignore the events when this is on.
    emit_turn_summaries: bool = True
    # per-(org_id, agent_id) max in-flight turns
    # cap used by TurnConcurrencyGate.  ``ctx.governance`` may override
    # per tenant in a later plan; until then this is the uniform cap.
    max_concurrent_turns_default: int = 10


class LLMSettings(BaseSettings):
    """LLM provider configuration."""

    model_config = {"env_prefix": "SUROGATES_LLM_"}

    model: str = "gpt-4o"
    base_url: str = ""  # custom endpoint (e.g. vLLM, Ollama)
    api_key: str = ""  # provider API key

    # Per-model metadata overrides.  Keyed by model id; values are dicts
    # accepting ``context_window`` and ``max_output_tokens``.  Takes
    # precedence over both the static catalog and provider /models
    # discovery, so operators have a deterministic escape hatch when a
    # provider reports the wrong number or lists no pricing at all.
    # Example (config.yaml):
    #   llm:
    #     models:
    #       minimax/minimax-m2.7:
    #         context_window: 204800
    #         max_output_tokens: 4096
    models: dict[str, dict[str, Any]] = Field(default_factory=dict)
    
    summary_model: str = ""  # cheap model for context compression summaries
    summary_base_url: str = ""  # optional auxiliary endpoint for summaries
    summary_api_key: str = ""  # optional auxiliary API key for summaries
    
    vision_model: str = ""  # model with vision capabilities for image inputs
    vision_base_url: str = ""  # optional auxiliary endpoint for vision model
    vision_api_key: str = ""  # optional auxiliary API key for vision model

    advisor_enabled: bool = False  # hidden strategic advisor for hard agent turns
    advisor_model: str = ""  # stronger model used for advisor guidance
    advisor_base_url: str = ""  # optional auxiliary endpoint for advisor model
    advisor_api_key: str = ""  # optional auxiliary API key for advisor model
    advisor_max_calls_per_turn: int = 2  # hard cap for hidden advisor calls
    advisor_max_tokens: int = 700  # requested advisor output budget


class SandboxSettings(BaseSettings):
    """Sandbox execution environment configuration.

    ``srt_enabled`` activates the Anthropic Sandbox Runtime (``srt``)
    which uses bubblewrap (Linux) for kernel-level filesystem and network
    restrictions on every terminal command.  This prevents shell escape
    attacks (``cd ~``, ``echo > /etc/passwd``, etc.) that application-level
    checks cannot catch.

    Requires ``srt``, ``bubblewrap``, ``socat``, and ``ripgrep`` on the
    worker node.  Disabled by default for local dev.
    """

    model_config = {"env_prefix": "SUROGATES_SANDBOX_"}

    backend: Literal["process", "kubernetes"] = "process"
    default_timeout: int = 300
    # Defaults match :class:`surogates.sandbox.base.SandboxSpec` so the
    # ``default_sandbox_spec`` factory yields identical values when no
    # ``SUROGATES_SANDBOX_DEFAULT_*`` env override is supplied.
    default_cpu: str = "2"
    default_memory: str = "4Gi"
    default_cpu_limit: str = "4"
    default_memory_limit: str = "8Gi"
    srt_enabled: bool = False
    srt_settings_dir: str = "/tmp/surogates/srt"

    # K8s sandbox backend settings (only used when backend == "kubernetes")
    k8s_namespace: str = "surogates"
    k8s_service_account: str = "surogates-sandbox"
    k8s_pod_ready_timeout: int = 60
    k8s_executor_path: str = "/usr/local/bin/tool-executor"
    k8s_s3fs_image: str = "ghcr.io/invergent-ai/surogates-s3fs:latest"
    # In-cluster S3 endpoint for sandbox pods (they can't use the host NodePort).
    # If empty, falls back to storage.endpoint.
    k8s_s3_endpoint: str = ""


class BrowserSettings(BaseSettings):
    """Agent browser configuration.

    The browser is implemented as a separate per-session resource. It is
    always available to the worker; ``backend`` selects the local-development
    process backend or the future Kubernetes backend.
    """

    model_config = {"env_prefix": "SUROGATES_BROWSER_"}

    backend: Literal["process", "kubernetes", "fleet"] = "process"
    image: str = "ghcr.io/invergent-ai/surogates-agent-browser:latest"
    rest_port_base: int = 30000
    cdp_port_base: int = 31000
    live_view_port_base: int = 32000
    k8s_namespace: str = "surogates"
    k8s_service_account: str = "surogates-browser"
    k8s_cluster_domain: str = "cluster.local"
    k8s_s3fs_image: str = "ghcr.io/invergent-ai/surogates-s3fs:latest"
    k8s_s3_endpoint: str = ""
    pod_ready_timeout: int = 60
    endpoint_probe_timeout: int = 30

    # Fleet-backend settings (only used when backend == "fleet").
    # ``fleet_endpoint`` is the surogate-ops /api/browser-fleet base URL
    # reachable from the worker pod (typically an in-cluster Service
    # DNS name). ``fleet_worker_token`` is the bearer token mounted from
    # the surogate-ops fleet Secret. ``fleet_timeout`` covers the
    # cold-spawn case (~60 s pod ready). ``fleet_fallback_backend`` is
    # the backend used when the fleet is at capacity or unreachable;
    # "none" disables fallback (and so propagates FleetAtCapacity to the
    # session).
    fleet_endpoint: str = (
        "http://surogate-ops.surogate.svc.cluster.local/api/browser-fleet"
    )
    fleet_worker_token: str = ""
    fleet_timeout: int = 75
    fleet_fallback_backend: Literal["none", "kubernetes", "process"] = "kubernetes"


class TransparencySettings(BaseSettings):
    """EU AI Act Art. 13/50 transparency enforcement."""

    model_config = {"env_prefix": "SUROGATES_GOVERNANCE_TRANSPARENCY_"}

    enabled: bool = False
    level: str = "basic"  # "none", "basic", "enhanced", "full"


class StorageSettings(BaseSettings):
    """Object storage configuration.

    ``backend`` selects the implementation:
    - ``"local"`` — maps buckets to directories under ``base_path``.
    - ``"s3"`` — uses an S3-compatible API (Garage, MinIO, AWS S3).
    """

    model_config = {"env_prefix": "SUROGATES_STORAGE_"}

    backend: Literal["local", "s3"] = "local"
    bucket: str = ""  # Agent bucket for session workspaces
    key_prefix: str = ""  # Object-key prefix under the shared bucket, e.g. "{project_id}/{agent_id}"
    base_path: str = ""  # LocalBackend root (defaults to tenant_assets_root)

    # S3-compatible settings (only used when backend == "s3")
    endpoint: str = ""  # e.g. "http://garage.surogates.svc:3900"
    access_key: str = ""
    secret_key: str = ""
    region: str = ""

    # dedicated bucket for per-user memory.
    # Defaults to '' which the harness treats as 'reuse
    # settings.storage.bucket'.  Set to a different bucket name
    # for deployments that isolate memory (different lifecycle
    # policy, regional replication, billing).
    memory_bucket: str = ""


class SlackSettings(BaseSettings):
    """Slack channel adapter configuration."""

    model_config = {"env_prefix": "SUROGATES_SLACK_"}

    app_token: str = ""       # xapp-... Socket Mode token
    bot_token: str = ""       # xoxb-... (comma-separated for multi-workspace)
    require_mention: bool = True
    free_response_channels: str = ""  # comma-separated channel IDs
    allow_bots: str = "none"  # "none", "mentions", "all"
    reply_in_thread: bool = True
    reply_broadcast: bool = False


class TelegramSettings(BaseSettings):
    """Telegram channel adapter configuration."""

    model_config = {"env_prefix": "SUROGATES_TELEGRAM_"}

    bot_token: str = ""
    webhook_url: str = ""           # empty = polling mode
    webhook_port: int = 8443
    webhook_secret: str = ""
    require_mention: bool = False    # require @mention in group chats
    free_response_chats: str = ""   # comma-separated chat IDs that skip mention gating
    mention_patterns: str = ""      # comma-separated regex wake-word patterns
    reply_to_mode: str = "first"    # "first", "all", "off"
    reactions_enabled: bool = False  # emoji reactions on message lifecycle
    per_user_groups: bool = True    # separate session per user in group chats
    fallback_ips: str = ""          # comma-separated Telegram API fallback IPs
    base_url: str = ""              # custom Bot API server URL
    http_pool_size: int = 512
    http_pool_timeout: float = 8.0
    http_connect_timeout: float = 10.0
    http_read_timeout: float = 20.0
    http_write_timeout: float = 20.0
    media_batch_delay: float = 0.8
    text_batch_delay: float = 0.6
    text_batch_split_delay: float = 2.0


class WebsiteSettings(BaseSettings):
    """Public-website embed channel configuration.

    The website channel exposes the deployment's agent to anonymous
    browser visitors via an embeddable JS widget.  Like every other
    channel adapter, this carries only transport-shaping fields —
    auth, routing, the visitor lifecycle.  Anything that defines
    *what the agent is* (model, system prompt, tool allow-list,
    skills) lives on the agent itself, not here: a visitor-facing
    agent that needs different capabilities than the same org's
    employee-facing agent is a different agent and belongs in a
    different deployment.

    The visitor-side caps and idle timer below are channel-level
    *because they describe the visitor*, not the agent.  Anonymous
    browser visitors warrant tighter caps and shorter idle than
    authenticated employee channels (Slack, Teams) — the agent is
    the same in both cases; the abuse model is different.

    ``allowed_origins`` is a comma-separated string to keep the env-var
    injection path consistent with :class:`SlackSettings`; consumers
    split it at the use site.  Rotate ``publishable_key`` by
    redeploying with a fresh value — the key only ever appears in the
    bootstrap ``Authorization`` header, so the blast radius of a leak
    is one redeploy.
    """

    model_config = {"env_prefix": "SUROGATES_WEBSITE_"}

    enabled: bool = False
    publishable_key: str = ""
    allowed_origins: str = ""           # CSV of scheme://host[:port]

    # Visitor message cap.  Anonymous browser visitors warrant a
    # tighter ceiling than authenticated channels; this is the only
    # cap the route enforces today.  ``0`` means "no cap".  Token-cap
    # and idle-policy knobs are deliberately not exposed here — there
    # is no per-iteration token enforcer in the worker yet, so adding
    # inert config fields would be misleading.
    session_message_cap: int = 0


class GovernanceSettings(BaseSettings):
    model_config = {"env_prefix": "SUROGATES_GOVERNANCE_"}

    transparency: TransparencySettings = Field(default_factory=TransparencySettings)

    # When True, every allowed tool call also emits a ``policy.allowed``
    # event into the session log.  Off by default because it doubles
    # event volume (each ``tool.call`` is already an implicit allow);
    # enable when a complete governance decision trail is required for
    # compliance audit.
    log_allowed: bool = False


class SagaSettings(BaseSettings):
    """Saga orchestration settings."""

    model_config = {"env_prefix": "SUROGATES_SAGA_"}

    enabled: bool = False
    default_step_timeout: int = 300
    default_max_retries: int = 2
    retry_delay: float = 1.0


class OutcomeSettings(BaseSettings):
    """Outcome-oriented /goal loop configuration."""

    model_config = {"env_prefix": "SUROGATES_OUTCOMES_"}

    max_iterations: int = 20
    max_parse_failures: int = 3
    evaluator_model: str = ""
    evaluator_response_max_chars: int = 16384


class ScheduledSessionSettings(BaseSettings):
    """Per-agent scheduled session ticker configuration."""

    model_config = {"env_prefix": "SUROGATES_SCHEDULED_SESSIONS_"}

    tick_interval_seconds: int = 60


class HubSettings(BaseSettings):
    """Surogate Hub credentials for the file-bundle accessor.

    Required at boot — the runtime fetches per-agent SOUL.md /
    skills / sub-agents from Hub bundles, so an empty ``endpoint``
    means the FileBundleCache cannot be wired and the api / worker
    bootstrap raises.
    """

    model_config = {"env_prefix": "SUROGATES_HUB_"}

    endpoint: str = ""
    username: str = ""
    password: str = ""


class Settings(BaseSettings):
    model_config = {"env_prefix": "SUROGATES_"}

    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    ops_db: OpsDatabaseSettings = Field(default_factory=OpsDatabaseSettings)
    kb_hub: KBHubSettings = Field(default_factory=KBHubSettings)
    hub: HubSettings = Field(default_factory=HubSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    api: APISettings = Field(default_factory=APISettings)
    tool_output: ToolOutputSettings = Field(default_factory=ToolOutputSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    governance: GovernanceSettings = Field(default_factory=GovernanceSettings)
    saga: SagaSettings = Field(default_factory=SagaSettings)
    outcomes: OutcomeSettings = Field(default_factory=OutcomeSettings)
    scheduled_sessions: ScheduledSessionSettings = Field(
        default_factory=ScheduledSessionSettings,
    )
    storage: StorageSettings = Field(default_factory=StorageSettings)
    slack: SlackSettings = Field(default_factory=SlackSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    website: WebsiteSettings = Field(default_factory=WebsiteSettings)

    # Tenant asset root.  Used by the per-session sandbox pods to mount
    # workspace volumes; the platform itself no longer reads filesystem
    # catalogs for skills / agents / MCP — those come from the Surogate
    # Hub bundle (skills + sub-agents) and the surogates DB (MCP).
    tenant_assets_root: str = "/data/tenant-assets"

    # Platform (surogate-ops) API base URL + bearer token used to
    # fetch per-tenant config via GET /api/agents/{id}/runtime-config.
    # The token must carry the ``runtime`` scope
    # (see surogate-ops mint-runtime-token CLI).  Both are required:
    # an empty url makes ``agent_runtime_context_dep`` raise on every
    # request so a misconfigured pod fails fast.
    platform_api_url: str = ""
    platform_api_token: str = ""

    worker_id: str = ""  # set from K8s downward API (pod name)
    jwt_secret: str = "change-me-in-production"
    encryption_key: str = ""  # Fernet key for credential vault

    # MCP proxy — when set, worker proxies MCP calls through the proxy
    # service instead of connecting to MCP servers directly.
    mcp_proxy_url: str = ""  # e.g. "http://mcp-proxy.surogates.svc:8001"

    log_level: str = "INFO"

    # Side-car health HTTP port for components that don't expose a
    # public API (worker, channel adapters).  The API and mcp-proxy
    # services serve /health on their primary port and ignore this.
    health_port: int = 8080


def load_settings() -> Settings:
    """Load settings from config file + environment variables.

    Merge order:
    1. YAML config file values are injected as environment variables
       (with ``SUROGATES_`` prefix)
    2. Real environment variables override YAML values
    3. pydantic-settings reads the final environment
    """
    yaml_config = _load_yaml_config()
    if yaml_config:
        flat = _flatten_yaml(yaml_config)
        for key, value in flat.items():
            if key not in os.environ:
                os.environ[key] = value

    return Settings()
