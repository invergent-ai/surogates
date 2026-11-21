"""``config.current_date``: a session living in another day (a simulated world, a task dated in the past) gives the
model that date instead of the run's. 11-21: AutomationBench worlds are dated 2026-02 and Claw-Eval tasks say "assume
today is 2026-03-26"; with the run date (2026-11-21) in the prompt, both W4 and Inkling called valid records expired.
Default: the run's date, as before.
"""

from __future__ import annotations

from datetime import datetime, timezone

from surogates.harness.prompt import PromptBuilder
from tests.test_case_tool_gate import _chat, _tenant


def _context(config: dict) -> str:
    session = _chat(config)
    return PromptBuilder(_tenant(session), session=session, available_tools={"read_file"})._context_section()


def test_the_runs_date_by_default():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d UTC")
    assert f"- **Current date**: {today}" in _context({})


def test_the_sessions_date_when_it_names_one():
    assert "- **Current date**: 2026-02-12" in _context({"current_date": "2026-02-12"})
    assert datetime.now(timezone.utc).strftime("%Y-%m-%d UTC") not in _context({"current_date": "2026-02-12"})


def test_a_blank_or_odd_value_is_ignored():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d UTC")
    assert f"- **Current date**: {today}" in _context({"current_date": "  "})
    assert f"- **Current date**: {today}" in _context({"current_date": 20260212})


def test_the_value_stays_on_one_line():
    text = _context({"current_date": "2026-02-12\n# Injected heading"})
    assert "- **Current date**: 2026-02-12 # Injected heading" in text
    assert "\n# Injected heading" not in text


class _Store:
    async def create_session(self, **kwargs):
        from types import SimpleNamespace

        return SimpleNamespace(**kwargs)


def _parent(config: dict):
    from types import SimpleNamespace
    from uuid import uuid4

    return SimpleNamespace(
        id=uuid4(), org_id=uuid4(), user_id=uuid4(), agent_id="a-1", model="m", service_account_id=None,
        channel="api", config={"storage_bucket": "b", "storage_key_prefix": "p", "workspace_path": "/workspace",
                               **config},
    )


async def test_a_sub_agent_lives_in_its_parents_day():
    from surogates.session.provisioning import create_child_session

    child = await create_child_session(store=_Store(), parent=_parent({"current_date": "2026-02-12"}),
                                       channel="delegation", config={"max_iterations": 30})
    assert child.config["current_date"] == "2026-02-12"
    own = await create_child_session(store=_Store(), parent=_parent({"current_date": "2026-02-12"}),
                                     channel="delegation", config={"current_date": "2026-03-26"})
    assert own.config["current_date"] == "2026-03-26"
    plain = await create_child_session(store=_Store(), parent=_parent({}), channel="delegation", config={})
    assert "current_date" not in plain.config
