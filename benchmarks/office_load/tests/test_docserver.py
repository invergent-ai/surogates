"""The tool's side toward the document server: editor configs, input files, callbacks, saved files and forcesave."""

from __future__ import annotations

import asyncio
import json
from typing import Any

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


def served(command_url: str = DS_HOST) -> CallbackState:
    """A state serving the document ``k1``."""
    state = CallbackState(settings(command_url))
    state.add_document("k1", "docx", b"input")
    return state


def signed(payload: dict[str, Any], secret: str = SECRET) -> dict[str, Any]:
    """A callback body as the document server sends it, its token in the body."""
    return {**payload, "token": jwt.encode(payload, secret, algorithm="HS256")}


def save(userdata: str, path: str = "/cache/x", status: int = 6) -> dict[str, Any]:
    return signed({"key": "k1", "status": status, "userdata": userdata, "url": f"{DS_HOST}{path}"})


def refused_port() -> str:
    """A URL nothing listens at."""
    return f"http://localhost:{unused_port()}"


class FakeDocumentServer:
    """Serves saved files under /cache and answers /command."""

    def __init__(self, *, saved: bytes = b"saved", command_delay: float = 0.0, command_error: int = 0,
                 command_status: int = 200, command_body: bytes | None = None,
                 slow_paths: tuple[str, ...] = (), slow_s: float = 0.0, cache_statuses: tuple[int, ...] = ()) -> None:
        self.saved = saved
        self.command_delay = command_delay
        self.command_error = command_error
        self.command_status = command_status
        self.command_body = command_body
        self.slow_paths = slow_paths
        self.slow_s = slow_s
        self.cache_statuses = list(cache_statuses)  # answered in turn before the file is
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
        if self.cache_statuses:
            return web.Response(status=self.cache_statuses.pop(0), text="no")
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
        if self.command_body is not None:
            return web.Response(status=self.command_status, body=self.command_body)
        return web.json_response({"error": self.command_error}, status=self.command_status)


# The editor config.

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
    assert claims["documentType"] == "word"
    assert claims["editorConfig"]["callbackUrl"] == "http://oload.example:8790/callback/load-r1-0-abc"
    assert claims["editorConfig"]["customization"]["forcesave"] is True


def test_editor_config_is_the_agent_page_config() -> None:
    config = editor_config(
        settings(), doc_key="load-r1-0-abc", kind="docx", user_id="agent-0", user_name="Load agent",
        file_url="http://oload.example:8790/files/load-r1-0-abc",
        callback_url="http://oload.example:8790/callback/load-r1-0-abc",
    )
    expected = {
        "width": "100%",
        "height": "100%",
        "document": {
            "fileType": "docx",
            "key": "load-r1-0-abc",
            "title": "load-r1-0-abc.docx",
            "url": "http://oload.example:8790/files/load-r1-0-abc",
            "permissions": {"edit": True, "review": True, "comment": True, "download": True, "print": True},
        },
        "documentType": "word",
        "editorConfig": {
            "callbackUrl": "http://oload.example:8790/callback/load-r1-0-abc",
            "mode": "edit",
            "lang": "en",
            "user": {"id": "agent-0", "name": "Load agent"},
            "coEditing": {"mode": "fast", "change": False},
            "customization": {"forcesave": True, "uiTheme": "theme-light", "plugins": False},
        },
    }
    token = config.pop("token")
    # Pinned in order too: the token signs the config as serialized.
    assert json.dumps(config) == json.dumps(expected)
    assert jwt.decode(token, SECRET, algorithms=["HS256"]) == expected


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


# The input files.

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


# The callbacks, answered as the runtime's route answers them.

@pytest.mark.parametrize("body", [
    signed({"key": "k1", "status": 6, "userdata": "t1", "url": f"{DS_HOST}/cache/x"}, secret=f"another-{SECRET}"),
    {"key": "k1", "status": 6, "userdata": "t1", "url": f"{DS_HOST}/cache/x"},
    signed({"key": "k2", "status": 6, "userdata": "t1", "url": f"{DS_HOST}/cache/x"}),
    {**signed({"key": "k1", "status": 6, "userdata": "t1"}), "token": "not-a-jwt"},
], ids=["another-secret", "unsigned", "another-document", "garbage-token"])
async def test_callback_rejects_a_bad_token(body: dict[str, Any]) -> None:
    fake = FakeDocumentServer()
    async with TestServer(fake.app()) as ds:
        state = served(str(ds.make_url("")))
        async with TestClient(TestServer(make_app(state))) as client:
            response = await client.post("/callback/k1", json=body)
            assert response.status == 403

    [recorded] = state.callbacks
    assert recorded.doc_key == "k1" and recorded.reply_status == 403 and recorded.error
    assert recorded.status is None and recorded.userdata is None and recorded.data is None
    assert fake.downloads == []
    assert await state.wait_for("k1", "t1", timeout=0.05) is None


