"""Application settings loaded from environment variables."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings


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


class WorkerSettings(BaseSettings):
    model_config = {"env_prefix": "SUROGATES_WORKER_"}

    concurrency: int = 50
    queue_name: str = "surogates:work_queue"
    poll_timeout: int = 5


class SandboxSettings(BaseSettings):
    model_config = {"env_prefix": "SUROGATES_SANDBOX_"}

    backend: str = "process"  # "process" or "kubernetes"
    runtime_url: str = "http://sandbox-runtime:8080"
    default_timeout: int = 300
    default_cpu: str = "500m"
    default_memory: str = "512Mi"


class GovernanceSettings(BaseSettings):
    model_config = {"env_prefix": "SUROGATES_GOVERNANCE_"}

    platform_policy_path: str = "/etc/surogates/policies"
    enabled: bool = True


class Settings(BaseSettings):
    model_config = {"env_prefix": "SUROGATES_"}

    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    api: APISettings = Field(default_factory=APISettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    governance: GovernanceSettings = Field(default_factory=GovernanceSettings)

    # Paths
    platform_skills_dir: str = "/etc/surogates/skills"
    platform_tools_dir: str = "/etc/surogates/tools"
    platform_mcp_dir: str = "/etc/surogates/mcp"
    tenant_assets_root: str = "/data/tenant-assets"
    model_metadata_path: str = "/etc/surogates/model-metadata.json"

    # Identity
    worker_id: str = ""  # set from K8s downward API (pod name)

    log_level: str = "INFO"


def load_settings() -> Settings:
    return Settings()
