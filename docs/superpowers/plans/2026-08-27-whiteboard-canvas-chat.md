# Whiteboard Canvas Chat Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a third chat type — a vector canvas the agent reads as an image and writes to through a `whiteboard_draw` tool — to the Surogates harness, the agent-chat-react SDK, the agent web app, and the ops Work console.

**Architecture:** A whiteboard session is an ordinary session stamped `config.surface == "whiteboard"`. The client renders the canvas to a white-background PNG atlas and sends it as an image attachment with geometry in the free-form `metadata.whiteboard` field; the harness renders that geometry into a transient system note and loads a whiteboard guidance fragment. The agent replies by calling `whiteboard_draw` with a command list, which the SDK renders straight off the `tool.call` event. The canvas is a list of vector objects, so move/resize/delete/undo are one implementation and the agent's output arrives as the active selection. PenEcho's zero-dependency geometry modules and system prompt are vendored; its widget/plugin layer is replaced by our existing artifacts subsystem.

**Tech Stack:** Python 3.12 / pytest / SQLAlchemy async (surogates harness) · React 19 / TypeScript / vitest + happy-dom / tsup (agent-chat-react SDK) · TanStack Router (web app) · Alembic + FastAPI (surogate-ops)

**Spec:** `docs/superpowers/specs/2026-08-27-whiteboard-canvas-chat-design.md`

## Global Constraints

