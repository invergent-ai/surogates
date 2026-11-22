"""A capped read fetches no more than its cap, on either backend and on the routes that use it."""

from __future__ import annotations

import base64
import contextlib
import json
from types import SimpleNamespace

import pytest

from surogates.harness.loop import AgentHarness
from surogates.harness.loop_vision_inject import MAX_INJECTED_IMAGE_BYTES
from surogates.sandbox import copy_files, executor_server
from surogates.storage.backend import LocalBackend, S3Backend, read_capped
from surogates.tools.builtin import vision
from surogates.tools.workspace_io.storage import StorageWorkspaceIO

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


class Counted:
    """A store that says how it was read."""

    def __init__(self, data: bytes) -> None:
        self.data, self.whole, self.capped = data, 0, []

    async def read(self, bucket, key):
        self.whole += 1
        return self.data

    async def read_at_most(self, bucket, key, max_bytes):
        self.capped.append(max_bytes)
        return self.data[:max_bytes]


class Body:
    def __init__(self, data: bytes) -> None:
        self.data, self.served, self.closed = data, 0, False

    async def iter_chunks(self, size):
        for at in range(0, len(self.data), size):
            self.served += len(self.data[at:at + size])
            yield self.data[at:at + size]

    def close(self):
        self.closed = True


def s3_with(monkeypatch, body: Body) -> S3Backend:
    backend = S3Backend("http://s3.invalid")

    class Client:
        async def get_object(self, *, Bucket, Key):
            return {"Body": body}

    @contextlib.asynccontextmanager
    async def client():
        yield Client()

    monkeypatch.setattr(backend, "_client", client)
    return backend


@pytest.mark.asyncio
async def test_local_read_stops_at_the_cap(tmp_path) -> None:
    backend = LocalBackend(str(tmp_path))
    await backend.write("bucket", "a.bin", b"0123456789")
    assert await backend.read_at_most("bucket", "a.bin", 4) == b"0123"
    assert await backend.read_at_most("bucket", "a.bin", 100) == b"0123456789"
    with pytest.raises(KeyError):
        await backend.read_at_most("bucket", "missing.bin", 4)


@pytest.mark.asyncio
async def test_local_capped_read_follows_no_link(tmp_path) -> None:
    backend = LocalBackend(str(tmp_path))
    await backend.write("bucket", "a.bin", b"inside")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    (tmp_path / "bucket" / "link.bin").symlink_to(outside)
    with pytest.raises(KeyError):
        await backend.read_at_most("bucket", "link.bin", 100)


@pytest.mark.asyncio
async def test_s3_read_stops_fetching_at_the_cap(monkeypatch) -> None:
    body = Body(b"x" * (8 * 1024 * 1024))
    held = await s3_with(monkeypatch, body).read_at_most("bucket", "a.bin", 1024)
    assert held == b"x" * 1024
    assert body.closed
    assert body.served == 1024


@pytest.mark.asyncio
async def test_s3_read_of_a_smaller_object_is_whole(monkeypatch) -> None:
    body = Body(b"abc")
    assert await s3_with(monkeypatch, body).read_at_most("bucket", "a.bin", 1024) == b"abc"
    assert body.closed


@pytest.mark.asyncio
async def test_a_store_with_no_capped_read_is_still_cut() -> None:
    class Plain:
        async def read(self, bucket, key):
            return b"0123456789"

    assert await read_capped(Plain(), "bucket", "a.bin", 4) == b"0123"


@pytest.mark.asyncio
async def test_the_workspace_read_with_a_cap_does_not_fetch_the_whole_object() -> None:
    store = Counted(b"0123456789")
    files = StorageWorkspaceIO(store, bucket="bucket", prefix="p/")
    assert await files.read("a.bin", max_bytes=4) == b"0123"
    assert (store.whole, store.capped) == (0, [4])
    assert await files.read("a.bin") == b"0123456789"
    assert store.whole == 1


@pytest.mark.asyncio
async def test_the_vision_storage_route_asks_for_no_more_than_the_cap(monkeypatch) -> None:
    monkeypatch.setattr(vision, "_MAX_IMAGE_BYTES", 10)
    store = Counted(PNG)
    with pytest.raises(ValueError, match="too large"):
        await vision._image_ref_to_data_url(
            "a.png", workspace_path="/workspace", storage=store, session_id="s1",
            session_config={"storage_bucket": "bucket"},
        )
    assert (store.whole, store.capped) == (0, [11])


@pytest.mark.asyncio
async def test_an_injected_image_over_the_cap_is_skipped() -> None:
    store = Counted(b"x" * (MAX_INJECTED_IMAGE_BYTES + 5))
    harness = SimpleNamespace(_storage=store, _sandbox_pool=None)
    session = SimpleNamespace(id="s1", config={"storage_bucket": "bucket"})
    assert await AgentHarness._read_workspace_image(harness, session, "a.png") is None
    assert (store.whole, store.capped) == (0, [MAX_INJECTED_IMAGE_BYTES + 1])


def _pod_read(workspace, **args):
    return json.loads(executor_server._run_file({"action": "read", "path": "a.bin", **args}, str(workspace)))


def test_the_pod_sends_no_more_than_a_read_asks_for(tmp_path) -> None:
    (tmp_path / "a.bin").write_bytes(b"0123456789")
    refused = _pod_read(tmp_path, max_bytes=4)
    assert "content_b64" not in refused and "over the 4 bytes" in refused["error"]
    assert base64.b64decode(_pod_read(tmp_path, max_bytes=10)["content_b64"]) == b"0123456789"


