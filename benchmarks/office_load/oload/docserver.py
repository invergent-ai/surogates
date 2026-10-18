"""The harness's part toward the document server: input files, callbacks, saved files and forcesave.

The callback answers as the runtime's: ``{"error": 0}`` once a save is kept, and an HTTP error otherwise
(400, 403, 404, 502), after which the document server retries the callback.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Literal
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

ForceSaveOutcome = Literal["answered", "timeout", "unreachable", "http_error", "bad_answer"]


@dataclass(slots=True)
class Callback:
    """One callback attempt from the document server; a retry is another attempt."""

    doc_key: str
    status: int | None  # None when it could not be verified
    userdata: str | None  # the forcesave's userdata: the turn it answers
    url: str | None  # the saved file, as the document server names it
    at: float  # time.time() at arrival: for lining up with other timelines
    mono: float  # time.monotonic() at arrival: latencies are measured from this
    data: bytes | None = None  # the saved file, for status 2 and 6, until released
    download_s: float | None = None
    settled_mono: float | None = None  # time.monotonic() once its file was fetched or its failure noted
    reply_status: int = 200  # the HTTP status the tool answered with
    error: str | None = None  # why it was refused, or its file could not be fetched

    def release(self) -> None:
        """Drop the saved file's bytes, once checked."""
        self.data = None


@dataclass(frozen=True, slots=True)
class Document:
    kind: str
    data: bytes


@dataclass(frozen=True, slots=True)
class ForceSave:
    """The Command Service's answer to a forcesave.

    ``error`` is 0 when a save is coming and 4 when there was nothing unsaved; None whenever ``outcome`` is not
    ``answered``. ``http_status`` is the reply's, when one came.
    """

    error: int | None
    seconds: float
    outcome: ForceSaveOutcome
    http_status: int | None = None


class Unverifiable(Exception):
    """A callback the document server cannot be shown to have sent, about this document."""


