"""The harness half of the whiteboard surface, composed.

Each piece has its own unit test; this asserts they agree with each
other on one session -- the tool catalogue, the system prompt, the
rendered user message and the replay pruning all keying off the same
``config.surface`` stamp.

Deliberately not under ``tests/integration``: that package's conftest
spins up real Postgres and Redis containers via testcontainers, and
nothing here needs a database.
"""
from types import SimpleNamespace
from uuid import uuid4

from surogates.harness.loop import (
    _latest_whiteboard_metadata,
    _whiteboard_sketch_filter,
)
from surogates.harness.loop_context_replay import (
    build_user_message_dict,
    prune_superseded_canvas_images,
)
from surogates.harness.prompt import PromptBuilder
from surogates.harness.tool_schemas import drop_unusable_tools
from surogates.orchestrator.worker import _filter_effective_tools
from surogates.session.events import EventType
from surogates.tenant.context import TenantContext
from surogates.tools.registry import ToolRegistry
from surogates.tools.runtime import ToolRuntime

WHITEBOARD_CONFIG = {"surface": "whiteboard"}
PLAIN_CONFIG: dict = {}

CATALOGUE = {"whiteboard_draw", "create_artifact", "web_search", "memory"}


def _tenant():
    return TenantContext(
        org_id=uuid4(),
        user_id=uuid4(),
        org_config={"default_model": "gpt-4o"},
        user_preferences={},
        permissions=frozenset(),
        asset_root="/tmp/test_assets",
    )


def _session(config):
    return SimpleNamespace(
        config=config, channel="web", model=None,
        service_account_id=None, task_id=None,
    )


def _atlas_metadata(mode):
    return {"whiteboard": {
        "sourceRect": {"x": 1000, "y": 2000, "w": 1600, "h": 1200},
        "imageScale": 0.5,
        "latestInput": {"x": 1200, "y": 2100, "w": 300, "h": 200},
        "hotspots": [[3, 4], [3, 5]],
        "infinite": True,
        "mode": mode,
    }}


def _turn_events(mode):
    return [SimpleNamespace(
        type=EventType.USER_MESSAGE.value,
        data={"content": "what is this", "metadata": _atlas_metadata(mode)},
    )]


def _effective_tools(config):
    """The tool set as both surfaces agree on it, for one session."""
    session = _session(config)
    prompt_surface = _filter_effective_tools(
        tools=set(CATALOGUE),
        tenant=SimpleNamespace(org_id="o", user_id="u", service_account_id=None),
        session=session,
        use_api_for_harness_tools=True,
    )
    schema_surface = {
        s["function"]["name"]
        for s in drop_unusable_tools(
            [{"function": {"name": n}} for n in sorted(CATALOGUE)],
            has_kbs=True, has_channel=True, is_scheduled=True,
            is_whiteboard=config.get("surface") == "whiteboard",
        )
    }
    return prompt_surface, schema_surface


# --- 1. the tool reaches a whiteboard session ------------------------

def test_a_whiteboard_session_gets_the_draw_tool_on_both_surfaces():
    prompt_surface, schema_surface = _effective_tools(WHITEBOARD_CONFIG)
    assert "whiteboard_draw" in prompt_surface
    assert "whiteboard_draw" in schema_surface


def test_a_plain_session_gets_it_on_neither_surface():
    prompt_surface, schema_surface = _effective_tools(PLAIN_CONFIG)
    assert "whiteboard_draw" not in prompt_surface
    assert "whiteboard_draw" not in schema_surface


# --- 2/3. the two speeds ---------------------------------------------

def test_a_sketch_turn_leaves_exactly_one_tool():
    session = _session(WHITEBOARD_CONFIG)
    narrowed = _whiteboard_sketch_filter(
        set(CATALOGUE), session,
        _latest_whiteboard_metadata(_turn_events("sketch")),
    )
    assert narrowed == {"whiteboard_draw"}


def test_a_deep_turn_keeps_the_full_catalogue():
    session = _session(WHITEBOARD_CONFIG)
    narrowed = _whiteboard_sketch_filter(
        set(CATALOGUE), session,
        _latest_whiteboard_metadata(_turn_events("deep")),
    )
    assert narrowed == CATALOGUE
    # place_artifact is only useful once create_artifact has run, so the
    # deep path is the one that can author a widget.
    assert "create_artifact" in narrowed


