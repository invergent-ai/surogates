from types import SimpleNamespace

from surogates.session.events import EventType
from surogates.session.interactive_input import (
    pending_input_for_session,
    resolve_input_response,
    valid_tool_call_id,
)


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
    def __init__(self, db):
        self._db = db
        self.emitted = []

    def _sf(self):
        return self._db

    async def emit_event(self, session_id, event_type, data):
        self.emitted.append((session_id, event_type, data))
        return 42


def test_valid_tool_call_id_rejects_bad_values():
    assert valid_tool_call_id("tc1") == "tc1"
    assert valid_tool_call_id("") is None
    assert valid_tool_call_id("x\nbad") is None
    assert valid_tool_call_id("x" * 129) is None


async def test_pending_input_returns_payload_for_newest_pending_item():
    row = SimpleNamespace(
        action_ref={"tool_call_id": "tc1"},
        payload={"questions": [{"prompt": "q"}], "context": "ctx"},
        created_at=None,
    )
    store = _Store(_DB(_ExecuteResult(row=row)))

    pending = await pending_input_for_session(store, session_id="s1")

    assert pending == {
        "tool_call_id": "tc1",
        "questions": [{"prompt": "q"}],
        "context": "ctx",
        "created_at": None,
    }


def _pending_row(questions):
    return SimpleNamespace(
        action_ref={"tool_call_id": "tc1"},
        payload={"questions": questions, "context": ""},
        created_at=None,
    )


async def test_resolve_emits_when_pending_row_claimed():
    # Two queries now: the asked questions are read so is_other can be
    # settled here, then the row is claimed.
    store = _Store(
        _DB(
            _ExecuteResult(row=_pending_row([{"prompt": "q"}])),
            _ExecuteResult(rowcount=1),
        ),
    )

    ok = await resolve_input_response(
        store,
        session_id="s1",
        tool_call_id="tc1",
        responses=[{"question": "q", "answer": "a", "is_other": False}],
    )

    assert ok == 42
    assert store._db.committed is True
    assert store.emitted == [
        (
            "s1",
            EventType.ASK_USER_QUESTION_RESPONSE,
            {
                "tool_call_id": "tc1",
                "responses": [{"question": "q", "answer": "a", "is_other": False}],
            },
        ),
    ]


async def test_resolve_overrides_a_submitted_is_other_that_the_menu_contradicts():
    # A client asserting "off the menu" for an answer that is on it does
    # not get to write that into the transcript.
    store = _Store(
        _DB(
            _ExecuteResult(
                row=_pending_row(
                    [{"prompt": "Which region?", "choices": [{"label": "eu-west"}]}],
                ),
            ),
            _ExecuteResult(rowcount=1),
        ),
    )

    await resolve_input_response(
        store,
        session_id="s1",
        tool_call_id="tc1",
        responses=[
            {"question": "Which region?", "answer": "eu-west", "is_other": True},
        ],
    )

    (_, _, data) = store.emitted[0]
    assert data["responses"][0]["is_other"] is False


async def test_resolve_skips_the_lookup_when_the_caller_supplies_questions():
    # try_resolve_text_answer already holds the payload; re-reading it
    # would be a second query on every channel reply.
    store = _Store(_DB(_ExecuteResult(rowcount=1)))

    ok = await resolve_input_response(
        store,
        session_id="s1",
        tool_call_id="tc1",
        responses=[{"question": "q", "answer": "a", "is_other": True}],
        questions=[{"prompt": "q"}],
    )

    assert ok == 42
    (_, _, data) = store.emitted[0]
    assert data["responses"][0]["is_other"] is False


async def test_resolve_skips_emit_when_no_pending_row_claimed():
    store = _Store(_DB(_ExecuteResult(rowcount=0)))

    ok = await resolve_input_response(
        store,
        session_id="s1",
        tool_call_id="tc1",
        responses=[],
    )

    assert ok is None
    assert store.emitted == []
