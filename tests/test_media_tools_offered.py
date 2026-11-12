"""A media tool is sent to the model only when a model is wired for it.

``generate_image`` needs an image model, ``generate_video`` a video model.
The worker already left an unwired one out of the prompt's tool list, but the
schemas sent to the model kept both in every session: an agent with no media
model was offered tools that could only answer "not available".
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import surogates.harness.loop as loop_module
from surogates.harness.budget import IterationBudget
from surogates.harness.loop import AgentHarness
from surogates.harness.tool_schemas import drop_unusable_tools
from surogates.orchestrator.worker import _media_tool_exclusions
from surogates.session.events import EventType
from surogates.session.models import Session
from surogates.storage.backend import LocalBackend
from surogates.tenant.context import TenantContext
from surogates.tools.builtin.media_gen import MediaGenConfig, unwired_media_tools
from tests.test_case_step_tools import _sandbox, _StepStore
from tests.test_case_tool_gate import MCP_TOOL, _registry
from tests.test_steer_loop import _final_response

MEDIA = {"generate_image", "generate_video"}
NONE_WIRED = MediaGenConfig()
IMAGE_ONLY = MediaGenConfig(image_client=object(), image_model="image-1")
BOTH = MediaGenConfig(
    image_client=object(), image_model="image-1", video_model="video-1", video_base_url="http://video.test/v1",
)


def _schema(name):
    return {"type": "function", "function": {"name": name, "description": "", "parameters": {"type": "object"}}}


def _session(channel: str = "web", config: dict | None = None, *, parent: bool = False) -> Session:
    now = datetime.now(UTC)
    return Session(
        id=uuid4(), user_id=uuid4(), org_id=uuid4(), agent_id="agent-1", channel=channel, status="active",
        config=config or {}, created_at=now, updated_at=now, parent_id=uuid4() if parent else None,
    )


async def _sent_tools(monkeypatch, tmp_path, session: Session, media_gen) -> set[str]:
    """The tool names of the first request a turn of *session* sends, with *media_gen* wired."""
    sent: list[dict] = []

    async def llm(**call):
        sent.append(call["create_kwargs"])
        return _final_response("Done.")

    monkeypatch.setattr(loop_module, "call_llm_with_retry", llm)
    store = _StepStore(session, LocalBackend(str(tmp_path)))
    tenant = TenantContext(
        org_id=session.org_id, user_id=session.user_id, org_config={}, user_preferences={},
        permissions=frozenset(), asset_root="/tmp/assets",
    )
    harness = AgentHarness(
        session_store=store, tool_registry=_registry(), llm_client=AsyncMock(), tenant=tenant,
        worker_id="w", budget=IterationBudget(max_total=4),
        context_compressor=SimpleNamespace(context_length=1000, _context_window=200_000, should_compress=lambda *a, **k: False),
        prompt_builder=SimpleNamespace(has_agents=False), sandbox_pool=_sandbox({}),
        mcp_tool_names=frozenset({MCP_TOOL}), media_gen=media_gen,
    )
    harness._storage = store.storage
    harness._streaming_enabled = False
    harness._prefetch_memory = AsyncMock(return_value="")
    harness._maybe_consult_required_expert = AsyncMock(return_value=None)
    harness._maybe_route_final_response_to_inbox = AsyncMock(return_value=None)
    harness._maybe_generate_title = MagicMock(return_value=None)
    harness._promote_fenced_artifacts = AsyncMock(return_value=None)
    harness._end_turn = AsyncMock(return_value=None)
    store.write(EventType.USER_MESSAGE, {"content": "Draw me a logo."})
    await harness._run_loop(
        store.row.model_copy(deep=True), [{"role": "user", "content": "Draw me a logo."}], "system",
        SimpleNamespace(lease_token=uuid4()), all_events=list(store.log),
    )
    return {tool["function"]["name"] for tool in sent[0]["tools"]}


def test_a_media_tool_needs_its_model():
    assert unwired_media_tools(NONE_WIRED) == MEDIA
    assert unwired_media_tools(IMAGE_ONLY) == {"generate_video"}
    assert unwired_media_tools(BOTH) == set()
    assert unwired_media_tools(MediaGenConfig(video_model="video-1")) == MEDIA
    # The prompt's tool list and the schemas use the same check.
    assert _media_tool_exclusions(IMAGE_ONLY) == unwired_media_tools(IMAGE_ONLY)


def test_the_schema_filter_drops_an_unwired_media_tool():
    schemas = [_schema(n) for n in ("generate_image", "generate_video", "read_file")]
    flags = dict(has_kbs=True, has_channel=False, makes_routines=False)

    kept = drop_unusable_tools(schemas, **flags, has_image_model=False, has_video_model=False)
    assert [s["function"]["name"] for s in kept] == ["read_file"]
    kept = drop_unusable_tools(schemas, **flags, has_image_model=True, has_video_model=False)
    assert [s["function"]["name"] for s in kept] == ["generate_image", "read_file"]
    assert drop_unusable_tools(schemas, **flags) == schemas


async def test_every_kind_of_session_is_sent_a_media_tool_only_with_its_model(monkeypatch, tmp_path):
    sessions = {
        "a web chat": _session("web"),
        "an API session": _session("api"),
        "an evaluation row": _session("api", {"eval_partition_id": "bench-1"}),
        "an agent that names its tools": _session("web", {"allowed_tools": ["generate_image", "generate_video", "read_file"]}),
        "a helper": _session("worker", parent=True),
    }
    for name, session in sessions.items():
        assert not MEDIA & await _sent_tools(monkeypatch, tmp_path, session.model_copy(deep=True), NONE_WIRED), name
        sent = await _sent_tools(monkeypatch, tmp_path, session.model_copy(deep=True), IMAGE_ONLY)
        assert "generate_image" in sent and "generate_video" not in sent, name
        assert MEDIA <= await _sent_tools(monkeypatch, tmp_path, session.model_copy(deep=True), BOTH), name


async def test_a_harness_built_without_media_wiring_keeps_them(monkeypatch, tmp_path):
    assert MEDIA <= await _sent_tools(monkeypatch, tmp_path, _session("web"), None)
