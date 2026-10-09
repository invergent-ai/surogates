"""Memory, message replay, and context engineering helpers for AgentHarness."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from uuid import UUID

from surogates.devices.binding import device_of
from surogates.devices.browser import of_a_sub_agent, resumes_the_agent
from surogates.harness.context_files import load_folder_context
from surogates.harness.loop_attachments import (
    _attachments_note_from_data,
    _render_inlined_attachments,
)
from surogates.harness.loop_messages import (
    _view_context_note_from_metadata,
    _whiteboard_note_from_metadata,
)
from surogates.harness.loop_tool_recovery import collapse_repeated_tool_rounds
from surogates.harness.sanitize import strip_budget_warnings
from surogates.harness.tool_exec import _WORKSPACE_TOKEN
from surogates.session.events import EventType
from surogates.session.files import HARNESS_WITHIN_S, gave_up_level, session_files

logger = logging.getLogger(__name__)


def sanitize_sender_name(name: str) -> str:
    """Neutralize an attributed display name for safe inline prefixing.

    Strips control chars/newlines, removes ':' characters that could forge the
    'Name: ' delimiter, collapses internal whitespace, and caps length. Purely a
    function of the input, so replay stays byte-stable.
    """
    cleaned = "".join(ch for ch in name if ch.isprintable())
    cleaned = cleaned.replace(":", " ")
    cleaned = " ".join(cleaned.split())
    return cleaned[:64]


def build_user_message_dict(
    event_data: dict,
    *,
    base_content: str | None = None,
) -> dict:
    """Construct the replayed LLM user message for one ``user.message`` event.

    Folds the per-turn ephemeral context — inlined attachment content,
    view-context note, path-only attachment note, and image vision blocks —
    onto the user's text, exactly as the conversation history is rebuilt.

    ``base_content`` overrides the event's own ``content``.  The slash-skill
    and ``/deep-research`` rewrite paths pass the expanded directive here so
    the skill/delegation body replaces the raw ``/command`` text while the
    attachment binding for *this* turn survives.  Without that, the rewrite
    discarded the note/inlined content and the model bound the request to an
    earlier upload still visible in history instead of the file the user just
    attached.
    """
    content = base_content if base_content is not None else event_data.get("content", "")
    content = _render_inlined_attachments(content, event_data.get("attachments"))
    # Attribute group messages so the model can tell participants apart
    # in a shared thread. Derived entirely from the durable event payload, so
    # the produced bytes are identical on every replay (prefix-cache stable).
    _source = event_data.get("source") or {}
    if _source.get("chat_type") == "group":
        _sender = sanitize_sender_name((_source.get("user_name") or "").strip())
        if _sender:
            content = f"{_sender}: {content}" if content else _sender
    # Fold per-user ephemeral notes (view-context, non-inlined attachments)
    # into the user content here so the bytes are determined entirely by the
    # durable event payload.  This keeps the provider's implicit prefix cache
    # stable across turns -- the previous design inserted the notes mid-array
    # before the latest user message, which left them present in turn T's
    # request but absent in turn T+1's prefix.
    note_parts: list[str] = []
    view_note = _view_context_note_from_metadata(event_data.get("metadata"))
    if view_note:
        note_parts.append(view_note)
    whiteboard_note = _whiteboard_note_from_metadata(event_data.get("metadata"))
    if whiteboard_note:
        note_parts.append(whiteboard_note)
    attachments_note = _attachments_note_from_data(event_data)
    if attachments_note:
        note_parts.append(attachments_note)
    if note_parts:
        notes_block = "\n\n".join(note_parts)
        content = f"{notes_block}\n\n{content}" if content else notes_block

    images = event_data.get("images")
    if images:
        logger.info(
            "User message has %d image(s), first mime: %s",
            len(images),
            images[0].get("mime_type", "?"),
        )
        blocks: list[dict] = [{"type": "text", "text": content}]
        for img in images:
            data_url = img["data"]
            if not data_url.startswith("data:"):
                mime = img.get("mime_type", "image/png")
                data_url = f"data:{mime};base64,{data_url}"
            blocks.append({
                "type": "image_url",
                "image_url": {"url": data_url, "detail": "auto"},
            })
        user_msg = {"role": "user", "content": blocks}
        from surogates.harness.image_shrink import shrink_image_parts_in_messages
        shrink_image_parts_in_messages([user_msg])
        return user_msg
    return {"role": "user", "content": content}


#: The events that carry a worker's report to the session that started it.
WORKER_REPORT_TYPES = frozenset({EventType.WORKER_COMPLETE.value, EventType.WORKER_FAILED.value})
#: What a coordinator hears of its workers between its own requests: their
#: reports, and the threads the user started (``worker.spawned`` with
#: ``started_by``).  A spawn of its own is its own tool call's result.
WORKER_NEWS_TYPES = WORKER_REPORT_TYPES | {EventType.WORKER_SPAWNED.value}


#: What the agent of a chat on its user's computer reads once they hand back
#: the browser they had taken over, when that had stopped it: a browser call
#: of the chat's was answered ``paused_by_user``.  The harness's words, the
#: same for every hand back, so the live loop and replay produce the same
#: bytes; never something the user typed.
BROWSER_HANDED_BACK = (
    "[The user has handed the browser back. The browser tools work again: go on with what you were "
    "doing when they took it over. They may have changed the page meanwhile, so read it again before "
    "you act on it.]"
)
#: The events a session reads as news at its next model request.
NEWS_TYPES = WORKER_NEWS_TYPES | {EventType.BROWSER_CONTROL_RETURNED.value}


#: The lines a thread's own words sit between in its report.  Only the
#: harness writes them: ``_thread_words`` takes them out of the words.
_REPORT_BEGIN = "<<thread report>>"
_REPORT_END = "<<end of thread report>>"
#: Either marker, in any case or spacing a model would still read as one.
_REPORT_MARKER = re.compile(r"<<\s*(?:end\s+of\s+)?thread\s+report\s*>>", re.IGNORECASE)


def _thread_words(text: str) -> str:
    """*text* with every report marker taken out, so a thread's words can
    neither end their block early nor open another report.  Repeated until
    none is left, since taking one out can join the pieces of another."""
    removed = 1
    while removed:
        text, removed = _REPORT_MARKER.subn("", text)
    return text.strip()


#: The most files a report names; the rest are counted.
_MAX_LISTED_FILES = 20
#: The excluded files and the repositories a report names; the rest are counted.
_MAX_LISTED_LEFT_OUT = 10


def _listed(files: list, limit: int = _MAX_LISTED_FILES, total: int | None = None) -> str:
    """A report's ``Files:`` line, at most *limit* names and how many more,
    of *total* when the payload counted more than it names.  An entry with
    neither a label nor a ref is skipped rather than failing the master's
    every wake."""
    labels = [_file_label(f.get("label") or f.get("ref") or "") for f in files if isinstance(f, dict)]
    labels = [label for label in labels if label]
    count = max(len(labels), total if isinstance(total, int) else 0)
    if count > limit:
        labels = [*labels[:limit], f"and {count - limit} more"]
    return ", ".join(labels) or "none"


def _file_label(label: str) -> str:
    """*label* on one line: a file name can hold a line break or another
    control character, and the ``Files:`` line comes after the report's end
    marker, where a second line would read as the harness's."""
    printable = "".join(c if c.isprintable() else " " for c in _thread_words(str(label)))
    return " ".join(printable.split())


