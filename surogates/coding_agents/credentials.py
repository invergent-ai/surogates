"""Per-user credential storage for external coding agents.

Capture model "A": the user runs the vendor CLI's own login on their
machine and pastes the binary-minted credential.  We validate it and
store an opaque JSON bundle in the encrypted ``CredentialVault`` — we
never run an OAuth flow and never call provider APIs ourselves.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Final

PROVIDERS: Final[tuple[str, ...]] = ("anthropic", "openai")

CRED_NAME: Final[dict[str, str]] = {
    "anthropic": "code_cred:anthropic",
    "openai": "code_cred:openai",
}


class CredentialError(ValueError):
    """A pasted credential was malformed.  The message is user-facing."""


@dataclass
class CredentialBundle:
    """The opaque value stored (encrypted) in the credentials vault."""

    provider: str
    auth_mode: str  # "oauth" | "api_key"
    token_kind: str | None = None  # "setup_token" for anthropic oauth
    oauth_token: str | None = None  # anthropic setup-token
    api_key: str | None = None  # api_key mode
    auth_json: dict | None = None  # codex ~/.codex/auth.json (parsed)
    expires_at: int | None = None  # reserved; None in v1
    version: int = 1

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "CredentialBundle":
        data = json.loads(raw)
        return cls(
            provider=data["provider"],
            auth_mode=data["auth_mode"],
            token_kind=data.get("token_kind"),
            oauth_token=data.get("oauth_token"),
            api_key=data.get("api_key"),
            auth_json=data.get("auth_json"),
            expires_at=data.get("expires_at"),
            version=data.get("version", 1),
        )

    def status(self) -> dict:
        """Connection metadata for the UI — never includes the secret."""
        return {
            "provider": self.provider,
            "connected": True,
            "auth_mode": self.auth_mode,
            "expires_at": self.expires_at,
        }
