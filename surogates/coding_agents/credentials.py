"""Per-user credential storage for external coding agents.

Capture model "A": the user runs the vendor CLI's own login on their
machine and pastes the binary-minted credential.  We validate it and
store an opaque JSON bundle in the encrypted ``CredentialVault`` — we
never run an OAuth flow and never call provider APIs ourselves.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Final
from uuid import UUID

if TYPE_CHECKING:
    from surogates.tenant.credentials import CredentialVault

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


def validate_pasted(provider: str, mode: str, value: str) -> CredentialBundle:
    """Validate a pasted credential and build a bundle, or raise CredentialError."""
    if provider not in PROVIDERS:
        raise CredentialError(
            f"Unknown provider {provider!r}; expected one of {', '.join(PROVIDERS)}."
        )
    if mode not in ("oauth", "api_key"):
        raise CredentialError(f"Unknown mode {mode!r}; expected 'oauth' or 'api_key'.")

    value = value.strip()
    if not value:
        raise CredentialError("Credential value is empty.")

    if provider == "anthropic":
        if mode == "oauth":
            if not value.startswith("sk-ant-oat"):
                raise CredentialError(
                    "That does not look like a Claude setup-token. Run "
                    "`claude setup-token` and paste the value starting with "
                    "'sk-ant-oat'."
                )
            return CredentialBundle(
                provider="anthropic",
                auth_mode="oauth",
                token_kind="setup_token",
                oauth_token=value,
            )
        if not value.startswith("sk-ant-api"):
            raise CredentialError("Anthropic API keys start with 'sk-ant-api'.")
        return CredentialBundle(
            provider="anthropic", auth_mode="api_key", api_key=value,
        )

    # provider == "openai"
    if mode == "oauth":
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise CredentialError(
                "Paste the full contents of ~/.codex/auth.json (valid JSON)."
            ) from exc
        if not isinstance(parsed, dict):
            raise CredentialError("auth.json must be a JSON object.")
        token = (parsed.get("tokens") or {}).get("access_token")
        if not token or not isinstance(token, str):
            raise CredentialError(
                "auth.json is missing tokens.access_token. Run `codex login` "
                "first, then paste ~/.codex/auth.json."
            )
        return CredentialBundle(
            provider="openai", auth_mode="oauth", auth_json=parsed,
        )

    # openai api_key
    if not value.startswith("sk-") or value.startswith("sk-ant-"):
        raise CredentialError("OpenAI API keys start with 'sk-'.")
    return CredentialBundle(provider="openai", auth_mode="api_key", api_key=value)


def _row_scope(
    provider: str,
    *,
    user_id: UUID | None,
    service_account_id: UUID | None,
) -> tuple[UUID | None, str]:
    """Return the ``(vault_user_id, vault_name)`` for a principal.

    The credential is keyed on whoever runs ``/code``:

    * **End user** — stored under the ``user_id`` column with the plain
      ``code_cred:<provider>`` name (the original scheme).
    * **Service account** — the ``credentials.user_id`` column is FK-bound
      to ``users.id`` so an SA id can't live there.  Instead the row is
      org-scoped (``user_id`` NULL) and the SA id rides in the name:
      ``code_cred:<provider>:sa:<sa_id>``.  The route only ever builds the
      name from the *caller's own* principal, so SAs can't read each other.

    Deliberately no org fallback: a missing per-principal credential never
    resolves to another principal's row.
    """
    if user_id is not None:
        return user_id, CRED_NAME[provider]
    if service_account_id is not None:
        return None, f"{CRED_NAME[provider]}:sa:{service_account_id}"
    raise ValueError(
        "coding-agent credential needs a user or service-account principal",
    )


class CodingAgentCredentials:
    """Per-principal coding-agent credential storage over the encrypted vault.

    Keyed on the session's *effective principal* — the end user in the
    agent UI, or the per-operator ``ops-chat`` service account on the ops
    work surface — so the same identity that connects a plan also resolves
    it at run time on each surface.
    """

    def __init__(self, vault: "CredentialVault") -> None:
        self._vault = vault

    async def store(
        self,
        *,
        org_id: UUID,
        bundle: CredentialBundle,
        user_id: UUID | None = None,
        service_account_id: UUID | None = None,
    ) -> None:
        vault_user_id, name = _row_scope(
            bundle.provider, user_id=user_id, service_account_id=service_account_id,
        )
        await self._vault.store(
            org_id, name, bundle.to_json(), user_id=vault_user_id,
        )

    async def load(
        self,
        *,
        org_id: UUID,
        provider: str,
        user_id: UUID | None = None,
        service_account_id: UUID | None = None,
    ) -> CredentialBundle | None:
        vault_user_id, name = _row_scope(
            provider, user_id=user_id, service_account_id=service_account_id,
        )
        raw = await self._vault.retrieve(org_id, name, user_id=vault_user_id)
        return CredentialBundle.from_json(raw) if raw else None

    async def delete(
        self,
        *,
        org_id: UUID,
        provider: str,
        user_id: UUID | None = None,
        service_account_id: UUID | None = None,
    ) -> bool:
        vault_user_id, name = _row_scope(
            provider, user_id=user_id, service_account_id=service_account_id,
        )
        return await self._vault.delete(org_id, name, user_id=vault_user_id)

    async def statuses(
        self,
        *,
        org_id: UUID,
        user_id: UUID | None = None,
        service_account_id: UUID | None = None,
    ) -> list[dict]:
        out: list[dict] = []
        for provider in PROVIDERS:
            bundle = await self.load(
                org_id=org_id, provider=provider,
                user_id=user_id, service_account_id=service_account_id,
            )
            out.append(
                bundle.status()
                if bundle is not None
                else {
                    "provider": provider,
                    "connected": False,
                    "auth_mode": None,
                    "expires_at": None,
                }
            )
        return out
