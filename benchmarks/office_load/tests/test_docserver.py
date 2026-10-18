"""The tool's side toward the document server: editor configs, input files, callbacks, saved files and forcesave."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
import jwt
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, unused_port

from oload.docserver import CallbackState, callback_url, file_url, forcesave, internal_url, make_app
from oload.editor import editor_config
from oload.settings import ENV_NAMES, MissingSettings, Settings, load_env_file

SECRET = "a-test-secret-of-thirty-two-bytes-or-more"
DS_HOST = "http://office-docs.example.svc"


def settings(command_url: str = DS_HOST) -> Settings:
    return Settings(
        ds_url=DS_HOST,
        command_url=command_url,
        jwt_secret=SECRET,
        client_token="a-test-client-token",
        listen="oload.example:8790",
        public_base="http://oload.example:8790",
        namespace="office-load",
    )


def signed(payload: dict[str, Any], secret: str = SECRET) -> dict[str, Any]:
    """A callback body as the document server sends it, its token in the body."""
    return {**payload, "token": jwt.encode(payload, secret, algorithm="HS256")}


class FakeDocumentServer:
    """Serves saved files under /cache and answers /command."""

    def __init__(self, *, saved: bytes = b"saved", command_delay: float = 0.0, command_error: int = 0,
                 slow_paths: tuple[str, ...] = (), slow_s: float = 0.0) -> None:
        self.saved = saved
        self.command_delay = command_delay
        self.command_error = command_error
        self.slow_paths = slow_paths
        self.slow_s = slow_s
        self.downloads: list[str] = []
        self.queries: list[str] = []
        self.commands: list[dict[str, Any]] = []
        self.release = asyncio.Event()

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/cache/{tail:.*}", self._cache)
        app.router.add_post("/command", self._command)
        return app

    async def _cache(self, request: web.Request) -> web.Response:
        self.downloads.append(request.path)
        self.queries.append(request.query_string)
        if request.path in self.slow_paths:
            await asyncio.sleep(self.slow_s)
        return web.Response(body=self.saved + request.path.encode())

    async def _command(self, request: web.Request) -> web.Response:
        self.commands.append(await request.json())
        if self.command_delay:
            try:
                await asyncio.wait_for(self.release.wait(), self.command_delay)
            except TimeoutError:
                pass
        return web.json_response({"error": self.command_error})


def test_editor_config_is_signed_and_shaped() -> None:
    config = editor_config(
        settings(), doc_key="load-r1-0-abc", kind="docx", user_id="agent-0", user_name="Load agent",
        file_url="http://oload.example:8790/files/load-r1-0-abc",
        callback_url="http://oload.example:8790/callback/load-r1-0-abc",
    )

    claims = jwt.decode(config["token"], SECRET, algorithms=["HS256"])
    assert claims == {key: value for key, value in config.items() if key != "token"}
    assert claims["document"]["key"] == "load-r1-0-abc"
    assert claims["document"]["fileType"] == "docx"
    assert claims["document"]["url"] == "http://oload.example:8790/files/load-r1-0-abc"
    assert claims["documentType"] == "word"
    editor = claims["editorConfig"]
    assert editor["callbackUrl"] == "http://oload.example:8790/callback/load-r1-0-abc"
    assert editor["mode"] == "edit"
    assert editor["user"] == {"id": "agent-0", "name": "Load agent"}
    assert editor["coEditing"] == {"mode": "fast", "change": False}
    assert editor["customization"]["forcesave"] is True


@pytest.mark.parametrize(("kind", "document_type"), [("docx", "word"), ("xlsx", "cell"), ("pptx", "slide"), ("pdf", "pdf")])
def test_editor_config_maps_each_kind(kind: str, document_type: str) -> None:
    config = editor_config(settings(), doc_key="k", kind=kind, user_id="u", user_name="U",
                           file_url="http://f.example/k", callback_url="http://c.example/k")
    assert (config["document"]["fileType"], config["documentType"]) == (kind, document_type)


def test_editor_config_refuses_another_kind() -> None:
    with pytest.raises(ValueError):
        editor_config(settings(), doc_key="k", kind="odt", user_id="u", user_name="U",
                      file_url="http://f.example/k", callback_url="http://c.example/k")


def test_urls_name_the_tool_routes() -> None:
    assert file_url(settings(), "load-r1-0-abc") == "http://oload.example:8790/files/load-r1-0-abc"
    assert callback_url(settings(), "load-r1-0-abc") == "http://oload.example:8790/callback/load-r1-0-abc"


def test_internal_url_keeps_the_path_and_query() -> None:
    assert internal_url(f"{DS_HOST}/cache/files/data/k/output.docx?md5=a_b&expires=9",
                        "https://ds.example:8443") == "https://ds.example:8443/cache/files/data/k/output.docx?md5=a_b&expires=9"


async def test_files_serves_the_current_input() -> None:
    state = CallbackState(settings())
    state.add_document("load-r1-0-abc", "docx", b"first")
    state.add_document("load-r1-0-abc", "docx", b"current")
    async with TestClient(TestServer(make_app(state))) as client:
        found = await client.get("/files/load-r1-0-abc")
        assert found.status == 200
        assert await found.read() == b"current"
        missing = await client.get("/files/load-r1-0-zzz")
        assert missing.status == 404


@pytest.mark.parametrize("body", [
    signed({"key": "k1", "status": 6, "userdata": "t1", "url": f"{DS_HOST}/cache/x"}, secret=f"another-{SECRET}"),
    {"key": "k1", "status": 6, "userdata": "t1", "url": f"{DS_HOST}/cache/x"},
    signed({"key": "k2", "status": 6, "userdata": "t1", "url": f"{DS_HOST}/cache/x"}),
    {**signed({"key": "k1", "status": 6, "userdata": "t1"}), "token": "not-a-jwt"},
], ids=["another-secret", "unsigned", "another-document", "garbage-token"])
async def test_callback_rejects_a_bad_token(body: dict[str, Any]) -> None:
    fake = FakeDocumentServer()
    async with TestServer(fake.app()) as ds:
        state = CallbackState(settings(command_url=str(ds.make_url(""))))
        state.add_document("k1", "docx", b"input")
        async with TestClient(TestServer(make_app(state))) as client:
            response = await client.post("/callback/k1", json=body)
            assert response.status == 200
            assert await response.json() == {"error": 1}

    assert len(state.callbacks) == 1
    recorded = state.callbacks[0]
    assert recorded.doc_key == "k1"
    assert recorded.error
    assert recorded.status is None and recorded.userdata is None and recorded.data is None
    assert fake.downloads == []
    assert await state.wait_for("k1", "t1", timeout=0.05) is None


async def test_callback_rejects_a_body_that_is_not_json() -> None:
    state = CallbackState(settings())
    async with TestClient(TestServer(make_app(state))) as client:
        response = await client.post("/callback/k1", data=b"not json", headers={"Content-Type": "application/json"})
        assert await response.json() == {"error": 1}
    assert state.callbacks[0].error


async def test_callback_status_2_downloads_through_the_command_host() -> None:
    fake = FakeDocumentServer(saved=b"the saved docx at ")
    async with TestServer(fake.app()) as ds:
        state = CallbackState(settings(command_url=str(ds.make_url(""))))
        state.add_document("k1", "docx", b"input")
        async with TestClient(TestServer(make_app(state))) as client:
            payload = {"key": "k1", "status": 2, "userdata": "t1", "url": f"{DS_HOST}/cache/x?md5=a_b&expires=9"}
            response = await client.post("/callback/k1", json=signed(payload))
            assert response.status == 200
            assert await response.json() == {"error": 0}

    assert fake.downloads == ["/cache/x"]
    assert fake.queries == ["md5=a_b&expires=9"]
    recorded = state.callbacks[0]
    assert (recorded.doc_key, recorded.status, recorded.userdata) == ("k1", 2, "t1")
    assert recorded.url == f"{DS_HOST}/cache/x?md5=a_b&expires=9"
    assert recorded.data == b"the saved docx at /cache/x"
    assert recorded.error is None
    assert recorded.download_s is not None and recorded.at > 0


async def test_callback_token_in_the_authorization_header() -> None:
    fake = FakeDocumentServer()
    async with TestServer(fake.app()) as ds:
        state = CallbackState(settings(command_url=str(ds.make_url(""))))
        async with TestClient(TestServer(make_app(state))) as client:
            payload = {"key": "k1", "status": 6, "userdata": "t1", "url": f"{DS_HOST}/cache/y"}
            token = jwt.encode({"payload": payload}, SECRET, algorithm="HS256")
            response = await client.post("/callback/k1", json=payload, headers={"Authorization": f"Bearer {token}"})
            assert await response.json() == {"error": 0}
    assert state.callbacks[0].data == b"saved/cache/y"


async def test_callback_status_6_whose_download_fails_is_an_error() -> None:
    state = CallbackState(settings(command_url=f"http://localhost:{unused_port()}"))
    async with TestClient(TestServer(make_app(state))) as client:
        payload = {"key": "k1", "status": 6, "userdata": "t1", "url": f"{DS_HOST}/cache/x"}
        response = await client.post("/callback/k1", json=signed(payload))
        assert await response.json() == {"error": 1}
    got = await state.wait_for("k1", "t1", timeout=1)
    assert got is state.callbacks[0]
    assert got.status == 6 and got.data is None and got.error


@pytest.mark.parametrize("status", [1, 4, 3, 7])
async def test_callback_without_a_file_is_recorded(status: int) -> None:
    state = CallbackState(settings())
    async with TestClient(TestServer(make_app(state))) as client:
        payload = {"key": "k1", "status": status, "userdata": "t1", "users": ["agent-0"]}
        response = await client.post("/callback/k1", json=signed(payload))
        assert await response.json() == {"error": 0}
    recorded = state.callbacks[0]
    assert (recorded.status, recorded.userdata, recorded.data, recorded.error) == (status, "t1", None, None)


async def test_wait_for_matches_userdata() -> None:
    fake = FakeDocumentServer()
    async with TestServer(fake.app()) as ds:
        state = CallbackState(settings(command_url=str(ds.make_url(""))))
        async with TestClient(TestServer(make_app(state))) as client:
            waiter = asyncio.create_task(state.wait_for("k1", "t2", timeout=5))
            for userdata in ("t1", "t2"):
                payload = {"key": "k1", "status": 6, "userdata": userdata, "url": f"{DS_HOST}/cache/{userdata}"}
                assert await (await client.post("/callback/k1", json=signed(payload))).json() == {"error": 0}
            got = await waiter

            assert got is state.callbacks[1]
            assert got.userdata == "t2" and got.data == b"saved/cache/t2"
            # Already arrived: answered at once.
            assert await state.wait_for("k1", "t1", timeout=0.05) is state.callbacks[0]
            # Another document's turn of the same name is not this one's.
            assert await state.wait_for("k9", "t2", timeout=0.05) is None
    assert [callback.userdata for callback in state.callbacks] == ["t1", "t2"]


async def test_wait_for_waits_for_the_download() -> None:
    fake = FakeDocumentServer(slow_paths=("/cache/slow",), slow_s=0.5)
    async with TestServer(fake.app()) as ds:
        state = CallbackState(settings(command_url=str(ds.make_url(""))))
        async with TestClient(TestServer(make_app(state))) as client:
            waiter = asyncio.create_task(state.wait_for("k1", "t2", timeout=5))
            slow = asyncio.create_task(client.post(
                "/callback/k1", json=signed({"key": "k1", "status": 6, "userdata": "t2", "url": f"{DS_HOST}/cache/slow"})))
            await asyncio.sleep(0.1)
            # Another save settles while the slow one is still downloading.
            await client.post(
                "/callback/k1", json=signed({"key": "k1", "status": 6, "userdata": "t1", "url": f"{DS_HOST}/cache/fast"}))
            got = await waiter
            await slow
    # Arrival order: the slow one came first.
    assert [callback.userdata for callback in state.callbacks] == ["t2", "t1"]
    assert got.userdata == "t2" and got.data == b"saved/cache/slow"


async def test_wait_for_times_out_with_none() -> None:
    state = CallbackState(settings())
    assert await state.wait_for("k1", "t1", timeout=0.05) is None


async def test_forcesave_signs_the_command_and_reads_its_error() -> None:
    fake = FakeDocumentServer(command_error=4)
    async with TestServer(fake.app()) as ds:
        async with aiohttp.ClientSession() as session:
            result = await forcesave(session, settings(command_url=str(ds.make_url(""))), "k1", "t1")
    assert result.error == 4
    assert 0 <= result.seconds < 5
    [sent] = fake.commands
    assert {key: sent[key] for key in ("c", "key", "userdata")} == {"c": "forcesave", "key": "k1", "userdata": "t1"}
    assert jwt.decode(sent["token"], SECRET, algorithms=["HS256"]) == {"c": "forcesave", "key": "k1", "userdata": "t1"}


async def test_forcesave_timeout_is_none() -> None:
    fake = FakeDocumentServer(command_delay=11)
    async with TestServer(fake.app()) as ds:
        async with aiohttp.ClientSession() as session:
            result = await forcesave(session, settings(command_url=str(ds.make_url(""))), "k1", "t1")
        fake.release.set()
    assert result.error is None
    assert result.seconds >= 10


async def test_forcesave_unreachable_is_none() -> None:
    async with aiohttp.ClientSession() as session:
        result = await forcesave(session, settings(command_url=f"http://localhost:{unused_port()}"), "k1", "t1")
    assert result.error is None


def test_settings_from_env_reads_every_name() -> None:
    env = {name: f"value-of-{name}" for name in ENV_NAMES.values()}
    loaded = Settings.from_env(env)
    assert loaded.command_url == "value-of-OLOAD_COMMAND_URL"
    assert loaded.public_base == "value-of-OLOAD_PUBLIC_BASE"
    assert SECRET not in repr(settings()) and "a-test-client-token" not in repr(settings())


def test_settings_from_env_names_what_is_missing() -> None:
    env = {name: "x" for name in ENV_NAMES.values() if name not in ("OLOAD_JWT_SECRET", "OLOAD_LISTEN")}
    with pytest.raises(MissingSettings) as raised:
        Settings.from_env(env)
    assert "OLOAD_JWT_SECRET" in str(raised.value) and "OLOAD_LISTEN" in str(raised.value)


def test_env_file_fills_what_the_environment_lacks(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("# site values\nOLOAD_NAMESPACE=from-file\nexport OLOAD_LISTEN='oload.example:1'\n\nOLOAD_JWT_SECRET=\"s=1\"\n")
    environ = {"OLOAD_NAMESPACE": "from-env"}
    load_env_file(env_file, environ)
    assert environ == {"OLOAD_NAMESPACE": "from-env", "OLOAD_LISTEN": "oload.example:1", "OLOAD_JWT_SECRET": "s=1"}
    load_env_file(tmp_path / "absent.env", environ)  # no file: nothing changes
