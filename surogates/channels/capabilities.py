"""What each channel can do when the platform writes first.

Outreach, team alerts, enrolment, forms and the Program opener read these
instead of asking ``platform == "whatsapp"``, so a channel is added by
declaring what it can do rather than by finding every such test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = ["ChannelCapabilities", "capabilities_for", "initiating_platforms"]


@dataclass(frozen=True)
class ChannelCapabilities:
    can_initiate: bool = False          # may we write first
    needs_prior_contact: bool = False   # only people who wrote to us before (Telegram)
    opener: Literal["template", "text"] | None = None  # how a first message opens a conversation
    window_hours: int | None = None     # provider service window; None = no window
    buttons: bool = False
    lists: bool = False
    forms: bool = False
    identity: Literal["phone", "member", "chat"] | None = None
    receipts: frozenset[str] = frozenset()   # {"delivered", "read"} the provider reports


_NONE = ChannelCapabilities()


def _registry():
    # The platform modules register themselves on import, and the processes
    # that ask (the ticker, the worker, ops) never import them otherwise.
    import surogates.channels.platforms  # noqa: F401
    from surogates.channels.registry import registry

    return registry


def capabilities_for(kind: str | None) -> ChannelCapabilities:
    """The registered platform's capabilities; nothing for web, website, voice."""
    if not kind:
        return _NONE
    platform = _registry().get(kind)
    return getattr(platform, "capabilities", None) or _NONE


def initiating_platforms() -> tuple[str, ...]:
    """Every channel kind the platform may write first on."""
    return tuple(
        p.kind for p in _registry().all_platforms()
        if getattr(p, "capabilities", _NONE).can_initiate
    )
