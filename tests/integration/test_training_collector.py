"""Integration tests for the training-data collector bootstrap path.

Focus: ``TrainingDataCollector.collect_for_skill`` — the flow that
graduates a prompt-based skill into an expert by extracting every
``skill.invoked`` trajectory as a labeled training example.
"""

from __future__ import annotations

import pytest

from surogates.jobs.training_collector import TrainingDataCollector
from surogates.session.events import EventType

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# Trajectory extraction
# ---------------------------------------------------------------------------


async def _seed_skill_invocation(
    session_store,
    session_id,
    *,
    skill: str = "sql_writer",
    raw_message: str = "/sql_writer find active users",
    assistant_content: str | None = "SELECT * FROM users WHERE active = true;",
    tool_call: tuple[str, str] | None = None,
    tool_result: str | None = None,
) -> None:
    """Emit a realistic skill.invoked trajectory into the event log."""
    await session_store.emit_event(
        session_id, EventType.USER_MESSAGE,
        {"content": raw_message},
    )
    await session_store.emit_event(
        session_id, EventType.SKILL_INVOKED,
        {"skill": skill, "raw_message": raw_message, "staged_at": None},
    )

    if tool_call is not None:
        tool_call_id, tool_name = tool_call
        await session_store.emit_event(
            session_id, EventType.LLM_RESPONSE,
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": tool_call_id,
                        "type": "function",
                        "function": {"name": tool_name, "arguments": "{}"},
                    }],
                },
                "model": "gpt-4o",
                "input_tokens": 1,
                "output_tokens": 1,
            },
        )
        await session_store.emit_event(
            session_id, EventType.TOOL_RESULT,
            {
                "tool_call_id": tool_call_id,
                "name": tool_name,
                "content": tool_result or "",
                "elapsed_ms": 10,
            },
        )

    if assistant_content is not None:
        await session_store.emit_event(
            session_id, EventType.LLM_RESPONSE,
            {
                "message": {"role": "assistant", "content": assistant_content},
                "model": "gpt-4o",
                "input_tokens": 1,
                "output_tokens": 1,
            },
        )


async def test_collect_for_skill_extracts_user_and_final_assistant(
    session_store, session_factory,
):
    """The simplest trajectory: /<skill> ask → LLM answer."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent",
    )

    await _seed_skill_invocation(
        session_store, session.id,
        raw_message="/sql_writer find active users",
        assistant_content="SELECT * FROM users WHERE active = true;",
    )

    collector = TrainingDataCollector(session_store=session_store)
    examples = await collector.collect_for_skill("sql_writer", org_id)

    assert len(examples) == 1
    msgs = examples[0].messages
    assert msgs[0] == {"role": "user", "content": "find active users"}
    assert msgs[-1]["role"] == "assistant"
    assert msgs[-1]["content"] == "SELECT * FROM users WHERE active = true;"
    assert examples[0].expert_name == "sql_writer"
    assert examples[0].session_id == session.id


async def test_collect_for_skill_includes_tool_trajectory(
    session_store, session_factory,
):
    """Assistant → tool_call → tool → assistant is preserved in order."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent",
    )

    await _seed_skill_invocation(
        session_store, session.id,
        raw_message="/sql_writer run it",
        tool_call=("tc_1", "terminal"),
        tool_result="(0 rows)",
        assistant_content="Done — 0 rows matched.",
    )

    collector = TrainingDataCollector(session_store=session_store)
    examples = await collector.collect_for_skill("sql_writer", org_id)

    assert len(examples) == 1
    roles = [m["role"] for m in examples[0].messages]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert examples[0].messages[1]["tool_calls"][0]["id"] == "tc_1"
    assert examples[0].messages[2]["tool_call_id"] == "tc_1"
    assert examples[0].messages[2]["content"] == "(0 rows)"


async def test_collect_for_skill_stops_at_next_user_message(
    session_store, session_factory,
):
    """A second user turn closes the trajectory; its content is NOT included."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent",
    )

    await _seed_skill_invocation(
        session_store, session.id,
        raw_message="/sql_writer first ask",
        assistant_content="first answer",
    )
    # Second user turn — trajectory boundary.
    await session_store.emit_event(
        session.id, EventType.USER_MESSAGE, {"content": "thanks!"},
    )
    # A spurious later assistant turn that must not be captured.
    await session_store.emit_event(
        session.id, EventType.LLM_RESPONSE,
        {"message": {"role": "assistant", "content": "you're welcome"},
         "model": "gpt-4o", "input_tokens": 1, "output_tokens": 1},
    )

    collector = TrainingDataCollector(session_store=session_store)
    examples = await collector.collect_for_skill("sql_writer", org_id)

    assert len(examples) == 1
    assistant_contents = [
        m.get("content") for m in examples[0].messages if m["role"] == "assistant"
    ]
    assert assistant_contents == ["first answer"]


async def test_collect_for_skill_handles_multiple_invocations_same_session(
    session_store, session_factory,
):
    """Two ``/<skill>`` invocations in one session yield two examples."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent",
    )

    await _seed_skill_invocation(
        session_store, session.id,
        raw_message="/sql_writer first",
        assistant_content="first answer",
    )
    await _seed_skill_invocation(
        session_store, session.id,
        raw_message="/sql_writer second",
        assistant_content="second answer",
    )

    collector = TrainingDataCollector(session_store=session_store)
    examples = await collector.collect_for_skill("sql_writer", org_id)

    assert len(examples) == 2
    asks = [ex.messages[0]["content"] for ex in examples]
    assert asks == ["first", "second"]


