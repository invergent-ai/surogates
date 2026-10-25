"""Per-session tool schema post-processing.

Strips parameters that cannot resolve for the current tenant so the
LLM never sees them and cannot hallucinate values.  Currently gates the
``agent_type`` parameter on ``delegate_task`` / ``spawn_worker``: when
the tenant has no enabled sub-agents, no value for it can resolve, so
it is removed from the exported schema.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from typing import Any

from surogates.channels.constants import (
    ADAPTER_CHANNELS,
    API_CHANNEL,
    STUDIO_CHANNEL,
    VOICE_TOOLS,
)
from surogates.devices.binding import device_of
from surogates.keel.items import ItemContract
from surogates.keel.steps import StepContract
from surogates.tools.builtin.ask_user_question import widen_for_channel
from surogates.tools.builtin.browser import DEVICE_SERVERS_NOTE
from surogates.tools.builtin.case_item import describe_item_completion
from surogates.tools.builtin.case_step import describe_completion
from surogates.tools.builtin.intake import describe_intake
from surogates.tools.builtin.send_form import describe_forms
from surogates.tools.builtin.terminal import DEVICE_GIT_NOTE, DEVICE_MAPPING_NOTE

_AGENT_TYPE_GATED_TOOLS: frozenset[str] = frozenset({
    "delegate_task",
    "spawn_worker",
    "spawn_task",
})


def filter_schemas_for_tenant(
    schemas: list[dict[str, Any]],
    *,
    has_agents: bool,
) -> list[dict[str, Any]]:
    """Return *schemas* with tenant-conditional parameters stripped.

    When *has_agents* is ``False``, the ``agent_type`` property is
    removed from :data:`_AGENT_TYPE_GATED_TOOLS`.  Input is never
    mutated -- affected entries are deep-copied, untouched entries are
    returned by reference.  When *has_agents* is ``True`` the input
    list is returned unchanged.
    """
    if has_agents:
        return schemas

    filtered: list[dict[str, Any]] = []
    for schema in schemas:
        name = schema["function"]["name"]
        if name not in _AGENT_TYPE_GATED_TOOLS:
            filtered.append(schema)
            continue

        clone = copy.deepcopy(schema)
        clone["function"]["parameters"]["properties"].pop("agent_type", None)
        filtered.append(clone)

    return filtered


# Tools whose backing resource is decidable from the agent's config. Shipping
# them when the resource is absent is pure overhead: the model cannot use a KB
# that is not attached or post to a channel that does not exist. Measured on
# the GAIA agent, 21 of 45 shipped tools were never called across 270
# sessions, costing ~5,825 tokens on every request.
_KB_TOOLS: frozenset[str] = frozenset({
    "kb_search_pages", "kb_list_pages", "kb_read_page",
})
_CHANNEL_TOOLS: frozenset[str] = frozenset({
    "fetch_channel_messages", "fetch_channel_file", "mate_ambient_post",
    # Check-ins and follow-up enrolment only ever run on a channel session:
    # the tools act on the conversation's own person and invitation.
    # ``escalate`` is not here: a web conversation can raise a hand too.
    "step_outcome", "followup_enroll", "followup_update",
    "followup_status", "followup_contacts", "followup_withdraw_consent",
})
_CRON_TOOLS: frozenset[str] = frozenset({
    "cron_create", "cron_list", "cron_delete",
})
_WHITEBOARD_TOOLS: frozenset[str] = frozenset({
    "whiteboard_draw",
})
_OFFICE_TOOLS: frozenset[str] = frozenset({
    "office_read", "office_edit",
})
#: The session config key an API caller sets to ``True`` when its own app shows the session's workspace and editor.
FILE_PANE_KEY = "file_pane"


def has_file_pane(session: Any) -> bool:
    """Whether the session's chat shows a document beside it: the web and Surogate Desktop (on a folder of the
    computer too), Surogate Studio's Work chat, a project's threads, or an API caller that declared a file pane when it
    created the session; never a phone call, a channel or a website visitor."""
    from surogates.workstreams import is_project_thread

    config = getattr(session, "config", None) or {}
    channel = getattr(session, "channel", None)
    return (
        channel in ("web", STUDIO_CHANNEL)
        or is_project_thread(config)
        or (channel == API_CHANNEL and config.get(FILE_PANE_KEY) is True)
    )


def channel_tool_flags(channel: str | None) -> dict[str, bool]:
    """Which channel-bound tools a session on ``channel`` can use: the text channels' tools (read the
    channel's messages or files, post into it) need a text channel adapter; a phone call has its own."""
    return {"has_channel": channel in ADAPTER_CHANNELS, "is_voice": channel == "voice"}


