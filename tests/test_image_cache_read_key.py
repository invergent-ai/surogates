"""The image cache keeps an analysis only under the object it was made from, and refusals stay short."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from surogates.harness import image_read
from surogates.harness.api_client import HarnessAPIClient
from surogates.harness.image_read import handle_image_read
from surogates.storage.backend import LocalBackend, read_first
from surogates.tools.builtin import skills as skills_mod, vision as vision_mod
from surogates.tools.builtin.file_ops import _read_file_handler
from surogates.tools.builtin.skills import _skill_view_handler
from surogates.tools.loader import SKILL_SOURCE_PLATFORM, SkillDef
from surogates.tools.utils.workspace_sandbox import shown_path
from tests.test_image_read_cache_scoped import BLUE, RED, Vision
from tests.test_skill_view_hides_graph_file import _make_tenant, _stub_load_all_skills

BUCKET = "agent-bucket"
LONG = "a/" * 1500 + "x.png"


class Refusing(LocalBackend):
    """Local storage whose stat finds a file its read may not open, as with a file of mode 000."""

    refused: set[str]

    async def read_at_most(self, bucket, key, max_bytes):
        if key in self.refused:
            raise PermissionError(13, "Permission denied")
        return await super().read_at_most(bucket, key, max_bytes)


@pytest.fixture(autouse=True)
def empty_cache():
    image_read._CACHE.clear()
    yield
    image_read._CACHE.clear()


@pytest.fixture
def two_places(tmp_path):
    storage = Refusing(str(tmp_path))
    storage.refused = set()
    session_id = str(uuid.uuid4())
    workspace = storage.resolve_workspace_path(BUCKET, session_id)
    (Path(workspace) / "pic.png").write_bytes(RED)
    return storage, session_id, {"storage_bucket": BUCKET, "workspace_path": workspace}


async def _read(vision, storage, session_id, config) -> str:
    kwargs = {
        "session_id": session_id, "workspace_path": config["workspace_path"], "storage": storage,
        "session_config": config,
    }
    return json.loads(await handle_image_read("pic.png", {"path": "pic.png"}, vision.dispatch, kwargs))["content"]


@pytest.mark.asyncio
async def test_an_analysis_of_the_second_object_is_not_kept_under_the_first(two_places) -> None:
    storage, session_id, config = two_places
    await storage.write(BUCKET, f"{session_id}/pic.png", BLUE)
    storage.refused = {f"sessions/{session_id}/pic.png"}
    vision = Vision()
    assert "a blue square" in await _read(vision, storage, session_id, config)
    storage.refused = set()
    assert "a red square" in await _read(vision, storage, session_id, config)
    assert vision.calls == 2


@pytest.mark.asyncio
async def test_an_analysis_of_the_first_object_is_kept(two_places) -> None:
    storage, session_id, config = two_places
    vision = Vision()
    for _ in range(2):
        assert "a red square" in await _read(vision, storage, session_id, config)
    assert vision.calls == 1


@pytest.mark.asyncio
async def test_an_analysis_of_a_copy_read_in_place_of_the_object_is_not_kept(two_places, monkeypatch) -> None:
    storage, session_id, config = two_places
    monkeypatch.setattr(vision_mod, "has_copy", lambda *_: True)

    async def read_copy(*_args, **_kwargs):
        return BLUE

    monkeypatch.setattr(vision_mod, "read_copy", read_copy)
    vision = Vision()
    assert "a blue square" in await _read(vision, storage, session_id, config)
    monkeypatch.setattr(vision_mod, "has_copy", lambda *_: False)
    assert "a red square" in await _read(vision, storage, session_id, config)


@pytest.mark.asyncio
async def test_read_first_says_which_key_it_read(tmp_path) -> None:
    storage = Refusing(str(tmp_path))
    storage.refused = {"first.bin"}
    await storage.write(BUCKET, "first.bin", b"1")
    await storage.write(BUCKET, "second.bin", b"2")
    assert await read_first(storage, BUCKET, ["first.bin", "second.bin"], 10) == ("second.bin", b"2")
    storage.refused = set()
    assert await read_first(storage, BUCKET, ["first.bin", "second.bin"], 10) == ("first.bin", b"1")


@pytest.mark.parametrize("value, shown", [(123, "123"), (None, "None"), (b"a/b", "a/b"), (Path("a/b"), "a/b")])
def test_shown_path_takes_any_value(value, shown) -> None:
    assert shown_path(value) == shown


def test_shown_path_shortens_any_long_value() -> None:
    assert len(shown_path(["x"] * 5000)) < 300
    assert len(shown_path(b"x" * 5000)) < 300


@pytest.mark.asyncio
async def test_a_file_the_api_does_not_have_is_answered_whatever_its_path_is() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/file"):
            return httpx.Response(404, json={"detail": "no such file"})
        return httpx.Response(200, json={"linked_files": ["notes.md"]})

    client = HarnessAPIClient.__new__(HarnessAPIClient)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(answer), base_url="http://api.invalid")
    client._session_id = None
    client._merge_params = lambda params: params
    try:
        said = json.loads(await client._missing_skill_file("proc", 123, {}))
    finally:
        await client._client.aclose()
    assert said["success"] is False and "'123' not found" in said["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["not json", "[1]", json.dumps({"error": "no"})])
async def test_a_failed_image_read_does_not_repeat_a_long_path(answer) -> None:
    async def dispatch(_name, _arguments, **_kwargs):
        return answer

    said = json.loads(await handle_image_read(LONG, {"path": LONG}, dispatch, {}))
    assert len(said["error"]) < 500 and LONG not in said["error"]


@pytest.mark.asyncio
async def test_vision_does_not_repeat_a_long_path_it_cannot_find(tmp_path) -> None:
    storage = LocalBackend(str(tmp_path))
    config = {"storage_bucket": BUCKET, "workspace_path": storage.resolve_workspace_path(BUCKET, "s1")}
    with pytest.raises(ValueError) as refused:
        await vision_mod._image_ref_to_data_url(
            LONG, workspace_path=config["workspace_path"], storage=storage, session_id="s1", session_config=config,
        )
    assert len(str(refused.value)) < 500


@pytest.mark.asyncio
async def test_a_skill_file_it_does_not_have_is_named_short(tmp_path, monkeypatch) -> None:
    skill_dir = tmp_path / "skills" / "proc"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: proc\ndescription: d\n---\nbody\n", encoding="utf-8")
    _stub_load_all_skills(monkeypatch, [SkillDef(name="proc", description="d", content="b", source=SKILL_SOURCE_PLATFORM)])
    monkeypatch.setattr(skills_mod, "_resolve_skill_dir", lambda *a, **kw: skill_dir)
    said = json.loads(await _skill_view_handler({"name": "proc", "file_path": LONG}, tenant=_make_tenant(tmp_path)))
    assert said["success"] is False and len(said["error"]) < 500


@pytest.mark.asyncio
async def test_the_read_tools_redirect_for_an_image_names_it_short(tmp_path) -> None:
    said = json.loads(await _read_file_handler({"path": LONG}, workspace_path=str(tmp_path)))
    assert "Image file detected" in said["error"] and len(said["error"]) < 500
