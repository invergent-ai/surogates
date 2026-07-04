"""The opaque bundle stored (encrypted) in the credentials vault for an SSH key.

Mirrors :class:`surogates.coding_agents.credentials.CredentialBundle`: a stdlib
dataclass with JSON (de)serialization.  Holds only the private key + optional
passphrase — host keys are per-target (``ssh_targets[].host_key``), not per-key.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass


def validate_private_key(value: str) -> str:
    """Return the stripped key if it looks like a PEM/OpenSSH private key.

    Raises ``ValueError`` otherwise.  Deliberately format-agnostic (RSA, EC,
    OpenSSH, PKCS#8 all carry the ``PRIVATE KEY-----`` marker) — the ssh-agent
    is the real parser; this only rejects obvious non-keys before we vault them.
    """
    v = (value or "").strip()
    if not (v.startswith("-----BEGIN") and "PRIVATE KEY-----" in v):
        raise ValueError(
            "Expected a PEM/OpenSSH private key (-----BEGIN … PRIVATE KEY-----).",
        )
    # OpenSSH/ssh-add rejects a key without a trailing newline; guarantee one.
    return v + "\n"


@dataclass
class SSHKeyBundle:
    """The vault value for a named SSH key."""

    private_key: str
    passphrase: str | None = None
    version: int = 1

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "SSHKeyBundle":
        data = json.loads(raw)
        return cls(
            private_key=data["private_key"],
            passphrase=data.get("passphrase"),
            version=data.get("version", 1),
        )

    def status(self) -> dict:
        """Connection metadata for the UI — never includes the secret."""
        return {"connected": True, "has_passphrase": self.passphrase is not None}