#: What a report says of a thread's files that did not land, by its landing's state.
_NOT_LANDED = {
    "compensated": "Not landed, and the project's files are as they were",
    "escalated": "Could not finish landing these; check them",
    "failed": "Not saved, because the landing failed",
}
#: What a report says of a thread's files a landing left out, by why it left them out.
_NOT_MERGED = {
    "changed": "Not merged, because the project's file changed after the thread started (the newer file was kept)",
    "shape": "Not merged, because the project has a folder where the thread made a file, or a file where it made a folder",
    "with": "Not merged, because they go with a change that was not merged (a move lands whole or not at all)",
}


def _landing_lines(data: dict, kept: list, deleted: list) -> str:
    """A thread report's lines on the files it deleted, the files that did
    not land, the excluded files it made, and the folders inside a git
    repository it wrote into."""
    lines = f"\nDeleted: {_listed(deleted)}" if deleted else ""
    if data.get("landing") in _NOT_LANDED:
        # Said even when no file is known: the master must hear the turn did not land.
        named = _listed(kept) if kept else "the turn's files could not be read"
        lines += f"\n{_NOT_LANDED[data['landing']]}: {named}"
    elif kept:
        # A report from before reasons were given says the file changed.
        why: dict[str, list] = {reason: [] for reason in _NOT_MERGED}
        for f in kept:
            why[f.get("reason") if f.get("reason") in _NOT_MERGED else "changed"].append(f)
        lines += "".join(f"\n{_NOT_MERGED[reason]}: {_listed(named)}" for reason, named in why.items() if named)
    for key, words in (
        ("excluded", "Not saved, because the project's history leaves them out"),
        ("repositories", "Not landed, because they are inside a git repository"),
    ):
        named = data.get(key)
        if isinstance(named, list) and named:
            names = [{"label": name} for name in named if isinstance(name, str)]
            lines += f"\n{words}: {_listed(names, limit=_MAX_LISTED_LEFT_OUT, total=data.get(f'{key}_count'))}"
    return lines


