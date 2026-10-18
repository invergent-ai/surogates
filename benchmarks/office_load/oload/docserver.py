"""The harness's part toward the document server: input files, callbacks, saved files and forcesave."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp
import jwt
from aiohttp import web
from yarl import URL

from oload.editor import ALGORITHM, KINDS, sign
from oload.settings import Settings

FORCESAVE_TIMEOUT_S = 10.0  # the harness gives up on /command at 10 s and never retries
DOWNLOAD_TIMEOUT_S = 60.0  # as the harness downloads a save
SAVE_STATUSES = (2, 6)  # 2: saved after the last editor left; 6: forcesaved
# This host's clock and the cluster's may differ a little; a token is not refused for it.
TOKEN_LEEWAY_S = 60


@dataclass(slots=True)
class Callback:
    """One callback from the document server."""

    doc_key: str
    status: int | None  # None when it could not be verified
    userdata: str | None  # the forcesave's userdata: the turn it answers
    url: str | None  # the saved file, as the document server names it
    at: float  # time.time() when it arrived
    data: bytes | None = None  # the saved file, for status 2 and 6
    download_s: float | None = None
    error: str | None = None  # why it was refused, or its file could not be fetched


@dataclass(frozen=True, slots=True)
class Document:
    kind: str
    data: bytes


@dataclass(frozen=True, slots=True)
class ForceSave:
    """The Command Service's answer to a forcesave.

    ``error`` is 0 when a save is coming and 4 when there was nothing unsaved; None when it did not answer
    within the timeout, or could not be reached.
    """

    error: int | None
    seconds: float


class Unverifiable(Exception):
    """A callback the document server cannot be shown to have sent, about this document."""


class CallbackState:
    """The documents the tool serves and the callbacks it received."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.documents: dict[str, Document] = {}
        self.callbacks: list[Callback] = []  # all, in arrival order
        self._settled: dict[tuple[str, str | None], Callback] = {}
        self._waiting: dict[tuple[str, str | None], asyncio.Event] = {}

    def add_document(self, doc_key: str, kind: str, data: bytes) -> None:
        """Serve *data* as *doc_key*'s input; a later call replaces it."""
        if kind not in KINDS:
            raise ValueError(f"the tool serves {', '.join(KINDS)}, not {kind!r}")
        self.documents[doc_key] = Document(kind, data)

    async def wait_for(self, doc_key: str, userdata: str, timeout: float) -> Callback | None:
        """The first verified callback for *doc_key* carrying *userdata*, once its file is fetched.

        None when none came within *timeout* seconds.
        """
        key = (doc_key, userdata)
        found = self._settled.get(key)
        if found is not None:
            return found
        arrived = self._waiting.setdefault(key, asyncio.Event())
        try:
            async with asyncio.timeout(timeout):
                await arrived.wait()
        except TimeoutError:
            return None
        return self._settled.get(key)

    def _arrive(self, callback: Callback) -> None:
        self.callbacks.append(callback)

    def _settle(self, callback: Callback) -> None:
        """*callback* is complete: its file fetched, or its failure noted."""
        key = (callback.doc_key, callback.userdata)
        self._settled.setdefault(key, callback)
        arrived = self._waiting.pop(key, None)
        if arrived is not None:
            arrived.set()


_STATE = web.AppKey("state", CallbackState)
_HTTP = web.AppKey("http", aiohttp.ClientSession)


def make_app(state: CallbackState) -> web.Application:
    """The routes the document server calls: the input files and the callbacks."""
    app = web.Application()
    app[_STATE] = state
    app.router.add_get("/files/{doc_key}", _file)
    app.router.add_post("/callback/{doc_key}", _callback)
    app.cleanup_ctx.append(_downloads)
    return app


def file_url(settings: Settings, doc_key: str) -> str:
    return f"{settings.public_base.rstrip('/')}/files/{quote(doc_key, safe='')}"


def callback_url(settings: Settings, doc_key: str) -> str:
    return f"{settings.public_base.rstrip('/')}/callback/{quote(doc_key, safe='')}"


def internal_url(url: str, command_url: str) -> str:
    """The document server's URL for a saved file, rewritten to how this host reaches it."""
    target, source = urlsplit(command_url), urlsplit(url)
    return urlunsplit((target.scheme, target.netloc, source.path, source.query, source.fragment))


