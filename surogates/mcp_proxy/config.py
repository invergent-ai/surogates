"""MCP Proxy settings.

Reuses the platform's database, Redis, and JWT infrastructure but
runs as a separate service with its own host/port and connection
pool tuning.
"""

from __future__ import annotations

import os
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings

from surogates.config import (
    DatabaseSettings,
    RedisSettings,
    _load_yaml_config,
    _flatten_yaml,
)


class McpProxySettings(BaseSettings):
    """Settings for the MCP proxy service."""

    model_config = {"env_prefix": "SUROGATES_"}

    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)

    # Service binding
    host: str = "0.0.0.0"
    port: int = 8001
    workers: int = 1

    # Connection pool tuning
    idle_connection_timeout: int = 300  # seconds before idle MCP connections are closed
    max_connections_per_org: int = 50   # max concurrent MCP connections per org

    # Security
    jwt_secret: str = "change-me-in-production"
    encryption_key: str = ""  # Fernet key for credential vault

    # Platform MCP configs (mounted from ConfigMap)
    platform_mcp_dir: str = "/etc/surogates/mcp"

    log_level: str = "INFO"


def load_proxy_settings() -> McpProxySettings:
    """Load proxy settings from config file + environment variables."""
    yaml_config = _load_yaml_config()
    if yaml_config:
        flat = _flatten_yaml(yaml_config)
        for key, value in flat.items():
            if key not in os.environ:
                os.environ[key] = value

    return McpProxySettings()