def drop_unusable_tools(
    schemas: list[dict[str, Any]],
    *,
    has_kbs: bool,
    has_channel: bool,
    makes_routines: bool,
    is_whiteboard: bool = False,
    is_voice: bool = False,
    has_forms: bool = False,
    has_file_pane: bool = True,
) -> list[dict[str, Any]]:
    """Drop tools whose backing resource this agent does not have.

    Deliberately conservative: only gates on facts already known from the
    runtime config, never on usage history. A tool that is merely unused
    stays, because "not called yet" is not "cannot be called" -- that
    distinction is what makes this safe to apply to every agent rather
    than to one benchmark workload.

    ``send_form`` goes only where it *has_forms*: a WhatsApp conversation
    of an agent with published forms.

    The cron tools go to a session that *makes_routines* through its
    model: a chat with an owner, ``/loop`` on and ``ask_user_question``
    to confirm with.  A scheduled run may not make another.

    The office tools go only where ``has_file_pane``.

    Never returns an empty list: a request with no tools at all is worse
    than an oversized one.
    """
    drop: set[str] = set()
    if not has_kbs:
        drop |= _KB_TOOLS
    if not has_channel:
        drop |= _CHANNEL_TOOLS
    if not makes_routines:
        drop |= _CRON_TOOLS
    if not is_whiteboard:
        drop |= _WHITEBOARD_TOOLS
    if not is_voice:
        drop |= VOICE_TOOLS
    if not has_forms:
        drop.add("send_form")
    if not has_file_pane:
        drop |= _OFFICE_TOOLS
    if not drop:
        return schemas

    kept = [s for s in schemas if s["function"]["name"] not in drop]
    return kept or schemas


# What only a session on a folder of the user's computer can use: a cloud session is not offered it.
DEVICE_ONLY_TOOLS = frozenset({"browser_upload_file"})


def describe_for_device(
    schemas: list[dict[str, Any]],
    config: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return *schemas* with the terminal told what git and shared mappings cannot do in a folder of the user's computer,
    and browser_navigate how the browser there opens the session's own servers.

    Only for a session whose ``config`` names a device; a cloud session's
    list is returned without DEVICE_ONLY_TOOLS.  Input is never mutated.
    """
    if device_of(config) is None:
        return [schema for schema in schemas if schema["function"]["name"] not in DEVICE_ONLY_TOOLS]
    described: list[dict[str, Any]] = []
    for schema in schemas:
        if schema["function"]["name"] == "terminal":
            schema = copy.deepcopy(schema)
            schema["function"]["description"] += f"{DEVICE_GIT_NOTE}\n{DEVICE_MAPPING_NOTE}\n"
        elif schema["function"]["name"] == "browser_navigate":
            schema = copy.deepcopy(schema)
            schema["function"]["description"] += f" {DEVICE_SERVERS_NOTE}"
        described.append(schema)
    return described


def describe_for_conversation(
    schemas: list[dict[str, Any]],
    *,
    channel: str | None,
    forms: Any,
    contract: StepContract | None = None,
    intake: Sequence[dict] = (),
    item: ItemContract | None = None,
) -> list[dict[str, Any]]:
    """Return *schemas* with ``send_form`` limited to the agent's *forms*,
    ``ask_user_question`` taking as many choices as *channel* does, and
    ``complete_step`` taking the outcomes, form and file fields of a case
    step's *contract*, ``complete_item`` what the work *item* takes (a step's
    or a vote's outcomes and form, or a raised hand's choices), and
    ``start_case`` taking only the case types of *intake*, those an intake
    agent may start, each named with the fields it requires.  Input is never
    mutated."""
    described: list[dict[str, Any]] = []
    for schema in schemas:
        name = schema["function"]["name"]
        if name == "send_form":
            schema = describe_forms(schema, forms)
        elif name == "ask_user_question":
            schema = widen_for_channel(schema, channel)
        elif name == "complete_step" and contract is not None:
            schema = describe_completion(schema, contract)
        elif name == "complete_item" and item is not None:
            schema = describe_item_completion(schema, item)
        elif name == "start_case" and intake:
            schema = describe_intake(schema, intake)
        described.append(schema)
    return described