def read_signed_body(secret: str, body: Any, authorization: str | None, doc_key: str) -> dict[str, Any]:
    """The callback the document server signed, about *doc_key*.

    The token is in the body's ``token``, or in the Authorization header with the body wrapped in ``payload``.
    """
    if not isinstance(body, dict):
        raise Unverifiable("the callback is not a JSON object")
    token = body.get("token")
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ")
    if not token:
        raise Unverifiable("the callback is not signed")
    try:
        claims = jwt.decode(str(token), secret, algorithms=[ALGORITHM], leeway=TOKEN_LEEWAY_S)
    except jwt.InvalidTokenError as exc:
        raise Unverifiable(f"the callback's token is not valid: {exc}") from None
    payload = claims.get("payload")
    payload = payload if isinstance(payload, dict) else claims
    # The signature proves the document server sent it, not which document it is about.
    if payload.get("key") != doc_key:
        raise Unverifiable(f"the callback is for another document: {payload.get('key')!r}")
    return payload


async def _downloads(app: web.Application) -> AsyncIterator[None]:
    # No connection limit: the tool must not queue the saves it measures.
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=0), timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT_S),
    ) as http:
        app[_HTTP] = http
        yield


async def _file(request: web.Request) -> web.Response:
    document = request.app[_STATE].documents.get(request.match_info["doc_key"])
    if document is None:
        raise web.HTTPNotFound(text="No such document.")
    return web.Response(body=document.data, content_type="application/octet-stream")


async def _callback(request: web.Request) -> web.Response:
    state = request.app[_STATE]
    doc_key = request.match_info["doc_key"]
    arrived = time.time()
    try:
        body = await request.json()
    except ValueError:
        body = None
    try:
        payload = read_signed_body(state.settings.jwt_secret, body, request.headers.get("Authorization"), doc_key)
        try:
            status = int(payload["status"])
        except (KeyError, TypeError, ValueError):
            raise Unverifiable(f"the callback has no status: {payload.get('status')!r}") from None
    except Unverifiable as exc:
        # Recorded, never matched to a turn: what it says cannot be trusted.
        state._arrive(Callback(doc_key, None, None, None, arrived, error=str(exc)))
        return web.json_response({"error": 1})

    userdata, url = payload.get("userdata"), payload.get("url")
    callback = Callback(
        doc_key, status, None if userdata is None else str(userdata), None if url is None else str(url), arrived,
    )
    state._arrive(callback)
    try:
        if status in SAVE_STATUSES:
            if not callback.url:
                callback.error = "the save names no file"
            else:
                started = time.monotonic()
                try:
                    saved = internal_url(callback.url, state.settings.command_url)
                    callback.data = await _download(request.app[_HTTP], saved)
                except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
                    callback.error = f"could not download the save: {exc!r}"
                callback.download_s = time.monotonic() - started
    except BaseException:
        callback.error = callback.error or "the callback was interrupted"
        raise
    finally:
        state._settle(callback)
    return web.json_response({"error": 1 if callback.error else 0})


async def _download(http: aiohttp.ClientSession, url: str) -> bytes:
    # The document server's URL is already encoded: sent as it is.
    async with http.get(URL(url, encoded=True)) as response:
        response.raise_for_status()
        return await response.read()


async def forcesave(session: aiohttp.ClientSession, settings: Settings, doc_key: str, userdata: str) -> ForceSave:
    """Ask the document server to save *doc_key* now, its save callback carrying *userdata*, as the harness does."""
    command = {"c": "forcesave", "key": doc_key, "userdata": userdata}
    started = time.monotonic()
    try:
        async with session.post(
            f"{settings.command_url.rstrip('/')}/command",
            json={**command, "token": sign(settings.jwt_secret, command)},
            timeout=aiohttp.ClientTimeout(total=FORCESAVE_TIMEOUT_S),
        ) as response:
            response.raise_for_status()
            answer = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return ForceSave(None, time.monotonic() - started)
    seconds = time.monotonic() - started
    if not isinstance(answer, dict):
        return ForceSave(None, seconds)
    try:
        return ForceSave(int(answer.get("error", -1)), seconds)
    except (TypeError, ValueError):
        return ForceSave(None, seconds)
