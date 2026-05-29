"""Per-session agent runtime context (Plan 1, Task 11).

``AgentRuntimeContext`` is the immutable snapshot of everything the
surogates worker needs to serve one session of one shared-runtime
agent.  The shared surogates api populates it at session start by
calling the management plane's
``/api/agents/{agent_id}/runtime-config`` endpoint and projecting the
response through :func:`surogates.runtime.resolver.build_agent_runtime_context`.

Distinct from :class:`surogates.tenant.TenantContext`, which represents
the *authentication principal* on a request (user / service-account /
channel-session).  The two compose at the handler layer:

* ``TenantContext`` answers "who is calling".
* ``AgentRuntimeContext`` answers "what agent + config are we serving".

The resolver guarantees ``TenantContext.org_id == AgentRuntimeContext.org_id``
so downstream code can use either as the tenant key without a second
lookup.

All fields are frozen.  Mutating a value means constructing a new
context.  This is intentional: every place we hold a reference to a
context (the harness loop, the credential vault, MCP proxy clients,
the bundle accessor) reads from a moment-in-time snapshot, and silent
mutation would let one session's edits leak into another's.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["AgentRuntimeContext", "LLMEndpoint"]


@dataclass(frozen=True, slots=True)
class LLMEndpoint:
    """One configured LLM client (main / summary / vision / advisor).

    ``api_key_ref`` is a vault reference (e.g. ``vault://<id>``) — the
    raw key never appears in this dataclass.  Resolution to a concrete
    secret happens at LLM-call time through the credential vault so
    workers, audit logs, and intermediate proxies never observe the
    plaintext.
    """

    model: str
    base_url: str
    api_key_ref: str


@dataclass(frozen=True, slots=True)
class AgentRuntimeContext:
    """Immutable per-session agent runtime configuration.

    Required fields:

    * ``agent_id`` — the shared-runtime agent serving this session.
    * ``org_id`` — tenant identity; equal to the project id on the
      management plane (no separate orgs table in this codebase).
    * ``enabled`` — the management plane's lifecycle gate.  When False
      the resolver short-circuits with a 503 before this context is
      ever constructed; callers can assume ``True`` at use time.
    * ``config_version`` — bumped on every runtime-config update;
      the resolver's TTL cache uses it to detect drift after a Redis
      pub/sub invalidation tick.
    * ``storage_key_prefix`` — per-tenant prefix into the shared
      workspaces bucket (typically ``{project_id}/{agent_id}``).

    Optional fields default to absent / empty.  ``project_id`` is
    ``None`` in legacy helm-mode contexts (helm pods do not carry the
    project id in ``settings``) and the management-plane-supplied
    value otherwise; consumers that need it must check for ``None``
    rather than receive a silent empty string.

    The four LLM endpoint triples are independent: only ``llm_main``
    is logically required for the harness to run; ``llm_summary`` /
    ``llm_vision`` / ``llm_advisor`` are auxiliary clients the harness
    falls back to ``llm_main`` for when absent.

    ``mcp_server_ids`` is a tuple (immutable counterpart to a list)
    so the frozen dataclass cannot be mutated through the field.
    """

    agent_id: str
    org_id: str
    enabled: bool
    config_version: int
    storage_key_prefix: str

    project_id: str | None = None

    api_web_url: str | None = None

    llm_main: LLMEndpoint | None = None
    llm_summary: LLMEndpoint | None = None
    llm_vision: LLMEndpoint | None = None
    llm_advisor: LLMEndpoint | None = None

    mcp_server_ids: tuple[str, ...] = ()
    governance: dict = field(default_factory=dict)

    @property
    def asset_root(self) -> str:
        """Path to the tenant's on-disk asset directory.

        Mirrors the layout used by the helm-deployed
        ``tenant-assets`` PVC (``/data/tenant-assets/{org_id}``) so
        legacy harness paths that read assets from disk keep working
        until Plan 4 migrates per-user memory to R2.
        """
        return f"/data/tenant-assets/{self.org_id}"
