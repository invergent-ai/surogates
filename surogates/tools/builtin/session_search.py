"""Builtin session search tool -- long-term conversation recall.

Searches past session transcripts via PostgreSQL full-text search, then
summarizes the top matching sessions using a cheap/fast auxiliary model.
Returns focused summaries of past conversations rather than raw transcripts,
keeping the main model's context window clean.

Flow:
  1. Full-text search finds matching events ranked by relevance
  2. Groups by session, takes the top N unique sessions (default 3)
  3. Loads each session's conversation, truncates to ~100k chars centered on matches
  4. Sends to an auxiliary LLM with a focused summarization prompt
  5. Returns per-session summaries with metadata

Registers the ``session_search`` tool with the tool registry.  The
handler queries the session store for matching content across the
user's sessions, using the ``session_store`` and ``tenant`` from kwargs.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from shlex import split as shlex_split
from typing import Any, Dict, List, Optional, Union
from uuid import UUID

from surogates.tools.owner_scope import is_owner_scoped as _is_owner_scoped
from surogates.db.narrative import (
    narrative_tsquery_sql,
    narrative_tsvector_sql,
    narrative_type_list_sql,
    narrative_types_for_roles,
)
from surogates.session.events import EventType
from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_SESSION_CHARS = 100_000
MAX_SUMMARY_TOKENS = 10000

# Sources excluded from session browsing/searching by default.
# Third-party integrations tag their sessions with specific channels
# so they don't clutter the user's session history.
_HIDDEN_SESSION_CHANNELS = ("tool",)


# ---------------------------------------------------------------------------
# Timestamp formatting
# ---------------------------------------------------------------------------


def _format_timestamp(ts: Union[int, float, str, datetime, None]) -> str:
    """Convert a Unix timestamp, ISO string, or datetime to a human-readable date.

    Returns "unknown" for None, str(ts) if conversion fails.
    """
    if ts is None:
        return "unknown"
    try:
        if isinstance(ts, datetime):
            return ts.strftime("%B %d, %Y at %I:%M %p")
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(ts)
            return dt.strftime("%B %d, %Y at %I:%M %p")
        if isinstance(ts, str):
            if ts.replace(".", "").replace("-", "").isdigit():
                dt = datetime.fromtimestamp(float(ts))
                return dt.strftime("%B %d, %Y at %I:%M %p")
            return ts
    except (ValueError, OSError, OverflowError) as e:
        # Log specific errors for debugging while gracefully handling edge cases
        logger.debug("Failed to format timestamp %s: %s", ts, e, exc_info=True)
    except Exception as e:
        logger.debug("Unexpected error formatting timestamp %s: %s", ts, e, exc_info=True)
    return str(ts)


# ---------------------------------------------------------------------------
# Conversation formatting
# ---------------------------------------------------------------------------


def _format_conversation(events: List[Dict[str, Any]]) -> str:
    """Format session events into a readable transcript for summarization.

    Expects a list of event dicts with ``type`` and ``data`` keys, as
    returned by the session store's event queries.
    """
    parts: list[str] = []
    for event in events:
        event_type = event.get("type", "")
        data = event.get("data") or {}

        if event_type == "user.message":
            content = data.get("content", "")
            parts.append(f"[USER]: {content}")

        elif event_type == "llm.response":
            # Assistant text is nested under ``message`` on every emit path;
            # the flat read this used to do returned "" for every turn, so
            # the transcript handed to the summarizer contained user messages
            # and bare tool names and no agent replies at all.
            message = data.get("message")
            if isinstance(message, dict):
                content = message.get("content") or ""
            else:
                content = data.get("content") or ""
            tool_calls = data.get("tool_calls")
            if not tool_calls and isinstance(message, dict):
                tool_calls = message.get("tool_calls")
            if tool_calls and isinstance(tool_calls, list):
                tc_names = []
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        name = tc.get("name") or tc.get("function", {}).get("name", "?")
                        tc_names.append(name)
                if tc_names:
                    parts.append(f"[ASSISTANT]: [Called: {', '.join(tc_names)}]")
                if content:
                    parts.append(f"[ASSISTANT]: {content}")
            else:
                if content:
                    parts.append(f"[ASSISTANT]: {content}")

        elif event_type == "tool.call":
            tool_name = data.get("name", "unknown")
            parts.append(f"[TOOL_CALL:{tool_name}]")

        elif event_type == "tool.result":
            tool_name = data.get("name", "unknown")
            # Result payloads carry ``content``; the ``result`` key this used
            # to read has never been written, so every tool output rendered
            # as an empty line.
            content = str(data.get("content") or "")
            # Truncate long tool outputs
            if len(content) > 500:
                content = content[:250] + "\n...[truncated]...\n" + content[-250:]
            parts.append(f"[TOOL:{tool_name}]: {content}")

        elif event_type in ("turn.summary", "iteration.summary"):
            recap = data.get("recap") or data.get("summary") or ""
            if recap:
                parts.append(f"[RECAP]: {recap}")

        elif event_type in ("llm.thinking", "llm.delta"):
            # Thinking is not conversation.  Deltas are the same assistant
            # text as ``llm.response``, but persisted one row per streamed
            # chunk — they carry a ``content`` key, so without this branch
            # they fall through to the generic case and re-emit every reply
            # as hundreds of per-token lines.  On a busy session that is the
            # majority of the transcript, and since the 100k-char window is
            # centred on the first match, the real conversation is what gets
            # truncated away.  ``surogates.db.narrative`` excludes them from
            # the search index for the same reason.
            pass

        else:
            # Include other event types generically.  Both keys are dicts on
            # LLM-shaped payloads, so render either only when it is plain
            # text — otherwise the transcript grows a Python repr of the
            # whole object and the summarizer reads that as conversation.
            raw = data.get("content")
            if not isinstance(raw, str):
                candidate = data.get("message")
                raw = candidate if isinstance(candidate, str) else None
            if raw:
                parts.append(f"[{event_type.upper()}]: {raw}")

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Truncation around matches
# ---------------------------------------------------------------------------


def _centering_terms(query: str) -> list[str]:
    """The literal words to look for when centring the truncation window.

    The query is ``websearch_to_tsquery`` syntax, so a plain ``.split()``
    yields tokens that never appear in the transcript — the bare operator
    ``OR``, the quote characters of a phrase, and the leading ``-`` of an
    exclusion. Each of those either matches nothing (so the window falls
    back to the start of the conversation and the summarizer is asked about
    text it was not given) or, worse, matches incidentally: ``or`` occurs
    inside "work order" within the first few hundred characters of almost
    any transcript, which pins the window to the beginning every time.

    So: drop the operator, unwrap phrases into their words, and discard
    negated terms — a term the caller excluded is the last thing the window
    should centre on.
    """
    try:
        tokens = shlex_split(query)
    except ValueError:
        # Unbalanced quote — the tsquery parser tolerates it, so neither
        # should this. Fall back to whitespace splitting with the quote
        # characters stripped.
        tokens = query.replace('"', " ").split()
    terms: list[str] = []
    for token in tokens:
        if not token or token.upper() == "OR" or token.startswith("-"):
            continue
        terms.extend(word for word in token.lower().split() if word)
    return terms


def _truncate_around_matches(
    full_text: str, query: str, max_chars: int = MAX_SESSION_CHARS
) -> str:
    """Truncate a conversation transcript to max_chars, centered around
    where the query terms appear. Keeps content near matches, trims the edges.
    """
    if len(full_text) <= max_chars:
        return full_text

    query_terms = _centering_terms(query)
    text_lower = full_text.lower()
    first_match = len(full_text)
    for term in query_terms:
        pos = text_lower.find(term)
        if pos != -1 and pos < first_match:
            first_match = pos

    if first_match == len(full_text):
        # No match found, take from the start
        first_match = 0

    # Center the window around the first match
    half = max_chars // 2
    start = max(0, first_match - half)
    end = min(len(full_text), start + max_chars)
    if end - start < max_chars:
        start = max(0, end - max_chars)

    truncated = full_text[start:end]
    prefix = "...[earlier conversation truncated]...\n\n" if start > 0 else ""
    suffix = "\n\n...[later conversation truncated]..." if end < len(full_text) else ""
    return prefix + truncated + suffix


# ---------------------------------------------------------------------------
# Auxiliary LLM summarization
# ---------------------------------------------------------------------------


async def _summarize_session(
    conversation_text: str,
    query: str,
    session_meta: Dict[str, Any],
    auxiliary_fn: Any | None = None,
) -> Optional[str]:
    """Summarize a single session conversation focused on the search query.

    Uses an auxiliary LLM function if provided, otherwise returns None
    to fall back to raw preview mode.
    """
    if auxiliary_fn is None:
        return None

    system_prompt = (
        "You are reviewing a past conversation transcript to help recall what happened. "
        "Summarize the conversation with a focus on the search topic. Include:\n"
        "1. What the user asked about or wanted to accomplish\n"
        "2. What actions were taken and what the outcomes were\n"
        "3. Key decisions, solutions found, or conclusions reached\n"
        "4. Any specific commands, files, URLs, or technical details that were important\n"
        "5. Anything left unresolved or notable\n\n"
        "Be thorough but concise. Preserve specific details (commands, paths, error messages) "
        "that would be useful to recall. Write in past tense as a factual recap."
    )

    channel = session_meta.get("channel", "unknown")
    started = _format_timestamp(session_meta.get("created_at"))

    user_prompt = (
        f"Search topic: {query}\n"
        f"Session channel: {channel}\n"
        f"Session date: {started}\n\n"
        f"CONVERSATION TRANSCRIPT:\n{conversation_text}\n\n"
        f"Summarize this conversation with focus on: {query}"
    )

    max_retries = 3
    for attempt in range(max_retries):
        try:
            content = await auxiliary_fn(
                task="session_search",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
                max_tokens=MAX_SUMMARY_TOKENS,
            )
            if content:
                return content
            # Empty response -- let the retry loop handle it
            logger.warning(
                "Session search LLM returned empty content (attempt %d/%d)",
                attempt + 1, max_retries,
            )
            if attempt < max_retries - 1:
                await asyncio.sleep(1 * (attempt + 1))
                continue
            return content
        except RuntimeError:
            logger.warning("No auxiliary model available for session summarization")
            return None
        except Exception as e:
            if attempt < max_retries - 1:
                await asyncio.sleep(1 * (attempt + 1))
            else:
                logger.warning(
                    "Session summarization failed after %d attempts: %s",
                    max_retries,
                    e,
                    exc_info=True,
                )
                return None


# ---------------------------------------------------------------------------
# Recent sessions mode (no LLM calls)
# ---------------------------------------------------------------------------


async def _list_recent_sessions(
    session_store: Any,
    org_id: UUID,
    user_id: UUID | None,
    agent_id: str,
    limit: int,
    current_session_id: UUID | None = None,
    service_account_id: UUID | None = None,
    owner_scope: bool = False,
) -> str:
    """Return metadata for the most recent sessions (no LLM calls)."""
    try:
        sessions = await session_store.list_sessions(
            org_id=org_id,
            user_id=user_id,
            agent_id=agent_id,
            service_account_id=service_account_id,
            limit=limit + 5,  # fetch extra to skip current
            any_principal=owner_scope,
        )

        # Resolve current session lineage to exclude it -- the walked root
        # is what appears in the roots-only listing, not the child itself.
        current_root: UUID | None = None
        if current_session_id:
            try:
                root = current_session_id
                visited: set[UUID] = set()
                while root not in visited:
                    visited.add(root)
                    s = await session_store.get_session(root)
                    parent = s.parent_id if s else None
                    if not parent:
                        break
                    root = parent
                current_root = root
            except Exception:
                current_root = current_session_id

        results: list[dict[str, Any]] = []
        for s in sessions:
            sid = s.id
            if current_root and (sid == current_root or sid == current_session_id):
                continue
            # Skip child/delegation sessions (they have parent_id)
            if s.parent_id:
                continue
            # Skip hidden channels
            if s.channel in _HIDDEN_SESSION_CHANNELS:
                continue

            # Build a preview from the first user message event
            preview = ""
            try:
                events = await session_store.get_events(
                    sid, types=[EventType.USER_MESSAGE], limit=1,
                )
                if events:
                    preview = (events[0].data.get("content", "") or "")[:200]
            except Exception:
                pass

            entry = {
                "session_id": str(sid),
                "title": s.title or None,
                "channel": s.channel,
                "started_at": _format_timestamp(s.created_at),
                "last_active": _format_timestamp(s.updated_at),
                "message_count": s.message_count,
                "preview": preview,
            }
            _attach_session_user(entry, s.user_id, owner_scope)
            results.append(entry)
            if len(results) >= limit:
                break

        return json.dumps({
            "success": True,
            "mode": "recent",
            "results": results,
            "count": len(results),
            "message": f"Showing {len(results)} most recent sessions. Use a keyword query to search specific topics.",
        }, ensure_ascii=False)
    except Exception as e:
        logger.error("Error listing recent sessions: %s", e, exc_info=True)
        return json.dumps({
            "success": False,
            "error": f"Failed to list recent sessions: {e}",
        })


# ---------------------------------------------------------------------------
# Main search function
# ---------------------------------------------------------------------------


def _attach_session_user(
    entry: dict[str, Any],
    user_id: Any,
    owner_scope: bool,
) -> None:
    """Under owner scope, stamp the session's owning user onto a result
    entry so the console can tell principals apart. ``None`` means a
    principal-less or service-account-owned session."""
    if owner_scope:
        entry["user_id"] = str(user_id) if user_id else None


# Owner scope widens search from "the caller's own sessions" to "every
# principal's sessions for this org + agent" — mirroring the ops
# Sessions Explorer, where ``GET /api/sessions`` defaults to
# ``scope="all"`` for managed agents but forces ``scope="mine"`` on
# system agents (copilot chats are private per user).  The predicate
# itself is shared with other operator-only tools — imported at the
# top of this module from tools/owner_scope.py as ``_is_owner_scoped``.


async def session_search(
    query: str,
    role_filter: str | None = None,
    limit: int = 3,
    session_store: Any = None,
    org_id: UUID | None = None,
    user_id: UUID | None = None,
    service_account_id: UUID | None = None,
    agent_id: str = "",
    current_session_id: UUID | None = None,
    auxiliary_fn: Any | None = None,
    owner_scope: bool = False,
) -> str:
    """Search past sessions and return focused summaries of matching conversations.

    Uses PostgreSQL full-text search to find matches, then summarizes the
    top sessions with an auxiliary LLM.  The current session is excluded
    from results since the agent already has that context.

    Args:
        query: Search terms in websearch syntax: bare words are ANDed,
            OR between alternatives, "quoted phrases" for a sequence,
            and a leading - to exclude. No stemming, no prefix wildcard.
        role_filter: Comma-separated roles to restrict search to.
        limit: Max sessions to summarize (capped at 5).
        session_store: The Surogates SessionStore instance.
        org_id: Organization UUID for the authenticated user.
        user_id: User UUID for the authenticated user.
        service_account_id: Service-account UUID for API sessions.
        current_session_id: The current session UUID to exclude.
        auxiliary_fn: Optional async callable for LLM summarization.
    """
    if session_store is None:
        return json.dumps({
            "success": False,
            "error": "Session store not available.",
        })

    if org_id is None or (user_id is None and service_account_id is None):
        return json.dumps({
            "success": False,
            "error": "Tenant context (org_id/principal_id) not available.",
        })

    # Search stays within the current session's agent — cross-agent history is
    # not addressable.
    if not agent_id:
        return json.dumps({
            "success": False,
            "error": "agent_id is required to scope search to this agent.",
        })

    limit = min(limit, 5)  # Cap at 5 sessions to avoid excessive LLM calls

    # Recent sessions mode: when query is empty, return metadata for recent sessions.
    # No LLM calls -- just DB queries for titles, previews, timestamps.
    if not query or not query.strip():
        return await _list_recent_sessions(
            session_store,
            org_id,
            user_id,
            agent_id,
            limit,
            current_session_id,
            service_account_id=service_account_id,
            owner_scope=owner_scope,
        )

    query = query.strip()

    try:
        # Narrow the searched event types to the caller's roles.  The type
        # predicate is not optional decoration: it is what keeps streamed
        # ``llm.delta`` chunks out of the results AND what lets the partial
        # GIN index apply.
        try:
            type_filter = narrative_types_for_roles(
                role_filter.split(",") if role_filter else None
            )
        except ValueError as exc:
            return json.dumps({"success": False, "error": str(exc)})

        # Full-text search across events for this org's sessions.
        from sqlalchemy import text as sa_text

        raw_results: list[dict[str, Any]] = []
        try:
            async with session_store._sf() as db:
                params: dict[str, Any] = {
                    "query": query,
                    "org_id": org_id,
                    "agent_id": agent_id,
                    "limit": 50,  # Get more matches to find unique sessions
                }
                # Owner scope (ops console sessions only -- see
                # _is_owner_scoped) drops the principal predicate so the
                # search spans every user's sessions for this org + agent.
                if owner_scope:
                    principal_clause = ""
                elif service_account_id is not None:
                    principal_clause = "AND s.service_account_id = :principal_id"
                    params["principal_id"] = service_account_id
                else:
                    principal_clause = "AND s.user_id = :principal_id"
                    params["principal_id"] = user_id
                hidden_clause = "AND s.channel NOT IN ({})".format(
                    ", ".join(f"'{c}'" for c in _HIDDEN_SESSION_CHANNELS)
                )
                type_clause = "AND e.type IN ({})".format(
                    narrative_type_list_sql(type_filter)
                )
                # Rendered from surogates.db.narrative so they stay identical
                # to the backing GIN index — see that module for why drift is
                # silent.
                tsvector_sql = narrative_tsvector_sql("e.")
                tsquery_sql = narrative_tsquery_sql("query")
                fts_query = sa_text(
                    f"""
                    SELECT e.id AS event_id,
                           e.session_id,
                           e.type,
                           e.data,
                           e.created_at AS event_created_at,
                           s.created_at AS session_started,
                           s.channel,
                           s.model,
                           s.title,
                           s.parent_id,
                           s.user_id,
                           ts_rank({tsvector_sql}, {tsquery_sql}) AS rank
                    FROM events e
                    JOIN sessions s ON s.id = e.session_id
                    WHERE s.org_id = :org_id
                      {principal_clause}
                      {hidden_clause}
                      {type_clause}
                      AND s.agent_id = :agent_id
                      AND s.status != 'archived'
                      AND {tsvector_sql} @@ {tsquery_sql}
                    ORDER BY rank DESC
                    LIMIT :limit
                    """
                )

                result = await db.execute(fts_query, params)
                rows = result.mappings().all()

                for row in rows:
                    raw_results.append({
                        "event_id": row["event_id"],
                        "session_id": row["session_id"],
                        "type": row["type"],
                        "data": row["data"],
                        "event_created_at": row["event_created_at"],
                        "session_started": row["session_started"],
                        "channel": row["channel"],
                        "model": row["model"],
                        "title": row["title"],
                        "parent_id": row["parent_id"],
                        "user_id": row["user_id"],
                        "rank": row["rank"],
                    })
        except Exception as e:
            logger.error("FTS query failed: %s", e, exc_info=True)
            return json.dumps({
                "success": False,
                "error": f"Search query failed: {e}",
            })

        if not raw_results:
            return json.dumps({
                "success": True,
                "query": query,
                "results": [],
                "count": 0,
                "message": "No matching sessions found.",
            }, ensure_ascii=False)

        # Resolve child sessions to their parent -- delegation stores detailed
        # content in child sessions, but the user's conversation is the parent.
        async def _resolve_to_parent(session_id: UUID) -> UUID:
            """Walk delegation chain to find the root parent session ID."""
            visited: set[UUID] = set()
            sid = session_id
            while sid and sid not in visited:
                visited.add(sid)
                try:
                    s = await session_store.get_session(sid)
                    if not s:
                        break
                    parent = s.parent_id
                    if parent:
                        sid = parent
                    else:
                        break
                except Exception as e:
                    logger.debug(
                        "Error resolving parent for session %s: %s",
                        sid,
                        e,
                        exc_info=True,
                    )
                    break
            return sid

        current_lineage_root = (
            await _resolve_to_parent(current_session_id)
            if current_session_id
            else None
        )

        # Group by resolved (parent) session_id, dedup, skip the current
        # session lineage. Compression and delegation create child sessions
        # that still belong to the same active conversation.
        seen_sessions: dict[UUID, dict[str, Any]] = {}
        for result_row in raw_results:
            raw_sid = result_row["session_id"]
            resolved_sid = await _resolve_to_parent(raw_sid)
            # Skip the current session lineage -- the agent already has that
            # context, even if older turns live in parent fragments.
            if current_lineage_root and resolved_sid == current_lineage_root:
                continue
            if current_session_id and raw_sid == current_session_id:
                continue
            if resolved_sid not in seen_sessions:
                entry = dict(result_row)
                entry["session_id"] = resolved_sid
                seen_sessions[resolved_sid] = entry
            if len(seen_sessions) >= limit:
                break

        # Prepare all sessions for parallel summarization
        tasks: list[tuple[UUID, dict[str, Any], str, dict[str, Any]]] = []
        for sid, match_info in seen_sessions.items():
            try:
                # Deltas are ~90% of the log and carry no text the transcript
                # does not already get from ``llm.response``; excluding them
                # in SQL keeps a long session from being loaded into memory
                # almost entirely as token fragments.

                events = await session_store.get_events(
                    sid, exclude_types=[EventType.LLM_DELTA],
                )
                if not events:
                    continue
                # Convert Event pydantic models to dicts for formatting
                event_dicts = [
                    {"type": e.type, "data": e.data, "created_at": e.created_at}
                    for e in events
                ]
                session_obj = await session_store.get_session(sid)
                session_meta: dict[str, Any] = {}
                if session_obj:
                    session_meta = {
                        "channel": session_obj.channel,
                        "created_at": session_obj.created_at,
                        "title": session_obj.title,
                        "model": session_obj.model,
                    }
                conversation_text = _format_conversation(event_dicts)
                conversation_text = _truncate_around_matches(conversation_text, query)
                tasks.append((sid, match_info, conversation_text, session_meta))
            except Exception as e:
                logger.warning(
                    "Failed to prepare session %s: %s",
                    sid,
                    e,
                    exc_info=True,
                )

        # Summarize all sessions in parallel
        async def _summarize_all() -> list[str | Exception | None]:
            """Summarize all sessions in parallel."""
            coros = [
                _summarize_session(text, query, meta, auxiliary_fn)
                for _, _, text, meta in tasks
            ]
            return await asyncio.gather(*coros, return_exceptions=True)

        try:
            results = await asyncio.wait_for(_summarize_all(), timeout=60.0)
        except asyncio.TimeoutError:
            logger.warning(
                "Session summarization timed out after 60 seconds",
                exc_info=True,
            )
            return json.dumps({
                "success": False,
                "error": "Session summarization timed out. Try a more specific query or reduce the limit.",
            }, ensure_ascii=False)

        summaries: list[dict[str, Any]] = []
        for (sid, match_info, conversation_text, _), result_val in zip(tasks, results):
            if isinstance(result_val, Exception):
                logger.warning(
                    "Failed to summarize session %s: %s",
                    sid, result_val, exc_info=True,
                )
                result_val = None

            entry: dict[str, Any] = {
                "session_id": str(sid),
                "when": _format_timestamp(match_info.get("session_started")),
                "channel": match_info.get("channel", "unknown"),
                "model": match_info.get("model"),
                "title": match_info.get("title"),
            }
            _attach_session_user(entry, match_info.get("user_id"), owner_scope)

            if result_val:
                entry["summary"] = result_val
            else:
                # Fallback: raw preview so matched sessions aren't silently
                # dropped when the summarizer is unavailable.
                preview = (
                    (conversation_text[:500] + "\n...[truncated]")
                    if conversation_text
                    else "No preview available."
                )
                entry["summary"] = f"[Raw preview -- summarization unavailable]\n{preview}"

            summaries.append(entry)

        return json.dumps({
            "success": True,
            "query": query,
            "results": summaries,
            "count": len(summaries),
            "sessions_searched": len(seen_sessions),
        }, ensure_ascii=False)

    except Exception as e:
        logger.error("Session search failed: %s", e, exc_info=True)
        return json.dumps({
            "success": False,
            "error": f"Search failed: {str(e)}",
        })


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------

SESSION_SEARCH_SCHEMA = ToolSchema(
    name="session_search",
    description=(
        "Search your long-term memory of past conversations, or browse recent sessions. This is your recall -- "
        "every past session is searchable, and this tool summarizes what happened.\n\n"
        "TWO MODES:\n"
        "1. Recent sessions (no query): Call with no arguments to see what was worked on recently. "
        "Returns titles, previews, and timestamps. Zero LLM cost, instant. "
        "Start here when the user asks what were we working on or what did we do recently.\n"
        "2. Keyword search (with query): Search for specific topics across all past sessions. "
        "Returns LLM-generated summaries of matching sessions.\n\n"
        "USE THIS PROACTIVELY when:\n"
        "- The user says 'we did this before', 'remember when', 'last time', 'as I mentioned'\n"
        "- The user asks about a topic you worked on before but don't have in current context\n"
        "- The user references a project, person, or concept that seems familiar but isn't in memory\n"
        "- You want to check if you've solved a similar problem before\n"
        "- The user asks 'what did we do about X?' or 'how did we fix Y?'\n\n"
        "Don't hesitate to search when it is actually cross-session -- it's fast and cheap. "
        "Better to search and confirm than to guess or ask the user to repeat themselves.\n\n"
        "Search syntax: keywords joined with OR for broad recall (elevenlabs OR baseten OR funding), "
        "quoted phrases for an exact sequence (\"docker networking\"), and a leading minus to exclude "
        "a term (python -java). Bare keywords are ANDed, so use OR when a session only needs to "
        "mention some of your terms.\n\n"
        "Matching is exact-token and case-insensitive, with no stemming or wildcards: 'deploy' will "
        "not find 'deployed' and 'deploy*' is not a prefix search. OR the word forms you care about "
        "(deploy OR deployed OR deployment). This is deliberate — one shared index serves every "
        "language the platform runs in, so no language's suffix rules are applied to another's.\n\n"
        "Searches user messages, assistant replies, tool results, and per-turn recaps. "
        "Returns summaries of the top matching sessions.\n\n"
        "In an ops console session, results span EVERY user of this agent and each result carries "
        "a user_id — attribute those conversations to that user, never to yourself or the person "
        "you are talking to."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Search query — keywords, phrases, or boolean expressions to find in past sessions. "
                    "Omit this parameter entirely to browse recent sessions instead "
                    "(returns titles, previews, timestamps with no LLM cost)."
                ),
            },
            "role_filter": {
                "type": "string",
                "description": (
                    "Optional: restrict the search to specific roles (comma-separated). "
                    "One or more of 'user', 'assistant', 'tool', 'summary'. "
                    "E.g. 'user,assistant' to skip tool outputs, or 'summary' to search "
                    "only the per-turn recaps. Defaults to all four."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Max sessions to summarize (default: 3, max: 5).",
                "default": 3,
            },
        },
        "required": [],
    },
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(registry: ToolRegistry) -> None:
    """Register the session_search tool."""
    registry.register(
        name="session_search",
        schema=SESSION_SEARCH_SCHEMA,
        handler=_session_search_handler,
        toolset="session_search",
    )


async def _session_search_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Handle session_search tool calls.

    Extracts ``session_store``, ``tenant`` (with org_id and principal),
    ``session_id``, and optional ``auxiliary_fn`` from kwargs, then
    delegates to the main :func:`session_search` function.
    """
    store = kwargs.get("session_store")
    if store is None:
        return json.dumps({
            "success": False,
            "error": "Session store not available.",
        })

    tenant = kwargs.get("tenant", {})
    org_id = tenant.get("org_id") if isinstance(tenant, dict) else getattr(tenant, "org_id", None)
    user_id = tenant.get("user_id") if isinstance(tenant, dict) else getattr(tenant, "user_id", None)
    service_account_id = (
        tenant.get("service_account_id")
        if isinstance(tenant, dict)
        else getattr(tenant, "service_account_id", None)
    )
    agent_id = kwargs.get("agent_id", "")
    current_session_id = kwargs.get("session_id")
    auxiliary_fn = kwargs.get("auxiliary_fn")

    # Coerce string UUIDs to UUID objects
    if isinstance(org_id, str):
        org_id = UUID(org_id)
    if isinstance(user_id, str):
        user_id = UUID(user_id)
    if isinstance(service_account_id, str):
        service_account_id = UUID(service_account_id)
    if isinstance(current_session_id, str):
        current_session_id = UUID(current_session_id)

    owner_scope = await _is_owner_scoped(
        store, service_account_id, kwargs.get("session_config"),
    )

    return await session_search(
        query=arguments.get("query", ""),
        role_filter=arguments.get("role_filter"),
        limit=arguments.get("limit", 3),
        session_store=store,
        org_id=org_id,
        user_id=user_id,
        service_account_id=service_account_id,
        agent_id=agent_id,
        current_session_id=current_session_id,
        auxiliary_fn=auxiliary_fn,
        owner_scope=owner_scope,
    )
