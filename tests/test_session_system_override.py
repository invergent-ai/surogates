"""``POST /v1/sessions {"system": ...}`` must actually reach the model.

The field is declared on `CreateSessionRequest` (api/routes/sessions.py:85),
persisted into `session.config["system"]` (:414-416), forwarded verbatim by
surogate-ops, exposed on the SDK's `createSession`, listed in the public API
reference, and asserted by a round-trip test.

Nothing read it. `_build_system_prompt` calls `self._prompt.build()` and
appends only the repos and ssh sections; `session.config` never enters. A
caller got a 201, could read the value back off the session row, and every
turn ran on the agent's default prompt with no error, warning or log line --
which is exactly why debugging went to the model rather than to the dropped
field.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.harness.loop import AgentHarness
from surogates.harness.prompt_cache import SystemPromptCache


def _harness(prompt_text: str = "BASE PROMPT") -> AgentHarness:
    h = AgentHarness.__new__(AgentHarness)
    h._prompt = SimpleNamespace(build=lambda: prompt_text)
    h._system_prompt_cache = SystemPromptCache()
    h._coding_repos = []
    h._ssh_targets = []
    return h


def _session(config: dict | None) -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), config=config)


@pytest.mark.asyncio
async def test_the_override_reaches_the_prompt():
    h = _harness()
    prompt = await h._build_system_prompt(
        _session({"system": "Answer only in SQL. Never write prose."}),
    )
    assert "Answer only in SQL. Never write prose." in prompt


@pytest.mark.asyncio
async def test_the_agents_own_prompt_is_kept():
    """Append, never replace.

    Replacing would strip the tool contract and behavioural rules the harness
    depends on -- the session-level field narrows behaviour, it does not
    redefine the agent.
    """
    h = _harness("BASE PROMPT WITH TOOL RULES")
    prompt = await h._build_system_prompt(_session({"system": "Be terse."}))
    assert "BASE PROMPT WITH TOOL RULES" in prompt
    assert prompt.index("BASE PROMPT WITH TOOL RULES") < prompt.index("Be terse.")


@pytest.mark.asyncio
async def test_no_override_leaves_the_prompt_untouched():
    h = _harness()
    assert await h._build_system_prompt(_session({})) == "BASE PROMPT"


@pytest.mark.asyncio
async def test_missing_config_is_safe():
    h = _harness()
    assert await h._build_system_prompt(_session(None)) == "BASE PROMPT"


@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
@pytest.mark.asyncio
async def test_blank_overrides_are_ignored(blank):
    """A whitespace-only value must not append an empty section."""
    h = _harness()
    assert await h._build_system_prompt(_session({"system": blank})) == "BASE PROMPT"


@pytest.mark.asyncio
async def test_non_string_override_is_ignored():
    """config is caller-supplied JSON; a wrong type must not crash the wake."""
    h = _harness()
    assert await h._build_system_prompt(_session({"system": {"a": 1}})) == "BASE PROMPT"


@pytest.mark.asyncio
async def test_two_sessions_do_not_share_an_override():
    """The cache is keyed by session id; prove the override rides along."""
    h = _harness()
    a = await h._build_system_prompt(_session({"system": "AAA"}))
    b = await h._build_system_prompt(_session({"system": "BBB"}))
    assert "AAA" in a and "BBB" not in a
    assert "BBB" in b and "AAA" not in b
