"""``read_file`` on an image: the worker's analysis cache is per session, checked first, and per version.

``_build_key`` stat'ed and realpath'ed the model's path (relative to the worker's own working folder) before
``vision_analyze``'s containment check, and the process-wide cache was looked up first: session B asking for session
A's image by its absolute host path got A's analysis, and nothing checked the path.  A relative path was keyed on the
session and the text alone, so a changed image was answered with the analysis of its old version.
"""

from __future__ import annotations

import base64
import json
import os
from io import BytesIO
from types import SimpleNamespace
from uuid import UUID

import pytest
from PIL import Image

from surogates.harness import image_read
from surogates.harness.image_read import handle_image_read
from surogates.storage.tenant import session_workspace_key
from surogates.tools.builtin.vision import _vision_analyze_handler


def _png(colour: tuple[int, int, int], size: int = 2) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (size, size), colour).save(buf, format="PNG")
    return buf.getvalue()


RED, BLUE = _png((200, 40, 10)), _png((10, 40, 200), size=3)


class Vision:
    """``vision_analyze`` as the worker dispatches it, with a model that names the image it was sent."""

    def __init__(self) -> None:
        self.calls = 0
        self.during = None  # run while the model looks, as a command could

    async def _create(self, **request):
        self.calls += 1
        if self.during is not None:
            self.during()
        url = request["messages"][0]["content"][1]["image_url"]["url"]
        data = base64.b64decode(url.split(",", 1)[1])
        seen = {RED: "a red square", BLUE: "a blue square"}.get(data, "another image")
        message = SimpleNamespace(model_dump=lambda **_: {"role": "assistant", "content": seen})
        return SimpleNamespace(model="m", choices=[SimpleNamespace(finish_reason="stop", message=message)], usage=None)

    async def dispatch(self, name, arguments, **kwargs):
        assert name == "vision_analyze"
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=self._create)))
        return await _vision_analyze_handler(arguments, **kwargs, llm_client=client, model="m")


@pytest.fixture(autouse=True)
def empty_cache():
    image_read._CACHE.clear()
    yield
    image_read._CACHE.clear()


async def _read(vision, path, session_id, workspace, **more):
    kwargs = {"session_id": session_id, "workspace_path": str(workspace), **more}
    return json.loads(await handle_image_read(path, {"path": path}, vision.dispatch, kwargs))


@pytest.fixture
def two_sessions(tmp_path):
    a, b = tmp_path / "sessions" / "A", tmp_path / "sessions" / "B"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    (a / "secret.png").write_bytes(RED)
    return a, b


async def test_another_session_never_gets_a_sessions_analysis_by_its_absolute_path(two_sessions):
    a, b = two_sessions
    vision = Vision()
    mine = await _read(vision, str(a / "secret.png"), "A", a)
    assert "a red square" in mine["content"]

    theirs = await _read(vision, str(a / "secret.png"), "B", b)
    # Master: the cached analysis of A's image, with no call and no check.
    assert "content" not in theirs and "a red square" not in json.dumps(theirs)
    assert "Path traversal blocked" in theirs["error"]


async def test_nothing_is_looked_at_or_up_for_a_path_out_of_the_workspace(two_sessions, monkeypatch):
    """The containment check comes before any stat, realpath or cache lookup."""
    a, b = two_sessions
    looked_up, stated = [], []
    monkeypatch.setattr(image_read._CACHE, "get", lambda key: looked_up.append(key))
    from surogates.tools.workspace_io import local

    real_stat = local.LocalWorkspaceIO.stat

    async def stat(self, key):
        stated.append(key)
        return await real_stat(self, key)

    monkeypatch.setattr(local.LocalWorkspaceIO, "stat", stat)
    for path in (str(a / "secret.png"), "../A/secret.png"):
        assert "Path traversal blocked" in (await _read(Vision(), path, "B", b))["error"]
    assert looked_up == [] and stated == []


@pytest.mark.parametrize("given", ["pic.png", "ABS", "/workspace/pic.png"])
async def test_a_changed_image_is_analyzed_anew(tmp_path, given):
    path = str(tmp_path / "pic.png") if given == "ABS" else given
    (tmp_path / "pic.png").write_bytes(RED)
    vision = Vision()
    assert "a red square" in (await _read(vision, path, "A", tmp_path))["content"]
    (tmp_path / "pic.png").write_bytes(BLUE)
    # Master, for a relative path: the red square again, from the cache.
    assert "a blue square" in (await _read(vision, path, "A", tmp_path))["content"]
    assert vision.calls == 2


async def test_an_unchanged_image_is_analyzed_once_whichever_way_it_is_named(tmp_path):
    (tmp_path / "pic.png").write_bytes(RED)
    vision = Vision()
    for path in ("pic.png", str(tmp_path / "pic.png"), "/workspace/pic.png", "./pic.png"):
        assert "a red square" in (await _read(vision, path, "A", tmp_path))["content"]
    assert vision.calls == 1


