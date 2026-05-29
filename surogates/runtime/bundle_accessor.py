"""Per-session read-only handle on an agent's file bundle.

Plan 3 / Task 4.  Constructed by the FileBundleCache (Task 6) per
session and passed into the harness's PromptBuilder + ResourceLoader.
Frozen so a careless harness mutation can't swap the underlying ref
mid-session — every read in the session sees the same bundle
version, even if an admin rotates the agent's hub_ref while the
session is in flight.

The full bundle isn't preloaded into memory; the handle is a
lightweight pointer that defers each read to the HubBundleClient
(which is itself L1+L2 cached at higher layers).  This keeps memory
bounded for agents that ship hundreds of skills.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["AgentFileBundle"]


_UTF8_BOM = b"\xef\xbb\xbf"


@dataclass(frozen=True, slots=True)
class _BundleSpec:
    """Parsed shape of a ``<owner>/<repo>`` Hub reference."""

    user: str
    repository: str

    @classmethod
    def parse(cls, hub_ref: str) -> "_BundleSpec":
        """Split ``acme/agent-bundles`` into ``("acme", "agent-bundles")``.

        Raises ``ValueError`` on missing slash or empty segments so
        a misconfigured payload (e.g., ``"acme"`` or ``"/repo"``)
        fails at session bootstrap instead of in the middle of a
        bundle fetch.
        """
        if "/" not in hub_ref:
            raise ValueError(
                f"bundle hub_ref must be owner/repo; got {hub_ref!r}",
            )
        user, _, repository = hub_ref.partition("/")
        if not user or not repository:
            raise ValueError(
                f"bundle hub_ref has empty segments; got {hub_ref!r}",
            )
        return cls(user=user, repository=repository)


@dataclass(frozen=True, slots=True)
class AgentFileBundle:
    """Read-only accessor on a single (agent_id, hub_ref, version)."""

    agent_id: str
    hub_ref: str
    version: str
    client: Any

    async def read_bytes(self, path: str) -> bytes:
        """Return raw bytes at ``path`` or raise ``LookupError``."""
        return await self.client.read_bytes(self.version, path)

    async def read_text(self, path: str, encoding: str = "utf-8") -> str:
        """Decode the bytes; strip a UTF-8 BOM if present.

        SOUL.md and friends are often saved from Windows tooling
        with a BOM; stripping it here keeps a stray U+FEFF out of
        the LLM's system prompt."""
        data = await self.read_bytes(path)
        if data.startswith(_UTF8_BOM):
            data = data[len(_UTF8_BOM):]
        return data.decode(encoding)

    async def exists(self, path: str) -> bool:
        """True iff ``read_bytes(path)`` would succeed.

        Implemented as a single ``read_bytes`` + LookupError catch
        (instead of, e.g., a separate HEAD-equivalent API) because
        the cache layer will short-circuit the round-trip on a
        cached read — the cost is dominated by the cache hit.
        """
        try:
            await self.read_bytes(path)
        except LookupError:
            return False
        return True

    async def list(self, prefix: str = "") -> list[str]:
        """List object paths under ``prefix`` (sorted)."""
        paths = await self.client.list_paths(self.version, prefix=prefix)
        return sorted(paths)
