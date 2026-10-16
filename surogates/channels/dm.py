"""Where a person's one-to-one conversation is, keyed as the inbound path keys it.

The platform writing first and the person replying must land in the same
session, so every channel's DM key is computed here and nowhere else.
"""

from __future__ import annotations

from dataclasses import dataclass

from surogates.channels.source import SessionSource, build_session_key

__all__ = ["DmTarget", "OpenerUndeliverable", "dm_key", "dm_target"]


class OpenerUndeliverable(Exception):
    """This opener cannot be handed over, and retrying will not change that.

    The send pass records it on the invitation as a failed delivery with
    ``reason`` as the error, so the operator sees one user missed rather than
    a row that stays queued and is retried every tick.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class DmTarget:
    """Where a person's one-to-one conversation is, as the inbound path keys it."""

    session_key: str
    channel_id: str
    destination: dict


def dm_key(platform: str, chat_id: str, user_id: str) -> str:
    return build_session_key(SessionSource(
        platform=platform, chat_id=chat_id, chat_type="dm", user_id=user_id,
        user_name="", thread_id=None, chat_name=chat_id,
    ))


def dm_target(platform: str, user: str, sender: str) -> DmTarget | None:
    """The DM a first message to *user* goes into; ``None`` on Slack and
    Discord, whose DM channel id only the platform knows, on Teams, whose
    personal chat only its recorded conversation reference knows (all in
    :func:`surogates.programs.opener.resolve_dm`), and on a channel the
    platform cannot write first on.

    *user* is the identity's ``platform_user_id``: the ``wa_id`` on WhatsApp,
    the chat id on Telegram, digits for voice, the person's user id for web,
    the integrator's own id for api.  *sender* is the agent's: its number for
    WhatsApp and voice, its webhook identifier for api; web needs none.
    """
    if platform == "whatsapp":
        return DmTarget(
            dm_key("whatsapp", user, user), user,
            {"wa_id": user, "phone_number_id": sender, "channel_identifier": sender},
        )
    if platform == "telegram":
        # A private chat's id is the user's id.
        return DmTarget(dm_key("telegram", user, user), user,
                        {"chat_id": user, "channel_identifier": sender})
    if platform == "voice":
        digits = "".join(c for c in user if c.isdigit())
        if not sender:
            raise OpenerUndeliverable("This agent has no phone number that can call out")
        return DmTarget(dm_key("voice", digits, digits), digits,
                        {"to": f"+{digits}", "channel_identifier": sender})
    if platform == "web":
        return DmTarget(dm_key("web", user, user), user,
                        {"user_id": user, "channel_identifier": sender or "web"})
    if platform == "api":
        if not sender:
            raise OpenerUndeliverable("This agent has no webhook to deliver to")
        return DmTarget(dm_key("api", user, user), user,
                        {"external_id": user, "channel_identifier": sender})
    return None
