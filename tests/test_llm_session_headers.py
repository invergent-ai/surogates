"""``llm.session_headers``: every model call names its session and its tree's root.

The headers ride on the clients of the session's LLM bundle, so every code
path that calls the model through them sends them -- the main loop, summaries,
titles, the advisor, fallbacks -- and the request body is byte for byte the
same with and without them.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from openai import DefaultAsyncHttpxClient

from surogates import config
from surogates.harness import session_llm
from surogates.harness.session_llm import (
    ROOT_HEADER,
    SESSION_HEADER,
    build_session_llm_clients,
    session_request_headers,
)
from surogates.orchestrator import worker
from surogates.runtime.resolver import build_agent_runtime_context
from surogates.session.store import SessionNotFoundError


def _payload() -> dict:
    endpoint = {"model": "m", "base_url": "https://proxy.invalid/_model/m/v1", "api_key_ref": "vault://k"}
    return {
        "agent_id": "a", "org_id": "o", "project_id": "p", "enabled": True,
        "version": 1, "storage_key_prefix": "",
        "llm_main": endpoint,
        "llm_summary": {**endpoint, "model": "s"},
        "llm_vision": {**endpoint, "model": "v"},
        "llm_advisor": {**endpoint, "model": "pro"},
        "llm_image": {**endpoint, "model": "i"},
        "llm_fallbacks": [{**endpoint, "model": "f"}],
    }


class _Vault:
    async def resolve_ref(self, ref, **_scope):
        return "sk-test"


def _sse() -> bytes:
    chunk = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m",
             "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}
    return b"data: " + json.dumps(chunk).encode() + b"\n\ndata: [DONE]\n\n"


@pytest.fixture
def sent(monkeypatch) -> list[httpx.Request]:
    """Every request the bundle's clients send, answered by a fake provider."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if json.loads(request.content).get("stream"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_sse())
        return httpx.Response(200, json={
            "id": "c", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    monkeypatch.setattr(
        session_llm, "llm_http_client",
        lambda: DefaultAsyncHttpxClient(transport=httpx.MockTransport(handler)),
    )
    return requests


def _slots(bundle) -> list:
    return [bundle.main, bundle.summary, bundle.vision, bundle.advisor, bundle.image, *bundle.fallbacks]


REQUEST = {
    "model": "m",
    "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "Ünïcode and \"quotes\""}],
    "tools": [{"type": "function", "function": {"name": "todo", "parameters": {"type": "object", "properties": {}}}}],
}


async def _call_every_slot(bundle, stream: bool) -> None:
    for slot in _slots(bundle):
        response = await slot.client.chat.completions.create(**REQUEST, stream=stream)
        if stream:
            async for _ in response:
                pass


@pytest.mark.parametrize("stream", [False, True])
async def test_every_slot_names_the_session_and_sends_the_same_body(sent, stream):
    ctx = build_agent_runtime_context(_payload())
    session_id, root_id = uuid4(), uuid4()
    plain = await build_session_llm_clients(ctx, vault=_Vault())
    named = await build_session_llm_clients(
        ctx, vault=_Vault(), request_headers=session_request_headers(session_id, root_id),
    )
    try:
        await _call_every_slot(plain, stream)
        await _call_every_slot(named, stream)
    finally:
        await plain.aclose()
        await named.aclose()
    count = len(_slots(plain))
    assert count == 6
    without, with_ = sent[:count], sent[count:]
    for request in without:
        assert SESSION_HEADER not in request.headers
        assert ROOT_HEADER not in request.headers
    for request in with_:
        assert request.headers[SESSION_HEADER] == str(session_id)
        assert request.headers[ROOT_HEADER] == str(root_id)
    # The same call through the same slot: the same bytes on the wire.
    assert [r.content for r in without] == [r.content for r in with_]
    assert [r.url for r in without] == [r.url for r in with_]
    # Nothing else in the headers moved.
    for a, b in zip(without, with_):
        rest = {k: v for k, v in b.headers.items() if k not in (SESSION_HEADER, ROOT_HEADER)}
        assert rest == dict(a.headers.items())


