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
    identity: Literal["phone", "member", "chat", "user", "external"] | None = None
    receipts: frozenset[str] = frozenset()   # {"delivered", "read"} the provider reports


_NONE = ChannelCapabilities()

#: Channels with no inbound webhook platform in the registry, written to by
#: the outbound loops in :mod:`surogates.channels.outbound` and the voice
#: worker.  Each writes into the person's own conversation
#: (``agent:<kind>:dm:<id>``) and delivers it its own way:
#:
#: * ``voice`` places a phone call from the agent's number and speaks it.
#:   Answered is delivered; an acknowledged alert call is read.  Whether an
#:   agent can call at all is its sender: a voice number with an outbound
#:   trunk.
#: * ``web`` leaves it in the person's web chat, shown on their next visit,
#:   and sends a browser push to each browser they allowed.  Written into the
#:   chat is delivered; nothing reports read.  ``platform_user_id`` is the
#:   person's user id.
#: * ``api`` hands it to ops, which posts it to the agent's webhook for an
#:   integrator's app (``platform_user_id`` is the integrator's own id for the
#:   person).  The integrator reports delivered and read back.
#:
#: ``website`` visitors stay anonymous (no user, an hour-long cookie), so there
#: is no one to write to first there.
BUILTIN: dict[str, ChannelCapabilities] = {
    "voice": ChannelCapabilities(
        can_initiate=True, opener="text", identity="phone", receipts=frozenset({"read"}),
    ),
    "web": ChannelCapabilities(can_initiate=True, opener="text", identity="user"),
    "api": ChannelCapabilities(
        can_initiate=True, opener="text", identity="external",
        receipts=frozenset({"delivered", "read"}),
    ),
}


def _registry():
    # The platform modules register themselves on import, and the processes
    # that ask (the ticker, the worker, ops) never import them otherwise.
    import surogates.channels.platforms  # noqa: F401
    from surogates.channels.registry import registry

    return registry


def capabilities_for(kind: str | None) -> ChannelCapabilities:
    """The registered platform's capabilities, else the built-in ones; nothing for website."""
    if not kind:
        return _NONE
    platform = _registry().get(kind)
    return getattr(platform, "capabilities", None) or BUILTIN.get(kind, _NONE)


def initiating_platforms() -> tuple[str, ...]:
    """Every channel kind the platform may write first on."""
    registered = tuple(
        p.kind for p in _registry().all_platforms()
        if getattr(p, "capabilities", _NONE).can_initiate
    )
    return registered + tuple(k for k, c in BUILTIN.items() if c.can_initiate and k not in registered)
