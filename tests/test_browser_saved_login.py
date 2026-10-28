"""Which saved login a session's browser starts with, and saying so when it can't.

A bound profile that could not be loaded used to leave only a worker log
line: the run browsed logged out and acted as if it were signed in.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.browser.base import BrowserEndpoint, BrowserStatus
from surogates.browser.pool import BrowserPool
from surogates.tools.builtin.browser import _saved_login, _scrubbing

ORG = uuid4()
PERSON_SA = uuid4()
AGENT_SA = uuid4()
OTHER_SA = uuid4()
STATE = {"cookies": [{"name": "SID"}], "origins": []}


class _Store:
    """Profiles keyed by (id, owner service account); one may fail to decrypt."""

    def __init__(self, profiles, *, undecryptable=frozenset(), granted=None):
        self.profiles = profiles
        self.undecryptable = undecryptable
        self.granted = granted or {}
        self.touched = []

    async def storage_state_for(self, profile_id, org_id, *, user_id, service_account_id):
        if profile_id in self.undecryptable:
            raise ValueError("failed to decrypt browser profile state")
        return self.profiles.get((profile_id, service_account_id))

    async def touch_last_used(self, profile_id, org_id, *, user_id, service_account_id):
        self.touched.append((profile_id, service_account_id))

    async def used_in_channel(self, org_id, *, service_account_id, channel):
        return self.granted.get((service_account_id, channel))


def _tenant(sa=PERSON_SA):
    return SimpleNamespace(org_id=ORG, user_id=None, service_account_id=sa)


def _config(profile_id=None, agent_sa=AGENT_SA):
    config = {"agent_service_account_id": str(agent_sa)} if agent_sa else {}
    if profile_id:
        config["browser"] = {"profile_id": str(profile_id)}
    return config


async def test_a_bound_personal_profile_loads_for_its_owner():
    pid = uuid4()
    store = _Store({(pid, PERSON_SA): STATE})
    assert await _saved_login(store, tenant=_tenant(), session_config=_config(pid), channel="scheduled") == (
        str(pid), STATE, None,
    )
    assert store.touched == [(pid, PERSON_SA)]


async def test_a_bound_agent_profile_loads_in_any_session_of_the_agent():
    pid = uuid4()
    store = _Store({(pid, AGENT_SA): STATE})
    _, state, problem = await _saved_login(store, tenant=_tenant(), session_config=_config(pid), channel="studio")
    assert state == STATE and problem is None


async def test_someone_elses_personal_profile_never_loads_and_says_why():
    pid = uuid4()
    store = _Store({(pid, OTHER_SA): STATE})
    profile_id, state, problem = await _saved_login(
        store, tenant=_tenant(), session_config=_config(pid), channel="studio",
    )
    assert (profile_id, state) == (str(pid), None)
    assert "someone else" in problem


async def test_an_undecryptable_profile_says_it_must_be_saved_again():
    pid = uuid4()
    store = _Store({(pid, PERSON_SA): STATE}, undecryptable={pid})
    _, state, problem = await _saved_login(store, tenant=_tenant(), session_config=_config(pid), channel="studio")
    assert state is None and "saved again" in problem


async def test_an_unbound_session_uses_the_agent_profile_granted_to_its_channel():
    pid = uuid4()
    store = _Store({(pid, AGENT_SA): STATE}, granted={(AGENT_SA, "slack"): pid})
    assert await _saved_login(store, tenant=_tenant(AGENT_SA), session_config=_config(), channel="slack") == (
        str(pid), STATE, None,
    )
    assert await _saved_login(store, tenant=_tenant(AGENT_SA), session_config=_config(), channel="web") == (
        None, None, None,
    )


async def test_a_helper_browses_with_the_login_granted_to_its_parents_channel():
    from surogates.session.execution_context import inherit_context

    pid = uuid4()
    store = _Store({(pid, AGENT_SA): STATE}, granted={(AGENT_SA, "slack"): pid})
    helper = {**_config(), **inherit_context(_config(), "slack")}
    its_helper = {**_config(), **inherit_context(helper, "delegation")}
    for config in (helper, its_helper):
        assert await _saved_login(
            store, tenant=_tenant(AGENT_SA), session_config=config, channel="delegation",
        ) == (str(pid), STATE, None)


async def test_a_bound_profile_without_a_store_is_reported():
    pid = uuid4()
    _, state, problem = await _saved_login(None, tenant=_tenant(), session_config=_config(pid), channel="studio")
    assert state is None and "not configured" in problem


class _Backend:
    async def provision(self, spec, *, session_id, org_id, user_id):
        return "bid-1", BrowserEndpoint(rest_url="http://b:1", cdp_url="ws://b:2", live_view_url="ws://b:3")

    async def status(self, browser_id):
        return BrowserStatus.RUNNING


class _Registry:
    async def set(self, entry):
        pass

    async def get(self, session_id):
        return None


@pytest.mark.asyncio
async def test_a_lost_login_is_an_event_and_one_note_on_the_next_tool_result():
    events = []

    async def emit(session_id, event_type, data):
        events.append((event_type, data))

    pool = BrowserPool(backend=_Backend(), registry=_Registry(), event_emitter=emit)
    await pool.report_profile_problem("s1", "p1", "was not found")

    async def handler(arguments, **kwargs):
        return json.dumps({"ok": True})

    run = _scrubbing(handler)
    first = json.loads(await run({}, browser_pool=pool, session_id="s1"))
    second = json.loads(await run({}, browser_pool=pool, session_id="s1"))

    assert events == [("browser.profile_unavailable", {"session_id": "s1", "profile_id": "p1", "reason": "was not found"})]
    assert "started logged out" in first["browser_profile"]
    assert "browser_profile" not in second
