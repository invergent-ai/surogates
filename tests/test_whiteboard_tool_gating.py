"""whiteboard_draw is visible iff this is a board session on an agent
that has the board capability.

Both surfaces are asserted here on purpose: the prompt surface
(worker._filter_effective_tools) and the schema surface
(harness.tool_schemas.drop_unusable_tools) have to agree, and they live
in different modules with no shared call site.

Two conditions, not one. The stamp is fixed at creation, so the agent
capability has to be checked as well or revoking the board would leave
every board that already exists still drawing. What varies per *turn* is
only the speed -- see ``test_whiteboard_turn_mode``.
"""
from types import SimpleNamespace

from surogates.harness.tool_schemas import drop_unusable_tools
from surogates.orchestrator.worker import _filter_effective_tools


def _tenant():
    return SimpleNamespace(org_id="o", user_id="u", service_account_id=None)


def _session(config=None, channel="web"):
    return SimpleNamespace(
        config=config or {}, channel=channel,
        service_account_id=None, task_id=None,
    )


def _schemas(*names):
    return [{"function": {"name": n}} for n in names]


def _names(schemas):
    return {s["function"]["name"] for s in schemas}


BOARD = {"surface": "whiteboard"}


def _prompt_surface(
    *, whiteboard_enabled, config=None, tools=("whiteboard_draw", "memory"),
):
    return _filter_effective_tools(
        tools=set(tools),
        tenant=_tenant(),
        session=_session(config=BOARD if config is None else config),
        use_api_for_harness_tools=True,
        whiteboard_enabled=whiteboard_enabled,
    )


# --- prompt surface ---------------------------------------------------

def test_prompt_surface_strips_the_tool_from_a_plain_session():
    result = _prompt_surface(whiteboard_enabled=True, config={})
    assert "whiteboard_draw" not in result
    assert "memory" in result


def test_prompt_surface_force_adds_the_tool_on_a_board_session():
    # Force-added even under a restrictive AgentDef allowlist, matching
    # the worker_* / board self-tool idiom: a whiteboard session that
    # cannot draw is not a whiteboard.
    result = _prompt_surface(whiteboard_enabled=True, tools=("memory",))
    assert "whiteboard_draw" in result


def test_a_board_session_loses_the_tool_when_the_capability_is_revoked():
    # The stamp is fixed at creation, so this is the only way an operator
    # can take the board away from a session that already has one.
    result = _prompt_surface(whiteboard_enabled=False)
    assert "whiteboard_draw" not in result


# --- schema surface ---------------------------------------------------

def test_schema_surface_drops_the_tool_when_the_board_is_off():
    kept = drop_unusable_tools(
        _schemas("whiteboard_draw", "memory"),
        has_kbs=True, has_channel=True, is_scheduled=True,
        is_whiteboard=False,
    )
    assert _names(kept) == {"memory"}


def test_schema_surface_keeps_the_tool_when_the_board_is_on():
    kept = drop_unusable_tools(
        _schemas("whiteboard_draw", "memory"),
        has_kbs=True, has_channel=True, is_scheduled=True,
        is_whiteboard=True,
    )
    assert _names(kept) == {"whiteboard_draw", "memory"}


def test_schema_surface_never_returns_an_empty_list():
    # Existing contract: a request with no tools at all is worse than an
    # oversized one.
    kept = drop_unusable_tools(
        _schemas("whiteboard_draw"),
        has_kbs=True, has_channel=True, is_scheduled=True,
        is_whiteboard=False,
    )
    assert _names(kept) == {"whiteboard_draw"}


def test_schema_surface_defaults_to_dropping_the_tool():
    # The keyword is optional so existing callers keep compiling; the
    # default must be the safe one (drop), not the permissive one.
    kept = drop_unusable_tools(
        _schemas("whiteboard_draw", "memory"),
        has_kbs=True, has_channel=True, is_scheduled=True,
    )
    assert _names(kept) == {"memory"}


# --- the two surfaces agree ------------------------------------------

def _both(whiteboard_enabled):
    """The tool set as each surface independently computes it."""
    prompt = _prompt_surface(whiteboard_enabled=whiteboard_enabled)
    # The harness reads ``has_whiteboard`` off the prompt builder it was
    # handed, which is built from exactly this set -- that shared fact is
    # what stops the two surfaces drifting.
    has_whiteboard = "whiteboard_draw" in prompt
    schema = _names(drop_unusable_tools(
        _schemas("whiteboard_draw", "memory"),
        has_kbs=True, has_channel=True, is_scheduled=True,
        is_whiteboard=has_whiteboard,
    ))
    return prompt, schema


def test_both_surfaces_agree_when_the_board_is_off():
    prompt, schema = _both(False)
    assert ("whiteboard_draw" in prompt) == ("whiteboard_draw" in schema)
    assert "whiteboard_draw" not in schema


def test_both_surfaces_agree_when_the_board_is_on():
    prompt, schema = _both(True)
    assert ("whiteboard_draw" in prompt) == ("whiteboard_draw" in schema)
    assert "whiteboard_draw" in schema


def test_the_prompt_builder_derives_the_flag_the_harness_reads():
    """The join between the two surfaces.

    ``loop.py`` passes ``self._prompt.has_whiteboard`` into
    ``drop_unusable_tools``. Derived from ``available_tools`` -- the very
    set the prompt surface produced -- so the prose contract and the
    model-visible schema are decided by one fact instead of two that can
    disagree, which is how the old ``config.surface`` gate drifted.
    """
    from uuid import uuid4

    from surogates.harness.prompt import PromptBuilder
    from surogates.tenant.context import TenantContext

    def _builder(tools):
        return PromptBuilder(
            TenantContext(
                org_id=uuid4(),
                user_id=uuid4(),
                org_config={"default_model": "gpt-4o"},
                user_preferences={},
                permissions=frozenset(),
                asset_root="/tmp/test_assets",
            ),
            session=_session(),
            available_tools=tools,
        )

    assert _builder({"whiteboard_draw", "memory"}).has_whiteboard is True
    assert _builder({"memory"}).has_whiteboard is False