def worker_note(event_type: str, data: dict) -> dict:
    """The user-role message a worker's report is read as, built from its
    payload alone, so the live loop and replay produce the same bytes.  A
    project's thread is named by its title, quoted, and lists the files of
    the turn it reports.  Its own words sit between the harness's markers:
    they may quote a document or a web page, and the master reads them as
    the thread's, never as the user's."""
    worker_id = data.get("worker_id", "?")
    title = data.get("title")
    failed = event_type == EventType.WORKER_FAILED.value
    if title is None and failed:
        content = f"[Worker {worker_id} failed: {data.get('error', 'unknown error')}]"
    elif title is None:
        content = f"[Worker {worker_id} completed]\n{data.get('result', '')}"
    else:
        # A title is one line, but it can hold a quote.
        named = f"[Thread {json.dumps(title, ensure_ascii=False)} ({worker_id})"
        if failed:
            content = f"{named} failed: {data.get('error', 'unknown error')}]"
        else:
            files = data.get("files")
            # A thread's files that did not land, and the ones it deleted,
            # are named apart from the ones it made or changed.
            kept, deleted, made = [], [], []
            for f in files if isinstance(files, list) else ():
                landing, change = (f.get("landing"), f.get("change")) if isinstance(f, dict) else (None, None)
                (kept if landing == "not_merged" else deleted if change == "deleted" else made).append(f)
            listed = _listed(made) if isinstance(files, list) else "not listed (the turn ended early)"
            content = (
                f"{named} reported]\n"
                f"{_REPORT_BEGIN}\n{_thread_words(str(data.get('result') or ''))}\n{_REPORT_END}\n"
                f"Files: {listed}"
            ) + _landing_lines(data, kept, deleted)
    return {"role": "user", "content": content}


def worker_news(event_type: str, data: dict) -> dict | None:
    """The message a coordinator reads a worker's news as: its report, or a
    thread the user started; None for a spawn the coordinator made."""
    if event_type != EventType.WORKER_SPAWNED.value:
        return worker_note(event_type, data)
    if data.get("started_by") != "user":
        return None
    title = json.dumps(data.get("title"), ensure_ascii=False)
    return {"role": "user", "content": f"[Thread {title} ({data.get('worker_id', '?')}) started by the user]"}


def news(event) -> dict | None:
    """The message a session reads an event as at its next model request, or
    None for an event that is no news: a worker's news to its coordinator,
    and, in a chat on its user's computer, the hand back of the browser whose
    take-over had stopped its agent."""
    if event.type in WORKER_NEWS_TYPES:
        return worker_news(event.type, event.data)
    if resumes_the_agent(event):
        return {"role": "user", "content": BROWSER_HANDED_BACK}
    return None


def unread_reports(events: list) -> list[dict]:
    """The news no model request has read, worker reports and hand backs of
    the browser alike: those after the log's last ``llm.request``.  Replay
    leaves them out, and the wake adds them right before its first request,
    after its compaction, its command and its board update, which is where
    replay puts them once that request is in the log."""
    held: list[dict] = []
    for event in events:
        if event.type == EventType.LLM_REQUEST.value:
            held = []
        elif (note := news(event)) is not None:
            held.append(note)
    return held