async def test_callback_for_a_document_not_served_is_404_without_a_download() -> None:
    fake = FakeDocumentServer()
    async with TestServer(fake.app()) as ds:
        state = served(str(ds.make_url("")))
        async with TestClient(TestServer(make_app(state))) as client:
            response = await client.post(
                "/callback/k9", json=signed({"key": "k9", "status": 6, "userdata": "t1", "url": f"{DS_HOST}/cache/x"}))
            assert response.status == 404
    assert fake.downloads == []
    [recorded] = state.callbacks
    assert (recorded.doc_key, recorded.reply_status, recorded.data) == ("k9", 404, None) and recorded.error


async def test_callback_rejects_a_body_that_is_not_json() -> None:
    state = served()
    async with TestClient(TestServer(make_app(state))) as client:
        response = await client.post("/callback/k1", data=b"not json", headers={"Content-Type": "application/json"})
        assert response.status == 400
    assert state.callbacks[0].reply_status == 400 and state.callbacks[0].error


async def test_callback_save_naming_no_file_is_400() -> None:
    state = served()
    async with TestClient(TestServer(make_app(state))) as client:
        response = await client.post("/callback/k1", json=signed({"key": "k1", "status": 6, "userdata": "t1"}))
        assert response.status == 400
    [recorded] = state.callbacks
    assert (recorded.status, recorded.userdata, recorded.reply_status) == (6, "t1", 400) and recorded.error
    # A failed attempt: given back once the wait is over.
    assert await state.wait_for("k1", "t1", timeout=0.05) is recorded


async def test_callback_without_a_status_is_status_0() -> None:
    state = served()
    async with TestClient(TestServer(make_app(state))) as client:
        response = await client.post("/callback/k1", json=signed({"key": "k1", "userdata": "t1"}))
        assert response.status == 200
        assert await response.json() == {"error": 0}
    [recorded] = state.callbacks
    assert (recorded.status, recorded.reply_status, recorded.error) == (0, 200, None)


async def test_callback_status_2_downloads_through_the_command_host() -> None:
    fake = FakeDocumentServer(saved=b"the saved docx at ")
    async with TestServer(fake.app()) as ds:
        state = served(str(ds.make_url("")))
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
    assert recorded.error is None and recorded.reply_status == 200
    assert recorded.download_s is not None and recorded.at > 0
    assert recorded.settled_mono is not None and recorded.settled_mono >= recorded.mono


async def test_callback_token_in_the_authorization_header() -> None:
    fake = FakeDocumentServer()
    async with TestServer(fake.app()) as ds:
        state = served(str(ds.make_url("")))
        async with TestClient(TestServer(make_app(state))) as client:
            payload = {"key": "k1", "status": 6, "userdata": "t1", "url": f"{DS_HOST}/cache/y"}
            token = jwt.encode({"payload": payload}, SECRET, algorithm="HS256")
            response = await client.post("/callback/k1", json=payload, headers={"Authorization": f"Bearer {token}"})
            assert await response.json() == {"error": 0}
    assert state.callbacks[0].data == b"saved/cache/y"


async def test_callback_whose_download_is_refused_is_502() -> None:
    state = served(refused_port())
    async with TestClient(TestServer(make_app(state))) as client:
        response = await client.post("/callback/k1", json=save("t1"))
        assert response.status == 502
    [recorded] = state.callbacks
    assert (recorded.status, recorded.reply_status, recorded.data) == (6, 502, None) and recorded.error
    assert await state.wait_for("k1", "t1", timeout=0.05) is recorded


@pytest.mark.parametrize("answer", [404, 500])
async def test_callback_whose_download_answers_an_error_is_502(answer: int) -> None:
    fake = FakeDocumentServer(cache_statuses=(answer,))
    async with TestServer(fake.app()) as ds:
        state = served(str(ds.make_url("")))
        async with TestClient(TestServer(make_app(state))) as client:
            response = await client.post("/callback/k1", json=save("t1"))
            assert response.status == 502
    assert fake.downloads == ["/cache/x"]
    [recorded] = state.callbacks
    assert (recorded.reply_status, recorded.data) == (502, None) and str(answer) in recorded.error


async def test_a_retry_after_a_502_is_the_turns_save() -> None:
    fake = FakeDocumentServer(cache_statuses=(500,))
    async with TestServer(fake.app()) as ds:
        state = served(str(ds.make_url("")))
        async with TestClient(TestServer(make_app(state))) as client:
            waiter = asyncio.create_task(state.wait_for("k1", "t1", timeout=5))
            first = await client.post("/callback/k1", json=save("t1"))
            assert first.status == 502
            await asyncio.sleep(0.05)
            assert not waiter.done()  # a failed attempt does not end the wait
            retry = await client.post("/callback/k1", json=save("t1"))
            assert retry.status == 200
            got = await waiter
    assert got is state.callbacks[1]
    assert got.data == b"saved/cache/x" and got.error is None
    # Every attempt is kept, so retries can be counted.
    assert [callback.reply_status for callback in state.callbacks] == [502, 200]
    assert not state._waiting


