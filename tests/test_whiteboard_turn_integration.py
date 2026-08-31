"""The harness half of the whiteboard surface, composed.

Each piece has its own unit test; this asserts they agree with each
other -- the tool catalogue and system prompt keying off the session's
``config.surface`` stamp, the turn's speed and the rendered user message
off the turn's own canvas metadata.

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

BOARD = {"surface": "whiteboard"}
PLAIN: dict = {}

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


def _effective_tools(config, *, whiteboard_enabled=True):
    """The tool set as both surfaces independently compute it."""
    prompt_surface = _filter_effective_tools(
        tools=set(CATALOGUE),
        tenant=SimpleNamespace(org_id="o", user_id="u", service_account_id=None),
        session=_session(config),
        use_api_for_harness_tools=True,
        whiteboard_enabled=whiteboard_enabled,
    )
    # What the harness passes to ``drop_unusable_tools``: the flag the
    # prompt builder derived from the very set above.
    has_whiteboard = PromptBuilder(
        _tenant(), session=_session(config), available_tools=prompt_surface,
    ).has_whiteboard
    schema_surface = {
        s["function"]["name"]
        for s in drop_unusable_tools(
            [{"function": {"name": n}} for n in sorted(CATALOGUE)],
            has_kbs=True, has_channel=True, is_scheduled=True,
            is_whiteboard=has_whiteboard,
        )
    }
    return prompt_surface, schema_surface


# --- 1. the tool reaches a board session -----------------------------

def test_a_board_session_gets_the_draw_tool_on_both_surfaces():
    prompt_surface, schema_surface = _effective_tools(BOARD)
    assert "whiteboard_draw" in prompt_surface
    assert "whiteboard_draw" in schema_surface


def test_a_plain_session_gets_it_on_neither_surface():
    prompt_surface, schema_surface = _effective_tools(PLAIN)
    assert "whiteboard_draw" not in prompt_surface
    assert "whiteboard_draw" not in schema_surface


def test_revoking_the_capability_takes_it_off_an_existing_board():
    prompt_surface, schema_surface = _effective_tools(
        BOARD, whiteboard_enabled=False,
    )
    assert "whiteboard_draw" not in prompt_surface
    assert "whiteboard_draw" not in schema_surface


# --- 2/3. the two speeds ---------------------------------------------

def test_a_sketch_turn_leaves_exactly_one_tool():
    narrowed = _whiteboard_sketch_filter(
        set(CATALOGUE),
        _latest_whiteboard_metadata(_turn_events("sketch")),
        has_whiteboard=True,
    )
    assert narrowed == {"whiteboard_draw"}


def test_a_deep_turn_keeps_the_full_catalogue():
    narrowed = _whiteboard_sketch_filter(
        set(CATALOGUE),
        _latest_whiteboard_metadata(_turn_events("deep")),
        has_whiteboard=True,
    )
    assert narrowed == CATALOGUE
    # place_artifact is only useful once create_artifact has run, so the
    # deep path is the one that can author a widget.
    assert "create_artifact" in narrowed


def test_a_typed_message_in_a_board_session_is_never_narrowed():
    # A board session also has a transcript view the user can type into.
    # Narrowing that turn to whiteboard_draw would leave the agent unable
    # to answer anything.
    plain = [SimpleNamespace(
        type=EventType.USER_MESSAGE.value,
        data={"content": "what is this", "metadata": {}},
    )]
    narrowed = _whiteboard_sketch_filter(
        set(CATALOGUE), _latest_whiteboard_metadata(plain),
        has_whiteboard=True,
    )
    assert narrowed == CATALOGUE


# --- 4. the system prompt --------------------------------------------

def _prompt(tools):
    return PromptBuilder(
        _tenant(), session=_session({}), available_tools=tools,
    ).build()


def test_a_board_session_prompt_carries_the_canvas_contract():
    prompt = _prompt({"whiteboard_draw"})
    assert "Whiteboard canvas" in prompt
    assert "latestInput" in prompt
    assert "infinite" in prompt


def test_an_agent_without_the_tool_gets_no_canvas_contract():
    # Prose and schema are decided by one fact: no tool, no contract.
    assert "Whiteboard canvas" not in _prompt({"web_search"})


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


def test_an_uploaded_image_is_never_pruned_as_a_stale_canvas():
    """The board shares a session with ordinary chat.

    Canvas renders are cumulative and safe to collapse; an image the
    user dragged in is not, and nothing supersedes it. Matching every
    image would silently replace the user's own attachment with a
    placeholder in any session that had also drawn.
    """
    upload = build_user_message_dict({
        "content": "and what about this screenshot",
        "images": [{"data": "UPLOAD", "mime_type": "image/png"}],
    })
    board = build_user_message_dict({
        "content": "turn 1",
        "metadata": _atlas_metadata("sketch"),
        "images": [{"data": "AAA1", "mime_type": "image/png"}],
    })
    later = build_user_message_dict({
        "content": "turn 2",
        "metadata": _atlas_metadata("sketch"),
        "images": [{"data": "AAA2", "mime_type": "image/png"}],
    })

    urls = [
        part["image_url"]["url"]
        for message in prune_superseded_canvas_images([board, upload, later])
        for part in message["content"]
        if isinstance(part, dict) and part.get("type") == "image_url"
    ]
    assert any("UPLOAD" in url for url in urls)
    assert not any("AAA1" in url for url in urls)
    assert any("AAA2" in url for url in urls)


def test_a_session_that_never_drew_is_untouched():
    uploads = [
        build_user_message_dict({
            "content": f"image {n}",
            "images": [{"data": f"IMG{n}", "mime_type": "image/png"}],
        })
        for n in (1, 2, 3)
    ]
    assert prune_superseded_canvas_images(uploads) == uploads


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
    # A board panned far into negative space: the rects the note still
    # carries have to survive the round trip with their signs intact.
    msg = build_user_message_dict({
        "content": "what is this",
        "metadata": {"whiteboard": {
            "sourceRect": {"x": -9000, "y": -7000, "w": 1600, "h": 1200},
            "imageScale": 0.5,
            "latestInput": {"x": -8800, "y": -6900, "w": 300, "h": 200},
            "mode": "sketch",
        }},
    })
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