async def test_an_image_changed_while_it_was_analyzed_is_not_kept(tmp_path, monkeypatch):
    (tmp_path / "pic.png").write_bytes(RED)
    kept = []
    real_put = image_read._CACHE.put
    monkeypatch.setattr(image_read._CACHE, "put", lambda key, value: (kept.append(key), real_put(key, value)))
    vision = Vision()
    vision.during = lambda: (tmp_path / "pic.png").write_bytes(BLUE)
    assert "a red square" in (await _read(vision, "pic.png", "A", tmp_path))["content"]
    assert kept == []  # neither under the red key nor under the blue one
    vision.during = None
    assert "a blue square" in (await _read(vision, "pic.png", "A", tmp_path))["content"]
    assert len(kept) == 1


async def test_a_path_is_keyed_as_vision_analyze_reads_it_stripped(tmp_path):
    (tmp_path / "pic.png").write_bytes(RED)
    (tmp_path / " pic.png").write_bytes(BLUE)  # what the unstripped text would name
    vision = Vision()
    assert "a red square" in (await _read(vision, " pic.png", "A", tmp_path))["content"]
    assert "a red square" in (await _read(vision, "pic.png", "A", tmp_path))["content"]
    assert vision.calls == 1


@pytest.mark.parametrize("ref", ["https://example.com/a.png", "http://example.com/a.png", "data:image/png;base64,AA==",
                                 "//[abc/x.png", "   "])
async def test_a_url_or_an_unparsable_reference_has_no_key(tmp_path, ref):
    assert await image_read._build_key(ref, {"session_id": "A", "workspace_path": str(tmp_path)}) is None


async def test_an_unparsable_reference_is_refused_as_a_read_not_raised(tmp_path):
    answer = await _read(Vision(), "//[abc/x.png", "A", tmp_path)
    assert "error" in answer


@pytest.mark.parametrize("ref", ["../B/pic.png", "/etc/pic.png", "/workspace/../B/pic.png"])
async def test_in_a_bucket_a_path_out_of_the_workspace_has_no_key_and_nothing_is_stated(ref):
    class Never(Bucket):
        async def stat(self, bucket, key):
            raise AssertionError(f"stat'ed {key}")

    kwargs = {"session_id": UUID(int=1), "workspace_path": "/workspace", "storage": Never(),
              "session_config": {"storage_bucket": "agent"}}
    assert await image_read._build_key(ref, kwargs) is None


async def test_a_threads_copy_of_the_workspace_has_no_key(monkeypatch):
    from surogates.sandbox import copy_files

    monkeypatch.setattr(copy_files, "has_copy", lambda pool, owner: True)
    bucket = Bucket()
    bucket.put("agent", session_workspace_key(UUID(int=1), "pic.png"), RED)
    kwargs = {"session_id": UUID(int=1), "workspace_path": "/workspace", "storage": bucket,
              "session_config": {"storage_bucket": "agent"}, "sandbox_pool": object(), "task_id": "t"}
    assert await image_read._build_key("pic.png", kwargs) is None


async def test_without_a_session_nothing_is_cached(tmp_path):
    (tmp_path / "pic.png").write_bytes(RED)
    vision = Vision()
    for _ in range(2):
        assert "a red square" in (await _read(vision, "pic.png", None, tmp_path))["content"]
    assert vision.calls == 2


class Bucket:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.writes = 0

    def put(self, bucket, key, data):
        self.writes += 1
        self.objects[(bucket, key)] = data

    async def read(self, bucket, key):
        try:
            return self.objects[(bucket, key)]
        except KeyError:
            raise KeyError(f"{bucket}/{key}") from None

    async def stat(self, bucket, key):
        data = await self.read(bucket, key)
        return {"size": len(data), "modified": None, "etag": f"e{self.writes}-{hash(data)}"}


async def test_in_a_bucket_a_changed_image_is_analyzed_anew_and_sessions_are_apart():
    a, b = UUID(int=1), UUID(int=2)
    bucket = Bucket()
    bucket.put("agent", session_workspace_key(a, "pic.png"), RED)
    bucket.put("agent", session_workspace_key(b, "pic.png"), BLUE)
    vision = Vision()
    on = {"storage": bucket, "session_config": {"storage_bucket": "agent"}}
    assert "a red square" in (await _read(vision, "pic.png", a, "/workspace", **on))["content"]
    assert "a blue square" in (await _read(vision, "/workspace/pic.png", b, "/workspace", **on))["content"]
    assert "a red square" in (await _read(vision, "pic.png", a, "/workspace", **on))["content"]
    assert vision.calls == 2
    bucket.put("agent", session_workspace_key(a, "pic.png"), BLUE)
    # Master: the red square again, keyed on the session and the text alone.
    assert "a blue square" in (await _read(vision, "pic.png", a, "/workspace", **on))["content"]
    assert vision.calls == 3


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="POSIX only")
async def test_a_link_out_of_the_workspace_has_no_key(two_sessions):
    a, b = two_sessions
    (b / "pic.png").symlink_to(a / "secret.png")
    vision = Vision()
    await _read(vision, str(a / "secret.png"), "A", a)
    assert "Path traversal blocked" in (await _read(vision, "pic.png", "B", b))["error"]
