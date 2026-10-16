"""Comments that call the agent.

A person @mentions the agent in a comment in a document open in the chat's file pane. The chat gets a message saying
so (built here, sent by the mention route); before the model reads it, the worker posts ACKNOWLEDGEMENT in the
comment's thread; the model then answers there with the result and resolves the thread.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

#: A mention's user.message names this as its source. Only the mention route sets a message's source to it.
MENTION_SOURCE = "office_mention"
#: What the worker posts in the thread before the model runs, so the person knows the agent took it.
ACKNOWLEDGEMENT = "On it, I'll reply here when done."
_MENTION_KEYS = ("path", "thread", "kind", "author", "author_id", "comment")


def agent_address(agent_id: str) -> str:
    """The address a comment mentions the agent by. The editor offers and matches a mention by an email-shaped
    address only; .invalid is reserved (RFC 2606), so no mail can ever go to it."""
    return f"{re.sub(r'[^A-Za-z0-9._-]', '-', agent_id)}@agent.invalid"


def mentionable_agent(agent_id: str, name: str) -> dict[str, str]:
    """The agent as the editor lists it to mention: the only one it lists."""
    return {"id": f"agent:{agent_id}", "name": name, "email": agent_address(agent_id)}


def mention_message(
    *, path: str, thread: str, kind: str, author: str, author_id: str, comment: str, agent_id: str, agent_name: str,
) -> tuple[str, dict[str, str]]:
    """The chat message a mention becomes, and its metadata.

    The content shows the chat who said what, and where, with the agent's address written as its name. The metadata
    keeps the comment's exact words: a PDF's mention carries no lasting thread id, and its thread is found by them."""
    said = re.sub(rf"[@+]{re.escape(agent_address(agent_id))}", lambda _: f"@{agent_name}", comment, flags=re.IGNORECASE)
    content = f"[Comment on {PurePosixPath(path).name}] {author}: «{said}»"
    return content, {"path": path, "thread": thread, "kind": kind, "author": author, "author_id": author_id, "comment": comment}


def mention_of(data: dict[str, Any]) -> dict[str, str] | None:
    """A mention's metadata, from a user.message's data; None for any other message."""
    metadata = data.get("metadata")
    if data.get("source") != MENTION_SOURCE or not isinstance(metadata, dict):
        return None
    if not all(isinstance(metadata.get(key), str) and metadata[key] for key in _MENTION_KEYS):
        return None
    return metadata
