"""Where the document server, the load clients and this tool are: from the environment, or a git-ignored .env."""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path

# Settings field -> the environment name it is read from.
ENV_NAMES: dict[str, str] = {
    "ds_url": "OLOAD_DS_URL",
    "command_url": "OLOAD_COMMAND_URL",
    "jwt_secret": "OLOAD_JWT_SECRET",
    "client_token": "OLOAD_CLIENT_TOKEN",
    "listen": "OLOAD_LISTEN",
    "public_base": "OLOAD_PUBLIC_BASE",
    "namespace": "OLOAD_NAMESPACE",
}


class MissingSettings(Exception):
    """Some of the tool's environment names are not set."""


@dataclass(frozen=True, slots=True)
class Settings:
    ds_url: str  # the document server as the pages reach it, in the cluster
    command_url: str  # the document server as this host reaches it: /command, and the saved files
    jwt_secret: str = field(repr=False)  # the document server's JWT secret
    client_token: str = field(repr=False)  # the load clients' bearer token
    listen: str  # host:port this tool binds
    public_base: str  # this tool as the document server reaches it
    namespace: str  # the office namespace: the load clients and the metrics

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        missing = [name for name in ENV_NAMES.values() if not env.get(name, "").strip()]
        if missing:
            raise MissingSettings(f"set {', '.join(missing)} in the environment or in .env")
        return cls(**{attribute: env[name].strip() for attribute, name in ENV_NAMES.items()})


def load_env_file(path: str | Path = ".env", environ: MutableMapping[str, str] | None = None) -> None:
    """Fill the environment from *path*'s ``NAME=value`` lines; what the environment already has wins.

    A missing file changes nothing. Kubectl reads ``KUBECONFIG`` from the environment, so it can come from here too.
    """
    env = os.environ if environ is None else environ
    file = Path(path)
    if not file.is_file():
        return
    for line in file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.removeprefix("export ").partition("=")
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        env.setdefault(name, value)
