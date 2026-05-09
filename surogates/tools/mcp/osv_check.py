"""OSV malware check for stdio MCP package launchers."""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

_OSV_ENDPOINT = os.getenv("OSV_ENDPOINT", "https://api.osv.dev/v1/query")
_TIMEOUT_SECONDS = 10


def check_package_for_malware(command: str, args: list[Any]) -> str | None:
    """Return a blocking error if *command*/*args* points at known malware."""
    ecosystem = _infer_ecosystem(command)
    if ecosystem is None:
        return None

    package, version = _parse_package_from_args(args, ecosystem)
    if not package:
        return None

    try:
        advisories = _query_osv(package, ecosystem, version)
    except Exception as exc:
        logger.debug(
            "OSV malware check failed for %s/%s; allowing launch: %s",
            ecosystem,
            package,
            exc,
        )
        return None

    malware = [v for v in advisories if str(v.get("id", "")).startswith("MAL-")]
    if not malware:
        return None

    ids = ", ".join(str(v.get("id", "")) for v in malware[:3])
    summaries = "; ".join(str(v.get("summary", v.get("id", "")))[:120] for v in malware[:3])
    return (
        f"BLOCKED: MCP package {package!r} ({ecosystem}) has known malware "
        f"advisories: {ids}. Details: {summaries}"
    )


def _infer_ecosystem(command: str) -> str | None:
    base = os.path.basename(str(command)).lower()
    if base in {"npx", "npx.cmd"}:
        return "npm"
    if base in {"uvx", "uvx.cmd", "pipx"}:
        return "PyPI"
    return None


def _parse_package_from_args(
    args: list[Any], ecosystem: str,
) -> tuple[str | None, str | None]:
    token = next(
        (
            str(arg)
            for arg in args
            if isinstance(arg, str) and arg and not arg.startswith("-")
        ),
        None,
    )
    if token is None:
        return None, None

    if ecosystem == "npm":
        return _parse_npm_package(token)
    if ecosystem == "PyPI":
        return _parse_pypi_package(token)
    return token, None


def _parse_npm_package(token: str) -> tuple[str | None, str | None]:
    if token.startswith("@"):
        match = re.match(r"^(@[^/]+/[^@]+)(?:@(.+))?$", token)
        if match:
            return match.group(1), match.group(2)
        return token, None
    if "@" in token:
        name, version = token.rsplit("@", 1)
        return name, version if version and version != "latest" else None
    return token, None


def _parse_pypi_package(token: str) -> tuple[str | None, str | None]:
    match = re.match(r"^([a-zA-Z0-9._-]+)(?:\[[^\]]*\])?(?:==(.+))?$", token)
    if not match:
        return token, None
    return match.group(1), match.group(2)


def _query_osv(package: str, ecosystem: str, version: str | None) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {"package": {"name": package, "ecosystem": ecosystem}}
    if version:
        payload["version"] = version

    req = urllib.request.Request(
        _OSV_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "surogates-mcp-osv-check/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as response:
        data = json.loads(response.read())
    vulns = data.get("vulns", [])
    return vulns if isinstance(vulns, list) else []
