"""Live ``llm.delta`` frames: published over Redis, never written to the event log.

A streamed model reply reaches a live viewer as ``llm.delta`` frames.  They
exist only to animate the reply while it is written: replay, context rebuild,
search and every page of the event log already leave them out, and the
``llm.response`` (with ``llm.thinking``) that ends the call holds the whole
text.  So the worker publishes them on the session's pub/sub channel,
``surogates:session:<id>``, beside the ``"<event id>:<event type>"``
notification every persisted event already sends there, and writes no row.

Wire format of a live frame on that channel::

    ~<after>:<seq>:<the frame's data as JSON>

*after* is the id of the newest event the session had when the model call
that wrote the frame sent its first one: it names the call, and a reader uses
it to tell a frame that is still news from one whose call it has already seen
end (:class:`DeltaGate`).  *seq* numbers the call's frames from 0, so a reader
that joins mid-call knows it missed the start.  Every other message on the
channel starts with a digit,
so a reader that only wakes on the channel and reads the log, as every reader
before this one did, keeps working: a live frame wakes it, and its read finds
nothing new.

Redis pub/sub keeps one channel's messages in the order Redis took them, and
a worker publishes a call's frames before it writes the call's
``llm.thinking`` / ``llm.response``.  A reader that takes the channel in order,
and reads the log only up to the event a notification names, therefore hands
its client every frame of a call before the event that ends it.  Without
Redis there are no live frames: a stream reads the log, as it always has, and
sees each reply whole at its ``llm.response``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from surogates.session.events import EventType

#: First character of a live frame's pub/sub message.
LIVE_FRAME_MARK = "~"

#: The events that end a model call, or show that it ended: a frame of the call
#: arriving after one of them is stale.  ``llm.request`` is here because calls
#: in one session run one after another, so a new request means the one before
#: it is over.
CALL_BOUNDARY_TYPES: frozenset[str] = frozenset(t.value for t in (
    EventType.LLM_REQUEST,
    EventType.LLM_THINKING,
    EventType.LLM_RESPONSE,
    EventType.HARNESS_CRASH,
    EventType.SESSION_COMPLETE,
    EventType.SESSION_FAIL,
    EventType.SESSION_PAUSE,
    EventType.SESSION_STOPPED,
))


def session_channel(session_id: Any) -> str:
    """The pub/sub channel of a session's events."""
    return f"surogates:session:{session_id}"


def encode_live_frame(after: int, seq: int, data_json: str) -> str:
    """The pub/sub message of frame *seq* of call *after*, whose data is *data_json*."""
    return f"{LIVE_FRAME_MARK}{int(after)}:{int(seq)}:{data_json}"


@dataclass(frozen=True, slots=True)
class LiveFrame:
    """One live ``llm.delta`` frame, as a reader takes it off the channel."""

    #: The call the frame belongs to (see the module docstring).
    after: int
    #: The frame's place in its call, from 0.
    seq: int
    #: The frame's data, as JSON text: an SSE stream forwards it unparsed.
    data_json: str

    @property
    def data(self) -> dict:
        try:
            value = json.loads(self.data_json)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}


@dataclass(frozen=True, slots=True)
class Notification:
    """A persisted event's notification: ``<event id>:<event type>``."""

    event_id: int


def decode_message(message: Any) -> LiveFrame | Notification | None:
    """What a pub/sub *message* (a redis-py ``get_message`` dict) carries.

    None for anything that is neither a live frame nor a well-formed event
    notification (a subscribe reply, a message of another shape); a reader
    should treat that as a nudge to read the log.
    """
    if not isinstance(message, dict) or message.get("type") not in ("message", "pmessage"):
        return None
    raw = message.get("data")
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str):
        return None
    if raw.startswith(LIVE_FRAME_MARK):
        after, sep, rest = raw[1:].partition(":")
        seq, sep2, data_json = rest.partition(":")
        if not (sep and sep2 and after.isdigit() and seq.isdigit()):
            return None
        return LiveFrame(after=int(after), seq=int(seq), data_json=data_json)
    head, sep, _ = raw.partition(":")
    if sep and head.isdigit():
        return Notification(event_id=int(head))
    return None


class DeltaGate:
    """Whether a live frame is still news to a reader of a session's events.

    A reader shows a frame only while the model call that wrote it has not
    ended in what the reader has already passed on.  It tells it by the
    frame's ``after``: an event of :data:`CALL_BOUNDARY_TYPES` with a higher
    id ended that call (or a later one), so the frame is stale.

    *floor* drops, as well, every frame of a call that started before the
    event with that id (a reader of one turn starting at the turn's
    ``user.message``: frames of the turn before it are not its own).
    """

    __slots__ = ("_closed",)

    def __init__(self, floor: int = 0) -> None:
        self._closed = floor

    def saw(self, event_type: str, event_id: int | None, *, synthetic: bool = False) -> None:
        """Note an event the reader has passed on.

        A *synthetic* ``llm.response`` (one a channel or a command wrote, not
        the model's) ends no model call.
        """
        if synthetic and event_type == EventType.LLM_RESPONSE.value:
            return
        if event_id is not None and event_type in CALL_BOUNDARY_TYPES and event_id > self._closed:
            self._closed = event_id

    def admits(self, frame: LiveFrame) -> bool:
        return frame.after >= self._closed


#: What :func:`next_message` returns for a message on the channel that is
#: neither a live frame nor an event's notification: a nudge to read the log.
NUDGE = object()


async def next_message(pubsub: Any, timeout: float) -> LiveFrame | Notification | object | None:
    """The next message on *pubsub* (redis-py) within *timeout* seconds, decoded.

    A :class:`LiveFrame`, an event's :class:`Notification`, :data:`NUDGE`
    for any other message, or None once *timeout* passes with none.
    Subscribe replies are skipped, not returned.  A subscription that fails
    raises.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while (remaining := deadline - loop.time()) > 0:
        try:
            message = await asyncio.wait_for(
                pubsub.get_message(ignore_subscribe_messages=True, timeout=remaining),
                timeout=remaining + 0.5,
            )
        except asyncio.TimeoutError:
            return None
        if message is not None:
            decoded = decode_message(message)
            return NUDGE if decoded is None else decoded
    return None