async def test_collect_for_skill_excludes_tainted_session(
    session_store, session_factory,
):
    """A session with policy.denied is excluded when exclude_tainted=True."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    clean = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent",
    )
    tainted = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent",
    )

    await _seed_skill_invocation(
        session_store, clean.id,
        raw_message="/sql_writer clean",
        assistant_content="ok",
    )
    await _seed_skill_invocation(
        session_store, tainted.id,
        raw_message="/sql_writer bad",
        assistant_content="ok",
    )
    await session_store.emit_event(
        tainted.id, EventType.POLICY_DENIED,
        {"tool": "x", "reason": "y", "timestamp": 1.0},
    )

    collector = TrainingDataCollector(session_store=session_store)
    examples = await collector.collect_for_skill("sql_writer", org_id)

    assert len(examples) == 1
    assert examples[0].session_id == clean.id

    # With exclude_tainted=False both show up.
    all_examples = await collector.collect_for_skill(
        "sql_writer", org_id, exclude_tainted=False,
    )
    assert {e.session_id for e in all_examples} == {clean.id, tainted.id}


async def test_collect_for_skill_excludes_trajectory_with_thumbs_down(
    session_store, session_factory,
):
    """A ``user.feedback`` rating=down on an LLM response in a trajectory
    rejects that trajectory only — sibling invocations in the same session
    still yield training examples.

    Regression: ``session_has_taint`` only checks ``policy.denied`` and
    friends, so the judge's thumbs-down verdicts used to pass the filter
    and poison the training set with negative class labels.
    """
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent",
    )

    # First invocation: rated down — should be excluded.
    await session_store.emit_event(
        session.id, EventType.USER_MESSAGE,
        {"content": "/sql_writer bad query"},
    )
    await session_store.emit_event(
        session.id, EventType.SKILL_INVOKED,
        {"skill": "sql_writer", "raw_message": "/sql_writer bad query",
         "staged_at": None},
    )
    bad_response_id = await session_store.emit_event(
        session.id, EventType.LLM_RESPONSE,
        {
            "message": {"role": "assistant", "content": "SELECT 1;"},
            "model": "gpt-4o",
            "input_tokens": 1,
            "output_tokens": 1,
        },
    )
    await session_store.emit_event(
        session.id, EventType.USER_FEEDBACK,
        {
            "target_event_id": bad_response_id,
            "rating": "down",
            "source": "service_account",
            "rated_by_service_account_id": "00000000-0000-0000-0000-000000000001",
            "reason": "query missed the WHERE clause",
        },
    )

    # Second invocation: untouched — should survive.
    await _seed_skill_invocation(
        session_store, session.id,
        raw_message="/sql_writer good query",
        assistant_content="SELECT * FROM users;",
    )

    collector = TrainingDataCollector(session_store=session_store)
    examples = await collector.collect_for_skill("sql_writer", org_id)

    assert len(examples) == 1
    assert examples[0].messages[0]["content"] == "good query"
    assert examples[0].messages[-1]["content"] == "SELECT * FROM users;"

    # With exclude_tainted=False the rejected trajectory comes back.
    all_examples = await collector.collect_for_skill(
        "sql_writer", org_id, exclude_tainted=False,
    )
    asks = sorted(ex.messages[0]["content"] for ex in all_examples)
    assert asks == ["bad query", "good query"]


async def test_collect_for_skill_skips_trajectory_with_no_final_assistant(
    session_store, session_factory,
):
    """No assistant content after skill.invoked → no training example."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent",
    )

    # User invokes but session ends before any LLM reply lands.
    await session_store.emit_event(
        session.id, EventType.USER_MESSAGE,
        {"content": "/sql_writer something"},
    )
    await session_store.emit_event(
        session.id, EventType.SKILL_INVOKED,
        {"skill": "sql_writer", "raw_message": "/sql_writer something",
         "staged_at": None},
    )
    # Session fails before LLM responds.
    await session_store.emit_event(
        session.id, EventType.SESSION_FAIL, {"error": "timeout"},
    )

    collector = TrainingDataCollector(session_store=session_store)
    examples = await collector.collect_for_skill(
        "sql_writer", org_id, exclude_tainted=False,
    )

    assert examples == []


async def test_collect_for_skill_scopes_to_org(
    session_store, session_factory,
):
    """Invocations in a different org must not leak into the result."""
    org_a = await create_org(session_factory)
    user_a = await create_user(session_factory, org_a)
    sess_a = await session_store.create_session(
        user_id=user_a, org_id=org_a, agent_id="test-agent",
    )
    org_b = await create_org(session_factory)
    user_b = await create_user(session_factory, org_b)
    sess_b = await session_store.create_session(
        user_id=user_b, org_id=org_b, agent_id="test-agent",
    )

    await _seed_skill_invocation(
        session_store, sess_a.id,
        raw_message="/sql_writer org a", assistant_content="a",
    )
    await _seed_skill_invocation(
        session_store, sess_b.id,
        raw_message="/sql_writer org b", assistant_content="b",
    )

    collector = TrainingDataCollector(session_store=session_store)
    a_examples = await collector.collect_for_skill("sql_writer", org_a)
    b_examples = await collector.collect_for_skill("sql_writer", org_b)

    assert len(a_examples) == 1
    assert a_examples[0].session_id == sess_a.id
    assert len(b_examples) == 1
    assert b_examples[0].session_id == sess_b.id
