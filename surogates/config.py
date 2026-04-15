"""Application settings — YAML config file + environment variable overrides.

Settings are loaded from two sources, merged in this order:

1. Config file at ``$SUROGATES_CONFIG`` (default: ``/etc/surogates/config.yaml``)
2. Environment variables (``SUROGATES_*``)

Environment variables **always take precedence** over config file values.

In Kubernetes, the config file is typically mounted from a ConfigMap.
All other paths (tenant assets, skills, tools, MCP, policies) are
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


class WorkerSettings(BaseSettings):
    """Worker process configuration."""

    model_config = {"env_prefix": "SUROGATES_WORKER_"}

    concurrency: int = 50
    queue_name: str = "surogates:work_queue"
    poll_timeout: int = 5
    workspace_path: str = "/tmp/surogates/workspaces"
    api_base_url: str = "http://localhost:8000"
    use_api_for_harness_tools: bool = True


class LLMSettings(BaseSettings):
    """LLM provider configuration."""

    model_config = {"env_prefix": "SUROGATES_LLM_"}

    model: str = "gpt-4o"
    provider: str = ""  # "openai", "anthropic", "openrouter", etc.
    base_url: str = ""  # custom endpoint (e.g. vLLM, Ollama)
    api_key: str = ""  # provider API key
    max_tokens: int | None = None
    temperature: float = 0.7

    # Fallback chain — list of dicts with provider/model/api_key/base_url
    # Configured via config.yaml only (too complex for env vars)
    fallback_providers: list[dict[str, Any]] = Field(default_factory=list)

    # Credential pool — list of dicts with api_key/base_url/label/priority
    # Configured via config.yaml only
    credential_pool: list[dict[str, Any]] = Field(default_factory=list)


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
    runtime_url: str = "http://sandbox-runtime:8080"
    default_timeout: int = 300
    default_cpu: str = "500m"
    default_memory: str = "512Mi"
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


class TransparencySettings(BaseSettings):
    """EU AI Act Art. 13/50 transparency enforcement."""

    model_config = {"env_prefix": "SUROGATES_GOVERNANCE_TRANSPARENCY_"}

    enabled: bool = False
    level: str = "basic"  # "none", "basic", "enhanced", "full"
    require_confirmation: bool = True
    emotion_recognition: bool = False


class StorageSettings(BaseSettings):
    """Object storage configuration.

    ``backend`` selects the implementation:
    - ``"local"`` — maps buckets to directories under ``base_path``.
    - ``"s3"`` — uses an S3-compatible API (Garage, MinIO, AWS S3).
    """

    model_config = {"env_prefix": "SUROGATES_STORAGE_"}

    backend: Literal["local", "s3"] = "local"
    base_path: str = ""  # LocalBackend root (defaults to tenant_assets_root)

    # S3-compatible settings (only used when backend == "s3")
    endpoint: str = ""  # e.g. "http://garage.surogates.svc:3900"
    access_key: str = ""
    secret_key: str = ""
    region: str = ""


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


class GovernanceSettings(BaseSettings):
    model_config = {"env_prefix": "SUROGATES_GOVERNANCE_"}

    platform_policy_path: str = "/etc/surogates/policies"
    enabled: bool = True
    transparency: TransparencySettings = Field(default_factory=TransparencySettings)


class SessionResetSettings(BaseSettings):
    """Session idle reset policy.

    When a session has been inactive beyond the configured threshold, the
    platform runs a temporary LLM agent that reviews the conversation
    transcript and saves important facts to memory, then tears down the
    sandbox pod.  The session itself (events, counters, cursor) is left
    untouched — the user can come back and continue at any time.

    Ported from Hermes ``SessionResetPolicy``.

    Modes:
    - ``"idle"``: Reset after *idle_minutes* of inactivity.
    - ``"daily"``: Reset at a specific hour each day.
    - ``"both"``: Whichever triggers first (daily boundary OR idle timeout).
    - ``"none"``: Never auto-reset.
    """

    model_config = {"env_prefix": "SUROGATES_SESSION_RESET_"}

    enabled: bool = False
    mode: Literal["idle", "daily", "both", "none"] = "idle"
    at_hour: int = 4
    idle_minutes: int = 1440
    flush_max_iterations: int = 8
    flush_max_retries: int = 3
    watcher_interval_seconds: int = 300
    notify: bool = True
    notify_exclude_channels: list[str] = Field(
        default_factory=lambda: ["webhook"],
    )


class SagaSettings(BaseSettings):
    """Saga orchestration settings."""

    model_config = {"env_prefix": "SUROGATES_SAGA_"}

    enabled: bool = False
    default_step_timeout: int = 300
    default_max_retries: int = 2
    retry_delay: float = 1.0


class Settings(BaseSettings):
    model_config = {"env_prefix": "SUROGATES_"}

    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    api: APISettings = Field(default_factory=APISettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    governance: GovernanceSettings = Field(default_factory=GovernanceSettings)
    saga: SagaSettings = Field(default_factory=SagaSettings)
    session_reset: SessionResetSettings = Field(default_factory=SessionResetSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    slack: SlackSettings = Field(default_factory=SlackSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)

    # Paths — each individually configurable, each a separate K8s volume mount
    platform_skills_dir: str = "/etc/surogates/skills"
    platform_tools_dir: str = "/etc/surogates/tools"
    platform_mcp_dir: str = "/etc/surogates/mcp"
    tenant_assets_root: str = "/data/tenant-assets"
    model_metadata_path: str = "/etc/surogates/model-metadata.json"

    # Identity
    org_id: str = ""  # the org this agent instance belongs to
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