def test_a_plain_session_is_never_narrowed():
    narrowed = _whiteboard_sketch_filter(
        set(CATALOGUE), _session(PLAIN_CONFIG),
        _latest_whiteboard_metadata(_turn_events("sketch")),
    )
    assert narrowed == CATALOGUE


# --- 4. the system prompt --------------------------------------------

def _prompt(config):
    return PromptBuilder(
        _tenant(),
        session=_session(config),
        available_tools={"whiteboard_draw"},
    ).build()


def test_the_whiteboard_prompt_carries_the_canvas_contract():
    prompt = _prompt(WHITEBOARD_CONFIG)
    assert "Whiteboard canvas" in prompt
    assert "sourceRect" in prompt
    assert "infinite" in prompt


def test_a_plain_prompt_does_not():
    assert "Whiteboard canvas" not in _prompt(PLAIN_CONFIG)


# --- 5. the rendered user message ------------------------------------

def test_the_user_message_carries_both_the_note_and_the_image():
    msg = build_user_message_dict({
        "content": "what is this",
        "metadata": _atlas_metadata("sketch"),
        "images": [{"data": "AAAA", "mime_type": "image/png"}],
    })
    blocks = msg["content"]
    assert isinstance(blocks, list)
    text = "".join(
        b.get("text", "") for b in blocks
        if isinstance(b, dict) and b.get("type") == "text"
    )
    assert "sourceRect" in text
    assert "latestInput" in text
    assert "what is this" in text
    assert any(b.get("type") == "image_url" for b in blocks)


# --- 6. cumulative snapshots collapse to one --------------------------

def test_a_multi_turn_board_replays_exactly_one_canvas_image():
    def turn(n):
        return build_user_message_dict({
            "content": f"turn {n}",
            "metadata": _atlas_metadata("sketch"),
            "images": [{"data": f"AAA{n}", "mime_type": "image/png"}],
        })

    pruned = prune_superseded_canvas_images([turn(1), turn(2), turn(3)])
    images = [
        part
        for message in pruned
        for part in message["content"]
        if isinstance(part, dict) and part.get("type") == "image_url"
    ]
    assert len(images) == 1
    assert "AAA3" in images[0]["image_url"]["url"]


# --- 7. the canvas really is unbounded -------------------------------

def test_a_command_list_in_negative_space_is_accepted():
    """The origin is arbitrary: a board drawn up and to the left of it is
    ordinary, not an error."""
    import asyncio

    runtime = ToolRuntime(ToolRegistry())
    runtime.register_builtins()
    result = asyncio.run(runtime.dispatch("whiteboard_draw", {
        "commands": [
            {"tool": "write_text", "x": -4200, "y": -9100, "text": "5",
             "fontSize": 32, "maxWidth": 300},
            {"tool": "draw", "origin": [-4200, -9000],
             "types": ["circle"], "items": [[0, 0, 40]]},
        ],
    }))
    assert not result.startswith("Error:")


def test_the_note_renders_negative_geometry():
    msg = build_user_message_dict({
        "content": "what is this",
        "metadata": {"whiteboard": {
            "sourceRect": {"x": -9000, "y": -7000, "w": 1600, "h": 1200},
            "imageScale": 0.5,
            "latestInput": {"x": -8800, "y": -6900, "w": 300, "h": 200},
            "mode": "sketch",
        }},
    })
    assert "-9000" in msg["content"]
    assert "-8800" in msg["content"]


# --- 8. a real command list survives the tool ------------------------

def test_a_valid_command_list_is_accepted_by_the_registered_tool():
    import asyncio

    runtime = ToolRuntime(ToolRegistry())
    runtime.register_builtins()
    result = asyncio.run(runtime.dispatch("whiteboard_draw", {
        "commands": [
            {"tool": "write_text", "x": 1500, "y": 2400, "text": "5",
             "fontSize": 32, "maxWidth": 300},
            {"tool": "draw", "origin": [1500, 2500],
             "types": ["circle"], "items": [[0, 0, 40]]},
        ],
    }))
    assert not result.startswith("Error:")
    assert "2" in result


def test_an_invalid_command_list_comes_back_as_a_precise_error():
    import asyncio

    runtime = ToolRuntime(ToolRegistry())
    runtime.register_builtins()
    result = asyncio.run(runtime.dispatch("whiteboard_draw", {
        "commands": [{"tool": "draw", "origin": [0, 0],
                      "types": ["line", "rect"], "items": [[0, 0, 1, 1]]}],
    }))
    assert "same length" in result
