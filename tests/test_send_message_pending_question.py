"""The web message route converts typed replies into pending-question
answers (mirrors the slack/telegram inbound behavior)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from surogates.api.routes.sessions import _resolve_pending_question
from surogates.session.events import EventType


class _ExecuteResult:
    def __init__(self, *, rowcount=0, row=None):
        self.rowcount = rowcount
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _DB:
    def __init__(self, *results):
        self.results = list(results)
        self.committed = False

    async def execute(self, *args, **kwargs):
        if not self.results:
            return _ExecuteResult()
        return self.results.pop(0)

    async def commit(self):
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Store:
    def __init__(self, db, prior_responses=()):
        self._db = db
        self.emitted = []
        self.prior_responses = list(prior_responses)

    def _sf(self):
        return self._db

    async def emit_event(self, session_id, event_type, data):
        self.emitted.append((session_id, event_type, data))
        return 77

    async def get_events(self, session_id, after=0, types=None):
        return self.prior_responses


def _pending_row(*, age_seconds: int = 0):
    return SimpleNamespace(
        action_ref={"tool_call_id": "tc-live"},
        payload={
            "questions": [{"prompt": "What subjects do you like?"}],
            "context": "",
        },
        created_at=(
            datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        ),
    )


async def test_fresh_pending_question_consumes_the_message():
    store = _Store(
        _DB(
            _ExecuteResult(row=_pending_row(age_seconds=60)),
            _ExecuteResult(rowcount=1),  # inbox claim
        ),
    )
    event_id = await _resolve_pending_question(store, "s1", "biology mostly")
    assert event_id == 77
    (session_id, event_type, data) = store.emitted[0]
    assert event_type is EventType.ASK_USER_QUESTION_RESPONSE
    assert data["tool_call_id"] == "tc-live"
    assert data["responses"] == [
        {
            "question": "What subjects do you like?",
            "answer": "biology mostly",
            "is_other": True,
        },
    ]


async def test_no_pending_question_returns_none():
    store = _Store(_DB(_ExecuteResult(row=None)))
    assert await _resolve_pending_question(store, "s1", "hello") is None
    assert store.emitted == []


async def test_stale_pending_row_is_ignored():
    """A row older than the tool's wait window belongs to a timed-out
    call — converting would swallow the message with nothing consuming
    the answer."""
    store = _Store(
        _DB(_ExecuteResult(row=_pending_row(age_seconds=31 * 60))),
    )
    assert await _resolve_pending_question(store, "s1", "hello") is None
    assert store.emitted == []


async def test_lost_claim_race_returns_none():
    """The form's respond route claimed the row first: exactly one
    response event, and the message falls through as a normal one."""
    store = _Store(
        _DB(
            _ExecuteResult(row=_pending_row(age_seconds=5)),
            _ExecuteResult(rowcount=0),  # someone else claimed it
        ),
    )
    assert await _resolve_pending_question(store, "s1", "hello") is None
    assert store.emitted == []


async def test_already_answered_question_is_not_reclaimed():
    """The form's respond route emits before its best-effort claim; a
    row left pending after a form answer must not eat the next typed
    message."""
    prior = SimpleNamespace(data={"tool_call_id": "tc-live"})
    store = _Store(
        _DB(_ExecuteResult(row=_pending_row(age_seconds=10))),
        prior_responses=[prior],
    )
    assert await _resolve_pending_question(store, "s1", "hello") is None
    assert store.emitted == []


async def test_resolution_failure_falls_back_to_normal_message():
    class _BrokenStore:
        def _sf(self):
            raise RuntimeError("db down")

    assert (
        await _resolve_pending_question(_BrokenStore(), "s1", "hello")
    ) is None