async def test_the_image_slot_from_settings_names_the_session_too(sent):
    payload = _payload()
    del payload["llm_image"]
    ctx = build_agent_runtime_context(payload)
    settings = SimpleNamespace(llm=SimpleNamespace(
        image_model="img", image_api_key="k", api_key="", image_base_url="https://images.invalid/v1", base_url="",
    ))
    named = await build_session_llm_clients(
        ctx, vault=_Vault(), settings=settings, request_headers=session_request_headers("s", "r"),
    )
    try:
        assert named.image.model == "img"
        await named.image.client.chat.completions.create(**REQUEST)
    finally:
        await named.aclose()
    assert sent[-1].url.host == "images.invalid"
    assert sent[-1].headers[SESSION_HEADER] == "s"
    assert sent[-1].headers[ROOT_HEADER] == "r"


async def test_a_per_call_header_keeps_the_session_headers(sent):
    ctx = build_agent_runtime_context(_payload())
    named = await build_session_llm_clients(
        ctx, vault=_Vault(), request_headers=session_request_headers("s", "r"),
    )
    try:
        await named.main.client.chat.completions.create(**REQUEST, extra_headers={"x-other": "1"})
        await named.main.client.with_options(timeout=5).chat.completions.create(**REQUEST)
    finally:
        await named.aclose()
    for request in sent:
        assert request.headers[SESSION_HEADER] == "s"
        assert request.headers[ROOT_HEADER] == "r"
    assert sent[0].headers["x-other"] == "1"


class _Store:
    def __init__(self, *sessions):
        self.by_id = {s.id: s for s in sessions}
        self.reads = 0

    async def get_session(self, session_id):
        self.reads += 1
        if session_id not in self.by_id:
            raise SessionNotFoundError(str(session_id))
        return self.by_id[session_id]


def _session(parent=None):
    return SimpleNamespace(id=uuid4(), parent_id=parent.id if parent is not None else None)


async def test_a_root_is_its_own_root_without_a_read():
    root = _session()
    store = _Store(root)
    assert await worker._session_tree_root(store, root) == root.id
    assert store.reads == 0


async def test_children_and_grandchildren_name_the_tree_root():
    root = _session()
    child = _session(root)
    grandchild = _session(child)
    store = _Store(root, child, grandchild)
    assert await worker._session_tree_root(store, child) == root.id
    assert await worker._session_tree_root(store, grandchild) == root.id


async def test_a_parent_that_cannot_be_found_ends_the_walk_at_its_id():
    gone = _session()
    child = _session(gone)
    grandchild = _session(child)
    store = _Store(child, grandchild)
    assert await worker._session_tree_root(store, grandchild) == gone.id


async def test_a_parent_loop_ends_the_walk():
    a, b = _session(), _session()
    a.parent_id, b.parent_id = b.id, a.id
    store = _Store(a, b)
    assert await worker._session_tree_root(store, a) in (a.id, b.id)
    assert store.reads <= 2


async def test_a_chain_deeper_than_the_limit_ends_the_walk(monkeypatch):
    monkeypatch.setattr(worker, "_SESSION_TREE_MAX_DEPTH", 3)
    chain = [_session()]
    for _ in range(5):
        chain.append(_session(chain[-1]))
    store = _Store(*chain)
    assert await worker._session_tree_root(store, chain[-1]) == chain[-4].id
    assert store.reads == 3


async def test_the_headers_follow_the_setting():
    root = _session()
    child = _session(root)
    store = _Store(root, child)
    off = SimpleNamespace(llm=SimpleNamespace(session_headers=False))
    on = SimpleNamespace(llm=SimpleNamespace(session_headers=True))
    assert await worker._llm_request_headers(off, store, child) is None
    assert store.reads == 0
    assert await worker._llm_request_headers(on, store, child) == {
        SESSION_HEADER: str(child.id), ROOT_HEADER: str(root.id),
    }


def test_the_setting_is_off_unless_turned_on(monkeypatch):
    monkeypatch.delenv("SUROGATES_LLM_SESSION_HEADERS", raising=False)
    assert config.LLMSettings().session_headers is False
    monkeypatch.setenv("SUROGATES_LLM_SESSION_HEADERS", "true")
    assert config.LLMSettings().session_headers is True