- Logical canvas is **20000 x 20000**. Every coordinate a command carries is a global logical coordinate, never an image coordinate.
- Atlas image caps: **`MAX_ATLAS_WIDTH = 2048`, `MAX_ATLAS_HEIGHT = 1536`** (PenEcho's values).
- `draw` command limits, copied from `study/penecho/public/draw.js:7-11`: **`MAX_ITEMS = 64`**, **`MAX_VALUES = 2048`** total coordinate values across all items, `width` integer **2..200**, `tension` integer **0..100**.
- A `whiteboard_draw` call carries at most **16 commands** (PenEcho's `maxItems` on its response schema).
- `metadata.whiteboard` is capped at **65_536 bytes** of serialised JSON, rejected with HTTP 422 at the send-message route.
- Whiteboard sessions are identified **only** by `config.surface == "whiteboard"`. Never add a `channel` value — see the spec's "Session shape" for why.
- Package name is `surogates/whiteboard/`. `surogates/board/` is the unrelated multi-agent coordination board; do not touch it.
- Context replay keeps **exactly one** whiteboard image (`keep_last = 1`).
- Vendored PenEcho files keep their original copyright headers verbatim. Both projects are AGPL-3.0-only.
- Branch per change; every commit follows Conventional Commits (`type(scope): subject`). **No `Co-Authored-By` trailer.**
- Never reference plan/task/phase numbers in code comments or commit messages.
- surogates base branch is `master`; surogate-ops base branch is `main`.
- Do not run `uv run` in surogate-ops — it reinstalls the pinned `surogates` wheel over the local dev install.

---

## File Structure

### surogates (branch `feat/whiteboard-canvas-chat`, base `master`)

| Path | Responsibility |
| --- | --- |
| `surogates/whiteboard/__init__.py` | Public exports: `WHITEBOARD_TOOLS`, `validate_commands`, `is_whiteboard_session` |
| `surogates/whiteboard/commands.py` | Pure command-list validation. No I/O, no DB. |
| `surogates/whiteboard/session.py` | `is_whiteboard_session(session)` and `turn_mode(metadata)` — the two predicates every other module asks. |
| `surogates/tools/builtin/whiteboard.py` | The `whiteboard_draw` tool: schema, registration, handler. |
| `surogates/tools/router.py` | Add the `HARNESS` routing entry. |
| `surogates/orchestrator/worker.py` | Prompt-surface tool gating. |
| `surogates/harness/tool_schemas.py` | Schema-surface tool gating. |
| `surogates/harness/prompts/guidance/whiteboard.md` | The ported PenEcho system prompt. |
| `surogates/harness/prompt.py` | Load the guidance fragment for whiteboard sessions. |
| `surogates/harness/loop_messages.py` | Render `metadata.whiteboard` into a per-turn system note. |
| `surogates/harness/loop_context_replay.py` | Prune superseded canvas images. |
| `surogates/harness/loop.py` | Thread the turn's `sketch`/`deep` mode into the tool filter. |
| `surogates/api/routes/sessions.py` | Cap `metadata.whiteboard`. |
| `sdk/agent-chat-react/src/vendor/penecho/` | Vendored `draw.js`, `mixed-text.js`, `selection.js` + `README.md` |
| `sdk/agent-chat-react/src/components/whiteboard/doc.ts` | Canvas document type, event fold, undo stack. |
| `sdk/agent-chat-react/src/components/whiteboard/render.ts` | Objects -> Canvas2D. |
| `sdk/agent-chat-react/src/components/whiteboard/atlas.ts` | Atlas + hotspot-grid builder. |
| `sdk/agent-chat-react/src/components/whiteboard/input.ts` | Pointer ink, pan, zoom, hit-testing. |
| `sdk/agent-chat-react/src/components/whiteboard/persist.ts` | Workspace load/save + event-tail reconcile. |
| `sdk/agent-chat-react/src/components/whiteboard/agent-whiteboard.tsx` | The component. |
| `sdk/agent-chat-react/src/components/whiteboard/tool-rail.tsx` | Tool rail UI. |
| `sdk/agent-chat-react/src/index.ts` | Export `AgentWhiteboard`. |
| `web/src/app/routes/whiteboard.tsx` | Route. |
| `web/src/features/whiteboard/whiteboard-page.tsx` | Page. |

### surogate-ops (branch `feat/whiteboard-capability`, base `main`)

| Path | Responsibility |
| --- | --- |
| `surogate_ops/core/db/models/operate.py` | `agents.whiteboard_enabled` column. |
| `surogate_ops/core/db/migrations/versions/<rev>_agent_whiteboard_enabled.py` | Migration. |
| `surogate_ops/server/models/agent.py` | Request/response models. |
| `surogate_ops/server/models/agent_runtime.py` | Runtime-config projection. |
| `frontend/src/features/work/work-agent-settings-page.tsx` | Capability toggle. |
| `frontend/src/features/work/work-agent-whiteboard-page.tsx` | The whiteboard page. |

---

# Phase A — Harness

## Task 1: Command validation

**Files:**
- Create: `surogates/whiteboard/__init__.py`
- Create: `surogates/whiteboard/commands.py`
- Test: `tests/test_whiteboard_commands.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `validate_commands(commands: list[Any]) -> str | None` — returns an error string, or `None` when the list is valid. `CANVAS_SIZE: int`, `MAX_COMMANDS: int`, `WHITEBOARD_TOOLS: frozenset[str]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_whiteboard_commands.py`:

```python
"""Whiteboard command-list validation.

Mirrors the structural rules PenEcho enforces in
``study/penecho/public/draw.js:123`` (``normalize``) and its server-side
response validator.  This is a cheap guard so a malformed command list
becomes a model retry instead of a silently-dropped object; the SDK's
vendored ``draw.js`` remains authoritative for rendering geometry.
"""
import pytest

from surogates.whiteboard.commands import (
    CANVAS_SIZE,
    MAX_COMMANDS,
    validate_commands,
)


def _text(**over):
    base = {
        "tool": "write_text", "x": 100, "y": 200, "text": "hi",
        "fontSize": 32, "maxWidth": 400, "lineHeight": 1.35,
    }
    base.update(over)
    return base


def test_accepts_a_minimal_valid_list():
    assert validate_commands([_text()]) is None


def test_rejects_an_empty_list():
    assert "at least one command" in (validate_commands([]) or "")


def test_rejects_more_than_max_commands():
    err = validate_commands([_text()] * (MAX_COMMANDS + 1))
    assert str(MAX_COMMANDS) in (err or "")


def test_rejects_an_unknown_tool():
    err = validate_commands([{"tool": "summon_dragon"}])
    assert "summon_dragon" in (err or "")


def test_rejects_a_missing_tool_key():
    assert "tool" in (validate_commands([{"x": 1, "y": 2}]) or "")


def test_rejects_coordinates_outside_the_canvas():
    err = validate_commands([_text(x=CANVAS_SIZE + 1)])
    assert "canvas" in (err or "").lower()


def test_rejects_negative_coordinates():
    assert validate_commands([_text(y=-1)]) is not None


def test_rejects_write_text_without_maxwidth():
    bad = _text()
    del bad["maxWidth"]
    assert "maxWidth" in (validate_commands([bad]) or "")


def test_draw_requires_equal_length_types_and_items():
    err = validate_commands([{
        "tool": "draw", "origin": [0, 0],
        "types": ["line", "rect"], "items": [[0, 0, 10, 10]],
    }])
    assert "same length" in (err or "")


def test_draw_rejects_an_unknown_primitive_type():
    err = validate_commands([{
        "tool": "draw", "origin": [0, 0],
        "types": ["squiggle"], "items": [[0, 0, 1, 1]],
    }])
    assert "squiggle" in (err or "")


def test_draw_rejects_too_many_items():
    err = validate_commands([{
        "tool": "draw", "origin": [0, 0],
        "types": ["rect"] * 65, "items": [[0, 0, 1, 1]] * 65,
    }])
    assert "64" in (err or "")


def test_draw_rejects_too_many_total_values():
    # 40 items x 60 values = 2400 > MAX_VALUES (2048)
    err = validate_commands([{
        "tool": "draw", "origin": [0, 0],
        "types": ["line"] * 40, "items": [[0] * 60] * 40,
    }])
    assert "2048" in (err or "")


def test_draw_rejects_width_out_of_range():
    err = validate_commands([{
        "tool": "draw", "origin": [0, 0], "types": ["rect"],
        "items": [[0, 0, 1, 1]], "width": 500,
    }])
    assert "width" in (err or "")


def test_erase_accepts_rect_and_path_modes():
    assert validate_commands([
        {"tool": "erase", "mode": "rect", "x": 0, "y": 0, "w": 10, "h": 10},
    ]) is None
    assert validate_commands([
        {"tool": "erase", "mode": "path", "points": [[0, 0], [5, 5]], "size": 20},
    ]) is None


def test_erase_rejects_an_unknown_mode():
    err = validate_commands([{"tool": "erase", "mode": "vanish"}])
    assert "vanish" in (err or "")


def test_place_artifact_requires_an_artifact_id():
    err = validate_commands([
        {"tool": "place_artifact", "x": 0, "y": 0, "w": 100, "h": 100},
    ])
    assert "artifact_id" in (err or "")


def test_draw_formula_requires_latex():
    err = validate_commands([
        {"tool": "draw_formula", "x": 0, "y": 0, "fontSize": 40},
    ])
    assert "latex" in (err or "")


@pytest.mark.parametrize("bad", ["not a list", None, 42, {"tool": "write_text"}])
def test_rejects_a_non_list_payload(bad):
    assert validate_commands(bad) is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_whiteboard_commands.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'surogates.whiteboard'`

- [ ] **Step 3: Write the implementation**

Create `surogates/whiteboard/commands.py`:

```python
"""Structural validation for a ``whiteboard_draw`` command list.

Pure functions, no I/O.  These rules are a port of the structural half of
PenEcho's ``normalize`` (``study/penecho/public/draw.js:123``) plus its
server-side response validator.  Deliberately structural only: geometry
(bounding boxes, curve extrema, arrowheads) is computed by the vendored
``draw.js`` in the browser, which remains authoritative for rendering.
The point of validating here is that a malformed list becomes a model
retry with a precise message instead of an object the client silently
drops.
"""
from __future__ import annotations

from typing import Any

#: Logical canvas edge.  Every command coordinate is a global logical
#: coordinate in ``[0, CANVAS_SIZE]``.
CANVAS_SIZE = 20_000

#: Commands per ``whiteboard_draw`` call.
MAX_COMMANDS = 16

#: ``draw`` limits, from ``draw.js:7-11``.
MAX_DRAW_ITEMS = 64
MAX_DRAW_VALUES = 2_048
DRAW_TYPES = frozenset({"line", "smooth", "rect", "ellipse", "circle", "arc"})

WHITEBOARD_TOOLS: frozenset[str] = frozenset({
    "write_text", "draw_formula", "draw", "erase", "place_artifact",
})

#: Required keys per command tool.  ``write_text`` requires ``maxWidth``
#: because the model owns layout: without an explicit wrap width the
#: client has to guess one, and the guess is wrong often enough that
#: PenEcho's prompt makes it mandatory too.
_REQUIRED: dict[str, tuple[str, ...]] = {
    "write_text": ("x", "y", "text", "fontSize", "maxWidth"),
    "draw_formula": ("x", "y", "latex", "fontSize"),
    "draw": ("origin", "types", "items"),
    "erase": ("mode",),
    "place_artifact": ("artifact_id", "x", "y", "w", "h"),
}

#: Keys whose value must be a coordinate inside the canvas.
_COORD_KEYS = frozenset({"x", "y", "w", "h", "maxWidth"})


def _in_canvas(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and 0 <= value <= CANVAS_SIZE


def _is_int(value: Any, lo: int, hi: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) \
        and lo <= value <= hi


def _validate_draw(cmd: dict[str, Any], idx: int) -> str | None:
    origin = cmd.get("origin")
    if not (isinstance(origin, list) and len(origin) == 2
            and all(_in_canvas(v) for v in origin)):
        return f"command[{idx}] draw: origin must be [x, y] inside the canvas."

    types, items = cmd.get("types"), cmd.get("items")
    if not isinstance(types, list) or not isinstance(items, list):
        return f"command[{idx}] draw: types and items must both be arrays."
    if not types:
        return f"command[{idx}] draw: types must not be empty."
    if len(types) != len(items):
        return (
            f"command[{idx}] draw: types and items must have the same "
            f"length (got {len(types)} and {len(items)})."
        )
    if len(types) > MAX_DRAW_ITEMS:
        return (
            f"command[{idx}] draw: at most {MAX_DRAW_ITEMS} items "
            f"(got {len(types)})."
        )

    total_values = 0
    for i, (kind, item) in enumerate(zip(types, items)):
        if kind not in DRAW_TYPES:
            valid = ", ".join(sorted(DRAW_TYPES))
            return (
                f"command[{idx}] draw: item {i} has unknown type "
                f"{kind!r}. Valid types: {valid}."
            )
        if not isinstance(item, list) or not item:
            return f"command[{idx}] draw: item {i} must be a non-empty array."
        # Item coordinates are offsets from origin, so they may be
        # negative; the magnitude bound is what matters.
        if not all(_is_int(v, -CANVAS_SIZE, CANVAS_SIZE) for v in item):
            return (
                f"command[{idx}] draw: item {i} values must be integers "
                f"within +/-{CANVAS_SIZE}."
            )
        total_values += len(item)
        if total_values > MAX_DRAW_VALUES:
            return (
                f"command[{idx}] draw: at most {MAX_DRAW_VALUES} coordinate "
                f"values across all items."
            )

    if "width" in cmd and not _is_int(cmd["width"], 2, 200):
        return f"command[{idx}] draw: width must be an integer 2..200."
    if "tension" in cmd and not _is_int(cmd["tension"], 0, 100):
        return f"command[{idx}] draw: tension must be an integer 0..100."
    return None


def _validate_erase(cmd: dict[str, Any], idx: int) -> str | None:
    mode = cmd.get("mode")
    if mode == "rect":
        missing = [k for k in ("x", "y", "w", "h") if k not in cmd]
        if missing:
            return (
                f"command[{idx}] erase mode=rect requires "
                f"{', '.join(missing)}."
            )
        return None
    if mode == "path":
        points = cmd.get("points")
        if not (isinstance(points, list) and points
                and all(isinstance(p, list) and len(p) == 2
                        and all(_in_canvas(v) for v in p) for p in points)):
            return (
                f"command[{idx}] erase mode=path requires points as a "
                f"non-empty array of [x, y] pairs inside the canvas."
            )
        return None
    return (
        f"command[{idx}] erase: unknown mode {mode!r}. "
        f"Valid modes: rect, path."
    )


def validate_commands(commands: Any) -> str | None:
    """Return an error message, or ``None`` when *commands* is valid."""
    if not isinstance(commands, list):
        return "commands must be an array."
    if not commands:
        return "commands must contain at least one command."
    if len(commands) > MAX_COMMANDS:
        return (
            f"At most {MAX_COMMANDS} commands per call (got "
            f"{len(commands)}). Split the work across turns."
        )

    for idx, cmd in enumerate(commands):
        if not isinstance(cmd, dict):
            return f"command[{idx}] must be an object."
        tool = cmd.get("tool")
        if not tool:
            return f"command[{idx}] is missing the required 'tool' key."
        if tool not in WHITEBOARD_TOOLS:
            valid = ", ".join(sorted(WHITEBOARD_TOOLS))
            return (
                f"command[{idx}] has unknown tool {tool!r}. "
                f"Valid tools: {valid}."
            )

        missing = [k for k in _REQUIRED[tool] if k not in cmd]
        if missing:
            return (
                f"command[{idx}] ({tool}) is missing required "
                f"field(s): {', '.join(missing)}."
            )

        for key in _COORD_KEYS & cmd.keys():
            if not _in_canvas(cmd[key]):
                return (
                    f"command[{idx}] ({tool}): {key}={cmd[key]!r} is "
                    f"outside the {CANVAS_SIZE}x{CANVAS_SIZE} canvas."
                )

        if tool == "draw":
            err = _validate_draw(cmd, idx)
            if err:
                return err
        elif tool == "erase":
            err = _validate_erase(cmd, idx)
            if err:
                return err
        elif tool == "place_artifact":
            if not isinstance(cmd["artifact_id"], str) or not cmd["artifact_id"]:
                return (
                    f"command[{idx}] place_artifact: artifact_id must be a "
                    f"non-empty string."
                )

    return None
```

Create `surogates/whiteboard/__init__.py`:

```python
"""Whiteboard canvas chat surface.

Spec: docs/superpowers/specs/2026-08-27-whiteboard-canvas-chat-design.md
"""

from surogates.whiteboard.commands import (
    CANVAS_SIZE,
    MAX_COMMANDS,
    WHITEBOARD_TOOLS,
    validate_commands,
)

__all__ = [
    "CANVAS_SIZE",
    "MAX_COMMANDS",
    "WHITEBOARD_TOOLS",
    "validate_commands",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_whiteboard_commands.py -q`
Expected: PASS, 19 passed

- [ ] **Step 5: Commit**

```bash
git add surogates/whiteboard/ tests/test_whiteboard_commands.py
git commit -m "feat(whiteboard): validate draw command lists"
```

---

## Task 2: Session predicates

**Files:**
- Create: `surogates/whiteboard/session.py`
- Modify: `surogates/whiteboard/__init__.py`
- Test: `tests/test_whiteboard_session.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `SURFACE_KEY: str`, `SURFACE_VALUE: str`, `is_whiteboard_session(session: Any) -> bool`, `turn_mode(metadata: Any) -> str` returning `"sketch"` or `"deep"`, `whiteboard_metadata(metadata: Any) -> dict[str, Any] | None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_whiteboard_session.py`:

```python
"""The two predicates every whiteboard code path asks."""
from types import SimpleNamespace

from surogates.whiteboard.session import (
    is_whiteboard_session,
    turn_mode,
    whiteboard_metadata,
)


def _session(config=None):
    return SimpleNamespace(config=config or {}, channel="web")


def test_plain_session_is_not_a_whiteboard():
    assert is_whiteboard_session(_session()) is False


def test_surface_stamp_marks_a_whiteboard():
    assert is_whiteboard_session(_session({"surface": "whiteboard"})) is True


def test_another_surface_is_not_a_whiteboard():
    assert is_whiteboard_session(_session({"surface": "browser"})) is False


def test_missing_config_attribute_is_tolerated():
    # Several test harnesses build partial session objects.
    assert is_whiteboard_session(SimpleNamespace()) is False


def test_none_config_is_tolerated():
    assert is_whiteboard_session(SimpleNamespace(config=None)) is False


def test_turn_mode_defaults_to_sketch():
    assert turn_mode(None) == "sketch"
    assert turn_mode({}) == "sketch"
    assert turn_mode({"whiteboard": {}}) == "sketch"


def test_turn_mode_reads_deep():
    assert turn_mode({"whiteboard": {"mode": "deep"}}) == "deep"


def test_turn_mode_rejects_an_unknown_value():
    # An unrecognised mode must fall back to the cheap path, never the
    # expensive one -- an attacker-controlled string must not be able to
    # promote a turn to the full tool catalogue and the pro tier.
    assert turn_mode({"whiteboard": {"mode": "unlimited"}}) == "sketch"


def test_turn_mode_tolerates_a_non_dict_metadata():
    assert turn_mode("nonsense") == "sketch"
    assert turn_mode({"whiteboard": "nonsense"}) == "sketch"


def test_whiteboard_metadata_extracts_the_payload():
    assert whiteboard_metadata({"whiteboard": {"imageScale": 0.5}}) == {
        "imageScale": 0.5,
    }


def test_whiteboard_metadata_returns_none_when_absent():
    assert whiteboard_metadata({"view_context": {}}) is None
    assert whiteboard_metadata(None) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_whiteboard_session.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'surogates.whiteboard.session'`

- [ ] **Step 3: Write the implementation**

Create `surogates/whiteboard/session.py`:

```python
"""Predicates identifying a whiteboard session and a whiteboard turn.

Kept in one module so the harness loop, the worker's prompt-surface
filter, the schema-surface filter and the prompt builder all ask the same
question the same way.  Scattering ``config.get("surface")`` literals is
how the equivalent channel checks drifted.
"""
from __future__ import annotations

from typing import Any

#: The ``session.config`` key and value that mark a whiteboard session.
#: Deliberately a *surface* within the web/studio channels rather than a
#: new ``channel`` value -- see the design doc's "Session shape".
SURFACE_KEY = "surface"
SURFACE_VALUE = "whiteboard"

#: The cheap single-round-trip mode.  Any unrecognised value resolves
#: here: ``mode`` arrives from the client, so an unknown string must
#: never be able to promote a turn to the full tool catalogue.
MODE_SKETCH = "sketch"
MODE_DEEP = "deep"


def is_whiteboard_session(session: Any) -> bool:
    """Whether *session* is a whiteboard surface.

    ``getattr`` with a ``None`` guard: several harnesses build partial
    session objects that skip ``__init__``, and a missing config means
    "not a whiteboard", not a programming error.
    """
    config = getattr(session, "config", None)
    if not isinstance(config, dict):
        return False
    return config.get(SURFACE_KEY) == SURFACE_VALUE


def whiteboard_metadata(metadata: Any) -> dict[str, Any] | None:
    """Return the ``whiteboard`` block of a message's metadata, or ``None``."""
    if not isinstance(metadata, dict):
        return None
    payload = metadata.get("whiteboard")
    return payload if isinstance(payload, dict) else None


def turn_mode(metadata: Any) -> str:
    """Return this turn's mode: :data:`MODE_SKETCH` or :data:`MODE_DEEP`."""
    payload = whiteboard_metadata(metadata)
    if payload is None:
        return MODE_SKETCH
    return MODE_DEEP if payload.get("mode") == MODE_DEEP else MODE_SKETCH
```

Append to `surogates/whiteboard/__init__.py` (extend the existing import and `__all__`):

```python
from surogates.whiteboard.session import (
    MODE_DEEP,
    MODE_SKETCH,
    SURFACE_KEY,
    SURFACE_VALUE,
    is_whiteboard_session,
    turn_mode,
    whiteboard_metadata,
)
```

and add `"MODE_DEEP"`, `"MODE_SKETCH"`, `"SURFACE_KEY"`, `"SURFACE_VALUE"`, `"is_whiteboard_session"`, `"turn_mode"`, `"whiteboard_metadata"` to `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_whiteboard_session.py -q`
Expected: PASS, 11 passed

- [ ] **Step 5: Commit**

```bash
git add surogates/whiteboard/ tests/test_whiteboard_session.py
git commit -m "feat(whiteboard): add session surface and turn-mode predicates"
```

---

## Task 3: The `whiteboard_draw` tool

**Files:**
- Create: `surogates/tools/builtin/whiteboard.py`
- Modify: `surogates/tools/router.py`
- Modify: `surogates/tools/builtin/__init__.py`
- Test: `tests/test_whiteboard_tool.py`

**Interfaces:**
- Consumes: `validate_commands`, `MAX_COMMANDS`, `CANVAS_SIZE` from Task 1.
- Produces: `register(registry: ToolRegistry) -> None`, `WHITEBOARD_TOOL_NAMES: frozenset[str]` (the single-entry set `{"whiteboard_draw"}`, used by the gating tasks and the routing test).

- [ ] **Step 1: Write the failing test**

Create `tests/test_whiteboard_tool.py`:

```python
"""The whiteboard_draw tool: schema, routing, and handler behaviour."""
import asyncio
import json

import pytest

from surogates.tools.builtin.whiteboard import (
    WHITEBOARD_TOOL_NAMES,
    register,
)
from surogates.tools.registry import ToolRegistry


def _registry():
    registry = ToolRegistry()
    register(registry)
    return registry


def _call(registry, arguments, **kwargs):
    entry = registry.get("whiteboard_draw")
    return asyncio.run(entry.handler(arguments, **kwargs))


def test_registers_under_the_whiteboard_toolset():
    entry = _registry().get("whiteboard_draw")
    assert entry.toolset == "whiteboard"


def test_schema_declares_commands_as_required():
    schema = _registry().get("whiteboard_draw").schema
    assert schema.parameters["required"] == ["commands"]
    assert schema.parameters["properties"]["commands"]["type"] == "array"


def test_routes_to_harness():
    """Regression: a tool absent from TOOL_LOCATIONS falls back to SANDBOX
    routing and dies there as 'Unknown tool'.
    """
    from surogates.tools.router import TOOL_LOCATIONS, ToolLocation

    for name in WHITEBOARD_TOOL_NAMES:
        assert TOOL_LOCATIONS.get(name) is ToolLocation.HARNESS, (
            f"{name} is not HARNESS-routed; the sandbox fallback surfaces "
            f"it as 'Unknown tool'"
        )


def test_handler_accepts_a_valid_command_list():
    result = _call(_registry(), {"commands": [{
        "tool": "write_text", "x": 10, "y": 20, "text": "5",
        "fontSize": 32, "maxWidth": 300,
    }]})
    assert "1" in result and "error" not in result.lower()


def test_handler_reports_the_object_count():
    result = _call(_registry(), {"commands": [
        {"tool": "write_text", "x": 1, "y": 1, "text": "a",
         "fontSize": 20, "maxWidth": 100},
        {"tool": "erase", "mode": "rect", "x": 0, "y": 0, "w": 5, "h": 5},
    ]})
    assert "2" in result


def test_handler_rejects_an_invalid_command_with_a_precise_message():
    result = _call(_registry(), {"commands": [{"tool": "nope"}]})
    assert "nope" in result


def test_handler_rejects_a_missing_commands_key():
    result = _call(_registry(), {})
    assert "commands" in result


def test_handler_recovers_a_json_encoded_commands_string():
    """Some models serialise the array as a string; recover rather than
    burn a retry on a silent-tic problem."""
    encoded = json.dumps([{
        "tool": "write_text", "x": 1, "y": 1, "text": "a",
        "fontSize": 20, "maxWidth": 100,
    }])
    result = _call(_registry(), {"commands": encoded})
    assert "1" in result and "error" not in result.lower()


def test_handler_rejects_a_malformed_commands_string():
    result = _call(_registry(), {"commands": "[{"})
    assert "commands" in result.lower()


def test_description_names_every_command_tool():
    description = _registry().get("whiteboard_draw").schema.description
    for tool in ("write_text", "draw_formula", "draw", "erase",
                 "place_artifact"):
        assert tool in description
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_whiteboard_tool.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'surogates.tools.builtin.whiteboard'`

- [ ] **Step 3: Write the implementation**

Create `surogates/tools/builtin/whiteboard.py`:

```python
"""Builtin ``whiteboard_draw`` tool.

The agent's write path onto the canvas.  The tool call itself carries the
payload: the SDK renders straight off the ``tool.call`` event's arguments,
so drawing begins as the call streams rather than after the result lands.
The handler therefore validates and acknowledges; it does not persist.
The client is the sole writer of the canvas document (see the design doc's
"Persistence: single writer").
"""
from __future__ import annotations

import json
import logging
from typing import Any

from surogates.tools.registry import ToolRegistry, ToolSchema
from surogates.whiteboard.commands import (
    CANVAS_SIZE,
    MAX_COMMANDS,
    validate_commands,
)

logger = logging.getLogger(__name__)

WHITEBOARD_TOOL_NAMES: frozenset[str] = frozenset({"whiteboard_draw"})

_DESCRIPTION = (
    "Draw on the shared whiteboard canvas. Every coordinate is a global "
    f"logical coordinate on a {CANVAS_SIZE}x{CANVAS_SIZE} canvas -- never "
    "an image coordinate. Use the geometry in the turn's canvas note to "
    "convert.\n\n"
    "Commands:\n"
    "- write_text {tool,x,y,text,fontSize,maxWidth,lineHeight?} -- prose. "
    "You own layout: x,y is the top-left start and maxWidth is the wrap "
    "width. Pick a blank region near the content you are answering.\n"
    "- draw_formula {tool,x,y,latex,fontSize} -- mathematical notation.\n"
    "- draw {tool,origin:[x,y],types:[...],items:[[...]],width?,tension?,"
    "closed?,fill?,arrows?} -- a simple sketch or annotation of about ten "
    "or fewer primitives. types and items must be the same length. "
    "Encodings: line/smooth [x1,y1,x2,y2,...]; rect [x,y,w,h]; ellipse "
    "[cx,cy,rx,ry]; circle [cx,cy,r]; arc [cx,cy,rx,ry,startDeg,sweepDeg]. "
    "Item coordinates are integer offsets from origin.\n"
    "- erase {tool,mode:'rect',x,y,w,h} or {tool,mode:'path',points,size}\n"
    "- place_artifact {tool,artifact_id,x,y,w,h} -- position an artifact "
    "you already created with create_artifact. For anything larger, "
    "richer, or interactive than a simple sketch -- a chart, a diagram, a "
    "table, an interactive widget -- create an artifact and place it "
    "rather than approximating it with many draw commands.\n\n"
    f"At most {MAX_COMMANDS} commands per call. Do not redraw content "
    "that is already on the canvas: add only the continuation, answer, or "
    "annotation that is missing."
)


def register(registry: ToolRegistry) -> None:
    """Register the ``whiteboard_draw`` tool."""
    registry.register(
        name="whiteboard_draw",
        schema=ToolSchema(
            name="whiteboard_draw",
            description=_DESCRIPTION,
            parameters={
                "type": "object",
                "properties": {
                    "commands": {
                        "type": "array",
                        "maxItems": MAX_COMMANDS,
                        "description": (
                            "Ordered list of drawing commands. Each object "
                            "must carry a 'tool' key naming one of "
                            "write_text, draw_formula, draw, erase, "
                            "place_artifact."
                        ),
                        "items": {
                            "type": "object",
                            "required": ["tool"],
                            "properties": {
                                "tool": {
                                    "type": "string",
                                    "enum": [
                                        "write_text", "draw_formula", "draw",
                                        "erase", "place_artifact",
                                    ],
                                },
                            },
                            "additionalProperties": True,
                        },
                    },
                },
                "required": ["commands"],
            },
        ),
        handler=_whiteboard_draw_handler,
        toolset="whiteboard",
    )


async def _whiteboard_draw_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Validate the command list and acknowledge.

    Returns a compact ack rather than echoing the payload: the commands
    are already in the event log as the call's arguments, and echoing
    them back would double their cost in the next turn's replay.
    """
    commands = arguments.get("commands")

    # Some models serialise the array as a JSON string.  Recover
    # transparently -- the shape is unambiguous and a retry buys nothing.
    if isinstance(commands, str):
        try:
            commands = json.loads(commands)
        except json.JSONDecodeError as exc:
            return (
                f"Error: commands is a malformed JSON string "
                f"({exc.msg} at position {exc.pos}). Pass it as an array, "
                f"not a string."
            )

    error = validate_commands(commands)
    if error:
        return f"Error: {error}"

    count = len(commands)
    logger.info("whiteboard_draw accepted %d command(s)", count)
    return (
        f"Drew {count} object{'s' if count != 1 else ''} on the canvas. "
        f"They are now the user's active selection, so they can move, "
        f"resize or delete them."
    )
```

Add to `surogates/tools/router.py`, in the `TOOL_LOCATIONS` dict alongside `"create_artifact": ToolLocation.HARNESS`:

```python
    # Whiteboard canvas — validation only, but it must not route to the
    # sandbox: an unlisted tool falls back there and dies as
    # "Unknown tool".
    "whiteboard_draw": ToolLocation.HARNESS,
```

Register the module wherever the other builtins are registered. Find the existing `artifact.register(registry)` call:

Run: `grep -rn "artifact" surogates/tools/builtin/__init__.py surogates/tools/*.py | grep -i register`

Add the matching `whiteboard.register(registry)` line next to it, following whatever import style that file already uses.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_whiteboard_tool.py -q`
Expected: PASS, 10 passed

- [ ] **Step 5: Commit**

```bash
git add surogates/tools/builtin/whiteboard.py surogates/tools/router.py \
        surogates/tools/builtin/__init__.py tests/test_whiteboard_tool.py
git commit -m "feat(whiteboard): add the whiteboard_draw tool"
```

---

## Task 4: Tool gating on both surfaces

Two filters decide what the model sees, and they must agree. `worker._filter_effective_tools` builds the **prompt** surface (which tools the prose says exist); `harness.tool_schemas.drop_unusable_tools` builds the **schema** surface (which tools the model is handed). Dropping a tool from one alone tells the model in prose it has a tool while withholding the schema, or vice versa — the exact bug documented at `surogates/orchestrator/worker.py:236-241`.

**Files:**
- Modify: `surogates/orchestrator/worker.py` (in `_filter_effective_tools`, after the `context_group_id` block around line 299)
- Modify: `surogates/harness/tool_schemas.py`
- Test: `tests/test_whiteboard_tool_gating.py`

**Interfaces:**
- Consumes: `is_whiteboard_session` (Task 2), `WHITEBOARD_TOOL_NAMES` (Task 3).
- Produces: `drop_unusable_tools(..., is_whiteboard: bool)` — one new keyword-only argument on the existing function.

- [ ] **Step 1: Write the failing test**

Create `tests/test_whiteboard_tool_gating.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_whiteboard_tool_gating.py -q`
Expected: FAIL — `TypeError: drop_unusable_tools() got an unexpected keyword argument 'is_whiteboard'`

- [ ] **Step 3: Write the implementation**

In `surogates/orchestrator/worker.py`, inside `_filter_effective_tools`, immediately after the `context_group_id` block (around line 302), add:

```python
    # whiteboard_draw is the canvas surface's write path: meaningless on
    # a message-thread session, and mandatory on a whiteboard one. Same
    # force-add idiom as the board self-tools above -- a whiteboard
    # session that cannot draw is not a whiteboard, whatever a
    # restrictive AgentDef allowlist says.
    if is_whiteboard_session(session):
        result |= WHITEBOARD_TOOL_NAMES
    else:
        result -= WHITEBOARD_TOOL_NAMES
```

with the imports at the top of the module:

```python
from surogates.tools.builtin.whiteboard import WHITEBOARD_TOOL_NAMES
from surogates.whiteboard.session import is_whiteboard_session
```

In `surogates/harness/tool_schemas.py`, add the drop set next to `_CRON_TOOLS`:

```python
_WHITEBOARD_TOOLS: frozenset[str] = frozenset({
    "whiteboard_draw",
})
```

and extend `drop_unusable_tools`:

```python
def drop_unusable_tools(
    schemas: list[dict[str, Any]],
    *,
    has_kbs: bool,
    has_channel: bool,
    is_scheduled: bool,
    is_whiteboard: bool = False,
) -> list[dict[str, Any]]:
```

adding to the body, alongside the other three:

```python
    if not is_whiteboard:
        drop |= _WHITEBOARD_TOOLS
```

Then find the caller and pass the flag:

Run: `grep -rn "drop_unusable_tools(" surogates/ --include=*.py`

At each non-test call site, pass `is_whiteboard=is_whiteboard_session(session)`, importing the predicate from `surogates.whiteboard.session`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_whiteboard_tool_gating.py -q`
Expected: PASS, 6 passed

- [ ] **Step 5: Run the neighbouring suites for regressions**

Run: `python -m pytest tests/test_board_tool_gating.py tests/test_arbor_tool_filter.py -q`
Expected: PASS — the shared filter still behaves for its existing callers.

- [ ] **Step 6: Commit**

```bash
git add surogates/orchestrator/worker.py surogates/harness/tool_schemas.py \
        tests/test_whiteboard_tool_gating.py
git commit -m "feat(whiteboard): gate whiteboard_draw on the canvas surface"
```

---

## Task 5: The guidance fragment

The prompt is the port with the highest value-to-effort ratio in this plan. Read `study/penecho/src/server/main.js:673-706` in full before writing it — `SYSTEM_PROMPT`, `ACTIVE_SYSTEM_PROMPT_BASE`, and `MANDATORY_VISIBLE_RESPONSE_PROMPT`.

**Files:**
- Create: `surogates/harness/prompts/guidance/whiteboard.md`
- Modify: `surogates/harness/prompt.py`
- Test: `tests/test_whiteboard_prompt.py`

**Interfaces:**
- Consumes: `is_whiteboard_session` (Task 2), `PromptLibrary.get` (`surogates/harness/prompt_library.py:99`).
- Produces: the fragment key `"guidance/whiteboard"`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_whiteboard_prompt.py`:

```python
"""The whiteboard guidance fragment exists, is loadable, and only lands
on whiteboard sessions."""
import pytest

from surogates.harness.prompt_library import PromptLibrary


def _library():
    return PromptLibrary()


def test_the_fragment_loads():
    body = _library().get("guidance/whiteboard")
    assert body.strip()


def test_the_fragment_states_the_canvas_size():
    assert "20000" in _library().get("guidance/whiteboard")


def test_the_fragment_names_the_tool():
    assert "whiteboard_draw" in _library().get("guidance/whiteboard")


@pytest.mark.parametrize("rule", [
    "latestInput",          # the authoritative attention region
    "sourceRect",           # image <-> global coordinate mapping
    "imageScale",
    "extend",               # extend the document, never reproduce it
    "arrow",                # spatial gesture reading
])
def test_the_fragment_carries_the_ported_rules(rule):
    assert rule in _library().get("guidance/whiteboard")


def test_the_fragment_credits_penecho():
    body = _library().get("guidance/whiteboard")
    assert "PenEcho" in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_whiteboard_prompt.py -q`
Expected: FAIL — `PromptNotFoundError` for `guidance/whiteboard`

- [ ] **Step 3: Write the fragment**

Create `surogates/harness/prompts/guidance/whiteboard.md`. Match the frontmatter style of the sibling fragments — check `surogates/harness/prompts/guidance/browser.md` first and copy its header shape. The body:

```markdown
## Whiteboard canvas

You are the visual reasoning brain for a shared handwritten canvas — not
only a maths board. The user writes, sketches and types on it; you answer
*on* it by calling `whiteboard_draw`.

Adapted from PenEcho (https://penecho.ai), AGPL-3.0-only.

### Reading the canvas

The attached image is a clean white-background rendering of canvas
content around the newest input. It may come from outside the user's
current viewport.

`sourceRect` is the image's full-resolution global canvas rectangle and
`imageScale` maps global units to image pixels:

    imageX = (globalX - sourceRect.x) * imageScale
    imageY = (globalY - sourceRect.y) * imageScale

`latestInput` is the AUTHORITATIVE attention region for this turn. Read
the newest user input inside it first. Older content may overlap that
rectangle, so use the hotspot trajectory and visible stroke continuity to
tell the newest writing apart. Pixels outside it are older context or
your own earlier output — do not fold them into what you are answering
unless the latest input visually refers to them.

`hotspots` contains only the current unconsumed writing segment, ordered
oldest to newest. Use it to refine reading order inside `latestInput`,
nothing more. Its absence is not evidence that there is no new input.

Handwriting in a logographic script needs deliberate character-by-
character inspection: examine stroke groups, radicals, spacing and
punctuation, and resolve a genuinely ambiguous character from the
surrounding phrase rather than silently changing the sentence's topic.

### Extending, not reproducing

Treat the canvas as an existing document to extend. Add only the missing
continuation, answer, annotation or new visual element. Never rewrite,
trace or redraw text, equations, labels, strokes, diagrams or plots that
are already there unless the user explicitly asks you to repeat or
replace them.

If the user has written `3+2=`, place only `5` after the equals sign —
not `3+2=5`.

When a requested visual uses existing canvas objects as actors, anchors
or targets, preserve their actual positions and overlay only the newly
requested content. Never recreate those objects in a standalone
duplicate.

### Spatial gestures are instructions

Interpret spatial editing marks as instructions, not as sentence text.

- A hand-drawn box or circle selects the content inside it.
- An arrow connects the selected source to a destination. Follow an arrow
  chain to its final arrowhead and place your answer in the clear space
  immediately beyond it.
- A short label near an arrow — "more", "detail", "expand", "explain",
  "why" — requests a fuller treatment of the selected content. It is an
  instruction. Never copy it into your response.

When `selection` is present in the canvas note, that lasso is the
exclusive context for this turn: ignore unrelated handwriting elsewhere
and place your answer in clear space beside the selected rectangle.

### Response language

Respond in the language of the newest substantive user content. If the
newest input is only a spatial control label such as "more", follow the
language of the content it points at. Preserve intentional mixed-language
terminology. Never choose a response language from the interface language
alone.

### Layout is your responsibility

Every `write_text` command MUST choose `x` and `y` as the top-left start
position and `maxWidth` as the wrapping width. Inspect the image and pick
the blank area where the answer is most useful. Do not mechanically
append text after the newest handwriting.

- For an arrow or box request, align `x`/`y` with the arrow destination.
- For an ordinary question, pick a nearby blank area that preserves
  reading flow and avoids existing writing.
- Never place an explanation at the top edge merely because that area is
  blank when the content it refers to is far below.
- Match `fontSize` roughly to nearby handwriting. `lineHeight` is a
  multiplier such as 1.35, not pixels.
- Do not send a colour: the client applies the user's chosen ink colour.

The logical canvas is 20000 by 20000. Every coordinate you return must be
a finite global logical coordinate, never an image coordinate.

### Choosing a command

- `write_text` for ordinary prose, knowledge and conversation. Keep each
  one short — roughly 200 tokens.
- `draw_formula` for mathematical notation.
- `draw` for a very simple static sketch or annotation: about ten or
  fewer primitives. A line or smooth path with *n* points counts as
  *n − 1* segments.
- For anything larger, richer, interactive or dynamic — a chart, a
  diagram, a table, a widget — call `create_artifact` and then
  `place_artifact`. Never approximate one by splitting it into many
  `draw` or `write_text` commands.

If the newest input is unclear, incomplete, or lacks the context to
answer, draw one short `write_text` asking precisely what is missing.
Every turn that reaches you represents a deliberate user action, so
always return at least one visible command — never answer a whiteboard
turn with prose alone.
```

- [ ] **Step 4: Wire it into the prompt builder**

In `surogates/harness/prompt.py`, in the same method that appends the
platform hint and the artifact-in-channel guidance (around line 800),
add before the expert footer:

```python
        # The whiteboard surface needs its own reading/layout contract:
        # the model is looking at an image of a canvas and answering with
        # coordinates on it, which no other surface asks of it.
        if is_whiteboard_session(self._session):
            parts.append("\n" + self._prompts.get("guidance/whiteboard"))
```

Import at the top:

```python
from surogates.whiteboard.session import is_whiteboard_session
```

If the builder does not hold a session reference under that name, find
what `self._get_channel()` reads from and use the same source.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_whiteboard_prompt.py -q`
Expected: PASS, 9 passed

- [ ] **Step 6: Commit**

```bash
git add surogates/harness/prompts/guidance/whiteboard.md \
        surogates/harness/prompt.py tests/test_whiteboard_prompt.py
git commit -m "feat(whiteboard): add the canvas reading and layout guidance"
```

---

## Task 6: The per-turn canvas note

**Files:**
- Modify: `surogates/harness/loop_messages.py`
- Modify: `surogates/harness/loop_context_replay.py`
- Test: `tests/test_whiteboard_note.py`

**Interfaces:**
- Consumes: `whiteboard_metadata` (Task 2).
- Produces: `_whiteboard_note_from_metadata(metadata: Any) -> str | None` in `loop_messages.py`, called from `build_user_message_dict` alongside `_view_context_note_from_metadata`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_whiteboard_note.py`:

```python
"""The canvas geometry note injected for a whiteboard turn."""
from surogates.harness.loop_messages import _whiteboard_note_from_metadata


def _meta(**over):
    payload = {
        "sourceRect": {"x": 1000, "y": 2000, "w": 800, "h": 600},
        "imageScale": 0.5,
        "latestInput": {"x": 1200, "y": 2100, "w": 300, "h": 200},
        "hotspots": [[3, 4], [3, 5]],
        "canvasSize": 20000,
        "mode": "sketch",
    }
    payload.update(over)
    return {"whiteboard": payload}


def test_returns_none_without_whiteboard_metadata():
    assert _whiteboard_note_from_metadata(None) is None
    assert _whiteboard_note_from_metadata({"view_context": {}}) is None
    assert _whiteboard_note_from_metadata("nonsense") is None


def test_renders_the_source_rect_and_scale():
    note = _whiteboard_note_from_metadata(_meta())
    assert "1000" in note and "2000" in note
    assert "0.5" in note


def test_renders_the_latest_input_rect():
    note = _whiteboard_note_from_metadata(_meta())
    assert "latestInput" in note
    assert "1200" in note


def test_renders_hotspots_when_present():
    assert "hotspot" in _whiteboard_note_from_metadata(_meta()).lower()


def test_omits_hotspots_when_empty():
    note = _whiteboard_note_from_metadata(_meta(hotspots=[]))
    assert "hotspot" not in note.lower()


def test_renders_a_selection_when_present():
    note = _whiteboard_note_from_metadata(
        _meta(selection={"x": 5, "y": 6, "w": 7, "h": 8}),
    )
    assert "selection" in note.lower()


def test_omits_selection_when_absent():
    assert "selection" not in _whiteboard_note_from_metadata(_meta()).lower()


def test_renders_typed_input_as_transcription_ground_truth():
    note = _whiteboard_note_from_metadata(_meta(typedInput="integral of x^2"))
    assert "integral of x^2" in note


def test_tolerates_a_malformed_rect():
    # Client-supplied data: a malformed block must degrade to a shorter
    # note, never raise into the turn.
    note = _whiteboard_note_from_metadata(_meta(sourceRect="nope"))
    assert note is None or isinstance(note, str)


def test_survives_every_field_being_absent():
    note = _whiteboard_note_from_metadata({"whiteboard": {}})
    assert note is None or isinstance(note, str)
```

Append to the same file the replay-pruning test:

```python
def test_replay_keeps_only_the_newest_canvas_image():
    """Canvas snapshots are cumulative: snapshot N contains everything
    N-1 did, so replaying all of them is pure waste and would dominate
    context within a dozen turns."""
    from surogates.harness.loop_context_replay import (
        prune_superseded_canvas_images,
    )

    def _turn(n):
        return {
            "role": "user",
            "content": [
                {"type": "text", "text": f"turn {n}"},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,AAA{n}"}},
            ],
        }

    messages = [_turn(1), _turn(2), _turn(3)]
    pruned = prune_superseded_canvas_images(messages)

    images = [
        part
        for message in pruned
        for part in (message["content"] if isinstance(message["content"], list) else [])
        if part.get("type") == "image_url"
    ]
    assert len(images) == 1
    assert "AAA3" in images[0]["image_url"]["url"]


def test_replay_leaves_a_single_canvas_image_alone():
    from surogates.harness.loop_context_replay import (
        prune_superseded_canvas_images,
    )

    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "only turn"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,Z"}},
        ],
    }]
    assert prune_superseded_canvas_images(messages) == messages
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_whiteboard_note.py -q`
Expected: FAIL — `ImportError: cannot import name '_whiteboard_note_from_metadata'`

- [ ] **Step 3: Write the note builder**

In `surogates/harness/loop_messages.py`, next to `_view_context_note_from_metadata`:

```python
def _whiteboard_note_from_metadata(metadata: Any) -> str | None:
    """Render the canvas geometry note for one whiteboard turn.

    The model is looking at a cropped image of a much larger canvas and
    must answer in *canvas* coordinates, so it needs the mapping. Pure
    function of client-supplied data: a malformed block degrades to a
    shorter note or ``None`` and never raises into the turn.
    """
    from surogates.whiteboard.session import whiteboard_metadata

    payload = whiteboard_metadata(metadata)
    if payload is None:
        return None

    def _rect(value: Any) -> str | None:
        if not isinstance(value, dict):
            return None
        try:
            return (
                f"x={value['x']}, y={value['y']}, "
                f"w={value['w']}, h={value['h']}"
            )
        except (KeyError, TypeError):
            return None

    lines: list[str] = ["The user is working on a whiteboard canvas."]

    source = _rect(payload.get("sourceRect"))
    scale = payload.get("imageScale")
    if source and isinstance(scale, (int, float)):
        lines.append(
            f"The attached image covers canvas rectangle sourceRect "
            f"({source}) at imageScale={scale}. Convert with "
            f"imageX=(globalX-sourceRect.x)*imageScale."
        )

    latest = _rect(payload.get("latestInput"))
    if latest:
        lines.append(
            f"latestInput ({latest}) is the authoritative attention "
            f"region for this turn."
        )

    hotspots = payload.get("hotspots")
    if isinstance(hotspots, list) and hotspots:
        lines.append(
            f"The pen trajectory covers {len(hotspots)} hotspot cell(s), "
            f"ordered oldest to newest: {hotspots}."
        )

    selection = _rect(payload.get("selection"))
    if selection:
        lines.append(
            f"The user lassoed a selection ({selection}). Treat it as the "
            f"exclusive context for this turn."
        )

    typed = payload.get("typedInput")
    if isinstance(typed, str) and typed.strip():
        lines.append(
            f"The user typed this text exactly (authoritative, do not "
            f"re-transcribe it from pixels):\n{typed.strip()}"
        )

    # One line means we recovered nothing beyond the header, which is
    # not worth spending a note on.
    return "\n".join(lines) if len(lines) > 1 else None
```

Then in `build_user_message_dict` (`surogates/harness/loop_context_replay.py:72`), add the note alongside the view-context one:

```python
    whiteboard_note = _whiteboard_note_from_metadata(event_data.get("metadata"))
    if whiteboard_note:
        note_parts.append(whiteboard_note)
```

importing `_whiteboard_note_from_metadata` from `loop_messages` next to the existing `_view_context_note_from_metadata` import at `loop_context_replay.py:14`.

- [ ] **Step 4: Write the replay pruner**

Add to `surogates/harness/loop_context_replay.py`:

```python
_PRUNED_CANVAS_PLACEHOLDER = (
    "[Earlier canvas snapshot pruned — it is fully contained in the "
    "current one.]"
)


def prune_superseded_canvas_images(
    messages: list[dict],
) -> list[dict]:
    """Keep only the newest canvas image in a whiteboard replay.

    Canvas snapshots are cumulative: snapshot N renders everything
    snapshot N-1 did plus whatever has been added since. Replaying all of
    them is pure waste, and on a long board it would dominate the context
    window within a dozen turns.

    Same shape as ``ContextManager.prune_stale_browser_states``, with
    ``keep_last`` fixed at 1 — unlike browser state there is no case for
    holding two, because the older one is a strict subset.
    """
    image_positions = [
        (m_idx, p_idx)
        for m_idx, message in enumerate(messages)
        if isinstance(message.get("content"), list)
        for p_idx, part in enumerate(message["content"])
        if isinstance(part, dict) and part.get("type") == "image_url"
    ]
    if len(image_positions) <= 1:
        return messages

    superseded = set(image_positions[:-1])
    pruned: list[dict] = []
    for m_idx, message in enumerate(messages):
        content = message.get("content")
        if not isinstance(content, list):
            pruned.append(message)
            continue
        new_content = [
            {"type": "text", "text": _PRUNED_CANVAS_PLACEHOLDER}
            if (m_idx, p_idx) in superseded
            else part
            for p_idx, part in enumerate(content)
        ]
        pruned.append({**message, "content": new_content})
    return pruned
```

Call it from the whiteboard branch of the replay. Find where the replayed message list is finalised:

Run: `grep -n "def rebuild\|_rebuild_messages\|prune_stale_browser_states" surogates/harness/loop.py surogates/harness/context.py`

Apply `prune_superseded_canvas_images` at the same point `prune_stale_browser_states` is applied, guarded by `is_whiteboard_session(session)` so no other surface is affected.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_whiteboard_note.py -q`
Expected: PASS, 12 passed

- [ ] **Step 6: Commit**

```bash
git add surogates/harness/loop_messages.py \
        surogates/harness/loop_context_replay.py \
        surogates/harness/loop.py tests/test_whiteboard_note.py
git commit -m "feat(whiteboard): inject canvas geometry and prune stale snapshots"
```

---

## Task 7: Cap `metadata.whiteboard` at the route

`SendMessageRequest.metadata` is free-form and currently unbounded. It is client-supplied data crossing a trust boundary into the event log, so the cap is not optional.

**Files:**
- Modify: `surogates/api/routes/sessions.py` (near `_MAX_IMAGE_BYTES` at line 201, and the `metadata` handling at line 1222)
- Test: `tests/api/test_whiteboard_metadata_cap.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `_MAX_WHITEBOARD_METADATA_BYTES: int` and a validator on `SendMessageRequest`.

- [ ] **Step 1: Write the failing test**

Create `tests/api/test_whiteboard_metadata_cap.py`:

```python
"""metadata.whiteboard is client-supplied and lands in the event log, so
it carries a hard size cap."""
import pytest
from pydantic import ValidationError

from surogates.api.routes.sessions import (
    _MAX_WHITEBOARD_METADATA_BYTES,
    SendMessageRequest,
)


def test_accepts_a_small_whiteboard_block():
    req = SendMessageRequest(
        content="what is this",
        metadata={"whiteboard": {"imageScale": 0.5}},
    )
    assert req.metadata["whiteboard"]["imageScale"] == 0.5


def test_rejects_an_oversized_whiteboard_block():
    huge = {"whiteboard": {"pad": "x" * (_MAX_WHITEBOARD_METADATA_BYTES + 1)}}
    with pytest.raises(ValidationError) as exc:
        SendMessageRequest(content="hi", metadata=huge)
    assert "whiteboard" in str(exc.value)


def test_leaves_other_metadata_keys_alone():
    # The cap is scoped to the whiteboard block; view_context and other
    # keys keep their existing (uncapped) behaviour.
    req = SendMessageRequest(
        content="hi",
        metadata={"view_context": {"page": "agents"}},
    )
    assert req.metadata["view_context"]["page"] == "agents"


def test_accepts_metadata_without_a_whiteboard_block():
    assert SendMessageRequest(content="hi", metadata={}).metadata == {}


def test_accepts_no_metadata_at_all():
    assert SendMessageRequest(content="hi").metadata is None


def test_rejects_a_non_dict_whiteboard_block():
    with pytest.raises(ValidationError):
        SendMessageRequest(content="hi", metadata={"whiteboard": "nope"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/api/test_whiteboard_metadata_cap.py -q`
Expected: FAIL — `ImportError: cannot import name '_MAX_WHITEBOARD_METADATA_BYTES'`

- [ ] **Step 3: Write the implementation**

In `surogates/api/routes/sessions.py`, next to `_MAX_IMAGE_BYTES` (line 201):

```python
# ``metadata`` is deliberately free-form, but the whiteboard block is
# client-authored geometry that lands verbatim in the event log and is
# replayed on every subsequent turn. Uncapped it is an unbounded write
# into a table nothing prunes.
_MAX_WHITEBOARD_METADATA_BYTES = 65_536
```

Add a validator to `SendMessageRequest` (after the existing
`_strip_server_set_attachment_fields`):

```python
    @model_validator(mode="after")
    def _cap_whiteboard_metadata(self) -> "SendMessageRequest":
        if not isinstance(self.metadata, dict):
            return self
        payload = self.metadata.get("whiteboard")
        if payload is None:
            return self
        if not isinstance(payload, dict):
            raise ValueError("metadata.whiteboard must be an object.")
        size = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        if size > _MAX_WHITEBOARD_METADATA_BYTES:
            raise ValueError(
                f"metadata.whiteboard is {size} bytes, over the "
                f"{_MAX_WHITEBOARD_METADATA_BYTES}-byte limit."
            )
        return self
```

Confirm `json` and `model_validator` are already imported in that module; add whichever is missing.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/api/test_whiteboard_metadata_cap.py -q`
Expected: PASS, 6 passed

- [ ] **Step 5: Commit**

```bash
git add surogates/api/routes/sessions.py tests/api/test_whiteboard_metadata_cap.py
git commit -m "feat(whiteboard): cap client-supplied canvas metadata"
```

---

## Task 8: Two-speed turns

**Files:**
- Modify: `surogates/harness/loop.py` (`_tool_filter_for_session`, called at line 1638)
- Test: `tests/test_whiteboard_turn_mode.py`

**Interfaces:**
- Consumes: `turn_mode`, `is_whiteboard_session` (Task 2), `WHITEBOARD_TOOL_NAMES` (Task 3).
- Produces: `_whiteboard_sketch_filter(tool_filter: set[str] | None, session: Any, metadata: Any) -> set[str] | None` — a module-level pure helper in `loop.py`, applied inside `_tool_filter_for_session`.

Keeping the rule as a pure module-level function is deliberate: it is the one new hook this design adds to the loop, and a pure function is the part that can be tested without constructing an `AgentHarness`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_whiteboard_turn_mode.py`:

```python
"""sketch narrows a whiteboard turn to one round-trip; deep restores the
full catalogue."""
from types import SimpleNamespace

from surogates.harness.loop import _whiteboard_sketch_filter


def _session(surface="whiteboard"):
    config = {"surface": surface} if surface else {}
    return SimpleNamespace(config=config, channel="web")


def _meta(mode):
    return {"whiteboard": {"mode": mode}}


ALL = {"whiteboard_draw", "web_search", "terminal", "create_artifact"}


def test_sketch_narrows_to_the_draw_tool():
    assert _whiteboard_sketch_filter(ALL, _session(), _meta("sketch")) == {
        "whiteboard_draw",
    }


def test_deep_leaves_the_filter_untouched():
    assert _whiteboard_sketch_filter(ALL, _session(), _meta("deep")) == ALL


def test_absent_mode_defaults_to_sketch():
    assert _whiteboard_sketch_filter(ALL, _session(), None) == {
        "whiteboard_draw",
    }


def test_an_unknown_mode_falls_back_to_sketch():
    # ``mode`` is client-supplied: an unrecognised string must never
    # promote a turn to the full catalogue.
    assert _whiteboard_sketch_filter(ALL, _session(), _meta("unlimited")) == {
        "whiteboard_draw",
    }


def test_a_non_whiteboard_session_is_untouched():
    assert _whiteboard_sketch_filter(ALL, _session(surface=None), _meta("sketch")) == ALL


def test_a_none_filter_on_sketch_materialises_to_the_draw_tool():
    # ``None`` is the "no filter applied" contract. On a sketch turn it
    # must still narrow, or sketch mode silently ships every tool.
    assert _whiteboard_sketch_filter(None, _session(), _meta("sketch")) == {
        "whiteboard_draw",
    }


def test_a_none_filter_on_deep_stays_none():
    assert _whiteboard_sketch_filter(None, _session(), _meta("deep")) is None


def test_sketch_keeps_the_draw_tool_even_if_the_filter_omitted_it():
    # The prompt-surface filter force-adds whiteboard_draw on a
    # whiteboard session; the schema surface must not then remove it.
    assert _whiteboard_sketch_filter({"web_search"}, _session(), _meta("sketch")) == {
        "whiteboard_draw",
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_whiteboard_turn_mode.py -q`
Expected: FAIL — `ImportError: cannot import name '_whiteboard_sketch_filter'`

- [ ] **Step 3: Write the implementation**

Add to `surogates/harness/loop.py`, near the other module-level filter helpers:

```python
def _whiteboard_sketch_filter(
    tool_filter: set[str] | None,
    session: Any,
    metadata: Any,
) -> set[str] | None:
    """Narrow a whiteboard turn to one model round-trip in sketch mode.

    A whiteboard turn has two speeds. ``sketch`` answers ink directly:
    the model gets exactly one tool, so it draws and stops, which is what
    keeps the interaction close to the latency of writing on paper.
    ``deep`` is the user asking for real work first — search, compute,
    build an artifact — and restores the full catalogue.

    ``mode`` arrives from the client, so anything that is not exactly
    ``deep`` resolves to ``sketch``: an unrecognised string must never be
    able to promote a turn to the full tool catalogue.
    """
    from surogates.tools.builtin.whiteboard import WHITEBOARD_TOOL_NAMES
    from surogates.whiteboard.session import (
        MODE_DEEP,
        is_whiteboard_session,
        turn_mode,
    )

    if not is_whiteboard_session(session):
        return tool_filter
    if turn_mode(metadata) == MODE_DEEP:
        return tool_filter
    return set(WHITEBOARD_TOOL_NAMES)
```

Then apply it inside `_tool_filter_for_session`. That method currently
takes only `session`; give it the turn's metadata:

```python
    def _tool_filter_for_session(
        self, session: Any, metadata: Any = None,
    ) -> set[str] | None:
        ...
        # existing body, ending with the current return value bound to
        # ``tool_filter``
        return _whiteboard_sketch_filter(tool_filter, session, metadata)
```

At the call site (line 1638), pass the current turn's metadata. Find the
newest `user.message` event's `metadata` the same way
`build_user_message_dict` reads it — if the loop already holds the
rendered messages, walk them; otherwise read the last `USER_MESSAGE`
event off the session's event list, which is exactly what
`surogates/harness/loop_messages.py:124` already does in
`_latest_view_context_note`.

Then pin the tier. Find where the turn's model is resolved:

Run: `grep -n "surogate-pro\|surogate-base\|self._model" surogates/harness/loop.py | head -20`

On a whiteboard session, a `sketch` turn requests the base-tier sentinel
and a `deep` turn the pro-tier sentinel. Both resolve through the proxy —
never write a literal model id.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_whiteboard_turn_mode.py -q`
Expected: PASS, 8 passed

- [ ] **Step 5: Run the harness suite for regressions**

Run: `python -m pytest tests/ -q -k "tool_filter or tool_gating or loop"`
Expected: PASS — `_tool_filter_for_session`'s new optional argument is
backward compatible with every existing caller.

- [ ] **Step 6: Commit**

```bash
git add surogates/harness/loop.py tests/test_whiteboard_turn_mode.py
git commit -m "feat(whiteboard): add sketch and deep turn speeds"
```

---

## Task 9: Phase A integration check

No new code. This task proves the harness half works end-to-end before any UI exists.

**Files:**
- Test: `tests/integration/test_whiteboard_turn.py`

- [ ] **Step 1: Write the integration test**

Create `tests/integration/test_whiteboard_turn.py`. Model it on the
closest existing integration test — read `tests/integration/` and pick
the one that already builds a session and drives a turn with a stubbed
LLM client. Assert, on one whiteboard session:

1. The tool catalogue handed to the model contains `whiteboard_draw`.
2. On a `sketch` turn it contains *only* `whiteboard_draw`.
3. On a `deep` turn it contains `whiteboard_draw` and `create_artifact`.
4. The system prompt contains the string `"whiteboard canvas"`.
5. The rendered user message contains both the canvas note (`sourceRect`)
   and an `image_url` block.
6. A second turn's rendered messages contain exactly one `image_url`.
7. A stubbed `whiteboard_draw` call with a valid command list produces a
   `tool.result` that does not start with `Error:`.

- [ ] **Step 2: Run it**

Run: `python -m pytest tests/integration/test_whiteboard_turn.py -q`
Expected: PASS

- [ ] **Step 3: Run the whole suite**

Run: `python -m pytest tests/ -q`
Expected: PASS, no new failures against `master`.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_whiteboard_turn.py
git commit -m "test(whiteboard): cover a whiteboard turn end to end"
```

---

# Phase B — SDK canvas

## Task 10: Vendor the PenEcho modules

**Files:**
- Create: `sdk/agent-chat-react/src/vendor/penecho/draw.js`
- Create: `sdk/agent-chat-react/src/vendor/penecho/mixed-text.js`
- Create: `sdk/agent-chat-react/src/vendor/penecho/selection.js`
- Create: `sdk/agent-chat-react/src/vendor/penecho/index.ts`
- Create: `sdk/agent-chat-react/src/vendor/penecho/README.md`
- Test: `sdk/agent-chat-react/tests/vendor-draw.test.ts`

**Interfaces:**
- Consumes: nothing.
- Produces: `import { DRAW, MIXED_TEXT, SELECTION } from "@/vendor/penecho"` where
  `DRAW.normalize(command: unknown, canvasSize?: number): NormalizedDraw | null`,
  `DRAW.render(command: unknown, createCanvas: (w: number, h: number) => HTMLCanvasElement, color?: string): { image: HTMLCanvasElement; x: number; y: number } | null`,
  `DRAW.smoothSegments(points, closed, tension)`.

- [ ] **Step 1: Copy the files verbatim**

```bash
cd /work/surogates
mkdir -p sdk/agent-chat-react/src/vendor/penecho
cp study/penecho/public/draw.js        sdk/agent-chat-react/src/vendor/penecho/
cp study/penecho/public/mixed-text.js  sdk/agent-chat-react/src/vendor/penecho/
cp study/penecho/public/selection.js   sdk/agent-chat-react/src/vendor/penecho/
```

Do **not** edit their contents. They are UMD modules with no imports, so
they work under both bundlers unchanged.

- [ ] **Step 2: Record the provenance**

Create `sdk/agent-chat-react/src/vendor/penecho/README.md`:

```markdown
# Vendored from PenEcho

These files are copied verbatim from [PenEcho](https://penecho.ai)
(https://github.com/penecho/penecho), licensed **AGPL-3.0-only** — the
same licence as this project.

| File | Upstream path |
| --- | --- |
| `draw.js` | `public/draw.js` |
| `mixed-text.js` | `public/mixed-text.js` |
| `selection.js` | `public/selection.js` |

They are zero-dependency UMD modules and are intentionally **not
modified**. Fixes belong upstream; re-copy to update. `tests/vendor-*.test.ts`
are ports of PenEcho's own `test/draw.test.js`, `test/mixed-text.test.js`
and `test/selection.test.js`, kept so a re-copy that changes behaviour
fails loudly here.

The whiteboard system prompt at
`surogates/harness/prompts/guidance/whiteboard.md` is also adapted from
PenEcho and carries its own attribution.
```

- [ ] **Step 3: Write the typed barrel**

Create `sdk/agent-chat-react/src/vendor/penecho/index.ts`:

```ts
// Typed access to the vendored PenEcho UMD modules. The .js files are
// verbatim upstream copies and carry no types of their own; this barrel
// is the only place that asserts their shape, so a re-copy that changes
// an export surfaces here rather than at twenty call sites.

// @ts-expect-error -- untyped vendored UMD module
import drawModule from "./draw.js";
// @ts-expect-error -- untyped vendored UMD module
import mixedTextModule from "./mixed-text.js";
// @ts-expect-error -- untyped vendored UMD module
import selectionModule from "./selection.js";

export interface DrawBounds {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface NormalizedDraw {
  width: number;
  tension: number;
  _draw: { bounds: DrawBounds; primitives: unknown[] };
}

export interface RenderedDraw {
  image: HTMLCanvasElement;
  x: number;
  y: number;
}

export interface DrawApi {
  /** Validate + normalize a `draw` command. Returns null when invalid. */
  normalize(command: unknown, canvasSize?: number): NormalizedDraw | null;
  /** Rasterise a `draw` command. Returns null when invalid. */
  render(
    command: unknown,
    createCanvas: (w: number, h: number) => HTMLCanvasElement,
    color?: string,
  ): RenderedDraw | null;
  smoothSegments(
    points: { x: number; y: number }[],
    closed: boolean,
    tension: number,
  ): unknown[];
}

export const DRAW = drawModule as DrawApi;
export const MIXED_TEXT = mixedTextModule as Record<string, unknown>;
export const SELECTION = selectionModule as Record<string, unknown>;
```

- [ ] **Step 4: Write the failing test**

Create `sdk/agent-chat-react/tests/vendor-draw.test.ts`. This is a port of
`study/penecho/test/draw.test.js` — read that file first and carry over
its cases. Its fake canvas uses a `Proxy` that records every 2D-context
call, which works under happy-dom (which has no real 2D context):

```ts
import { describe, expect, it } from "vitest";
import { DRAW } from "@/vendor/penecho";

/** Records every 2D-context call. Ported from PenEcho's test/draw.test.js:6 —
 *  happy-dom has no real canvas 2D context, so the renderer is exercised
 *  against a recorder instead. */
function fakeCanvas(width: number, height: number) {
  const calls: unknown[][] = [];
  const context = new Proxy({ calls } as Record<string, unknown>, {
    get(target, property) {
      if (property in target) return target[property as string];
      return (...args: unknown[]) => calls.push([property, ...args]);
    },
    set(target, property, value) {
      target[property as string] = value;
      return true;
    },
  });
  return {
    width,
    height,
    calls,
    getContext: () => context,
  } as unknown as HTMLCanvasElement & { calls: unknown[][] };
}

const rect = {
  origin: [100, 100],
  types: ["rect"],
  items: [[0, 0, 200, 120]],
};

describe("vendored draw.js", () => {
  it("normalizes a valid rect command", () => {
    const result = DRAW.normalize(rect);
    expect(result).not.toBeNull();
    expect(result!._draw.bounds.w).toBeGreaterThan(0);
  });

  it("rejects mismatched types and items", () => {
    expect(DRAW.normalize({ ...rect, types: ["rect", "circle"] })).toBeNull();
  });

  it("rejects an unknown primitive type", () => {
    expect(DRAW.normalize({ ...rect, types: ["squiggle"] })).toBeNull();
  });

  it("rejects a width outside 2..200", () => {
    expect(DRAW.normalize({ ...rect, width: 500 })).toBeNull();
    expect(DRAW.normalize({ ...rect, width: 1 })).toBeNull();
  });

  it("rejects an origin outside the canvas", () => {
    expect(DRAW.normalize({ ...rect, origin: [999999, 0] })).toBeNull();
  });

  it("includes stroke padding in the bounds", () => {
    const bounds = DRAW.normalize({ ...rect, width: 30 })!._draw.bounds;
    expect(bounds.w).toBeGreaterThan(200);
    expect(bounds.h).toBeGreaterThan(120);
  });

  it("renders a rect and returns its logical origin", () => {
    const rendered = DRAW.render(rect, (w, h) => fakeCanvas(w, h));
    expect(rendered).not.toBeNull();
    expect(rendered!.image.width).toBeGreaterThan(0);
    expect(typeof rendered!.x).toBe("number");
  });

  it("returns null from render for an invalid command", () => {
    expect(DRAW.render({ types: [] }, (w, h) => fakeCanvas(w, h))).toBeNull();
  });

  it("produces one smooth segment per gap on an open path", () => {
    const points = [
      { x: 0, y: 0 },
      { x: 10, y: 10 },
      { x: 20, y: 0 },
    ];
    expect(DRAW.smoothSegments(points, false, 50)).toHaveLength(2);
  });

  it("closes the loop on a closed path", () => {
    const points = [
      { x: 0, y: 0 },
      { x: 10, y: 10 },
      { x: 20, y: 0 },
    ];
    expect(DRAW.smoothSegments(points, true, 50)).toHaveLength(3);
  });
});
```

- [ ] **Step 5: Run the test**

Run: `cd sdk/agent-chat-react && pnpm vitest run tests/vendor-draw.test.ts`
Expected: PASS, 10 passed

If the UMD default-import shape does not resolve under vitest, change the
barrel's imports to `import * as drawModule from "./draw.js"` and adjust
the cast; do not modify the vendored file.

- [ ] **Step 6: Verify tsup still builds**

Run: `pnpm build`
Expected: build succeeds, `dist/index.js` emitted.

- [ ] **Step 7: Commit**

```bash
git add sdk/agent-chat-react/src/vendor/ sdk/agent-chat-react/tests/vendor-draw.test.ts
git commit -m "feat(whiteboard): vendor PenEcho geometry modules"
```

---

## Task 11: Canvas document and event fold

**Files:**
- Create: `sdk/agent-chat-react/src/components/whiteboard/doc.ts`
- Test: `sdk/agent-chat-react/tests/whiteboard-doc.test.ts`

**Interfaces:**
- Consumes: `AgentChatMessage`, `AgentChatToolCallInfo` from `@/types`.
- Produces:
  - `type WbObject` and the `WbDoc` shape `{ version: 1; objects: WbObject[]; lastEventId: number }`
  - `emptyDoc(): WbDoc`
  - `applyCommands(doc: WbDoc, commands: unknown[], eventId: number): WbDoc`
  - `foldToolCalls(doc: WbDoc, messages: AgentChatMessage[]): WbDoc`
  - `CANVAS_SIZE: 20000`

- [ ] **Step 1: Write the failing test**

Create `sdk/agent-chat-react/tests/whiteboard-doc.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import {
  applyCommands,
  emptyDoc,
  foldToolCalls,
} from "@/components/whiteboard/doc";
import type { AgentChatMessage } from "@/types";

const text = {
  tool: "write_text",
  x: 10,
  y: 20,
  text: "5",
  fontSize: 32,
  maxWidth: 300,
};

function message(
  id: string,
  toolName: string,
  args: unknown,
): AgentChatMessage {
  return {
    id,
    role: "assistant",
    content: "",
    toolCalls: [
      { id, toolName, args: JSON.stringify(args), status: "complete" },
    ],
  } as unknown as AgentChatMessage;
}

describe("canvas document", () => {
  it("starts empty", () => {
    const doc = emptyDoc();
    expect(doc.objects).toEqual([]);
    expect(doc.lastEventId).toBe(0);
  });

  it("appends one object per command", () => {
    const doc = applyCommands(emptyDoc(), [text, text], 7);
    expect(doc.objects).toHaveLength(2);
    expect(doc.lastEventId).toBe(7);
  });

  it("gives every object a distinct id", () => {
    const doc = applyCommands(emptyDoc(), [text, text], 1);
    expect(doc.objects[0].id).not.toBe(doc.objects[1].id);
  });

  it("maps write_text onto a text object", () => {
    const [obj] = applyCommands(emptyDoc(), [text], 1).objects;
    expect(obj.kind).toBe("text");
    expect(obj).toMatchObject({ x: 10, y: 20, text: "5", maxWidth: 300 });
  });

  it("defaults lineHeight when the model omits it", () => {
    const [obj] = applyCommands(emptyDoc(), [text], 1).objects;
    expect((obj as { lineHeight: number }).lineHeight).toBe(1.35);
  });

  it("maps draw_formula onto a formula object", () => {
    const [obj] = applyCommands(emptyDoc(), [{
      tool: "draw_formula", x: 1, y: 2, latex: "x^2", fontSize: 40,
    }], 1).objects;
    expect(obj.kind).toBe("formula");
  });

  it("maps place_artifact onto an artifact object", () => {
    const [obj] = applyCommands(emptyDoc(), [{
      tool: "place_artifact", artifact_id: "a1", x: 0, y: 0, w: 100, h: 80,
    }], 1).objects;
    expect(obj).toMatchObject({ kind: "artifact", artifactId: "a1" });
  });

  it("skips a command the vendored validator rejects", () => {
    const doc = applyCommands(emptyDoc(), [
      text,
      { tool: "draw", origin: [0, 0], types: ["rect", "circle"], items: [[0, 0, 1, 1]] },
    ], 1);
    expect(doc.objects).toHaveLength(1);
  });

  it("skips an unknown command tool", () => {
    expect(applyCommands(emptyDoc(), [{ tool: "nope" }], 1).objects).toHaveLength(0);
  });

  it("marks objects from one call as the active selection", () => {
    const doc = applyCommands(emptyDoc(), [text, text], 1);
    expect(doc.objects.every((o) => o.selected)).toBe(true);
  });

  it("clears the previous selection when a new call lands", () => {
    const first = applyCommands(emptyDoc(), [text], 1);
    const second = applyCommands(first, [text], 2);
    expect(second.objects[0].selected).toBe(false);
    expect(second.objects[1].selected).toBe(true);
  });

  it("folds whiteboard_draw tool calls out of the message list", () => {
    const doc = foldToolCalls(emptyDoc(), [
      message("m1", "whiteboard_draw", { commands: [text] }),
      message("m2", "web_search", { query: "x" }),
      message("m3", "whiteboard_draw", { commands: [text, text] }),
    ]);
    expect(doc.objects).toHaveLength(3);
  });

  it("ignores a tool call whose args are not valid JSON", () => {
    const broken = {
      id: "m1",
      role: "assistant",
      content: "",
      toolCalls: [
        { id: "m1", toolName: "whiteboard_draw", args: "{", status: "complete" },
      ],
    } as unknown as AgentChatMessage;
    expect(foldToolCalls(emptyDoc(), [broken]).objects).toHaveLength(0);
  });

  it("ignores a message with no tool calls", () => {
    const plain = {
      id: "m1", role: "assistant", content: "hello",
    } as unknown as AgentChatMessage;
    expect(foldToolCalls(emptyDoc(), [plain]).objects).toHaveLength(0);
  });

  it("is idempotent when folded twice over the same messages", () => {
    // The SSE stream and the reconciliation poll both deliver the same
    // events, so a re-fold must not duplicate objects.
    const messages = [message("m1", "whiteboard_draw", { commands: [text] })];
    const once = foldToolCalls(emptyDoc(), messages);
    const twice = foldToolCalls(once, messages);
    expect(twice.objects).toHaveLength(1);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd sdk/agent-chat-react && pnpm vitest run tests/whiteboard-doc.test.ts`
Expected: FAIL — cannot resolve `@/components/whiteboard/doc`

- [ ] **Step 3: Write the implementation**

Create `sdk/agent-chat-react/src/components/whiteboard/doc.ts`:

```ts
import type { AgentChatMessage } from "../../types";
import { DRAW } from "../../vendor/penecho";

/** Logical canvas edge. Must match surogates/whiteboard/commands.py. */
export const CANVAS_SIZE = 20_000;

/** Default text line-height multiplier when the model omits one. */
const DEFAULT_LINE_HEIGHT = 1.35;

export interface WbBase {
  id: string;
  /** Source tool call id, or "local" for the user's own edits. Folding
   *  is keyed on this so a re-delivered event cannot duplicate objects. */
  origin: string;
  selected: boolean;
}

export type WbObject = WbBase &
  (
    | { kind: "ink"; pts: number[]; width: number; color: string }
    | {
        kind: "draw";
        origin_: [number, number];
        types: string[];
        items: number[][];
        width?: number;
        tension?: number;
        closed?: number[];
        fill?: number[];
        arrows?: number[];
      }
    | {
        kind: "text";
        x: number;
        y: number;
        text: string;
        fontSize: number;
        maxWidth: number;
        lineHeight: number;
      }
    | { kind: "formula"; x: number; y: number; latex: string; fontSize: number }
    | {
        kind: "artifact";
        x: number;
        y: number;
        w: number;
        h: number;
        artifactId: string;
        version?: number;
      }
    | {
        kind: "erase";
        mode: "rect" | "path";
        x?: number;
        y?: number;
        w?: number;
        h?: number;
        points?: number[][];
        size?: number;
      }
  );

export interface WbDoc {
  version: 1;
  objects: WbObject[];
  /** Highest event id folded in. The persistence layer replays only tool
   *  calls newer than this after loading the saved document. */
  lastEventId: number;
}

export function emptyDoc(): WbDoc {
  return { version: 1, objects: [], lastEventId: 0 };
}

let localCounter = 0;
function nextId(origin: string): string {
  localCounter += 1;
  return `${origin}:${localCounter}`;
}

/** Convert one validated command into an object, or null to skip it. */
function toObject(cmd: Record<string, unknown>, origin: string): WbObject | null {
  const base = { id: nextId(origin), origin, selected: true };
  switch (cmd.tool) {
    case "write_text":
      return {
        ...base,
        kind: "text",
        x: Number(cmd.x),
        y: Number(cmd.y),
        text: String(cmd.text ?? ""),
        fontSize: Number(cmd.fontSize),
        maxWidth: Number(cmd.maxWidth),
        lineHeight: Number(cmd.lineHeight ?? DEFAULT_LINE_HEIGHT),
      };
    case "draw_formula":
      return {
        ...base,
        kind: "formula",
        x: Number(cmd.x),
        y: Number(cmd.y),
        latex: String(cmd.latex ?? ""),
        fontSize: Number(cmd.fontSize),
      };
    case "draw":
      // The vendored normalizer is authoritative for geometry: if it
      // rejects the command the renderer would produce nothing, so drop
      // it here rather than carry an object that can never paint.
      if (DRAW.normalize(cmd, CANVAS_SIZE) === null) return null;
      return {
        ...base,
        kind: "draw",
        origin_: cmd.origin as [number, number],
        types: cmd.types as string[],
        items: cmd.items as number[][],
        width: cmd.width as number | undefined,
        tension: cmd.tension as number | undefined,
        closed: cmd.closed as number[] | undefined,
        fill: cmd.fill as number[] | undefined,
        arrows: cmd.arrows as number[] | undefined,
      };
    case "erase":
      if (cmd.mode !== "rect" && cmd.mode !== "path") return null;
      return {
        ...base,
        kind: "erase",
        mode: cmd.mode,
        x: cmd.x as number | undefined,
        y: cmd.y as number | undefined,
        w: cmd.w as number | undefined,
        h: cmd.h as number | undefined,
        points: cmd.points as number[][] | undefined,
        size: cmd.size as number | undefined,
      };
    case "place_artifact":
      if (typeof cmd.artifact_id !== "string" || !cmd.artifact_id) return null;
      return {
        ...base,
        kind: "artifact",
        artifactId: cmd.artifact_id,
        x: Number(cmd.x),
        y: Number(cmd.y),
        w: Number(cmd.w),
        h: Number(cmd.h),
      };
    default:
      return null;
  }
}

/**
 * Append one `whiteboard_draw` call's commands.
 *
 * New objects arrive as the active selection and clear the previous one,
 * so the user can immediately drag, resize or delete what the agent just
 * drew — this is what replaces PenEcho's unconfirmed-draft layer.
 */
export function applyCommands(
  doc: WbDoc,
  commands: unknown[],
  eventId: number,
  origin = `evt${eventId}`,
): WbDoc {
  if (!Array.isArray(commands)) return doc;
  const added: WbObject[] = [];
  for (const cmd of commands) {
    if (typeof cmd !== "object" || cmd === null) continue;
    const obj = toObject(cmd as Record<string, unknown>, origin);
    if (obj) added.push(obj);
  }
  if (added.length === 0) {
    return { ...doc, lastEventId: Math.max(doc.lastEventId, eventId) };
  }
  return {
    version: 1,
    objects: [
      ...doc.objects.map((o) => (o.selected ? { ...o, selected: false } : o)),
      ...added,
    ],
    lastEventId: Math.max(doc.lastEventId, eventId),
  };
}

/**
 * Fold every `whiteboard_draw` tool call in *messages* into *doc*.
 *
 * Idempotent on the tool call id: the SSE stream and the reconciliation
 * poll deliver the same events, so a re-fold must not duplicate objects.
 */
export function foldToolCalls(
  doc: WbDoc,
  messages: AgentChatMessage[],
): WbDoc {
  const seen = new Set(doc.objects.map((o) => o.origin));
  let next = doc;
  for (const message of messages) {
    for (const call of message.toolCalls ?? []) {
      if (call.toolName !== "whiteboard_draw") continue;
      if (seen.has(call.id)) continue;
      let parsed: unknown;
      try {
        parsed = JSON.parse(call.args);
      } catch {
        continue;
      }
      const commands = (parsed as { commands?: unknown })?.commands;
      if (!Array.isArray(commands)) continue;
      next = applyCommands(next, commands, next.lastEventId, call.id);
      seen.add(call.id);
    }
  }
  return next;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pnpm vitest run tests/whiteboard-doc.test.ts`
Expected: PASS, 15 passed

- [ ] **Step 5: Typecheck**

Run: `pnpm typecheck`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add sdk/agent-chat-react/src/components/whiteboard/doc.ts \
        sdk/agent-chat-react/tests/whiteboard-doc.test.ts
git commit -m "feat(whiteboard): add the canvas document and event fold"
```

---

## Task 12: Canvas renderer

**Files:**
- Create: `sdk/agent-chat-react/src/components/whiteboard/render.ts`
- Test: `sdk/agent-chat-react/tests/whiteboard-render.test.ts`

**Interfaces:**
- Consumes: `WbDoc`, `WbObject`, `CANVAS_SIZE` (Task 11); `DRAW` (Task 10).
- Produces:
  - `interface View { x: number; y: number; zoom: number }`
  - `renderDoc(ctx: CanvasRenderingContext2D, doc: WbDoc, view: View, size: { w: number; h: number }): void`
  - `objectBounds(obj: WbObject, measure: TextMeasure): DrawBounds | null`
  - `type TextMeasure = (text: string, fontSize: number, maxWidth: number) => { w: number; h: number }`
  - `hitTest(doc: WbDoc, pt: { x: number; y: number }, measure: TextMeasure): WbObject | null`

- [ ] **Step 1: Write the failing test**

Create `sdk/agent-chat-react/tests/whiteboard-render.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { emptyDoc, applyCommands } from "@/components/whiteboard/doc";
import {
  hitTest,
  objectBounds,
  renderDoc,
} from "@/components/whiteboard/render";

/** happy-dom has no 2D context; record calls instead. */
function recordingContext() {
  const calls: unknown[][] = [];
  return new Proxy({ calls } as Record<string, unknown>, {
    get(target, property) {
      if (property in target) return target[property as string];
      if (property === "measureText") {
        return (t: string) => ({ width: t.length * 8 });
      }
      return (...args: unknown[]) => calls.push([property, ...args]);
    },
    set(target, property, value) {
      target[property as string] = value;
      return true;
    },
  }) as unknown as CanvasRenderingContext2D & { calls: unknown[][] };
}

const measure = (text: string, fontSize: number, maxWidth: number) => ({
  w: maxWidth,
  h: Math.ceil((text.length * fontSize * 0.6) / maxWidth) * fontSize * 1.35,
});

const view = { x: 0, y: 0, zoom: 1 };
const size = { w: 800, h: 600 };

const text = {
  tool: "write_text", x: 10, y: 20, text: "hello",
  fontSize: 32, maxWidth: 300,
};

describe("canvas renderer", () => {
  it("clears before painting", () => {
    const ctx = recordingContext();
    renderDoc(ctx, emptyDoc(), view, size);
    expect(ctx.calls.some((c) => c[0] === "clearRect")).toBe(true);
  });

  it("paints nothing else for an empty document", () => {
    const ctx = recordingContext();
    renderDoc(ctx, emptyDoc(), view, size);
    expect(ctx.calls.some((c) => c[0] === "fillText")).toBe(false);
  });

  it("paints a text object", () => {
    const ctx = recordingContext();
    renderDoc(ctx, applyCommands(emptyDoc(), [text], 1), view, size);
    expect(ctx.calls.some((c) => c[0] === "fillText")).toBe(true);
  });

  it("strokes an ink object", () => {
    const ctx = recordingContext();
    const doc = emptyDoc();
    doc.objects.push({
      id: "i1", origin: "local", selected: false, kind: "ink",
      pts: [0, 0, 10, 10, 20, 5], width: 4, color: "#111",
    });
    renderDoc(ctx, doc, view, size);
    expect(ctx.calls.some((c) => c[0] === "stroke")).toBe(true);
  });

  it("paints selection chrome for a selected object", () => {
    const ctx = recordingContext();
    renderDoc(ctx, applyCommands(emptyDoc(), [text], 1), view, size);
    expect(ctx.calls.some((c) => c[0] === "strokeRect")).toBe(true);
  });

  it("paints no selection chrome when nothing is selected", () => {
    const ctx = recordingContext();
    const doc = applyCommands(emptyDoc(), [text], 1);
    doc.objects[0].selected = false;
    renderDoc(ctx, doc, view, size);
    expect(ctx.calls.some((c) => c[0] === "strokeRect")).toBe(false);
  });

  it("applies the view transform", () => {
    const ctx = recordingContext();
    renderDoc(ctx, emptyDoc(), { x: 100, y: 50, zoom: 2 }, size);
    expect(ctx.calls.some((c) => c[0] === "setTransform")).toBe(true);
  });

  it("computes bounds for a text object", () => {
    const [obj] = applyCommands(emptyDoc(), [text], 1).objects;
    const bounds = objectBounds(obj, measure);
    expect(bounds).not.toBeNull();
    expect(bounds!.w).toBe(300);
  });

  it("computes bounds for an ink object from its points", () => {
    const bounds = objectBounds({
      id: "i1", origin: "local", selected: false, kind: "ink",
      pts: [0, 0, 100, 40], width: 4, color: "#111",
    }, measure);
    expect(bounds!.w).toBeGreaterThanOrEqual(100);
  });

  it("returns null bounds for an erase object", () => {
    // Erase is a clipping instruction, not a hittable object.
    expect(objectBounds({
      id: "e1", origin: "local", selected: false, kind: "erase",
      mode: "rect", x: 0, y: 0, w: 10, h: 10,
    }, measure)).toBeNull();
  });

  it("hit-tests a point inside an object", () => {
    const doc = applyCommands(emptyDoc(), [text], 1);
    expect(hitTest(doc, { x: 20, y: 30 }, measure)).not.toBeNull();
  });

  it("returns null for a point over empty canvas", () => {
    const doc = applyCommands(emptyDoc(), [text], 1);
    expect(hitTest(doc, { x: 9000, y: 9000 }, measure)).toBeNull();
  });

  it("hit-tests topmost first", () => {
    let doc = applyCommands(emptyDoc(), [text], 1);
    doc = applyCommands(doc, [{ ...text, text: "second" }], 2);
    const hit = hitTest(doc, { x: 20, y: 30 }, measure);
    expect(hit!.id).toBe(doc.objects[1].id);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm vitest run tests/whiteboard-render.test.ts`
Expected: FAIL — cannot resolve `@/components/whiteboard/render`

- [ ] **Step 3: Write the implementation**

Create `sdk/agent-chat-react/src/components/whiteboard/render.ts`. It
must:

1. `ctx.setTransform(zoom, 0, 0, zoom, -view.x * zoom, -view.y * zoom)` then
   `ctx.clearRect` over the visible logical rectangle.
2. Paint objects in array order — array position is z-order.
3. `ink`: `beginPath` / `moveTo` / `lineTo` over `pts` pairs, `lineCap` and
   `lineJoin` `"round"`, `lineWidth = width`, `strokeStyle = color`, `stroke()`.
4. `draw`: call `DRAW.render(cmd, createCanvas, color)` and `drawImage` the
   returned canvas at `{x, y}`. Build `cmd` back from the object's
   `origin_`/`types`/`items`/`width`/`tension`/`closed`/`fill`/`arrows`.
5. `text`: wrap at `maxWidth` using `ctx.measureText`, `fillText` each line
   at `lineHeight * fontSize` steps.
6. `formula`: render via `@streamdown/math` (already a dependency) into an
   offscreen element and `drawImage` it; cache by `latex + fontSize` since
   re-typesetting every frame is the obvious way to make this drop frames.
7. `artifact`: paint a placeholder frame only. The real artifact renders in
   a positioned DOM overlay above the canvas — reuse the existing
   `components/artifacts/artifact-block.tsx`. A canvas cannot host an
   iframe, and `html` artifacts are iframes.
8. `erase`: `save()`, `globalCompositeOperation = "destination-out"`, paint
   the rect or the stroked path, `restore()`.
9. Selection chrome: for each `obj.selected`, `strokeRect` a dashed box
   around `objectBounds(obj, measure)` plus corner handles.

`objectBounds` returns `null` for `erase` (a clipping instruction, not a
hittable object) and a `DrawBounds` otherwise. For `draw`, take
`DRAW.normalize(cmd, CANVAS_SIZE)!._draw.bounds` — it already accounts for
curve extrema, stroke padding and arrowheads, which is exactly the
computation not worth rewriting.

`hitTest` walks `doc.objects` **backwards** so the topmost object wins, and
returns the first whose bounds contain the point.

- [ ] **Step 4: Run test to verify it passes**

Run: `pnpm vitest run tests/whiteboard-render.test.ts`
Expected: PASS, 13 passed

- [ ] **Step 5: Commit**

```bash
git add sdk/agent-chat-react/src/components/whiteboard/render.ts \
        sdk/agent-chat-react/tests/whiteboard-render.test.ts
git commit -m "feat(whiteboard): render canvas objects"
```

---

## Task 13: Pointer input, pan and zoom

**Files:**
- Create: `sdk/agent-chat-react/src/components/whiteboard/input.ts`
- Test: `sdk/agent-chat-react/tests/whiteboard-input.test.ts`

**Interfaces:**
- Consumes: `View` (Task 12), `WbObject`, `CANVAS_SIZE` (Task 11).
- Produces:
  - `screenToLogical(pt: {x,y}, view: View): {x,y}` and `logicalToScreen(pt, view)`
  - `class StrokeBuilder` with `begin(pt, pressure)`, `extend(pt, pressure)`, `finish(): WbObject | null`
  - `strokePointsFromEvent(event: PointerEvent): {x,y,pressure}[]` — uses `getCoalescedEvents()` when available
  - `clampView(view: View, size: {w,h}): View`
  - `zoomAt(view: View, screenPt: {x,y}, factor: number): View`

- [ ] **Step 1: Write the failing test**

Create `sdk/agent-chat-react/tests/whiteboard-input.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import {
  StrokeBuilder,
  clampView,
  logicalToScreen,
  screenToLogical,
  strokePointsFromEvent,
  zoomAt,
} from "@/components/whiteboard/input";
import { CANVAS_SIZE } from "@/components/whiteboard/doc";

describe("coordinate mapping", () => {
  it("round-trips a point through screen and back", () => {
    const view = { x: 500, y: 300, zoom: 1.5 };
    const logical = { x: 1234, y: 5678 };
    const back = screenToLogical(logicalToScreen(logical, view), view);
    expect(back.x).toBeCloseTo(logical.x, 6);
    expect(back.y).toBeCloseTo(logical.y, 6);
  });

  it("is identity at origin and zoom 1", () => {
    const view = { x: 0, y: 0, zoom: 1 };
    expect(screenToLogical({ x: 10, y: 20 }, view)).toEqual({ x: 10, y: 20 });
  });
});

describe("zoom", () => {
  it("keeps the anchor point stationary", () => {
    const view = { x: 100, y: 100, zoom: 1 };
    const anchor = { x: 400, y: 300 };
    const before = screenToLogical(anchor, view);
    const after = screenToLogical(anchor, zoomAt(view, anchor, 2));
    expect(after.x).toBeCloseTo(before.x, 4);
    expect(after.y).toBeCloseTo(before.y, 4);
  });

  it("multiplies the zoom factor", () => {
    expect(zoomAt({ x: 0, y: 0, zoom: 1 }, { x: 0, y: 0 }, 2).zoom).toBeCloseTo(2);
  });

  it("clamps zoom to a sane range", () => {
    const far = zoomAt({ x: 0, y: 0, zoom: 1 }, { x: 0, y: 0 }, 1000);
    expect(far.zoom).toBeLessThanOrEqual(8);
    const near = zoomAt({ x: 0, y: 0, zoom: 1 }, { x: 0, y: 0 }, 0.0001);
    expect(near.zoom).toBeGreaterThanOrEqual(0.05);
  });
});

describe("view clamping", () => {
  it("keeps the viewport inside the canvas", () => {
    const clamped = clampView({ x: -5000, y: -5000, zoom: 1 }, { w: 800, h: 600 });
    expect(clamped.x).toBeGreaterThanOrEqual(0);
    expect(clamped.y).toBeGreaterThanOrEqual(0);
  });

  it("stops the viewport past the far edge", () => {
    const clamped = clampView(
      { x: CANVAS_SIZE + 1000, y: 0, zoom: 1 },
      { w: 800, h: 600 },
    );
    expect(clamped.x).toBeLessThanOrEqual(CANVAS_SIZE);
  });
});

describe("stroke building", () => {
  it("produces an ink object from three points", () => {
    const b = new StrokeBuilder("#111", 4);
    b.begin({ x: 0, y: 0 }, 0.5);
    b.extend({ x: 10, y: 10 }, 0.5);
    b.extend({ x: 20, y: 0 }, 0.5);
    const obj = b.finish();
    expect(obj).not.toBeNull();
    expect(obj!.kind).toBe("ink");
    expect((obj as { pts: number[] }).pts).toEqual([0, 0, 10, 10, 20, 0]);
  });

  it("discards a single-point tap", () => {
    const b = new StrokeBuilder("#111", 4);
    b.begin({ x: 5, y: 5 }, 0.5);
    expect(b.finish()).toBeNull();
  });

  it("discards a stroke that never began", () => {
    expect(new StrokeBuilder("#111", 4).finish()).toBeNull();
  });

  it("scales width by mean pressure", () => {
    const light = new StrokeBuilder("#111", 10);
    light.begin({ x: 0, y: 0 }, 0.1);
    light.extend({ x: 10, y: 0 }, 0.1);
    const heavy = new StrokeBuilder("#111", 10);
    heavy.begin({ x: 0, y: 0 }, 1);
    heavy.extend({ x: 10, y: 0 }, 1);
    expect((heavy.finish() as { width: number }).width)
      .toBeGreaterThan((light.finish() as { width: number }).width);
  });

  it("clamps points to the canvas", () => {
    const b = new StrokeBuilder("#111", 4);
    b.begin({ x: -50, y: -50 }, 0.5);
    b.extend({ x: CANVAS_SIZE + 50, y: 10 }, 0.5);
    const pts = (b.finish() as { pts: number[] }).pts;
    expect(Math.min(...pts)).toBeGreaterThanOrEqual(0);
    expect(Math.max(...pts)).toBeLessThanOrEqual(CANVAS_SIZE);
  });
});

describe("coalesced pointer samples", () => {
  it("uses getCoalescedEvents when the browser provides it", () => {
    const event = {
      clientX: 30, clientY: 40, pressure: 0.9,
      getCoalescedEvents: () => [
        { clientX: 10, clientY: 20, pressure: 0.5 },
        { clientX: 20, clientY: 30, pressure: 0.7 },
      ],
    } as unknown as PointerEvent;
    expect(strokePointsFromEvent(event)).toHaveLength(2);
  });

  it("falls back to the event itself", () => {
    const event = {
      clientX: 30, clientY: 40, pressure: 0.9,
    } as unknown as PointerEvent;
    const pts = strokePointsFromEvent(event);
    expect(pts).toHaveLength(1);
    expect(pts[0]).toMatchObject({ x: 30, y: 40 });
  });

  it("substitutes a default pressure for a mouse", () => {
    // A mouse reports pressure 0 while the button is down; treating that
    // literally makes every mouse stroke invisible.
    const event = {
      clientX: 1, clientY: 2, pressure: 0,
    } as unknown as PointerEvent;
    expect(strokePointsFromEvent(event)[0].pressure).toBeGreaterThan(0);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm vitest run tests/whiteboard-input.test.ts`
Expected: FAIL — cannot resolve `@/components/whiteboard/input`

- [ ] **Step 3: Write the implementation**

Create `sdk/agent-chat-react/src/components/whiteboard/input.ts`.

Read `study/penecho/src/client/app/persistence.js:2500` for the coalesced-
event handling and `study/penecho/src/client/app/ui-bootstrap.js:16`
(`beginCanvasPointerAction`) for the tool dispatch shape before writing.

Required behaviour, all covered by the test above:

- `screenToLogical({x,y}, view)` = `{ x: view.x + x / view.zoom, y: view.y + y / view.zoom }`;
  `logicalToScreen` is its inverse.
- `zoomAt(view, screenPt, factor)` clamps the resulting zoom to `[0.05, 8]`
  and solves for the new `x`/`y` so the logical point under `screenPt`
  does not move.
- `clampView(view, size)` keeps `x` and `y` in `[0, CANVAS_SIZE]`.
- `strokePointsFromEvent(event)` returns `event.getCoalescedEvents()`
  mapped to `{x, y, pressure}` when the method exists, else a single
  sample from the event. Pressure `0` (mouse) becomes `0.5`.
- `StrokeBuilder(color, baseWidth)` accumulates points, clamps each to
  `[0, CANVAS_SIZE]`, and `finish()` returns `null` for fewer than two
  points, otherwise an `ink` object with `width` scaled by mean pressure
  (`baseWidth * (0.5 + meanPressure)`), rounded and clamped to the same
  `2..200` range `draw.js` uses.

- [ ] **Step 4: Run test to verify it passes**

Run: `pnpm vitest run tests/whiteboard-input.test.ts`
Expected: PASS, 16 passed

- [ ] **Step 5: Commit**

```bash
git add sdk/agent-chat-react/src/components/whiteboard/input.ts \
        sdk/agent-chat-react/tests/whiteboard-input.test.ts
git commit -m "feat(whiteboard): add pointer ink, pan and zoom"
```

---

## Task 14: Atlas and hotspot grid

This is the port with the most behavioural nuance. Read
`study/penecho/src/client/app/ai-runtime.js` first, specifically
`inkBox` (line 182), `viewportRect` (488), `visibleInkBounds` (496),
`mapHotspots` (523), `captureRectFor` (553), `planViewportImage` (557)
and `buildViewportImage` (580).

**Files:**
- Create: `sdk/agent-chat-react/src/components/whiteboard/atlas.ts`
- Test: `sdk/agent-chat-react/tests/whiteboard-atlas.test.ts`

**Interfaces:**
- Consumes: `WbDoc`, `View`, `objectBounds`, `renderDoc` (Tasks 11-12).
- Produces:
  - `MAX_ATLAS_WIDTH = 2048`, `MAX_ATLAS_HEIGHT = 1536`, `HOTSPOT_GRID = 8`
  - `interface Rect { x: number; y: number; w: number; h: number }`
  - `planAtlas(doc: WbDoc, latest: Rect | null, view: View, viewport: {w,h}): { sourceRect: Rect; imageScale: number; imageSize: {w,h} }`
  - `mapHotspots(sourceRect: Rect, points: {x,y}[]): number[][]`
  - `buildAtlas(doc, plan, createCanvas): HTMLCanvasElement`
  - `atlasMetadata(plan, latest, hotspots, extra): Record<string, unknown>`

- [ ] **Step 1: Write the failing test**

Create `sdk/agent-chat-react/tests/whiteboard-atlas.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import {
  HOTSPOT_GRID,
  MAX_ATLAS_HEIGHT,
  MAX_ATLAS_WIDTH,
  atlasMetadata,
  mapHotspots,
  planAtlas,
} from "@/components/whiteboard/atlas";
import { applyCommands, emptyDoc } from "@/components/whiteboard/doc";

const viewport = { w: 800, h: 600 };
const view = { x: 0, y: 0, zoom: 1 };
const text = {
  tool: "write_text", x: 1000, y: 1000, text: "hello",
  fontSize: 32, maxWidth: 300,
};

describe("atlas planning", () => {
  it("never exceeds the image caps", () => {
    const plan = planAtlas(
      emptyDoc(),
      { x: 0, y: 0, w: 19000, h: 18000 },
      view,
      viewport,
    );
    expect(plan.imageSize.w).toBeLessThanOrEqual(MAX_ATLAS_WIDTH);
    expect(plan.imageSize.h).toBeLessThanOrEqual(MAX_ATLAS_HEIGHT);
  });

  it("never upscales past 1:1", () => {
    const plan = planAtlas(emptyDoc(), { x: 0, y: 0, w: 10, h: 10 }, view, viewport);
    expect(plan.imageScale).toBeLessThanOrEqual(1);
  });

  it("covers the latest input rectangle", () => {
    const latest = { x: 1000, y: 1000, w: 200, h: 100 };
    const { sourceRect } = planAtlas(emptyDoc(), latest, view, viewport);
    expect(sourceRect.x).toBeLessThanOrEqual(latest.x);
    expect(sourceRect.y).toBeLessThanOrEqual(latest.y);
    expect(sourceRect.x + sourceRect.w).toBeGreaterThanOrEqual(latest.x + latest.w);
    expect(sourceRect.y + sourceRect.h).toBeGreaterThanOrEqual(latest.y + latest.h);
  });

  it("covers existing content when there is no latest input", () => {
    const doc = applyCommands(emptyDoc(), [text], 1);
    const { sourceRect } = planAtlas(doc, null, view, viewport);
    expect(sourceRect.x).toBeLessThanOrEqual(1000);
  });

  it("falls back to the viewport for an empty canvas", () => {
    const { sourceRect } = planAtlas(emptyDoc(), null, view, viewport);
    expect(sourceRect.w).toBeGreaterThan(0);
    expect(sourceRect.h).toBeGreaterThan(0);
  });

  it("produces a positive image size for an empty canvas", () => {
    const { imageSize } = planAtlas(emptyDoc(), null, view, viewport);
    expect(imageSize.w).toBeGreaterThan(0);
    expect(imageSize.h).toBeGreaterThan(0);
  });
});

describe("hotspot grid", () => {
  const rect = { x: 0, y: 0, w: 800, h: 800 };

  it("maps a point to its grid cell", () => {
    expect(mapHotspots(rect, [{ x: 50, y: 50 }])).toEqual([[0, 0]]);
  });

  it("maps the far corner to the last cell", () => {
    const [cell] = mapHotspots(rect, [{ x: 799, y: 799 }]);
    expect(cell).toEqual([HOTSPOT_GRID - 1, HOTSPOT_GRID - 1]);
  });

  it("drops points outside the rectangle", () => {
    expect(mapHotspots(rect, [{ x: -5, y: 10 }, { x: 900, y: 10 }])).toEqual([]);
  });

  it("preserves order oldest to newest", () => {
    const cells = mapHotspots(rect, [
      { x: 50, y: 50 }, { x: 750, y: 750 }, { x: 400, y: 50 },
    ]);
    expect(cells[0]).toEqual([0, 0]);
    expect(cells[1]).toEqual([7, 7]);
  });

  it("collapses consecutive duplicates", () => {
    const cells = mapHotspots(rect, [
      { x: 10, y: 10 }, { x: 12, y: 12 }, { x: 750, y: 750 },
    ]);
    expect(cells).toHaveLength(2);
  });

  it("returns an empty array for no points", () => {
    expect(mapHotspots(rect, [])).toEqual([]);
  });
});

describe("atlas metadata", () => {
  const plan = planAtlas(emptyDoc(), { x: 0, y: 0, w: 100, h: 100 }, view, viewport);

  it("carries the geometry the prompt reads", () => {
    const meta = atlasMetadata(plan, { x: 0, y: 0, w: 100, h: 100 }, [], {});
    expect(meta).toHaveProperty("sourceRect");
    expect(meta).toHaveProperty("imageScale");
    expect(meta).toHaveProperty("latestInput");
    expect(meta).toHaveProperty("canvasSize");
  });

  it("defaults mode to sketch", () => {
    expect(atlasMetadata(plan, null, [], {}).mode).toBe("sketch");
  });

  it("carries an explicit deep mode", () => {
    expect(atlasMetadata(plan, null, [], { mode: "deep" }).mode).toBe("deep");
  });

  it("omits an empty hotspot list", () => {
    expect(atlasMetadata(plan, null, [], {})).not.toHaveProperty("hotspots");
  });

  it("stays under the server's 64KB metadata cap", () => {
    const many = Array.from({ length: 64 }, (_, i) => [i % 8, Math.floor(i / 8)]);
    const size = new TextEncoder().encode(
      JSON.stringify(atlasMetadata(plan, null, many, {})),
    ).length;
    expect(size).toBeLessThan(65_536);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm vitest run tests/whiteboard-atlas.test.ts`
Expected: FAIL — cannot resolve `@/components/whiteboard/atlas`

- [ ] **Step 3: Write the implementation**

Create `sdk/agent-chat-react/src/components/whiteboard/atlas.ts`.

`planAtlas` follows `captureRectFor` / `planViewportImage`
(`ai-runtime.js:553,557`):

1. Start from `latest` when given, else the union of `objectBounds` over
   every object, else the current viewport rectangle in logical space.
2. Pad it to at least the viewport aspect so the model gets surrounding
   context, and clamp to `[0, CANVAS_SIZE]`.
3. `imageScale = Math.min(1, MAX_ATLAS_WIDTH / sourceRect.w, MAX_ATLAS_HEIGHT / sourceRect.h)`
   — the `Math.min(1, ...)` is what stops upscaling.
4. `imageSize = { w: max(1, min(MAX_ATLAS_WIDTH, ceil(sourceRect.w * imageScale))), h: ... }`.

`mapHotspots` follows `ai-runtime.js:523`: an `8 x 8` grid over
`sourceRect`, points outside dropped, consecutive duplicate cells
collapsed, order preserved oldest to newest.

`buildAtlas` fills the canvas **white** (the model is told it is a clean
white-background rendering; a transparent PNG reads as black on some
providers), then calls `renderDoc` with a view derived from the plan.

`atlasMetadata` returns exactly the keys the guidance fragment and
`_whiteboard_note_from_metadata` read: `sourceRect`, `imageScale`,
`latestInput`, `hotspots` (omitted when empty), `viewport`, `canvasSize`,
`mode` (defaulting to `"sketch"`), plus optional `selection` and
`typedInput` from `extra`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pnpm vitest run tests/whiteboard-atlas.test.ts`
Expected: PASS, 17 passed

- [ ] **Step 5: Commit**

```bash
git add sdk/agent-chat-react/src/components/whiteboard/atlas.ts \
        sdk/agent-chat-react/tests/whiteboard-atlas.test.ts
git commit -m "feat(whiteboard): build the request atlas and hotspot grid"
```

---

## Task 15: Persistence

**Files:**
- Create: `sdk/agent-chat-react/src/components/whiteboard/persist.ts`
- Test: `sdk/agent-chat-react/tests/whiteboard-persist.test.ts`

**Interfaces:**
- Consumes: `WbDoc`, `emptyDoc`, `foldToolCalls` (Task 11); `AgentChatAdapter` from `@/types`.
- Produces:
  - `CANVAS_PATH = "_whiteboard/canvas.json"`
  - `loadDoc(adapter, sessionId, messages): Promise<WbDoc>`
  - `saveDoc(adapter, sessionId, doc): Promise<void>`
  - `useDebouncedSave(adapter, sessionId, doc, delayMs?): void`

- [ ] **Step 1: Write the failing test**

Create `sdk/agent-chat-react/tests/whiteboard-persist.test.ts`:

```ts
import { describe, expect, it, vi } from "vitest";
import { CANVAS_PATH, loadDoc, saveDoc } from "@/components/whiteboard/persist";
import { applyCommands, emptyDoc } from "@/components/whiteboard/doc";
import type { AgentChatAdapter, AgentChatMessage } from "@/types";

const text = {
  tool: "write_text", x: 1, y: 2, text: "a", fontSize: 20, maxWidth: 100,
};

function adapterWith(file: unknown, upload = vi.fn()) {
  return {
    readWorkspaceFile: vi.fn(async () =>
      file === null ? null : JSON.stringify(file),
    ),
    uploadWorkspaceFile: upload,
  } as unknown as AgentChatAdapter;
}

function drawMessage(id: string, commands: unknown[]): AgentChatMessage {
  return {
    id, role: "assistant", content: "",
    toolCalls: [{
      id, toolName: "whiteboard_draw",
      args: JSON.stringify({ commands }), status: "complete",
    }],
  } as unknown as AgentChatMessage;
}

describe("loading", () => {
  it("returns an empty document when no file exists", async () => {
    expect((await loadDoc(adapterWith(null), "s1", [])).objects).toEqual([]);
  });

  it("returns an empty document when the file is corrupt", async () => {
    const adapter = {
      readWorkspaceFile: vi.fn(async () => "{not json"),
    } as unknown as AgentChatAdapter;
    expect((await loadDoc(adapter, "s1", [])).objects).toEqual([]);
  });

  it("restores a saved document", async () => {
    const saved = applyCommands(emptyDoc(), [text], 5);
    const doc = await loadDoc(adapterWith(saved), "s1", []);
    expect(doc.objects).toHaveLength(1);
  });

  it("replays tool calls newer than the saved document", async () => {
    // The recovery tail: a tab closed between an agent reply and the
    // next debounce flush must not lose the agent's objects.
    const saved = applyCommands(emptyDoc(), [text], 5);
    const doc = await loadDoc(adapterWith(saved), "s1", [
      drawMessage("newer", [text, text]),
    ]);
    expect(doc.objects).toHaveLength(3);
  });

  it("does not replay a tool call already folded into the file", async () => {
    const saved = applyCommands(emptyDoc(), [text], 5, "already");
    const doc = await loadDoc(adapterWith(saved), "s1", [
      drawMessage("already", [text]),
    ]);
    expect(doc.objects).toHaveLength(1);
  });

  it("rejects a document with an unknown version", async () => {
    const doc = await loadDoc(
      adapterWith({ version: 99, objects: [{}], lastEventId: 0 }),
      "s1",
      [],
    );
    expect(doc.objects).toEqual([]);
  });
});

describe("saving", () => {
  it("uploads to the internal canvas path", async () => {
    const upload = vi.fn();
    await saveDoc(adapterWith(null, upload), "s1", emptyDoc());
    expect(upload).toHaveBeenCalled();
    expect(String(upload.mock.calls[0])).toContain(CANVAS_PATH);
  });

  it("writes the underscore-prefixed directory", () => {
    // The prefix marks it server-internal so the workspace file browser
    // hides it, matching _artifacts/.
    expect(CANVAS_PATH.startsWith("_")).toBe(true);
  });

  it("serialises a round-trippable document", async () => {
    const upload = vi.fn();
    const doc = applyCommands(emptyDoc(), [text], 3);
    await saveDoc(adapterWith(null, upload), "s1", doc);
    const body = upload.mock.calls[0].find((a) => typeof a === "string" && a.includes("objects"));
    expect(JSON.parse(body as string).objects).toHaveLength(1);
  });

  it("swallows an upload failure", async () => {
    // Persistence is best-effort: the event log is the recovery tail, so
    // a failed save must never break the canvas.
    const upload = vi.fn(async () => {
      throw new Error("offline");
    });
    await expect(saveDoc(adapterWith(null, upload), "s1", emptyDoc()))
      .resolves.toBeUndefined();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm vitest run tests/whiteboard-persist.test.ts`
Expected: FAIL — cannot resolve `@/components/whiteboard/persist`

- [ ] **Step 3: Check the adapter surface first**

Run: `grep -n "orkspace" sdk/agent-chat-react/src/types.ts`

If `AgentChatAdapter` has no workspace read/upload methods, add them as
**optional** members (`readWorkspaceFile?`, `uploadWorkspaceFile?`) so
existing host adapters keep compiling, and have `loadDoc`/`saveDoc`
degrade to in-memory-only when they are absent. Add that case to the test
before implementing.

- [ ] **Step 4: Write the implementation**

Create `sdk/agent-chat-react/src/components/whiteboard/persist.ts` per the
interfaces above. `loadDoc`:

1. Read `CANVAS_PATH`; on any failure, missing file, parse error, or
   `version !== 1`, start from `emptyDoc()`.
2. `foldToolCalls(doc, messages)` — idempotent on tool-call id, so calls
   already in the file are skipped and newer ones are appended.

`saveDoc` uploads `JSON.stringify(doc)` to `CANVAS_PATH` and swallows
failures: the event log is the recovery tail, so a failed save must never
surface as a broken canvas.

`useDebouncedSave` is a `useEffect` with a 1500 ms timer, cancelling on
change and flushing on unmount.

- [ ] **Step 5: Run test to verify it passes**

Run: `pnpm vitest run tests/whiteboard-persist.test.ts`
Expected: PASS, 10 passed

- [ ] **Step 6: Commit**

```bash
git add sdk/agent-chat-react/src/components/whiteboard/persist.ts \
        sdk/agent-chat-react/src/types.ts \
        sdk/agent-chat-react/tests/whiteboard-persist.test.ts
git commit -m "feat(whiteboard): persist the canvas to the session workspace"
```

---

## Task 16: The `<AgentWhiteboard>` component

**Files:**
- Create: `sdk/agent-chat-react/src/components/whiteboard/tool-rail.tsx`
- Create: `sdk/agent-chat-react/src/components/whiteboard/agent-whiteboard.tsx`
- Modify: `sdk/agent-chat-react/src/index.ts`
- Test: `sdk/agent-chat-react/tests/whiteboard-component.test.tsx`

**Interfaces:**
- Consumes: everything from Tasks 11-15, plus `useAgentChatRuntime` from `@/runtime/use-agent-chat-runtime`.
- Produces:
  ```ts
  export interface AgentWhiteboardProps {
    adapter: AgentChatAdapter;
    agentId?: string;
    sessionId: string | null;
    onSessionChange?: (sessionId: string) => void;
    disabled?: boolean;
    onOpenBilling?: () => void;
  }
  export function AgentWhiteboard(props: AgentWhiteboardProps): JSX.Element;
  export type WbTool = "pen" | "eraser" | "text" | "select" | "pan";
  ```

- [ ] **Step 1: Write the failing test**

Create `sdk/agent-chat-react/tests/whiteboard-component.test.tsx`. Model
the harness on an existing component test — read
`sdk/agent-chat-react/tests/browser-pane.test.tsx` for how the adapter is
stubbed. Cover:

```tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { AgentWhiteboard } from "@/components/whiteboard/agent-whiteboard";

// ... stub adapter per browser-pane.test.tsx ...

describe("AgentWhiteboard", () => {
  it("renders a canvas element", () => { /* ... */ });
  it("renders the tool rail with every tool", () => { /* ... */ });
  it("starts on the pen tool", () => { /* ... */ });
  it("switches the active tool on click", () => { /* ... */ });
  it("disables Ask while the session is running", () => { /* ... */ });
  it("sends an image and whiteboard metadata on Ask", async () => {
    // The core contract: sendMessage receives images[0] and
    // metadata.whiteboard with sourceRect + imageScale + mode.
  });
  it("sends mode sketch by default", async () => { /* ... */ });
  it("sends mode deep from the think-harder control", async () => { /* ... */ });
  it("renders agent objects from a whiteboard_draw tool call", () => { /* ... */ });
  it("keeps the transcript drawer collapsed by default", () => { /* ... */ });
  it("opens the transcript drawer on click", () => { /* ... */ });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm vitest run tests/whiteboard-component.test.tsx`
Expected: FAIL — cannot resolve `@/components/whiteboard/agent-whiteboard`

- [ ] **Step 3: Write the tool rail**

Create `tool-rail.tsx`: a vertical rail of `lucide-react` icon buttons
(`Pen`, `Eraser`, `Type`, `MousePointer2`, `Hand`), a colour swatch row
and a width slider. Use the SDK's existing `Button` and `cn` helpers —
check `src/components/ui/` for what is already there rather than adding
new primitives.

- [ ] **Step 4: Write the component**

Create `agent-whiteboard.tsx`:

1. `const runtime = useAgentChatRuntime({ adapter, agentId, sessionId, onSessionChange })`
2. `const [doc, setDoc] = useState(emptyDoc())`, `const [view, setView] = useState({x: 0, y: 0, zoom: 1})`,
   `const [tool, setTool] = useState<WbTool>("pen")`.
3. On mount and session change: `loadDoc(adapter, sessionId, runtime.messages).then(setDoc)`.
4. On `runtime.messages` change: `setDoc((d) => foldToolCalls(d, runtime.messages))`.
5. `useDebouncedSave(adapter, sessionId, doc)`.
6. A `<canvas>` sized by `ResizeObserver`, repainted in a
   `requestAnimationFrame` loop that runs **only when the document or
   view changed** — repainting every frame unconditionally is the obvious
   way to burn a laptop battery on a static board.
7. Pointer handlers dispatching on `tool` via `StrokeBuilder`, `hitTest`,
   `zoomAt` and `clampView`.
8. Undo/redo: a bounded stack of previous `doc` values, capped at 30
   entries (PenEcho's `MAX_HISTORY`); `Cmd/Ctrl+Z` and `Shift+Cmd/Ctrl+Z`;
   `Delete` removes selected objects.
9. **Ask**: build the plan with `planAtlas`, rasterise with `buildAtlas`,
   `toDataURL("image/png")`, then
   ```ts
   await runtime.sendMessage({
     text: typedQuestion,
     images: [{ data: dataUrl, mime_type: "image/png" }],
     metadata: { whiteboard: atlasMetadata(plan, latest, hotspots, { mode }) },
   });
   ```
   Track `latest` as the union of object bounds added since the last Ask,
   and `hotspots` as the stroke points captured since the last Ask; clear
   both after sending.
10. A collapsible transcript drawer rendering the existing chat thread
    component, collapsed by default.
11. Artifact objects render as absolutely-positioned DOM overlays above
    the canvas, reusing `components/artifacts/artifact-block.tsx`,
    positioned with `logicalToScreen`.

- [ ] **Step 5: Export it**

In `sdk/agent-chat-react/src/index.ts`, next to `export { AgentChat }`:

```ts
export { AgentWhiteboard } from "./components/whiteboard/agent-whiteboard";
export type {
  AgentWhiteboardProps,
  WbTool,
} from "./components/whiteboard/agent-whiteboard";
export type { WbDoc, WbObject } from "./components/whiteboard/doc";
```

- [ ] **Step 6: Run tests, typecheck and build**

Run: `pnpm vitest run && pnpm typecheck && pnpm build`
Expected: all pass; `dist/index.d.ts` contains `AgentWhiteboard`.

- [ ] **Step 7: Commit**

```bash
git add sdk/agent-chat-react/src/components/whiteboard/ \
        sdk/agent-chat-react/src/index.ts \
        sdk/agent-chat-react/tests/whiteboard-component.test.tsx
git commit -m "feat(whiteboard): add the AgentWhiteboard component"
```

---

# Phase C — Hosts

## Task 17: Agent web app route

**Files:**
- Create: `web/src/app/routes/whiteboard.tsx`
- Create: `web/src/features/whiteboard/whiteboard-page.tsx`
- Modify: `web/src/app/router.tsx`
- Modify: `web/src/features/chat/surogates-web-chat-adapter.ts`
- Test: `web/src/features/whiteboard/__tests__/whiteboard-page.test.tsx` (match the directory convention already used in `web/src`)

**Interfaces:**
- Consumes: `AgentWhiteboard` (Task 16), `surogatesWebChatAdapter`.
- Produces: routes `/whiteboard` and `/whiteboard/$sessionId`.

- [ ] **Step 1: Write the failing test**

Cover: the page renders `AgentWhiteboard`; creating a session stamps
`config.surface === "whiteboard"`; an existing non-whiteboard session id
redirects to `/chat`.

- [ ] **Step 2: Run it to verify it fails**

Run: `cd web && pnpm vitest run src/features/whiteboard`
Expected: FAIL

- [ ] **Step 3: Stamp the surface on create**

In `web/src/features/chat/surogates-web-chat-adapter.ts:53`, `createSession`
currently forwards only `{ system: input.system }`. Widen it to forward
`config` as well, then create a thin wrapper in the whiteboard feature:

```ts
// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import type { AgentChatAdapter } from "@invergent/agent-chat-react";
import { surogatesWebChatAdapter } from "@/features/chat/surogates-web-chat-adapter";

/** The web adapter with every new session stamped as a whiteboard
 *  surface. The stamp has to happen at creation: the harness reads
 *  ``config.surface`` at wake to pick the tool set and the guidance
 *  fragment, and session config is not editable afterwards. */
export const whiteboardChatAdapter: AgentChatAdapter = {
  ...surogatesWebChatAdapter,
  async createSession(input) {
    return surogatesWebChatAdapter.createSession({
      ...input,
      config: { ...(input as { config?: object }).config, surface: "whiteboard" },
    });
  },
};
```

- [ ] **Step 4: Write the page and route**

`whiteboard-page.tsx` mirrors `web/src/features/chat/chat-page.tsx` —
same `AppShell`, `SessionSidebar` and URL/store sync — but renders
`<AgentWhiteboard adapter={whiteboardChatAdapter} ... />`. Filter the
sidebar to whiteboard sessions.

`routes/whiteboard.tsx` mirrors `routes/chat.tsx` with
`whiteboardRoute` / `whiteboardSessionRoute`; register them in
`router.tsx`'s `routeTree` next to the chat routes.

- [ ] **Step 5: Run tests, typecheck and lint**

Run: `pnpm vitest run && pnpm typecheck && pnpm biome:check`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add web/src/app/routes/whiteboard.tsx web/src/app/router.tsx \
        web/src/features/whiteboard/
git commit -m "feat(whiteboard): add the whiteboard route to the agent web app"
```

---

## Task 18: Push the SDK and open the surogates PR

- [ ] **Step 1: Bump the SDK version**

In `sdk/agent-chat-react/package.json`, bump `version` from `2.13.0` to
`2.14.0` — a new export is a minor.

- [ ] **Step 2: Verify the SDK's declared dependencies**

The whiteboard adds no new runtime dependency: `@streamdown/math`,
`chart.js`, `lucide-react` and `nanoid` are already declared. Confirm:

Run: `cd sdk/agent-chat-react && pnpm build && node -e "require('./dist/index.cjs')"`
Expected: no missing-module error.

If the SDK is symlinked into `web/`, remember that `web/package-lock.json`
is what the release image installs — a dependency declared only in the
SDK's own `package.json` breaks the release build. Verify:

Run: `cd /work/surogates && docker build --target web-build .`
Expected: build succeeds.

- [ ] **Step 3: Full suite**

Run: `cd /work/surogates && python -m pytest tests/ -q`
Run: `cd sdk/agent-chat-react && pnpm vitest run && pnpm typecheck`
Run: `cd web && pnpm typecheck && pnpm biome:check`
Expected: all pass.

- [ ] **Step 4: Commit and open the PR**

```bash
git add sdk/agent-chat-react/package.json
git commit -m "chore(sdk): release 2.14.0 with AgentWhiteboard"
git push -u origin feat/whiteboard-canvas-chat
gh pr create --base master \
  --title "feat(whiteboard): canvas chat surface" \
  --body "$(cat <<'EOF'
Adds a third chat type: a vector canvas the agent reads as an image and
writes to through a `whiteboard_draw` tool.

Design: `docs/superpowers/specs/2026-08-27-whiteboard-canvas-chat-design.md`
Plan: `docs/superpowers/plans/2026-08-27-whiteboard-canvas-chat.md`

- Harness: `whiteboard_draw` tool, command validation, canvas guidance
  fragment, per-turn geometry note, cumulative-snapshot pruning,
  `metadata.whiteboard` cap, sketch/deep turn speeds.
- SDK: `AgentWhiteboard` component with a vector canvas, ink capture,
  atlas builder and workspace persistence.
- Web app: `/whiteboard` route.

Vendors four zero-dependency modules and the system prompt from
[PenEcho](https://penecho.ai) (AGPL-3.0-only, same licence); attribution
in `sdk/agent-chat-react/src/vendor/penecho/README.md`.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Task 19: ops capability flag

**Files:**
- Modify: `surogate_ops/core/db/models/operate.py`
- Create: `surogate_ops/core/db/migrations/versions/<rev>_agent_whiteboard_enabled.py`
- Modify: `surogate_ops/server/models/agent.py` (lines ~141, ~199, ~306)
- Modify: `surogate_ops/server/models/agent_runtime.py` (line ~115)
- Modify: `/work/surogates/surogates/runtime/context.py` (line ~175)
- Test: `surogate_ops/tests/test_agent_whiteboard_capability.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `agents.whiteboard_enabled: bool` (default `False`), projected as `AgentRuntimeConfig.whiteboard_enabled` and consumed as `AgentRuntimeContext.whiteboard_enabled`.

- [ ] **Step 1: Create the branch**

```bash
cd /work/surogate-ops
git fetch origin
git checkout -b feat/whiteboard-capability origin/main
```

- [ ] **Step 2: Write the failing test**

Assert: the column defaults to `False`; the create/update request models
accept it; `AgentRuntimeConfig` projects it; a `True` value reaches the
runtime config payload.

- [ ] **Step 3: Add the column and migration**

Add `whiteboard_enabled: Mapped[bool]` with `server_default="false"` to
the agents model in `operate.py`, mirroring `research_enabled`.

Generate the migration:

```bash
surogate-ops migrate revision -m "agent whiteboard_enabled"
```

Model the file on
`surogate_ops/core/db/migrations/versions/d4e9f1a2c7b8_agent_research_enabled.py`.
**Verify the head stays singular** — two heads broke `create-all` in
production before:

```bash
alembic -c surogate_ops/core/db/alembic.ini heads
```
Expected: exactly one head.

- [ ] **Step 4: Thread it through the models**

Add `whiteboard_enabled: bool = False` to the agent create/response models
and `Optional[bool] = None` to the update model in
`surogate_ops/server/models/agent.py`, alongside `research_enabled`. Add
`whiteboard_enabled: bool = False` to `AgentRuntimeConfig` in
`agent_runtime.py`, and the matching field to `AgentRuntimeContext` in
`/work/surogates/surogates/runtime/context.py`.

- [ ] **Step 5: Run the tests and migration**

Run: `python -m pytest surogate_ops/tests/test_agent_whiteboard_capability.py -q`
Run: `surogate-ops migrate upgrade`
Expected: PASS; migration applies cleanly.

Do **not** use `uv run` here — it reinstalls the pinned `surogates` wheel
over the local dev install.

- [ ] **Step 6: Commit**

```bash
git add surogate_ops/core/db/ surogate_ops/server/models/
git commit -m "feat(agents): add the whiteboard capability flag"
```

---

## Task 20: ops whiteboard page

**Files:**
- Create: `frontend/src/features/work/work-agent-whiteboard-page.tsx`
- Modify: `frontend/src/features/work/work-agent-tabs.ts`
- Modify: `frontend/src/features/work/work-agent-section-index.tsx`
- Modify: `frontend/src/features/work/work-agent-settings-page.tsx` (capabilities tab)
- Modify: `frontend/src/app/router.tsx`
- Test: `frontend/src/features/work/__tests__/work-agent-whiteboard.test.tsx`

**Interfaces:**
- Consumes: `AgentWhiteboard` (Task 16), `workAgentChatAdapter` from `work-agent-chat-adapter.ts`.
- Produces: the `whiteboard` route under an agent, gated on `agent.whiteboard_enabled`.

- [ ] **Step 1: Bump the SDK dependency**

In `frontend/package.json`, raise `@invergent/agent-chat-react` to
`^2.14.0` and install.

- [ ] **Step 2: Write the failing test**

Cover: the page renders `AgentWhiteboard`; session creation stamps
`config.surface === "whiteboard"`; the whiteboard entry is hidden when
`agent.whiteboard_enabled` is false; the capabilities tab renders the
toggle.

- [ ] **Step 3: Write the page**

`work-agent-whiteboard-page.tsx` mirrors `work-agent-chat-page.tsx` but
renders `<AgentWhiteboard>` and wraps the adapter to stamp
`config.surface = "whiteboard"`. The ops adapter already forwards `config`
verbatim (`work-agent-chat-adapter.ts:275`) and `create_live_session`
already spreads `**body.config` (`surogate_ops/server/routes/sessions.py:780`),
so **no ops backend session work is needed**.

- [ ] **Step 4: Add the capability toggle**

In the capabilities section of `work-agent-settings-page.tsx`, add a
"Whiteboard" switch bound to `whiteboard_enabled`, following the existing
"Live browser support" toggle.

- [ ] **Step 5: Run tests, typecheck and lint**

Run: `cd frontend && npm run typecheck && npm run lint && npx vitest run src/features/work`
Expected: all pass.

- [ ] **Step 6: Commit and open the PR**

```bash
git add frontend/
git commit -m "feat(whiteboard): add the whiteboard page to the Work console"
git push -u origin feat/whiteboard-capability
gh pr create --base main \
  --title "feat(whiteboard): canvas chat surface in the Work console" \
  --body "$(cat <<'EOF'
Adds the per-agent whiteboard capability and the Work console page.

Design: `surogates:docs/superpowers/specs/2026-08-27-whiteboard-canvas-chat-design.md`

Depends on `@invergent/agent-chat-react@2.14.0` (surogates PR).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review

**Spec coverage.** Every spec section maps to a task:

| Spec section | Task(s) |
| --- | --- |
| Session shape (`config.surface`) | 2, 17, 20 |
| Canvas data model | 11 |
| Persistence: single writer | 15 |
| The turn: what goes up | 14, 16 |
| Metadata cap | 7 |
| Cumulative-snapshot pruning | 6 |
| The turn: what comes down | 3 |
| `place_artifact` absorbs the plugin layer | 3, 12 |
| Two speeds | 8 |
| SDK surface | 16 |
| Prompting | 5 |
| Testing (every listed case) | 1, 3, 4, 6, 7, 10 |
| Attribution | 10 |
| Files (surogates) | 1-18 |
| Files (surogate-ops) | 19, 20 |

**Type consistency.** `WbDoc`/`WbObject` defined in Task 11 are consumed
under those names in 12, 14, 15, 16. `View` is defined in Task 12 and
consumed in 13, 14, 16. `validate_commands` (Task 1) is consumed in Task 3.
`WHITEBOARD_TOOL_NAMES` (Task 3) is consumed in 4 and 8.
`is_whiteboard_session`/`turn_mode` (Task 2) are consumed in 4, 5, 6, 8.
`atlasMetadata`'s output keys (Task 14) are exactly the keys
`_whiteboard_note_from_metadata` reads (Task 6): `sourceRect`,
`imageScale`, `latestInput`, `hotspots`, `selection`, `typedInput`, `mode`.
`CANVAS_SIZE` is 20000 in both `surogates/whiteboard/commands.py` (Task 1)
and `doc.ts` (Task 11); the note in Task 5's fragment states the same
number.

**Known duplication, accepted.** The `draw` structural rules exist twice —
Python in Task 1, and the vendored `draw.js` `normalize` in Task 10. The
Python copy is a cheap guard that turns a malformed list into a precise
model retry; the vendored copy is the authoritative renderer. Porting the
full geometry (cubic extrema, arc sweeps, arrowheads) to Python to
de-duplicate would be strictly more code for no behaviour, so the
duplication stays and Task 1's docstring says so.

**Open item deferred by design, not omission.** Task 8 leaves the
tier-sentinel lookup as a `grep` rather than a literal, because the
sentinel names must never be hardcoded in a plan that outlives a proxy
config change.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-27-whiteboard-canvas-chat.md`. Two execution options:

**1. Subagent-Driven (recommended)** — a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
