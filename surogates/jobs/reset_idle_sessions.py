"""Idle session reset job with LLM-powered memory flush.

Detects sessions that have been inactive beyond the configured threshold,
runs a temporary LLM agent to review the conversation transcript and save
important facts to memory, then tears down the sandbox pod.  The session
itself (events, counters, cursor) is left untouched — the user can come
back and continue at any time.

Runs as a K8s CronJob (every 5 minutes recommended) or via CLI::

    python -m surogates.jobs.reset_idle_sessions
    python -m surogates.jobs.reset_idle_sessions --dry-run

The job also copies memory files to durable TenantStorage (S3/Garage)
after the flush agent finishes, so memories survive PV loss.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from surogates.config import INTERRUPT_CHANNEL_PREFIX, Settings, load_settings
from surogates.db.engine import async_engine_from_settings, async_session_factory
from surogates.memory.manager import MemoryManager
from surogates.memory.store import MemoryStore
from surogates.session.events import EventType
from surogates.session.models import Event, Session
from surogates.session.store import SessionStore
from surogates.storage.backend import create_backend
from surogates.storage.tenant import TenantStorage

logger = logging.getLogger(__name__)

# Minimum number of user+assistant messages in a transcript before a
# flush is worthwhile.  Hermes uses 4 (2 turns).
_MIN_TRANSCRIPT_MESSAGES = 4

# Maximum number of LLM iterations the flush agent is allowed.
_DEFAULT_FLUSH_MAX_ITERATIONS = 8


# ---------------------------------------------------------------------------
# Transcript extraction
# ---------------------------------------------------------------------------


def extract_transcript(events: list[Event]) -> list[dict[str, str]]:
    """Build a simplified user/assistant transcript from the event log.

    Extracts only user messages and assistant text content — tool calls,
    tool results, thinking, and deltas are excluded.  This matches the
    Hermes ``load_transcript`` format that the flush agent expects.

    A ``CONTEXT_COMPACT`` event replaces all prior messages with the
    compacted set, just like the harness replay logic.
    """
    messages: list[dict[str, str]] = []

    for event in events:
        etype = event.type

        if etype == EventType.USER_MESSAGE.value:
            content = event.data.get("content", "")
            if content:
                messages.append({"role": "user", "content": content})

        elif etype == EventType.LLM_RESPONSE.value:
            stored = event.data.get("message")
            if stored and isinstance(stored, dict):
                raw_content = stored.get("content", "")
                if isinstance(raw_content, str) and raw_content.strip():
                    messages.append({"role": "assistant", "content": raw_content})
                elif isinstance(raw_content, list):
                    # Content blocks: [{"type": "text", "text": "..."}]
                    text_parts = [
                        b.get("text", "")
                        for b in raw_content
                        if isinstance(b, dict) and b.get("type") == "text"
                    ]
                    joined = "\n".join(t for t in text_parts if t)
                    if joined:
                        messages.append({"role": "assistant", "content": joined})

        elif etype == EventType.CONTEXT_COMPACT.value:
            compacted = event.data.get("compacted_messages")
            if compacted is not None:
                messages = [
                    {"role": m["role"], "content": m.get("content", "")}
                    for m in compacted
                    if m.get("role") in ("user", "assistant")
                    and isinstance(m.get("content"), str)
                    and m["content"].strip()
                ]

    return messages


# ---------------------------------------------------------------------------
# Flush prompt construction (ported from Hermes)
# ---------------------------------------------------------------------------


def build_flush_prompt(memory_dir: Path) -> str:
    """Build the LLM prompt that instructs the flush agent to save memories.

    Reads current memory files from disk and includes them in the prompt
    so the agent can see what's already saved and avoid overwrites.
    Ported from Hermes ``_flush_memories_for_session``.
    """
    flush_prompt = (
        "[System: This session is about to be automatically reset due to "
        "inactivity or a scheduled daily reset. The conversation context "
        "will be cleared after this turn.\n\n"
        "Review the conversation above and:\n"
        "1. Save any important facts, preferences, or decisions to memory "
        "(user profile or your notes) that would be useful in future sessions.\n"
        "2. If you discovered a reusable workflow or solved a non-trivial "
        "problem, consider saving it as a skill.\n"
        "3. If nothing is worth saving, that's fine — just skip.\n\n"
    )

    # Read current memory state from disk as a stale-overwrite guard.
    current_memory = ""
    for fname, label in [
        ("MEMORY.md", "MEMORY (your personal notes)"),
        ("USER.md", "USER PROFILE (who the user is)"),
    ]:
        fpath = memory_dir / fname
        try:
            content = fpath.read_text(encoding="utf-8").strip()
            if content:
                current_memory += f"\n\n## Current {label}:\n{content}"
        except (OSError, IOError, FileNotFoundError):
            pass

    if current_memory:
        flush_prompt += (
            "IMPORTANT — here is the current live state of memory. Other "
            "sessions, cron jobs, or the user may have updated it since this "
            "conversation ended. Do NOT overwrite or remove entries unless "
            "the conversation above reveals something that genuinely "
            "supersedes them. Only add new information that is not already "
            "captured below."
            f"{current_memory}\n\n"
        )

    flush_prompt += (
        "Do NOT respond to the user. Just use the memory "
        "tool if needed, then stop.]"
    )

    return flush_prompt


# ---------------------------------------------------------------------------
# Flush agent — runs a mini LLM loop with only the memory tool
# ---------------------------------------------------------------------------


async def run_flush_agent(
    *,
    transcript: list[dict[str, str]],
    flush_prompt: str,
    memory_manager: MemoryManager,
    llm_client: Any,
    model: str,
    max_iterations: int = _DEFAULT_FLUSH_MAX_ITERATIONS,
) -> None:
    """Run a temporary LLM agent that reviews the transcript and saves memories.

    This is a minimal agent loop with only the ``memory`` tool available.
    It sends the conversation transcript plus a flush prompt to the LLM,
    processes any memory tool calls, and repeats until the LLM stops
    calling tools or the iteration limit is reached.

    Ported from Hermes ``_flush_memories_for_session`` which creates a
    temporary ``AIAgent`` with ``enabled_toolsets=["memory", "skills"]``.
    We use only the memory tool since skills are managed differently in
    Surogates.
    """
    messages: list[dict[str, Any]] = list(transcript)
    messages.append({"role": "user", "content": flush_prompt})

    system_prompt = (
        "You are a memory extraction agent. Your ONLY job is to review "
        "the conversation transcript and use the memory tool to save "
        "important facts before the session is reset. Do not respond to "
        "the user. Do not produce any text output. Only call the memory "
        "tool if there is something worth saving, then stop."
    )

    tool_schemas = memory_manager.get_all_tool_schemas()
    tools = [
        {
            "type": "function",
            "function": schema,
        }
        for schema in tool_schemas
    ]

    for iteration in range(max_iterations):
        try:
            response = await llm_client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system_prompt}] + messages,
                tools=tools if tools else None,
                temperature=0.3,
            )
        except Exception as e:
            logger.warning("Flush agent LLM call failed (iteration %d): %s", iteration, e)
            return

        choice = response.choices[0]
        message = choice.message

        assistant_msg: dict[str, Any] = {"role": "assistant"}
        if message.content:
            assistant_msg["content"] = message.content
        if message.tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in message.tool_calls
            ]
        messages.append(assistant_msg)

        if not message.tool_calls:
            logger.debug("Flush agent finished after %d iterations (no tool calls)", iteration + 1)
            return

        for tc in message.tool_calls:
            tool_name = tc.function.name
            try:
                args = json.loads(tc.function.arguments)
            except (json.JSONDecodeError, TypeError):
                args = {}

            if memory_manager.has_tool(tool_name):
                result = memory_manager.handle_tool_call(tool_name, args)
                logger.debug(
                    "Flush agent tool call: %s(%s) → %s",
                    tool_name, args.get("action", ""), result[:200],
                )
            else:
                result = json.dumps({
                    "success": False,
                    "error": f"Tool '{tool_name}' is not available. Only the 'memory' tool is enabled.",
                })

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

    logger.debug("Flush agent hit max iterations (%d)", max_iterations)


# ---------------------------------------------------------------------------
# Sandbox pod teardown (K8s backend only)
# ---------------------------------------------------------------------------


async def _destroy_sandbox_pod(
    *,
    session_id: UUID,
    namespace: str,
) -> None:
    """Delete the sandbox pod and its S3 credential secret for a session.

    Uses the K8s API directly — the CronJob runs in a separate pod from
    the worker, so it can't use the worker's in-memory SandboxPool.

    Pod naming convention: ``sandbox-{session_id_hex[:12]}``.
    Secret naming convention: ``sandbox-s3-{session_id_hex[:12]}``.
    """
    sandbox_id = str(session_id).replace("-", "")
    pod_name = f"sandbox-{sandbox_id[:12]}"
    secret_name = f"sandbox-s3-{sandbox_id[:12]}"

    try:
        from kubernetes_asyncio import client, config as k8s_config
        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            await k8s_config.load_kube_config()

        async with client.ApiClient() as api_client:
            api = client.CoreV1Api(api_client=api_client)

            try:
                await api.delete_namespaced_pod(pod_name, namespace)
                logger.info("Deleted sandbox pod %s", pod_name)
            except client.ApiException as exc:
                if exc.status != 404:
                    logger.debug("Failed to delete sandbox pod %s: %s", pod_name, exc)

            try:
                await api.delete_namespaced_secret(secret_name, namespace)
            except client.ApiException as exc:
                if exc.status != 404:
                    logger.debug("Failed to delete sandbox secret %s: %s", secret_name, exc)

    except ImportError:
        logger.debug("kubernetes_asyncio not installed, skipping sandbox teardown")
    except Exception as e:
        logger.debug("Sandbox teardown failed for session %s (non-fatal): %s", session_id, e)


# ---------------------------------------------------------------------------
# Memory persistence to durable storage (TenantStorage / S3)
# ---------------------------------------------------------------------------


async def persist_memory_to_storage(
    *,
    memory_dir: Path,
    storage: Any,
    org_id: UUID,
    user_id: UUID,
) -> None:
    """Copy memory files from local disk to TenantStorage (S3/Garage).

    This ensures memories survive PV loss.  Called after the flush agent
    finishes writing to local memory files.
    """
    tenant_storage = TenantStorage(storage, org_id=org_id, user_id=user_id)
    await tenant_storage.ensure_bucket()

    for fname in ("MEMORY.md", "USER.md"):
        fpath = memory_dir / fname
        try:
            content = fpath.read_text(encoding="utf-8")
            if content.strip():
                await tenant_storage.write_memory_file(fname, content)
                logger.debug(
                    "Persisted %s to TenantStorage for user %s",
                    fname, user_id,
                )
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(
                "Failed to persist %s to TenantStorage: %s",
                fname, e,
            )


# ---------------------------------------------------------------------------
# Session reset with memory flush (main orchestration)
# ---------------------------------------------------------------------------


def _memory_dir_for_session(settings: Settings, org_id: UUID, user_id: UUID) -> Path:
    """Return the user-scoped memory directory for a session.

    Matches ``TenantAssetManager.memory_dir()`` and the worker's convention:
    ``{tenant_assets_root}/{org_id}/users/{user_id}/memory``.
    """
    return (
        Path(settings.tenant_assets_root)
        / str(org_id) / "users" / str(user_id) / "memory"
    )


async def flush_and_reset_session(
    *,
    session: Session,
    session_store: SessionStore,
    settings: Settings,
    llm_client: Any,
    storage: Any,
    redis_client: Any,
) -> bool:
    """Flush memory for a single session, then reset it.

    Returns ``True`` if the session was successfully reset, ``False`` if
    the flush or reset failed (the session is left untouched on failure).
    """
    session_id = session.id
    user_id = session.user_id
    org_id = session.org_id
    reset_settings = settings.session_reset

    # API-channel sessions are owned by a service account, not a user, and
    # carry no per-user memory to flush.  Reset them in place without running
    # the flush agent.
    if user_id is None:
        logger.info(
            "Resetting service-account session %s (channel=%s) without memory flush",
            session_id, session.channel,
        )
        await session_store.reset_session(session_id, reason="idle_service_account")
        return True

    logger.info(
        "Flushing memory for session %s (user=%s, org=%s, last_active=%s)",
        session_id, user_id, org_id, session.updated_at,
    )

    # 1. Load only the event types the transcript needs.
    events = await session_store.get_events(
        session_id,
        types=[EventType.USER_MESSAGE, EventType.LLM_RESPONSE, EventType.CONTEXT_COMPACT],
    )
    if not events:
        logger.debug("Session %s has no events, skipping flush", session_id)
        await session_store.reset_session(session_id, reason="idle_no_events")
        return True

    # 2. Extract a user/assistant transcript.
    transcript = extract_transcript(events)
    if len(transcript) < _MIN_TRANSCRIPT_MESSAGES:
        logger.debug(
            "Session %s transcript too short (%d messages), skipping flush",
            session_id, len(transcript),
        )
        await session_store.reset_session(session_id, reason="idle_short_transcript")
        return True

    # 3. Set up memory store pointing at the user-scoped memory directory.
    memory_dir = _memory_dir_for_session(settings, org_id, user_id)
    memory_store = MemoryStore(memory_dir=memory_dir)
    memory_manager = MemoryManager(memory_store)
    memory_manager.initialize_all()

    # 4. Build the flush prompt with stale-overwrite guard.
    flush_prompt = build_flush_prompt(memory_dir)

    # 5. Run the flush agent.
    try:
        await run_flush_agent(
            transcript=transcript,
            flush_prompt=flush_prompt,
            memory_manager=memory_manager,
            llm_client=llm_client,
            model=settings.llm.model,
            max_iterations=reset_settings.flush_max_iterations,
        )
        logger.info("Memory flush completed for session %s", session_id)
    except Exception as e:
        logger.warning(
            "Memory flush failed for session %s: %s (proceeding with reset)",
            session_id, e,
        )

    # 6. Persist memory files to durable storage (S3/Garage).
    try:
        await persist_memory_to_storage(
            memory_dir=memory_dir,
            storage=storage,
            org_id=org_id,
            user_id=user_id,
        )
    except Exception as e:
        logger.warning(
            "Memory persistence to S3 failed for session %s: %s",
            session_id, e,
        )

    # 7. Interrupt any running harness via Redis pub/sub.
    #    The harness's finally block destroys the sandbox pod on interrupt.
    try:
        await redis_client.publish(
            f"{INTERRUPT_CHANNEL_PREFIX}:{session_id}",
            json.dumps({"reason": "session_reset_idle"}).encode(),
        )
    except Exception as e:
        logger.debug("Redis interrupt publish failed (non-fatal): %s", e)

    # 8. Tear down sandbox pod (K8s backend only).
    #    Fallback for cases where the harness isn't running or the
    #    interrupt didn't trigger cleanup.
    if settings.sandbox.backend == "kubernetes":
        await _destroy_sandbox_pod(
            session_id=session_id,
            namespace=settings.sandbox.k8s_namespace,
        )

    # 9. Mark session as reset.  Events, counters, and cursor are left
    #    untouched — the user can come back and continue at any time.
    await session_store.reset_session(session_id, reason="idle")

    logger.info("Session %s reset complete", session_id)
    return True


# ---------------------------------------------------------------------------
# Main job entry point
# ---------------------------------------------------------------------------


async def reset_idle_sessions(dry_run: bool = False) -> int:
    """Find and reset all idle sessions.

    Returns the number of sessions reset (or that would be reset in
    dry-run mode).
    """
    settings = load_settings()
    reset_cfg = settings.session_reset

    if not reset_cfg.enabled:
        logger.info("Session reset is disabled (session_reset.enabled=false)")
        return 0

    if reset_cfg.mode == "none":
        logger.info("Session reset mode is 'none', nothing to do")
        return 0

    # Bootstrap database.
    engine = async_engine_from_settings(settings.db)
    factory = async_session_factory(engine)
    session_store = SessionStore(factory)

    # Find idle sessions.
    daily_at_hour = reset_cfg.at_hour if reset_cfg.mode in ("daily", "both") else None
    idle_sessions = await session_store.find_idle_sessions(
        idle_minutes=reset_cfg.idle_minutes,
        agent_id=settings.agent_id,
        daily_at_hour=daily_at_hour,
        mode=reset_cfg.mode,
    )

    if not idle_sessions:
        logger.info("No idle sessions found")
        await engine.dispose()
        return 0

    logger.info("Found %d idle session(s) to reset", len(idle_sessions))

    if dry_run:
        for s in idle_sessions:
            logger.info(
                "[DRY RUN] Would reset session %s (user=%s, last_active=%s)",
                s.id, s.user_id, s.updated_at,
            )
        await engine.dispose()
        return len(idle_sessions)

    # Bootstrap LLM client and Redis (shared across all sessions).
    from openai import AsyncOpenAI
    from redis.asyncio import Redis

    llm_kwargs: dict[str, Any] = {}
    if settings.llm.api_key:
        llm_kwargs["api_key"] = settings.llm.api_key
    if settings.llm.base_url:
        llm_kwargs["base_url"] = settings.llm.base_url
    llm_client = AsyncOpenAI(**llm_kwargs)
    redis_client = Redis.from_url(settings.redis.url, decode_responses=False)
    storage = create_backend(settings)

    reset_count = 0

    try:
        for session in idle_sessions:
            try:
                success = await flush_and_reset_session(
                    session=session,
                    session_store=session_store,
                    settings=settings,
                    llm_client=llm_client,
                    storage=storage,
                    redis_client=redis_client,
                )
                if success:
                    reset_count += 1
            except Exception as e:
                logger.warning(
                    "Failed to reset session %s: %s",
                    session.id, e,
                )
    finally:
        await redis_client.aclose()
        await llm_client.close()
        await engine.dispose()

    logger.info(
        "Reset %d of %d idle session(s) (%d failed)",
        reset_count, len(idle_sessions), len(idle_sessions) - reset_count,
    )

    return reset_count


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dry_run = "--dry-run" in sys.argv
    count = asyncio.run(reset_idle_sessions(dry_run=dry_run))
    sys.exit(0 if count >= 0 else 1)


if __name__ == "__main__":
    main()
