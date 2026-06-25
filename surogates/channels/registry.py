"""Channel platform protocol, descriptors, and module-level registry.

Every messaging-platform strategy (Slack, Telegram, …) implements
:class:`ChannelPlatform` and self-registers via::

    from surogates.channels.registry import registry
    registry.register(MyPlatform())

The :class:`ChannelRegistry` stores platforms keyed by :attr:`ChannelPlatform.kind`
and provides :meth:`~ChannelRegistry.enabled_platforms` to filter by the
runtime settings object.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal, Protocol, runtime_checkable

from surogates.channels.base import SendResult
from surogates.channels.inbound import InboundMessage

if TYPE_CHECKING:
    pass

__all__ = [
    "VerificationResult",
    "ChannelDescriptor",
    "ChannelPlatform",
    "ChannelRegistry",
    "registry",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# VerificationResult
# ---------------------------------------------------------------------------


@dataclass
class VerificationResult:
    """Result returned by :meth:`ChannelPlatform.verify`.

    Used for handshake responses that need a custom body or status code
    (e.g. Slack URL-verification challenge).

    Attributes
    ----------
    accepted:
        ``True`` when the request signature/challenge is valid.
    response_body:
        Optional response payload to return to the platform (dict → JSON,
        str → plain text).  ``None`` means respond with an empty 200.
    status_code:
        HTTP status code to use in the response.  Defaults to ``200``.
    """

    accepted: bool
    response_body: dict | str | None = None
    status_code: int = 200


# ---------------------------------------------------------------------------
# ChannelDescriptor
# ---------------------------------------------------------------------------


@dataclass
class ChannelDescriptor:
    """Static metadata about a channel platform used by provisioning code.

    Attributes
    ----------
    vault_refs:
        Callable ``(identifier) -> dict[str, str]`` that maps credential
        names to their Vault/secret-store paths for the given workspace
        identifier.
    config_keys:
        Tuple of setting keys that must be present in the per-platform
        config block for this platform to operate.
    webhook_registration:
        ``"api"`` if the platform supports programmatic webhook registration
        (e.g. Slack `/api/apps.connections.open`); ``"manual"`` if the
        operator must register the URL in the platform's developer console.
    register_webhook:
        Optional async callable ``(identifier, url, creds) -> None`` invoked
        when the platform supports API-driven webhook registration.
    """

    vault_refs: Callable[[str], dict[str, str]]
    config_keys: tuple[str, ...]
    webhook_registration: Literal["api", "manual"]
    register_webhook: Callable[[str, str, dict], Awaitable[None]] | None = None


# ---------------------------------------------------------------------------
# ChannelPlatform protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ChannelPlatform(Protocol):
    """Structural protocol that every messaging-platform strategy must satisfy.

    Required members
    ----------------
    kind:
        Unique slug identifying the platform (``"slack"``, ``"telegram"``).
    topology:
        How the platform delivers events: ``"webhook"`` (platform pushes
        HTTP requests to our server) or ``"socket"`` (we maintain a
        persistent outbound connection).
    route_path(identifier):
        Returns the URL path at which this platform's webhook is mounted.
        ``identifier`` is optional (``None`` for single-tenant deployments).
    identifier_of(request, body):
        Extracts the workspace/channel identifier from the raw incoming
        request and parsed body.
    verify(request, body, *, creds):
        Validates the request signature/challenge.  Returns a plain ``bool``
        or a :class:`VerificationResult` when a custom response is needed.
    parse(body):
        Converts the platform payload to an :class:`InboundMessage`, or
        ``None`` for events that do not carry a user message (e.g. reactions,
        edits, the Slack URL-verification challenge itself).
    send(item, *, creds):
        Sends an outbound message.  Returns a :class:`SendResult`.
    descriptor:
        Static :class:`ChannelDescriptor` for provisioning.

    Optional members
    ----------------
    interactive_paths:
        Tuple of URL paths that receive interactive payloads (button clicks,
        modal submissions).  Omit or set to ``()`` if not needed.
    handle_non_message_update(body, *, routing, creds, deps):
        Called for verified updates that are *not* user messages (e.g.
        Telegram ``callback_query`` approval buttons).  Returns ``True`` when
        the update was fully handled (dispatcher should ack 200 and skip the
        inbound pipeline), ``False`` to fall through.
    """

    kind: str
    topology: Literal["webhook", "socket"]
    descriptor: ChannelDescriptor

    def route_path(self, identifier: str | None = None) -> str: ...

    def identifier_of(self, request: Any, body: Any) -> str: ...

    def verify(
        self, request: Any, body: Any, *, creds: dict
    ) -> bool | VerificationResult: ...

    def parse(self, body: Any, *, creds: dict | None = None) -> InboundMessage | None: ...

    async def send(self, item: Any, *, creds: dict) -> SendResult: ...

    # Optional — dispatcher uses getattr with defaults
    interactive_paths: tuple[str, ...]

    async def handle_non_message_update(
        self, body: Any, *, routing: Any, creds: dict, deps: Any
    ) -> bool: ...

    async def handle_interactive(
        self,
        path_template: str,
        form: dict,
        *,
        request: Any,
        creds: dict,
        routing: Any,
    ) -> Any:
        """Handle a form-encoded interactive request (slash commands, button clicks).

        Called by the dispatcher for requests arriving on any of the paths
        declared in ``interactive_paths``.  Only platforms that declare
        ``interactive_paths`` and implement this method will receive these calls
        — the dispatcher uses ``getattr`` to check for its presence.

        Parameters
        ----------
        path_template:
            The FastAPI route path template that matched this request.
        form:
            Parsed ``application/x-www-form-urlencoded`` body as a plain dict.
        request:
            Starlette-like request object.
        creds:
            Resolved credential dict.
        routing:
            Routing object from the dispatcher.

        Returns
        -------
        InboundMessage
            Forward through the inbound pipeline (enrich + pipeline.handle).
        Response
            Return directly to the caller (e.g. usage hint, ack-only).
        None
            Silently ack with 200, no side effects.
        """
        ...


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ChannelRegistry:
    """Registry of :class:`ChannelPlatform` instances keyed by ``kind``.

    Usage::

        reg = ChannelRegistry()
        reg.register(SlackPlatform())
        platform = reg.get("slack")

    Self-registration on import::

        from surogates.channels.registry import registry
        registry.register(MyPlatform())
    """

    def __init__(self) -> None:
        self._platforms: dict[str, ChannelPlatform] = {}

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def register(self, platform: ChannelPlatform) -> None:
        """Register *platform* under its :attr:`~ChannelPlatform.kind`.

        Raises
        ------
        ValueError
            If a platform with the same ``kind`` is already registered.
        """
        kind = platform.kind
        if kind in self._platforms:
            raise ValueError(
                f"Channel platform '{kind}' is already registered; "
                "unregister it first or use a unique kind."
            )
        self._platforms[kind] = platform
        logger.debug("Registered channel platform: %s (%s)", kind, type(platform).__name__)

    def unregister(self, kind: str) -> None:
        """Remove the platform registered under *kind* (no-op if absent)."""
        self._platforms.pop(kind, None)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get(self, kind: str) -> ChannelPlatform | None:
        """Return the platform for *kind*, or ``None`` if not registered."""
        return self._platforms.get(kind)

    def all_platforms(self) -> list[ChannelPlatform]:
        """Return all registered platforms in registration order."""
        return list(self._platforms.values())

    def enabled_platforms(self, settings: Any) -> list[ChannelPlatform]:
        """Return registered platforms that are enabled in *settings*.

        *settings* is duck-typed: ``settings.channels`` must be a mapping
        from kind string to an object with an ``enabled`` boolean attribute.
        Platforms whose kind has no entry in ``settings.channels`` are
        treated as disabled.

        Parameters
        ----------
        settings:
            Application settings object with a ``channels`` attribute.
        """
        channels_config: dict = getattr(settings, "channels", {}) or {}
        result: list[ChannelPlatform] = []
        for kind, platform in self._platforms.items():
            cfg = channels_config.get(kind)
            if cfg is not None and getattr(cfg, "enabled", False):
                result.append(platform)
        return result


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

#: Global channel platform registry.  Platform modules self-register here on
#: import so that ``registry.all_platforms()`` returns all available strategies.
registry: ChannelRegistry = ChannelRegistry()