@pytest.mark.parametrize("status", [1, 4, 3, 7])
async def test_callback_without_a_file_is_recorded(status: int) -> None:
    state = served()
    async with TestClient(TestServer(make_app(state))) as client:
        payload = {"key": "k1", "status": status, "userdata": "t1", "users": ["agent-0"]}
        response = await client.post("/callback/k1", json=signed(payload))
        assert await response.json() == {"error": 0}
    recorded = state.callbacks[0]
    assert (recorded.status, recorded.userdata, recorded.data, recorded.error) == (status, "t1", None, None)


# Waiting for a turn's save.

async def test_wait_for_matches_userdata() -> None:
    fake = FakeDocumentServer()
    async with TestServer(fake.app()) as ds:
        state = served(str(ds.make_url("")))
        async with TestClient(TestServer(make_app(state))) as client:
            waiter = asyncio.create_task(state.wait_for("k1", "t2", timeout=5))
            for userdata in ("t1", "t2"):
                response = await client.post("/callback/k1", json=save(userdata, f"/cache/{userdata}"))
                assert await response.json() == {"error": 0}
            got = await waiter

            assert got is state.callbacks[1]
            assert got.userdata == "t2" and got.data == b"saved/cache/t2"
            # Already arrived: answered at once.
            assert await state.wait_for("k1", "t1", timeout=0.05) is state.callbacks[0]
            # Another document's turn of the same name is not this one's.
            assert await state.wait_for("k9", "t2", timeout=0.05) is None
    assert [callback.userdata for callback in state.callbacks] == ["t1", "t2"]
    assert not state._waiting


async def test_wait_for_waits_for_the_download() -> None:
    fake = FakeDocumentServer(slow_paths=("/cache/slow",), slow_s=0.5)
    async with TestServer(fake.app()) as ds:
        state = served(str(ds.make_url("")))
        async with TestClient(TestServer(make_app(state))) as client:
            waiter = asyncio.create_task(state.wait_for("k1", "t2", timeout=5))
            slow = asyncio.create_task(client.post("/callback/k1", json=save("t2", "/cache/slow")))
            await asyncio.sleep(0.1)
            # Another save settles while the slow one is still downloading.
            await client.post("/callback/k1", json=save("t1", "/cache/fast"))
            got = await waiter
            await slow
    # Arrival order: the slow one came first.
    assert [callback.userdata for callback in state.callbacks] == ["t2", "t1"]
    assert got.userdata == "t2" and got.data == b"saved/cache/slow"


async def test_wait_for_times_out_with_none_and_lets_go_of_its_waiter() -> None:
    state = served()
    assert await state.wait_for("k1", "t1", timeout=0.05) is None
    assert not state._waiting


async def test_a_checked_save_releases_its_bytes() -> None:
    fake = FakeDocumentServer()
    async with TestServer(fake.app()) as ds:
        state = served(str(ds.make_url("")))
        async with TestClient(TestServer(make_app(state))) as client:
            await client.post("/callback/k1", json=save("t1"))
    got = await state.wait_for("k1", "t1", timeout=1)
    assert got.data == b"saved/cache/x"
    got.release()
    assert state.callbacks[0].data is None


# Forcesave.

async def test_forcesave_signs_the_command_and_reads_its_error() -> None:
    fake = FakeDocumentServer(command_error=4)
    async with TestServer(fake.app()) as ds:
        result = await forcesave(settings(str(ds.make_url(""))), "k1", "t1")
    assert (result.error, result.outcome, result.http_status) == (4, "answered", 200)
    assert 0 <= result.seconds < 5
    [sent] = fake.commands
    assert {key: sent[key] for key in ("c", "key", "userdata")} == {"c": "forcesave", "key": "k1", "userdata": "t1"}
    assert jwt.decode(sent["token"], SECRET, algorithms=["HS256"]) == {"c": "forcesave", "key": "k1", "userdata": "t1"}


async def test_forcesave_timeout_is_none() -> None:
    fake = FakeDocumentServer(command_delay=11)
    async with TestServer(fake.app()) as ds:
        result = await forcesave(settings(str(ds.make_url(""))), "k1", "t1")
        fake.release.set()
    assert (result.error, result.outcome) == (None, "timeout")
    assert result.seconds >= 10


async def test_forcesave_refused_connection_is_unreachable() -> None:
    result = await forcesave(settings(refused_port()), "k1", "t1")
    assert (result.error, result.outcome, result.http_status) == (None, "unreachable", None)


async def test_forcesave_http_5xx_is_an_http_error() -> None:
    fake = FakeDocumentServer(command_status=503)
    async with TestServer(fake.app()) as ds:
        result = await forcesave(settings(str(ds.make_url(""))), "k1", "t1")
    assert (result.error, result.outcome, result.http_status) == (None, "http_error", 503)


@pytest.mark.parametrize("body", [b"<html>busy</html>", b"[0]", b'{"error": "x"}'])
async def test_forcesave_answer_that_is_not_an_error_code_is_a_bad_answer(body: bytes) -> None:
    fake = FakeDocumentServer(command_body=body)
    async with TestServer(fake.app()) as ds:
        result = await forcesave(settings(str(ds.make_url(""))), "k1", "t1")
    assert (result.error, result.outcome, result.http_status) == (None, "bad_answer", 200)


# Settings.

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