class _Refused(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class CallbackState:
    """The documents the tool serves and the callbacks it received."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.documents: dict[str, Document] = {}
        self.callbacks: list[Callback] = []  # every attempt, in arrival order
        self._kept: dict[tuple[str, str], Callback] = {}  # the first attempt without an error
        self._failed: dict[tuple[str, str], Callback] = {}  # the last attempt with one
        self._waiting: dict[tuple[str, str], list[asyncio.Event]] = {}

    def add_document(self, doc_key: str, kind: str, data: bytes) -> None:
        """Serve *data* as *doc_key*'s input; a later call replaces it."""
        if kind not in KINDS:
            raise ValueError(f"the tool serves {', '.join(KINDS)}, not {kind!r}")
        self.documents[doc_key] = Document(kind, data)

    async def wait_for(self, doc_key: str, userdata: str, timeout: float) -> Callback | None:
        """The first verified callback for *doc_key* carrying *userdata* that the tool kept, its file fetched.

        A failed attempt is not it: the document server retries, and a later attempt may be kept. After *timeout*
        seconds, the last failed attempt, or None when none came.
        """
        key = (doc_key, userdata)
        kept = self._kept.get(key)
        if kept is not None:
            return kept
        arrived = asyncio.Event()
        self._waiting.setdefault(key, []).append(arrived)
        try:
            async with asyncio.timeout(timeout):
                await arrived.wait()
        except TimeoutError:
            return self._failed.get(key)
        finally:
            waiters = self._waiting.get(key)
            if waiters is not None and arrived in waiters:
                waiters.remove(arrived)
                if not waiters:
                    del self._waiting[key]
        return self._kept[key]

    def _settle(self, callback: Callback) -> None:
        """*callback* is complete: its file fetched, or its failure noted."""
        callback.settled_mono = time.monotonic()
        if callback.userdata is None:
            return  # unverified, or carries no turn: nothing waits for it
        key = (callback.doc_key, callback.userdata)
        if callback.error is not None:
            self._failed[key] = callback
            return
        if key in self._kept:
            return
        self._kept[key] = callback
        for arrived in self._waiting.pop(key, []):
            arrived.set()


_STATE = web.AppKey("state", CallbackState)


def make_app(state: CallbackState) -> web.Application:
    """The routes the document server calls: the input files and the callbacks."""
    app = web.Application()
    app[_STATE] = state
    app.router.add_get("/files/{doc_key}", _file)
    app.router.add_post("/callback/{doc_key}", _callback)
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
        raise Unverifiable("The callback is not a JSON object.")
    token = body.get("token")
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ")
    if not token:
        raise Unverifiable("The callback is not signed by the document server.")
    try:
        claims = jwt.decode(str(token), secret, algorithms=[ALGORITHM], leeway=TOKEN_LEEWAY_S)
    except jwt.InvalidTokenError as exc:
        raise Unverifiable(f"The callback is not signed by the document server: {exc}") from None
    payload = claims.get("payload")
    payload = payload if isinstance(payload, dict) else claims
    # The signature proves the document server sent it, not which document it is about.
    if payload.get("key") != doc_key:
        raise Unverifiable(f"The callback is for another document: {payload.get('key')!r}")
    return payload


async def _file(request: web.Request) -> web.Response:
    document = request.app[_STATE].documents.get(request.match_info["doc_key"])
    if document is None:
        raise web.HTTPNotFound(text="No such document.")
    return web.Response(body=document.data, content_type="application/octet-stream")


async def _callback(request: web.Request) -> web.Response:
    state = request.app[_STATE]
    callback = Callback(request.match_info["doc_key"], None, None, None, at=time.time(), mono=time.monotonic())
    state.callbacks.append(callback)
    try:
        await _take(request, state, callback)
    except _Refused as refused:
        callback.reply_status, callback.error = refused.status, refused.detail
    except BaseException:
        callback.reply_status, callback.error = 500, "The callback was interrupted."
        raise
    finally:
        state._settle(callback)
    if callback.error is not None:
        return web.json_response({"detail": callback.error}, status=callback.reply_status)
    return web.json_response({"error": 0})


async def _take(request: web.Request, state: CallbackState, callback: Callback) -> None:
    """Verify *callback* and, for a save, fetch its file; refused as the runtime's route refuses."""
    if callback.doc_key not in state.documents:
        raise _Refused(404, "The document is no longer open.")
    try:
        body = await request.json()
    except ValueError:
        raise _Refused(400, "The callback is not JSON.") from None
    try:
        payload = read_signed_body(
            state.settings.jwt_secret, body, request.headers.get("Authorization"), callback.doc_key)
    except Unverifiable as exc:
        raise _Refused(403, str(exc)) from None
    userdata, url = payload.get("userdata"), payload.get("url")
    callback.userdata = None if userdata is None else str(userdata)
    callback.url = None if url is None else str(url)
    try:
        callback.status = int(payload.get("status", 0))
    except (TypeError, ValueError):
        raise _Refused(500, f"The callback's status is not a number: {payload.get('status')!r}") from None
    if callback.status not in SAVE_STATUSES:
        return
    if not callback.url:
        raise _Refused(400, "The save names no file.")
    started = time.monotonic()
    try:
        callback.data = await _download(internal_url(callback.url, state.settings.command_url))
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        raise _Refused(502, f"Could not download the saved document: {exc!r}") from None
    finally:
        callback.download_s = time.monotonic() - started


async def _download(url: str) -> bytes:
    # A client of its own per download, as the harness has: no save waits for another's connection.
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT_S)) as http:
        # The document server's URL is already encoded: sent as it is. The runtime's client follows no redirect and
        # refuses a 3xx as it refuses an error.
        async with http.get(URL(url, encoded=True), allow_redirects=False) as response:
            response.raise_for_status()
            if response.status >= 300:
                raise aiohttp.ClientResponseError(
                    response.request_info, response.history, status=response.status, message=response.reason or "",
                    headers=response.headers)
            return await response.read()


async def forcesave(settings: Settings, doc_key: str, userdata: str) -> ForceSave:
    """Ask the document server to save *doc_key* now, its save callback carrying *userdata*, as the harness does.

    Each command gets a client of its own, as the harness's does, so none waits for another's connection.
    """
    command = {"c": "forcesave", "key": doc_key, "userdata": userdata}
    started = time.monotonic()
    http_status: int | None = None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=FORCESAVE_TIMEOUT_S)) as http:
            async with http.post(
                f"{settings.command_url.rstrip('/')}/command",
                json={**command, "token": sign(settings.jwt_secret, command)},
                allow_redirects=False,
            ) as response:
                http_status = response.status
                body = await response.read()
    # aiohttp's timeout errors are client errors too: the timeout is caught first.
    except TimeoutError:
        return ForceSave(None, time.monotonic() - started, "timeout", http_status)
    except aiohttp.ClientError:
        return ForceSave(None, time.monotonic() - started, "unreachable", http_status)
    seconds = time.monotonic() - started
    if not 200 <= http_status < 300:
        return ForceSave(None, seconds, "http_error", http_status)
    try:
        answer = json.loads(body)
        error = int(answer.get("error", -1)) if isinstance(answer, dict) else None
    except (TypeError, ValueError):
        error = None
    if error is None:
        return ForceSave(None, seconds, "bad_answer", http_status)
    return ForceSave(error, seconds, "answered", http_status)