def coalesce_user_messages(messages: list[dict]) -> dict:
    """Merge one or more rendered user-message dicts into a single user turn.

    Both the live boundary injector and the replay re-sequencer pass the
    same rendered messages here so a steered turn looks byte-identical
    whether it was injected live or reconstructed from the event log.

    Text-only messages join with a blank-line separator. If any message
    is multimodal (its ``content`` is a block list), the result is a
    single block list preserving every text and image block in order.
    """
    if len(messages) == 1:
        return messages[0]

    if any(isinstance(m.get("content"), list) for m in messages):
        blocks: list[dict] = []
        for m in messages:
            content = m.get("content")
            if isinstance(content, list):
                blocks.extend(content)
            elif content:
                blocks.append({"type": "text", "text": content})
        return {"role": "user", "content": blocks}

    text = "\n\n".join(m.get("content") or "" for m in messages if m.get("content"))
    return {"role": "user", "content": text}


class ContextReplayMixin:
    async def _prefetch_memory(self, session_id: UUID) -> str | None:
        """Prefetch user memory and snapshot it for the session.

        The first wake() of a session reads memory from disk; every
        subsequent wake() reuses the cached snapshot byte-identically so
        the memory_context message stays in the provider's prefix cache.
        The snapshot is invalidated alongside the system prompt cache
        (compression / context overflow / explicit reset).

        If a MemoryManager is available, delegates to it and wraps the
        result in a ``<memory-context>`` fence.  Otherwise falls back to
        direct file I/O.
        """
        if session_id in self._memory_snapshot_cache:
            return self._memory_snapshot_cache[session_id]

        snapshot = await self._load_memory_snapshot()
        self._memory_snapshot_cache[session_id] = snapshot
        return snapshot

    async def _load_memory_snapshot(self) -> str | None:
        """Read the current memory context from disk (no caching)."""
        # Use memory manager if available.
        if self._memory_manager is not None:
            try:
                raw = self._memory_manager.prefetch_all("")
                if raw and raw.strip():
                    from surogates.memory.manager import build_memory_context_block
                    return build_memory_context_block(raw)
            except Exception:
                logger.debug("Memory manager prefetch failed", exc_info=True)
            return None

        # Fall back to direct file read.
        try:
            memory_dir = self._tenant.asset_root
            if not memory_dir:
                return None
            from pathlib import Path

            # Try user-scoped memory first, fall back to org shared
            for subdir in (
                f"users/{self._tenant.user_id}/memory",
                "shared/memory",
            ):
                memory_path = Path(memory_dir) / subdir / "MEMORY.md"
                if memory_path.is_file():
                    content = memory_path.read_text(encoding="utf-8").strip()
                    if content:
                        logger.debug("Prefetched memory from %s (%d chars)", memory_path, len(content))
                        return content
        except Exception:
            logger.debug("Memory prefetch failed", exc_info=True)
        return None

    def _rebuild_messages(
        self, events: list[Event], workspace_path: str | None = None,
    ) -> list[dict]:
        """Replay event log to reconstruct conversation messages.

        Processes events in order.  A ``CONTEXT_COMPACT`` event replaces
        all previously accumulated messages with the compacted set stored
        in its data payload.

        ``workspace_path`` undoes the event-payload path sanitisation on the
        way back in.  ``execute_tool`` hands the LLM raw paths but stores
        ``_sanitize_paths(...)`` in the ``tool.result`` event so SSE
        consumers never see real filesystem paths (see the comment above
        ``sanitized_content`` in ``tool_exec.py``).  Replaying the stored
        form verbatim would feed the model the very string that comment
        says caused "cascades of broken commands" -- ``__WORKSPACE__``
        treated as a real path -- and would contradict the assistant's own
        ``tool_calls`` arguments, which ``llm.response`` stores raw.  So
        the token is expanded here, making a replayed tool message
        byte-identical to what the live turn returned.

        ``LLM_THINKING`` events are **skipped** during replay -- they are
        informational only and should not re-enter the conversation.

        ``LLM_DELTA`` events are likewise skipped; the full response is
        captured in the subsequent ``LLM_RESPONSE`` event.

        Mid-turn steering: a real ``user.message`` can land in the log
        while an LLM iteration is still open (mid-stream, or while its
        tool calls are running), because the API appends it the instant
        it arrives.  Such a message is deferred to the iteration's close
        and coalesced, so the rebuilt order matches the live
        boundary-injection order and never splits a tool-call / tool
        result pair.  ``tool.result`` events carry no iteration marker,
        so an open tool-calling iteration is closed by tracking the
        ``tool_calls[*].id`` set from its ``llm.response`` until every id
        has a matching result.
        """
        # Exact inverse of ``_sanitize_paths``, which replaces
        # ``workspace_path.rstrip("/")``.
        workspace_root = (workspace_path or "").rstrip("/")
        messages: list[dict] = []
        iteration_open = False
        awaiting_tool_ids: set[str] = set()
        deferred_users: list[dict] = []
        deferred_advisors: list[dict] = []
        # Worker reports wait for the next model request, which is where the
        # wake adds them: after the user's messages, one message each.  The
        # ones no request has read yet are left out (``unread_reports``).
        held_reports: list[dict] = []

        def _flush_deferred() -> None:
            nonlocal deferred_users, deferred_advisors
            if deferred_users:
                messages.append(coalesce_user_messages(deferred_users))
                deferred_users = []
            if deferred_advisors:
                # Kept separate from user coalescing: live turns inject
                # guidance as its own message at an iteration boundary,
                # and replay must produce the same shape.
                messages.extend(deferred_advisors)
                deferred_advisors = []

        for event in events:
            etype = event.type

            if etype == EventType.LLM_REQUEST.value:
                messages.extend(held_reports)
                held_reports = []
                iteration_open = True
                awaiting_tool_ids = set()

            elif etype == EventType.USER_MESSAGE.value:
                rendered = build_user_message_dict(event.data)
                if iteration_open and not (event.data or {}).get("synthetic"):
                    deferred_users.append(rendered)
                else:
                    messages.append(rendered)

            elif etype == EventType.LLM_RESPONSE.value:
                stored_message = event.data.get("message")
                if stored_message is not None:
                    messages.append(stored_message)
                tool_calls = (stored_message or {}).get("tool_calls") or []
                ids = {tc.get("id") for tc in tool_calls if tc.get("id")}
                if ids:
                    awaiting_tool_ids = ids
                else:
                    iteration_open = False
                    awaiting_tool_ids = set()
                    _flush_deferred()

            elif etype == EventType.TOOL_RESULT.value:
                content = event.data.get("content", "")
                if workspace_root and _WORKSPACE_TOKEN in content:
                    content = content.replace(_WORKSPACE_TOKEN, workspace_root)
                messages.append({
                    "role": "tool",
                    "tool_call_id": event.data.get("tool_call_id", ""),
                    "content": content,
                })
                if awaiting_tool_ids:
                    awaiting_tool_ids.discard(event.data.get("tool_call_id"))
                    if not awaiting_tool_ids:
                        iteration_open = False
                        _flush_deferred()

            elif (
                etype == EventType.BOARD_UPDATE.value
                and event.data.get("content")
            ):
                # Board snapshots/deltas re-enter the conversation exactly
                # as emitted: message bytes are determined by the durable
                # event payload, keeping the provider prefix cache
                # replay-stable.
                messages.append({
                    "role": "user",
                    "content": str(event.data["content"]),
                })

            elif etype == EventType.CONTEXT_COMPACT.value:
                compacted = event.data.get("compacted_messages")
                if compacted is not None:
                    messages = list(compacted)
                    # The compacted snapshot already contains any earlier
                    # steered turn in its proper place; drop the buffer and
                    # close the window so it is not re-appended.
                    deferred_users = []
                    iteration_open = False
                    awaiting_tool_ids = set()

            # Worker coordination events — injected as synthetic user
            # messages so the coordinator LLM sees worker results.  A worker
            # reports whenever it finishes, even between the coordinator's
            # tool call and its result, where a user-role message is refused
            # by the provider; and merged into a user's message it would
            # carry the user's words as a report.  So each waits for the
            # next request, on its own.
            elif etype in WORKER_NEWS_TYPES:
                note = worker_news(etype, event.data)
                if note is not None:
                    held_reports.append(note)

            # The hand back of the browser that had stopped the agent is read
            # the same way: at the next request, on its own.  One that
            # stopped nothing, as the take-over itself, is for the pane.
            elif resumes_the_agent(event):
                held_reports.append({"role": "user", "content": BROWSER_HANDED_BACK})

            # A sub-agent's tab on the user's computer is in this log for the
            # chat's pane alone: this session's own tab is as it was.
            elif etype == EventType.BROWSER_DESTROYED.value and not of_a_sub_agent(event):
                # Without this the close is a UI-only event: the model keeps
                # the screenshots and page text it already has, and its next
                # browser call quietly provisions a fresh blank one. Nothing
                # would mark the discontinuity, which is how an agent comes
                # to describe a page it no longer has open.
                note = {
                    "role": "user",
                    "content": (
                        "[The browser was closed. Any page it had open is "
                        "gone — using a browser tool again starts from a "
                        "blank page.]"
                    ),
                }
                # Deferred like a real user message when an iteration is
                # open: a human closes the browser whenever they like,
                # including between an assistant's tool_calls and its
                # results, and a user-role message in that gap is rejected
                # by the provider outright.
                if iteration_open:
                    deferred_users.append(note)
                else:
                    messages.append(note)

            # Task-layer terminal signals.  Same synthetic-user-message
            # shape as the worker events above: without a branch here a
            # coordinator woken by ``notify_parent_of_task_event`` replays
            # its log, finds nothing new, and re-runs its previous turn.
            elif etype == EventType.TASK_BLOCKED.value:
                task_id = event.data.get("task_id", "?")
                reason = event.data.get("reason", "no reason given")
                messages.append({
                    "role": "user",
                    "content": (
                        f"[Task {task_id} is blocked: {reason}] "
                        "It will not run again until it is unblocked."
                    ),
                })

            elif etype == EventType.TASK_FAILED.value:
                task_id = event.data.get("task_id", "?")
                attempts = event.data.get("attempt_count", "?")
                messages.append({
                    "role": "user",
                    "content": (
                        f"[Task {task_id} failed after {attempts} attempts "
                        "and will not be retried]"
                    ),
                })

            # Coding-agent run result — surface the final message to the
            # coordinator LLM so it can follow up on what /code did.  Progress
            # events are UI-only (not replayed); STARTED is bookkeeping.
            elif etype == EventType.CODE_RUN_RESULT.value:
                agent = event.data.get("agent", "coding agent")
                if event.data.get("error"):
                    messages.append({
                        "role": "user",
                        "content": f"[/code {agent} failed: {event.data['error']}]",
                    })
                else:
                    final = event.data.get("final_message", "")
                    messages.append({
                        "role": "user",
                        "content": f"[/code {agent} finished]\n{final}",
                    })

            # LLM_THINKING and LLM_DELTA are intentionally skipped.

        # Flush any users deferred by an iteration that never closed (the
        # log ends mid-tool-execution because this is an in-progress wake).
        _flush_deferred()

        # Strip stale budget warnings from replayed tool results.
        strip_budget_warnings(messages)

        # Repair histories poisoned by a prior identical-call loop:
        # providers reject conversations that repeat the same tool call
        # across consecutive rounds, which would make every resume of
        # such a session fail with the same provider 400.
        return collapse_repeated_tool_rounds(messages)

    # ------------------------------------------------------------------
    # Context engineering
    # ------------------------------------------------------------------

    async def _engineer_context(
        self,
        session: Session,
        events: list[Event],
        messages: list[dict],
    ) -> list[dict]:
        """Apply context compression if needed."""
        system_prompt = self._prompt.build()
        if not self._compressor.should_compress(messages, system_prompt):
            return messages

        pre_compress_text = ""
        if self._memory_manager is not None:
            try:
                pre_compress_text = self._memory_manager.on_pre_compress(messages)
            except Exception:
                logger.debug("Memory on_pre_compress failed at replay compaction", exc_info=True)

        compressed, summary_data = await self._compressor.compress(
            messages, self._llm, pre_compress_guidance=pre_compress_text,
        )

        await self._store.emit_event(
            session.id,
            EventType.CONTEXT_COMPACT,
            {
                **summary_data,
                "compacted_messages": compressed,
            },
        )

        # Invalidate system prompt cache -- conversation shape changed.
        self._system_prompt_cache.invalidate(session.id)
        self._memory_snapshot_cache.pop(session.id, None)
        self._forget_compacted_reads(session)

        return compressed

    # ------------------------------------------------------------------
    # System prompt
    # ------------------------------------------------------------------

    async def _read_folder_context(self, session: Session) -> tuple[bool, str | None]:
        """Whether a local folder's computer answered within ``HARNESS_WITHIN_S``, and its AGENTS.md (or CLAUDE.md, …) if it has one."""
        try:
            async with asyncio.timeout(HARNESS_WITHIN_S), session_files(
                session, storage=self._storage, session_factory=self._session_factory, redis=self._redis,
            ) as files:
                return True, await load_folder_context(files)
        except Exception as exc:
            logger.log(
                gave_up_level(exc), "Session %s: its folder's project context could not be read", session.id,
                exc_info=True,
            )
            return False, None

    async def _build_system_prompt(self, session: Session) -> str:
        """Delegate to PromptBuilder, with per-session caching."""
        cached = self._system_prompt_cache.get(session.id)
        if cached is not None:
            return cached

        # A prompt built without what its computer could not say is not kept:
        # the next wake asks again.
        answered = True
        if device_of(session.config) is not None:
            answered, self._prompt.folder_context = await self._read_folder_context(session)
        prompt = self._prompt.build()
        # Tell the agent which GitHub repos it can act on (so it doesn't guess
        # names) and to link the issues/commits/PRs it mentions.
        from surogates.coding_agents.repo_resolve import render_repos_prompt

        repos_section = render_repos_prompt(self._coding_repos)
        if repos_section:
            prompt = f"{prompt}\n\n{repos_section}"
        # Tell the agent which SSH hosts its terminal is authenticated for.
        from surogates.ssh_access.resolve import render_ssh_targets_prompt

        ssh_section = render_ssh_targets_prompt(self._ssh_targets)
        if ssh_section:
            prompt = f"{prompt}\n\n{ssh_section}"
        # Session-level instruction from ``POST /v1/sessions {"system": ...}``.
        # Appended, never substituted: it narrows behaviour for one session,
        # while the agent's own prompt carries the tool contract the harness
        # depends on.  Ignored unless it is a non-blank string — ``config`` is
        # caller-supplied JSON and a wrong type must not break the wake.
        override = (session.config or {}).get("system")
        if isinstance(override, str) and override.strip():
            prompt = (
                f"{prompt}\n\n## Session instructions\n\n{override.strip()}"
            )
        if answered:
            self._system_prompt_cache.set(session.id, prompt)
        return prompt