@pytest.mark.parametrize("asked", [True, -1, "4", 4.0, None, 10**12])
def test_the_pod_ignores_a_limit_that_is_not_a_smaller_size(tmp_path, asked) -> None:
    (tmp_path / "a.bin").write_bytes(b"0123456789")
    assert base64.b64decode(_pod_read(tmp_path, max_bytes=asked)["content_b64"]) == b"0123456789"


@pytest.mark.asyncio
async def test_a_copy_read_passes_its_cap_to_the_pod() -> None:
    sent = []

    class Pool:
        async def execute(self, _owner, _name, payload):
            sent.append(json.loads(payload))
            return json.dumps({"content_b64": base64.b64encode(b"x").decode()})

    assert await copy_files.read_copy(Pool(), "owner", "a.png", max_bytes=7) == b"x"
    assert await copy_files.read_copy(Pool(), "owner", "a.png") == b"x"
    assert sent[0]["max_bytes"] == 7 and "max_bytes" not in sent[1]


@pytest.mark.asyncio
async def test_the_vision_copy_route_asks_the_pod_for_no_more_than_the_cap(monkeypatch) -> None:
    monkeypatch.setattr(vision, "_MAX_IMAGE_BYTES", 10)
    monkeypatch.setattr(vision, "has_copy", lambda *_: True)
    asked = {}

    async def read_copy(_pool, _owner, _path, *, max_bytes=None):
        asked["max_bytes"] = max_bytes
        return PNG  # a pod from before the cap sends the whole file

    monkeypatch.setattr(vision, "read_copy", read_copy)
    with pytest.raises(ValueError, match="too large"):
        await vision._image_ref_to_data_url(
            "a.png", workspace_path="/workspace", storage=Counted(PNG), session_id="s1",
            session_config={"storage_bucket": "bucket"}, sandbox_pool=object(), owner="s1",
        )
    assert asked["max_bytes"] == 10


def test_a_file_that_grows_past_the_cap_during_the_read_is_refused(tmp_path, monkeypatch) -> None:
    (tmp_path / "a.bin").write_bytes(b"0123456789")
    real = executor_server.os.fstat

    def small(fd):
        found = real(fd)
        return SimpleNamespace(st_size=1, st_mode=found.st_mode, st_nlink=found.st_nlink)

    monkeypatch.setattr(executor_server.os, "fstat", small)
    answer = _pod_read(tmp_path, max_bytes=4)
    assert answer.get("too_large") is True and "content_b64" not in answer


@pytest.mark.asyncio
async def test_a_copy_too_large_is_said_as_every_vision_route_says_it(tmp_path, monkeypatch) -> None:
    (tmp_path / "a.png").write_bytes(PNG)
    monkeypatch.setattr(vision, "_MAX_IMAGE_BYTES", 10)
    monkeypatch.setattr(vision, "has_copy", lambda *_: True)

    class Pod:
        async def execute(self, _owner, _name, payload):
            return executor_server._run_file(json.loads(payload), str(tmp_path))

    with pytest.raises(ValueError, match="^Image file is too large:"):
        await vision._image_ref_to_data_url(
            "a.png", workspace_path="/workspace", storage=Counted(PNG), session_id="s1",
            session_config={"storage_bucket": "bucket"}, sandbox_pool=Pod(), owner="s1",
        )


@pytest.mark.asyncio
async def test_an_old_pod_sending_too_much_is_not_injected(monkeypatch) -> None:
    import surogates.harness.loop as loop_module

    monkeypatch.setattr(loop_module, "has_copy", lambda *_: True)

    async def read_copy(*_args, **_kwargs):
        return b"x" * (MAX_INJECTED_IMAGE_BYTES + 1)

    monkeypatch.setattr(loop_module, "read_copy", read_copy)
    harness = SimpleNamespace(_storage=Counted(b""), _sandbox_pool=object())
    session = SimpleNamespace(id="s1", config={"storage_bucket": "bucket"})
    assert await AgentHarness._read_workspace_image(harness, session, "a.png") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", [-1, 0])
async def test_a_negative_cap_reads_nothing_on_either_backend(tmp_path, monkeypatch, cap) -> None:
    local = LocalBackend(str(tmp_path))
    await local.write("bucket", "a.bin", b"0123456789")
    assert await local.read_at_most("bucket", "a.bin", cap) == b""
    assert await s3_with(monkeypatch, Body(b"0123456789")).read_at_most("bucket", "a.bin", cap) == b""


@pytest.mark.asyncio
async def test_channel_media_grown_past_its_cap_is_skipped(tmp_path) -> None:
    from surogates.channels.channel_media import resolve_workspace_media

    class Grown(LocalBackend):
        async def stat(self, bucket, key):
            return {"size": 1}

    store = Grown(str(tmp_path))
    await store.write("bucket", "s1/a.bin", b"0123456789")
    session = SimpleNamespace(id="s1", channel="web", config={"storage_bucket": "bucket"})
    assert await resolve_workspace_media(store, session, paths=["a.bin"], max_files=1, max_bytes=4) == []
    kept = await resolve_workspace_media(store, session, paths=["a.bin"], max_files=1, max_bytes=10)
    assert [file.data for file in kept] == [b"0123456789"]
