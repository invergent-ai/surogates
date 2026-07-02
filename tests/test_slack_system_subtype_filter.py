"""Slack ``parse`` drops system-notification messages, keeps real user messages.

Regression: a membership/system notification (any ``message`` subtype that
isn't a real user message — join/leave, topic/name/purpose changes, archive,
pins, bot_add, …) must NOT create a turn.  The old denylist only covered
join/leave, so other system subtypes slipped through and the agent replied
"no action needed" to them.
"""

from surogates.channels.platforms.slack import parse


def _msg(**overrides):
    event = {"type": "message", "channel": "C1", "user": "U1", "text": "hi", "ts": "100.0"}
    event.update(overrides)
    return {"type": "event_callback", "event": event}


# --- system subtypes: all dropped -------------------------------------------

def test_membership_and_system_subtypes_are_dropped():
    for subtype in (
        "channel_join", "channel_leave", "channel_topic", "channel_purpose",
        "channel_name", "channel_archive", "channel_unarchive",
        "group_join", "group_leave", "bot_add", "bot_remove",
        "pinned_item", "unpinned_item", "reminder_add", "tombstone",
    ):
        assert parse(_msg(subtype=subtype), bot_user_id="U_BOT") is None, subtype


# --- real user messages: all kept -------------------------------------------

def test_plain_user_message_is_kept():
    msg = parse(_msg(), bot_user_id="U_BOT")
    assert msg is not None
    assert msg.text == "hi"


def test_user_message_subtypes_are_kept():
    for subtype in ("file_share", "thread_broadcast", "me_message"):
        assert parse(_msg(subtype=subtype), bot_user_id="U_BOT") is not None, subtype


def test_other_bot_message_still_reaches_pipeline():
    # A different bot's message keeps flowing (the allow_bots gate decides later),
    # so the system-subtype allowlist must not drop it.
    msg = parse(
        _msg(subtype="bot_message", bot_id="B999", user="U_OTHERBOT"),
        bot_user_id="U_BOT",
    )
    assert msg is not None
    assert msg.is_bot is True