_PRUNED_CANVAS_PLACEHOLDER = (
    "[Earlier canvas snapshot pruned — it is fully contained in the "
    "current one.]"
)


def prune_superseded_canvas_images(
    messages: list[dict],
) -> list[dict]:
    """Keep only the newest canvas image in a whiteboard replay.

    Canvas snapshots are cumulative: snapshot N renders everything
    snapshot N-1 did plus whatever has been added since. Replaying all of
    them is pure waste, and on a long board it would dominate the context
    window within a dozen turns.

    Same shape as ``ContextManager.prune_stale_browser_states``, with
    ``keep_last`` fixed at 1 -- unlike browser state there is no case for
    holding two, because the older one is a strict subset.

    Only images in a message that carries the canvas note are eligible.
    The board is a view mode rather than a session type, so a session
    that drew can also hold screenshots the user uploaded -- and those
    are not superseded by anything. Matching every image instead would
    silently replace the user's own attachments with a placeholder.

    Returns a new list; the input is never mutated.
    """
    from surogates.whiteboard.session import CANVAS_NOTE_HEADER

    def _is_canvas_message(message: dict) -> bool:
        return any(
            isinstance(part, dict)
            and part.get("type") == "text"
            and CANVAS_NOTE_HEADER in str(part.get("text") or "")
            for part in message["content"]
        )

    image_positions = [
        (m_idx, p_idx)
        for m_idx, message in enumerate(messages)
        if isinstance(message.get("content"), list)
        and _is_canvas_message(message)
        for p_idx, part in enumerate(message["content"])
        if isinstance(part, dict) and part.get("type") == "image_url"
    ]
    if len(image_positions) <= 1:
        return messages

    # Superseded by *message*, not by image part: a canvas turn carries
    # the overview plus close-ups of the new ink, and keeping only the
    # last part would drop the overview of the very turn being answered
    # while keeping its crop.
    newest_message = image_positions[-1][0]
    superseded = {pos for pos in image_positions if pos[0] != newest_message}
    if not superseded:
        return messages
    pruned: list[dict] = []
    for m_idx, message in enumerate(messages):
        content = message.get("content")
        if not isinstance(content, list):
            pruned.append(message)
            continue
        pruned.append({
            **message,
            "content": [
                {"type": "text", "text": _PRUNED_CANVAS_PLACEHOLDER}
                if (m_idx, p_idx) in superseded
                else part
                for p_idx, part in enumerate(content)
            ],
        })
    return pruned
