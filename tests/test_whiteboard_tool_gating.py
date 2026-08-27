"""whiteboard_draw is visible iff the session is a whiteboard surface.

Both surfaces are asserted here on purpose: the prompt surface
(worker._filter_effective_tools) and the schema surface
(harness.tool_schemas.drop_unusable_tools) have to agree, and they live
in different modules with no shared call site.
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


# --- prompt surface ---------------------------------------------------

def test_prompt_surface_strips_the_tool_off_a_plain_session():
    result = _filter_effective_tools(
        tools={"whiteboard_draw", "memory"},
        tenant=_tenant(),
        session=_session(),
        use_api_for_harness_tools=True,
    )
    assert "whiteboard_draw" not in result
    assert "memory" in result


def test_prompt_surface_force_adds_the_tool_on_a_whiteboard():
    # Force-added even under a restrictive AgentDef allowlist, matching
    # the worker_* / board self-tool idiom: a whiteboard session with no
    # way to draw is not a whiteboard.
    result = _filter_effective_tools(
        tools={"memory"},
        tenant=_tenant(),
        session=_session(config={"surface": "whiteboard"}),
        use_api_for_harness_tools=True,
    )
    assert "whiteboard_draw" in result


# --- schema surface ---------------------------------------------------

def test_schema_surface_drops_the_tool_off_a_plain_session():
    kept = drop_unusable_tools(
        _schemas("whiteboard_draw", "memory"),
        has_kbs=True, has_channel=True, is_scheduled=True,
        is_whiteboard=False,
    )
    assert _names(kept) == {"memory"}


def test_schema_surface_keeps_the_tool_on_a_whiteboard():
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
    # The new keyword is optional so existing callers keep compiling;
    # the default must be the safe one (drop), not the permissive one.
    kept = drop_unusable_tools(
        _schemas("whiteboard_draw", "memory"),
        has_kbs=True, has_channel=True, is_scheduled=True,
    )
    assert _names(kept) == {"memory"}


# --- the two surfaces agree ------------------------------------------

def test_both_surfaces_agree_on_a_plain_session():
    session = _session()
    prompt = _filter_effective_tools(
        tools={"whiteboard_draw", "memory"},
        tenant=_tenant(), session=session, use_api_for_harness_tools=True,
    )
    schema = _names(drop_unusable_tools(
        _schemas("whiteboard_draw", "memory"),
        has_kbs=True, has_channel=True, is_scheduled=True,
        is_whiteboard=False,
    ))
    assert ("whiteboard_draw" in prompt) == ("whiteboard_draw" in schema)


def test_both_surfaces_agree_on_a_whiteboard_session():
    session = _session(config={"surface": "whiteboard"})
    prompt = _filter_effective_tools(
        tools={"whiteboard_draw", "memory"},
        tenant=_tenant(), session=session, use_api_for_harness_tools=True,
    )
    schema = _names(drop_unusable_tools(
        _schemas("whiteboard_draw", "memory"),
        has_kbs=True, has_channel=True, is_scheduled=True,
        is_whiteboard=True,
    ))
    assert ("whiteboard_draw" in prompt) == ("whiteboard_draw" in schema)
