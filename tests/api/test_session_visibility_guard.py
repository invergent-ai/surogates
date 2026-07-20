"""``require_session_visible`` — the shared guard that makes hidden
multi-era web sessions unreachable on every session-scoped read surface
(events stream, workspace, artifacts, …), not just the sessions CRUD."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from surogates.api.session_guards import (
    is_multi_era_web_session,
    require_session_visible,
)

pytestmark = pytest.mark.asyncio


class _Cache:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error

    async def get(self, agent_id):
        if self.error is not None:
            raise self.error
        return self.payload


def _session(channel="web", *, parent_id=None, config=None):
    return SimpleNamespace(
        id=uuid4(),
        agent_id="support-bot",
        channel=channel,
        parent_id=parent_id,
        config=config or {},
    )


def _request(cache):
    state = SimpleNamespace()
    if cache is not None:
        state.runtime_config_cache = cache
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_multi_era_predicate():
    assert is_multi_era_web_session(_session("web"))
    assert not is_multi_era_web_session(
        _session("web", config={"single_session": True})
    )
    assert not is_multi_era_web_session(_session("web", parent_id=uuid4()))
    assert not is_multi_era_web_session(_session("api"))
    assert not is_multi_era_web_session(_session("website"))


async def test_hides_multi_era_session_when_capability_off():
    request = _request(_Cache(payload={"multi_session": False}))
    with pytest.raises(HTTPException) as exc:
        await require_session_visible(request, _session("web"))
    assert exc.value.status_code == 404


async def test_allows_canonical_child_and_other_channels():
    request = _request(_Cache(payload={"multi_session": False}))
    for session in (
        _session("web", config={"single_session": True}),
        _session("web", parent_id=uuid4()),
        _session("api"),
    ):
        await require_session_visible(request, session)


async def test_allows_when_capability_on_or_absent():
    for payload in ({"multi_session": True}, {}):
        request = _request(_Cache(payload=payload))
        await require_session_visible(request, _session("web"))


async def test_fails_open_without_cache_or_on_resolution_error():
    await require_session_visible(_request(None), _session("web"))
    request = _request(_Cache(error=LookupError("agent gone")))
    await require_session_visible(request, _session("web"))
