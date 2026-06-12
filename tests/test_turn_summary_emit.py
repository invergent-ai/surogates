"""turn.summary emission contract.

Covers :meth:`AgentHarness._drain_and_emit_turn_summary` directly (the
core logic) plus the :meth:`_complete_session` integration that fires
the drain when a turn ends successfully.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from surogates.harness.loop import AgentHarness
from surogates.harness.turn_summarizer import TurnArtifact, TurnSummary
from surogates.session.events import EventType
from tests.test_iteration_summary_emit import stub_turn_summarizer  # noqa: F401
from tests.test_loop_turn_id import (
    _make_loop_harness,
    _make_session,
)


def _event(event_id: int, event_type: EventType, data: dict[str, Any]):
    """Mimic surogates.session.models.Event for store.get_events results."""
    return SimpleNamespace(
        id=event_id,
        type=event_type.value,
        data=data,
        session_id=uuid4(),
        created_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# _drain_and_emit_turn_summary direct tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_emits_turn_summary_with_recap_and_artifacts(
    stub_turn_summarizer,
) -> None:
    stub_turn_summarizer.turn_response = TurnSummary(
        recap="Reworked the hero around brain/hands.",
        artifacts=[
            TurnArtifact(kind="file", label="landing.html", ref="landing.html"),
        ],
    )

    store = AsyncMock()
    store.emit_event = AsyncMock()
    store.get_events = AsyncMock(return_value=[])
    harness = _make_loop_harness(
        session_store=store, turn_summarizer=stub_turn_summarizer,
    )

    session_id = uuid4()
    await harness._drain_and_emit_turn_summary(
        session_id=session_id,
        turn_id="turn-1",
        user_message="rewrite the hero",
    )

    emits = [
        c.args[2]
        for c in store.emit_event.await_args_list
        if c.args[1] == EventType.TURN_SUMMARY
    ]
    assert len(emits) == 1
    payload = emits[0]
    assert payload["turn_id"] == "turn-1"
    assert payload["recap"].startswith("Reworked the hero")
    assert payload["artifacts"] == [
        {"kind": "file", "label": "landing.html", "ref": "landing.html"},
    ]


@pytest.mark.asyncio
async def test_drain_skips_emit_when_summarizer_returns_none(
    stub_turn_summarizer,
) -> None:
    stub_turn_summarizer.turn_response = None
    store = AsyncMock()
    store.emit_event = AsyncMock()
    store.get_events = AsyncMock(return_value=[])
    harness = _make_loop_harness(
        session_store=store, turn_summarizer=stub_turn_summarizer,
    )

    await harness._drain_and_emit_turn_summary(
        session_id=uuid4(),
        turn_id="turn-1",
        user_message="x",
    )
    assert not any(
        c.args[1] == EventType.TURN_SUMMARY
        for c in store.emit_event.await_args_list
    )


@pytest.mark.asyncio
async def test_drain_is_noop_when_summarizer_absent() -> None:
    store = AsyncMock()
    store.emit_event = AsyncMock()
    store.get_events = AsyncMock(return_value=[])
    harness = _make_loop_harness(session_store=store, turn_summarizer=None)

    await harness._drain_and_emit_turn_summary(
        session_id=uuid4(),
        turn_id="turn-1",
        user_message="x",
    )
    assert store.emit_event.await_count == 0


@pytest.mark.asyncio
async def test_drain_passes_iteration_summaries_and_candidates(
    stub_turn_summarizer,
) -> None:
    """The drain re-reads iteration summaries from the event log and
    builds candidate artifacts from TOOL_CALL events in the same turn."""
    stub_turn_summarizer.turn_response = TurnSummary(
        recap="r", artifacts=[],
    )

    store = AsyncMock()
    store.emit_event = AsyncMock()

    iteration_events = [
        _event(1, EventType.ITERATION_SUMMARY, {
            "turn_id": "turn-1",
            "iteration_index": 0,
            "summary": "Outline the patch plan",
        }),
        _event(2, EventType.ITERATION_SUMMARY, {
            "turn_id": "turn-1",
            "iteration_index": 1,
            "summary": "Apply the rewrite",
        }),
    ]
    all_events = [
        _event(0, EventType.USER_MESSAGE, {"content": "rewrite the hero"}),
        _event(3, EventType.LLM_RESPONSE, {"turn_id": "turn-1"}),
        _event(4, EventType.TOOL_CALL, {
            "turn_id": "turn-1",
            "name": "patch",
            "arguments": '{"path": "landing.html"}',
            "tool_call_id": "c1",
        }),
        _event(5, EventType.TOOL_CALL, {
            "turn_id": "turn-1",
            "name": "read_file",  # not notable — must be filtered out
            "arguments": '{"path": "x.md"}',
            "tool_call_id": "c2",
        }),
        _event(6, EventType.TOOL_CALL, {
            "turn_id": "turn-1",
            "name": "web_extract",
            "arguments": '{"url": "https://example.com"}',
            "tool_call_id": "c3",
        }),
        _event(7, EventType.TOOL_CALL, {
            "turn_id": "turn-1",
            # Internal agent state (e.g. the /product-marketing skill's
            # canonical context file) — hidden paths are never
            # presented as downloadable deliverables.
            "name": "write_file",
            "arguments": '{"path": ".agents/product-marketing.md"}',
            "tool_call_id": "c4",
        }),
    ]

    async def _get_events(session_id, **kwargs):
        types = kwargs.get("types") or []
        if EventType.ITERATION_SUMMARY in types:
            return iteration_events
        return all_events

    store.get_events = AsyncMock(side_effect=_get_events)
    harness = _make_loop_harness(
        session_store=store, turn_summarizer=stub_turn_summarizer,
    )

    await harness._drain_and_emit_turn_summary(
        session_id=uuid4(),
        turn_id="turn-1",
        user_message="rewrite the hero",
    )

    assert len(stub_turn_summarizer.turn_calls) == 1
    call = stub_turn_summarizer.turn_calls[0]
    assert call["user_message"] == "rewrite the hero"
    assert call["iteration_summaries"] == [
        "Outline the patch plan", "Apply the rewrite",
    ]
    # patch is surfaced; read_file (not notable), web_extract (not
    # downloadable — the summary card only presents downloadable
    # artifacts) and the .agents/ write (internal agent state) are
    # dropped.
    candidate_kinds = [a.kind for a in call["candidate_artifacts"]]
    assert "file" in candidate_kinds
    assert "url" not in candidate_kinds
    assert all(a.label != "x.md" for a in call["candidate_artifacts"])
    assert all(
        "example.com" not in a.ref for a in call["candidate_artifacts"]
    )
    assert all(
        not a.ref.startswith(".agents/")
        for a in call["candidate_artifacts"]
    )


@pytest.mark.asyncio
async def test_drain_swallows_summarizer_exception(monkeypatch) -> None:
    """A throwing summarizer must not propagate up — emit nothing,
    log a warning, and return cleanly."""
    class _Boom:
        async def summarize_turn(self, **_kwargs):
            raise RuntimeError("provider down")

    store = AsyncMock()
    store.emit_event = AsyncMock()
    store.get_events = AsyncMock(return_value=[])
    harness = _make_loop_harness(
        session_store=store, turn_summarizer=_Boom(),
    )

    await harness._drain_and_emit_turn_summary(
        session_id=uuid4(),
        turn_id="turn-1",
        user_message="x",
    )
    # No TURN_SUMMARY event was emitted.
    assert not any(
        c.args[1] == EventType.TURN_SUMMARY
        for c in store.emit_event.await_args_list
    )


# ---------------------------------------------------------------------------
# _complete_session integration: drain runs only on success reasons + turn_id
# ---------------------------------------------------------------------------


async def _call_complete_session(
    harness,
    *,
    reason: str,
    turn_id: str | None,
    user_message: str | None = None,
):
    """Invoke the REAL _complete_session bypassing the harness fixture's
    AsyncMock by re-binding the unbound method."""
    real = AgentHarness._complete_session.__get__(harness, AgentHarness)
    session = _make_session()
    lease = SimpleNamespace(lease_token=uuid4())
    await real(
        session, [{"role": "user", "content": "do it"}], lease,
        reason=reason,
        turn_id=turn_id,
        user_message=user_message,
    )
    return session


@pytest.mark.asyncio
async def test_complete_session_drains_on_success_reason(
    stub_turn_summarizer,
) -> None:
    stub_turn_summarizer.turn_response = TurnSummary(
        recap="Did the thing.", artifacts=[],
    )
    store = AsyncMock()
    store.emit_event = AsyncMock()
    store.get_events = AsyncMock(return_value=[])
    store.update_session_status = AsyncMock()
    harness = _make_loop_harness(
        session_store=store, turn_summarizer=stub_turn_summarizer,
    )

    await _call_complete_session(
        harness, reason="completed", turn_id="t-1",
        user_message="do it",
    )

    emit_types = [c.args[1] for c in store.emit_event.await_args_list]
    # TURN_SUMMARY appears before SESSION_COMPLETE in the same call list.
    assert EventType.TURN_SUMMARY in emit_types
    assert emit_types.index(EventType.TURN_SUMMARY) < emit_types.index(
        EventType.SESSION_COMPLETE,
    )


@pytest.mark.asyncio
async def test_complete_session_skips_drain_on_failure_reason(
    stub_turn_summarizer,
) -> None:
    stub_turn_summarizer.turn_response = TurnSummary(
        recap="should not appear", artifacts=[],
    )
    store = AsyncMock()
    store.emit_event = AsyncMock()
    store.get_events = AsyncMock(return_value=[])
    store.update_session_status = AsyncMock()
    harness = _make_loop_harness(
        session_store=store, turn_summarizer=stub_turn_summarizer,
    )

    await _call_complete_session(
        harness, reason="invalid_tool_calls", turn_id="t-1",
    )

    assert not any(
        c.args[1] == EventType.TURN_SUMMARY
        for c in store.emit_event.await_args_list
    )


@pytest.mark.asyncio
async def test_complete_session_skips_drain_without_turn_id(
    stub_turn_summarizer,
) -> None:
    stub_turn_summarizer.turn_response = TurnSummary(
        recap="should not appear", artifacts=[],
    )
    store = AsyncMock()
    store.emit_event = AsyncMock()
    store.get_events = AsyncMock(return_value=[])
    store.update_session_status = AsyncMock()
    harness = _make_loop_harness(
        session_store=store, turn_summarizer=stub_turn_summarizer,
    )

    # Old callers (or non-turn completions) pass no turn_id.
    await _call_complete_session(harness, reason="completed", turn_id=None)

    assert not any(
        c.args[1] == EventType.TURN_SUMMARY
        for c in store.emit_event.await_args_list
    )


@pytest.mark.asyncio
async def test_collect_candidate_artifacts_includes_workspace_mtime_files() -> None:
    """Files modified during the turn but produced without a write_file/
    patch/create_artifact call still surface — e.g. a python script run
    through the terminal that writes a .docx to the workspace."""
    from datetime import datetime, timedelta, timezone

    store = AsyncMock()
    store.get_events = AsyncMock(return_value=[])
    # Session config carries the bucket name and (optionally) the
    # sandbox_root_session_id for shared workspaces.
    fake_session = SimpleNamespace(
        id=uuid4(),
        config={"storage_bucket": "bucket-1"},
    )
    store.get_session = AsyncMock(return_value=fake_session)

    turn_start = datetime.now(timezone.utc)
    fresh_key = "sessions/abc/reports/Summary.docx"
    stale_key = "sessions/abc/old.txt"

    class _FakeStorage:
        async def list_entries(
            self, _bucket: str, prefix: str = "",
        ) -> list[dict[str, Any]]:
            return [
                {"key": fresh_key, "modified": turn_start + timedelta(seconds=5), "size": 1},
                {"key": stale_key, "modified": turn_start - timedelta(hours=1), "size": 2},
            ]

    harness = _make_loop_harness(session_store=store, turn_summarizer=None)
    harness._storage = _FakeStorage()
    harness._turn_started_at = turn_start

    candidates = await harness._collect_candidate_artifacts(
        session_id=fake_session.id, turn_id="turn-X",
    )

    files = [c for c in candidates if c.kind == "file"]
    refs = [f.ref for f in files]
    # The post-turn-start file appears; the pre-turn-start file doesn't.
    assert any("Summary.docx" in r for r in refs)
    assert all("old.txt" not in r for r in refs)


@pytest.mark.asyncio
async def test_complete_session_skips_drain_without_summarizer() -> None:
    store = AsyncMock()
    store.emit_event = AsyncMock()
    store.get_events = AsyncMock(return_value=[])
    store.update_session_status = AsyncMock()
    harness = _make_loop_harness(
        session_store=store, turn_summarizer=None,
    )

    await _call_complete_session(harness, reason="completed", turn_id="t-1")

    assert not any(
        c.args[1] == EventType.TURN_SUMMARY
        for c in store.emit_event.await_args_list
    )


# ---------------------------------------------------------------------------
# _complete_session closes the session's browser, like the sandbox
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_session_closes_browser() -> None:
    store = AsyncMock()
    store.emit_event = AsyncMock()
    store.get_events = AsyncMock(return_value=[])
    store.update_session_status = AsyncMock()
    harness = _make_loop_harness(session_store=store, turn_summarizer=None)
    harness._browser_pool = AsyncMock()

    session = await _call_complete_session(
        harness, reason="completed", turn_id="t-1",
    )

    harness._browser_pool.destroy_for_session.assert_awaited_once_with(
        str(session.id),
    )


@pytest.mark.asyncio
async def test_complete_session_closes_browser_on_failure_reason() -> None:
    """A leaked browser pod is worse than a failed turn — cleanup must
    run regardless of the completion reason, not just on success."""
    store = AsyncMock()
    store.emit_event = AsyncMock()
    store.get_events = AsyncMock(return_value=[])
    store.update_session_status = AsyncMock()
    harness = _make_loop_harness(session_store=store, turn_summarizer=None)
    harness._browser_pool = AsyncMock()

    session = await _call_complete_session(
        harness, reason="invalid_tool_calls", turn_id="t-1",
    )

    harness._browser_pool.destroy_for_session.assert_awaited_once_with(
        str(session.id),
    )


@pytest.mark.asyncio
async def test_complete_session_swallows_browser_cleanup_error() -> None:
    """Browser cleanup is best effort — a backend failure must not
    abort completion (SESSION_COMPLETE still emitted)."""
    store = AsyncMock()
    store.emit_event = AsyncMock()
    store.get_events = AsyncMock(return_value=[])
    store.update_session_status = AsyncMock()
    harness = _make_loop_harness(session_store=store, turn_summarizer=None)
    harness._browser_pool = AsyncMock()
    harness._browser_pool.destroy_for_session = AsyncMock(
        side_effect=RuntimeError("backend down"),
    )

    await _call_complete_session(harness, reason="completed", turn_id="t-1")

    assert any(
        c.args[1] == EventType.SESSION_COMPLETE
        for c in store.emit_event.await_args_list
    )


# ---------------------------------------------------------------------------
# Internal workspace paths are never deliverable candidates
# ---------------------------------------------------------------------------


def test_internal_workspace_paths_are_not_candidates() -> None:
    from surogates.harness.turn_summarizer import (
        _is_internal_workspace_path,
    )

    # Hidden directories hold agent state (.agents/, .claude/, .cache/);
    # uploads/ holds user-provided inputs. Neither is ever a deliverable.
    assert _is_internal_workspace_path(".agents/product-marketing.md")
    assert _is_internal_workspace_path(".claude/settings.json")
    assert _is_internal_workspace_path("notes/.scratch/tmp.md")
    assert _is_internal_workspace_path(".env")
    assert _is_internal_workspace_path("uploads/123-datasheet.pdf")

    assert not _is_internal_workspace_path("report.pdf")
    assert not _is_internal_workspace_path("docs/strategy.md")
