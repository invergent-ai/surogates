"""Tests for the boot catch-up watermark + replay."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from surogates.channels.channel_catchup import (
    _watermark_from,
    latest_catchup_watermark,
)


class TestWatermarkFrom:
    def test_prefers_exact_ts(self):
        created = datetime(2024, 4, 5, 12, 0, 0, tzinfo=timezone.utc)
        assert _watermark_from("1712345678.123456", created) == "1712345678.123456"

    def test_falls_back_to_created_at(self):
        created = datetime(2024, 4, 5, 12, 0, 0, 500000, tzinfo=timezone.utc)
        assert _watermark_from(None, created) == f"{created.timestamp():.6f}"

    def test_none_when_no_events(self):
        assert _watermark_from(None, None) is None


# --- watermark query (mocked db) ---------------------------------------------


class _FakeResult:
    def __init__(self, value: str | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> str | None:
        return self._value


class _FakeDB:
    def __init__(self, value: str | None) -> None:
        self._value = value
        self.executed = False
        self.params: dict[str, Any] | None = None

    async def __aenter__(self) -> "_FakeDB":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def execute(self, _stmt: Any, _params: dict[str, Any] | None = None) -> _FakeResult:
        self.executed = True
        self.params = _params
        return _FakeResult(self._value)


def _sf(value: str | None):
    db = _FakeDB(value)

    def factory() -> _FakeDB:
        return db

    factory.db = db  # type: ignore[attr-defined]
    return factory


class TestLatestCatchupWatermark:
    async def test_returns_exact_ts(self):
        wm = await latest_catchup_watermark(
            _sf("1712345678.123456"),
            org_id="org-1", agent_id="agent-1", api_app_id="A1", chat_id="C1",
        )
        assert wm == "1712345678.123456"

    async def test_falls_back_to_created_at(self):
        created = datetime(2024, 4, 5, 12, 0, 0, tzinfo=timezone.utc)
        wm = await latest_catchup_watermark(
            _sf(f"{created.timestamp():.6f}"),
            org_id="org-1", agent_id="agent-1", api_app_id="A1", chat_id="C1",
        )
        assert wm == f"{created.timestamp():.6f}"

    async def test_none_when_never_seen(self):
        wm = await latest_catchup_watermark(
            _sf(None),
            org_id="org-1", agent_id="agent-1", api_app_id="A1", chat_id="C1",
        )
        assert wm is None

    async def test_forwards_scoping_params(self):
        from surogates.session.events import EventType

        sf = _sf("1712345678.123456")
        await latest_catchup_watermark(
            sf, org_id="org-1", agent_id="agent-1", api_app_id="A1", chat_id="C1",
        )
        params = sf.db.params  # type: ignore[attr-defined]
        assert params["org_id"] == "org-1"
        assert params["agent_id"] == "agent-1"
        assert params["api_app_id"] == "A1"
        assert params["chat_id"] == "C1"
        assert params["event_type"] == EventType.USER_MESSAGE.value


# --- ChannelCatchup replay tests ---------------------------------------------

from dataclasses import dataclass

from surogates.channels.channel_backfill import BackfillLimits
from surogates.channels.channel_catchup import ChannelCatchup


@dataclass
class _Msg:
    """Stand-in InboundMessage (only the fields the catch-up reads/passes)."""
    identifier: str
    ts: str
    text: str = "hi"
    is_bot: bool = False


class _FakeSlackClient:
    """Records conversations_list / conversations_history; returns canned data."""

    def __init__(self, conversations: list[dict], history: dict[str, list[dict]]) -> None:
        self._conversations = conversations
        self._history = history  # channel_id -> newest-first messages
        self.history_calls: list[dict] = []

    async def conversations_list(self, **kwargs: Any) -> dict:
        return {"channels": self._conversations, "response_metadata": {"next_cursor": ""}}

    async def conversations_history(self, **kwargs: Any) -> dict:
        self.history_calls.append(kwargs)
        return {"messages": list(self._history.get(kwargs["channel"], [])),
                "response_metadata": {"next_cursor": ""}}


class _FakePlatform:
    kind = "slack"

    def __init__(self, client: _FakeSlackClient) -> None:
        self._client = client
        from surogates.channels.registry import ChannelDescriptor
        self.descriptor = ChannelDescriptor(
            vault_refs=lambda ident: {"bot_token": "bot_token"},
            config_keys=("bot_token",),
            webhook_registration="manual",
        )

    def _get_client(self, bot_token: str) -> _FakeSlackClient:
        return self._client

    async def parse(self, body: Any, *, creds: dict | None = None, identifier: str | None = None):
        if body.get("type") != "event_callback":
            return None
        ev = body["event"]
        if ev.get("user") == "UBOT" or ev.get("bot_id") or ev.get("subtype"):
            return None
        return _Msg(identifier=ev["channel"], ts=ev["ts"], text=ev.get("text", ""))

    async def enrich(self, msg: Any, *, creds: dict) -> Any:
        return msg


class _FakePipeline:
    def __init__(self) -> None:
        self.handled: list[Any] = []

    async def handle(self, msg: Any, *, routing: Any, config: dict, deps: Any):
        self.handled.append(msg)


class _FakeVault:
    async def resolve_ref(self, ref: str, *, org_id: str) -> str | None:
        return "xoxb-token"


class _FakePlatformClient:
    def __init__(self, routings: list[dict]) -> None:
        self._routings = routings

    async def list_channel_routings(self, kind: str) -> list[dict]:
        return list(self._routings)


def _wm(watermarks: dict[str, str | None]):
    """An async stand-in for ChannelCatchup._watermark (which is awaited)."""
    async def _w(**kw: Any) -> str | None:
        return watermarks.get(kw["chat_id"])
    return _w


class _AcquiredLock:
    async def acquire(self) -> bool:
        return True

    async def heartbeat(self) -> bool:
        return True

    async def release(self) -> None:
        return None


def _catchup(*, client, pipeline, routings, watermarks, limits=None):
    platform = _FakePlatform(client)
    cu = ChannelCatchup(
        redis=None,
        session_factory=_sf(None),             # bypassed by the _watermark injection
        vault=_FakeVault(),
        platform_client=_FakePlatformClient(routings),
        registry=None,
        pipeline=pipeline,
        deps_factory=lambda kind, routing, creds, plat: object(),
        settings=None,
        limits=limits or BackfillLimits(),
        pace_s=0.0,
        lock=None,
    )
    cu._resolve_platform = lambda app: platform        # inject the fake platform (sync)
    cu._watermark = _wm(watermarks)                    # inject watermarks (async)
    cu._make_lock = lambda *a, **k: _AcquiredLock()
    return cu


_ROUTINGS = [{"org_id": "org-1", "agent_id": "agent-1",
              "channel_identifier": "A1", "config": {}}]


class TestChannelCatchupReplay:
    async def test_replays_only_messages_newer_than_watermark(self):
        client = _FakeSlackClient(
            conversations=[{"id": "C1", "is_member": True}],
            history={"C1": [
                {"user": "U1", "text": "new",  "ts": "1712345680.000000"},  # > wm → replay
                {"user": "U1", "text": "seen", "ts": "1712345670.000000"},  # <= wm → skip
            ]},
        )
        pipeline = _FakePipeline()
        cu = _catchup(client=client, pipeline=pipeline, routings=_ROUTINGS,
                      watermarks={"C1": "1712345675.000000"})
        await cu.run()
        assert [m.ts for m in pipeline.handled] == ["1712345680.000000"]

    async def test_first_run_conversation_is_skipped(self):
        client = _FakeSlackClient(
            conversations=[{"id": "C2", "is_member": True}],
            history={"C2": [{"user": "U1", "text": "hi", "ts": "1712345680.000000"}]},
        )
        pipeline = _FakePipeline()
        cu = _catchup(client=client, pipeline=pipeline, routings=_ROUTINGS,
                      watermarks={"C2": None})  # never seen
        await cu.run()
        assert pipeline.handled == []
        assert client.history_calls == []  # no fetch for a first-run conversation

    async def test_bot_and_subtype_messages_are_skipped(self):
        client = _FakeSlackClient(
            conversations=[{"id": "C1", "is_member": True}],
            history={"C1": [
                {"user": "UBOT", "text": "mine", "ts": "1712345681.000000"},
                {"bot_id": "B1", "text": "bot",  "ts": "1712345682.000000"},
                {"user": "U1", "subtype": "channel_join", "ts": "1712345683.000000"},
                {"user": "U1", "text": "real", "ts": "1712345684.000000"},
            ]},
        )
        pipeline = _FakePipeline()
        cu = _catchup(client=client, pipeline=pipeline, routings=_ROUTINGS,
                      watermarks={"C1": "1712345680.000000"})
        await cu.run()
        assert [m.ts for m in pipeline.handled] == ["1712345684.000000"]

    async def test_replays_oldest_first(self):
        client = _FakeSlackClient(
            conversations=[{"id": "C1", "is_member": True}],
            history={"C1": [  # Slack returns newest-first
                {"user": "U1", "text": "b", "ts": "1712345686.000000"},
                {"user": "U1", "text": "a", "ts": "1712345685.000000"},
            ]},
        )
        pipeline = _FakePipeline()
        cu = _catchup(client=client, pipeline=pipeline, routings=_ROUTINGS,
                      watermarks={"C1": "1712345680.000000"})
        await cu.run()
        assert [m.ts for m in pipeline.handled] == ["1712345685.000000", "1712345686.000000"]

    async def test_respects_message_cap(self):
        client = _FakeSlackClient(
            conversations=[{"id": "C1", "is_member": True}],
            history={"C1": [
                {"user": "U1", "text": "c", "ts": "1712345687.000000"},
                {"user": "U1", "text": "b", "ts": "1712345686.000000"},
                {"user": "U1", "text": "a", "ts": "1712345685.000000"},
            ]},
        )
        pipeline = _FakePipeline()
        cu = _catchup(
            client=client,
            pipeline=pipeline,
            routings=_ROUTINGS,
            watermarks={"C1": "1712345680.000000"},
            limits=BackfillLimits(max_messages=2),
        )
        await cu.run()
        assert len(pipeline.handled) == 2

    async def test_private_channel_replays_with_group_channel_type(self):
        class _InspectingPlatform(_FakePlatform):
            def __init__(self, client: _FakeSlackClient) -> None:
                super().__init__(client)
                self.channel_types: list[str] = []

            async def parse(self, body: Any, *, creds: dict | None = None, identifier: str | None = None):
                self.channel_types.append(body["event"]["channel_type"])
                return await super().parse(body, creds=creds, identifier=identifier)

        client = _FakeSlackClient(
            conversations=[{"id": "G1", "is_member": True, "is_private": True}],
            history={"G1": [{"user": "U1", "text": "private", "ts": "1712345688.000000"}]},
        )
        platform = _InspectingPlatform(client)
        pipeline = _FakePipeline()
        cu = _catchup(client=client, pipeline=pipeline, routings=_ROUTINGS,
                      watermarks={"G1": "1712345680.000000"})
        cu._resolve_platform = lambda app: platform
        await cu.run()
        assert platform.channel_types == ["group"]

    async def test_per_conversation_isolation(self):
        class _Boom(_FakeSlackClient):
            async def conversations_history(self, **kwargs):
                if kwargs["channel"] == "C1":
                    raise RuntimeError("slack 500")
                return await super().conversations_history(**kwargs)

        client = _Boom(
            conversations=[{"id": "C1", "is_member": True}, {"id": "C2", "is_member": True}],
            history={"C2": [{"user": "U1", "text": "ok", "ts": "1712345690.000000"}]},
        )
        pipeline = _FakePipeline()
        cu = _catchup(client=client, pipeline=pipeline, routings=_ROUTINGS,
                      watermarks={"C1": "1712345680.000000", "C2": "1712345680.000000"})
        await cu.run()  # C1 raises, C2 still processed
        assert [m.ts for m in pipeline.handled] == ["1712345690.000000"]

    async def test_fetch_oldest_clamped_to_age_window(self):
        import surogates.channels.channel_catchup as cc
        from unittest import mock

        client = _FakeSlackClient(
            conversations=[{"id": "C1", "is_member": True}],
            history={"C1": [{"user": "U1", "text": "x", "ts": "1712345690.000000"}]},
        )
        cu = _catchup(client=client, pipeline=_FakePipeline(), routings=_ROUTINGS,
                      watermarks={"C1": "1000000000.000000"})  # far older than 7d
        fixed_now = 1712345700.0
        with mock.patch.object(cc, "_now", lambda: fixed_now):
            await cu.run()
        # watermark is far older than the 7-day window → oldest clamps to now-7d
        cutoff = f"{(fixed_now - 7 * 86400.0):.6f}"
        assert client.history_calls[0]["oldest"] == cutoff

    async def test_fetch_oldest_is_watermark_when_recent(self):
        import surogates.channels.channel_catchup as cc
        from unittest import mock

        client = _FakeSlackClient(
            conversations=[{"id": "C1", "is_member": True}],
            history={"C1": []},
        )
        cu = _catchup(client=client, pipeline=_FakePipeline(), routings=_ROUTINGS,
                      watermarks={"C1": "1712345695.000000"})  # within 7d of fixed_now
        fixed_now = 1712345700.0
        with mock.patch.object(cc, "_now", lambda: fixed_now):
            await cu.run()
        assert client.history_calls[0]["oldest"] == "1712345695.000000"


class _HeldLock:
    """A lock that is already held by someone else — acquire returns False."""

    async def acquire(self) -> bool:
        return False

    async def heartbeat(self) -> bool:
        return True

    async def release(self) -> None:
        return None


class _HeartbeatFailsLock:
    """acquire() succeeds, heartbeat() fails on first call — exercises the
    mid-loop abort + release-in-finally path."""

    def __init__(self) -> None:
        self.released = False

    async def acquire(self) -> bool:
        return True

    async def heartbeat(self) -> bool:
        return False

    async def release(self) -> None:
        self.released = True


class TestChannelCatchupLock:
    async def test_app_skipped_when_lock_not_acquired(self):
        client = _FakeSlackClient(
            conversations=[{"id": "C1", "is_member": True}],
            history={"C1": [{"user": "U1", "text": "hi", "ts": "1712345690.000000"}]},
        )
        pipeline = _FakePipeline()
        cu = _catchup(client=client, pipeline=pipeline, routings=_ROUTINGS,
                      watermarks={"C1": "1712345680.000000"})
        cu._lock = _HeldLock()                # lock held elsewhere
        cu._make_lock = lambda *a, **k: cu._lock
        await cu.run()
        assert pipeline.handled == []         # app skipped, nothing replayed
        assert client.history_calls == []

    async def test_heartbeat_abort_stops_loop_and_releases_lock(self):
        # Two conversations, both with a replayable message newer than their watermark.
        # The heartbeat fails after the first conversation is processed, so only C1
        # should be fetched/replayed and the lock must still be released via finally.
        lock = _HeartbeatFailsLock()
        client = _FakeSlackClient(
            conversations=[
                {"id": "C1", "is_member": True},
                {"id": "C2", "is_member": True},
            ],
            history={
                "C1": [{"user": "U1", "text": "first",  "ts": "1712345691.000000"}],
                "C2": [{"user": "U2", "text": "second", "ts": "1712345692.000000"}],
            },
        )
        pipeline = _FakePipeline()
        cu = _catchup(
            client=client,
            pipeline=pipeline,
            routings=_ROUTINGS,
            watermarks={"C1": "1712345680.000000", "C2": "1712345680.000000"},
        )
        cu._make_lock = lambda *a, **k: lock
        await cu.run()

        # The lock is lost before the first conversation (top-of-loop heartbeat),
        # so nothing is fetched or replayed and the lock is still released.
        assert client.history_calls == []
        assert pipeline.handled == []
        assert lock.released is True

    async def test_heartbeat_lost_mid_conversation_stops_and_releases(self):
        # A lock that succeeds once (first top-of-loop heartbeat for C1) then fails
        # (the per-message heartbeat inside _catchup_conversation).
        class _MidConvLock:
            def __init__(self) -> None:
                self.released = False
                self._calls = 0

            async def acquire(self) -> bool:
                return True

            async def heartbeat(self) -> bool:
                self._calls += 1
                return self._calls <= 1  # True on 1st call, False on 2nd

            async def release(self) -> None:
                self.released = True

        lock = _MidConvLock()
        client = _FakeSlackClient(
            conversations=[{"id": "C1", "is_member": True}],
            history={"C1": [
                {"user": "U1", "text": "first",  "ts": "1712345691.000000"},
                {"user": "U1", "text": "second", "ts": "1712345692.000000"},
            ]},
        )
        pipeline = _FakePipeline()
        cu = _catchup(
            client=client,
            pipeline=pipeline,
            routings=_ROUTINGS,
            watermarks={"C1": "1712345680.000000"},
        )
        cu._make_lock = lambda *a, **k: lock
        await cu.run()

        # Exactly one message replayed before per-message heartbeat failed.
        assert len(pipeline.handled) == 1
        assert lock.released is True

    async def test_lock_key_scoped_per_routing(self):
        # Two routings sharing the same channel_identifier but different agent_id.
        # _make_lock must be called with distinct (org, agent, app) tuples.
        routings = [
            {"org_id": "org-1", "agent_id": "agent-1", "channel_identifier": "A1", "config": {}},
            {"org_id": "org-1", "agent_id": "agent-2", "channel_identifier": "A1", "config": {}},
        ]
        client = _FakeSlackClient(
            conversations=[{"id": "C1", "is_member": True}],
            history={"C1": [{"user": "U1", "text": "hi", "ts": "1712345691.000000"}]},
        )
        pipeline = _FakePipeline()
        seen: list[tuple] = []
        cu = _catchup(client=client, pipeline=pipeline, routings=routings,
                      watermarks={"C1": "1712345680.000000"})
        cu._make_lock = lambda *a, **k: (seen.append(a), _AcquiredLock())[1]
        await cu.run()

        # Must have been called twice with distinct (org, agent, app) triples.
        assert len(seen) == 2
        assert seen[0] != seen[1]
        # Each call carries the same app_id but different agent_ids.
        org_0, agent_0, app_0 = seen[0]
        org_1, agent_1, app_1 = seen[1]
        assert app_0 == "A1" and app_1 == "A1"
        assert agent_0 != agent_1

    async def test_per_routing_history_backfill_overrides_limits(self):
        # Routing declares max_messages=1 via history_backfill config;
        # the _catchup instance default is 200.  Only one message must replay.
        routings = [{"org_id": "org-1", "agent_id": "agent-1",
                     "channel_identifier": "A1",
                     "config": {"history_backfill": {"max_messages": 1}}}]
        client = _FakeSlackClient(
            conversations=[{"id": "C1", "is_member": True}],
            history={"C1": [
                {"user": "U1", "text": "c", "ts": "1712345687.000000"},
                {"user": "U1", "text": "b", "ts": "1712345686.000000"},
                {"user": "U1", "text": "a", "ts": "1712345685.000000"},
            ]},
        )
        pipeline = _FakePipeline()
        cu = _catchup(client=client, pipeline=pipeline, routings=routings,
                      watermarks={"C1": "1712345680.000000"})
        await cu.run()
        assert len(pipeline.handled) == 1

    async def test_fetch_history_retries_on_rate_limit(self):
        import surogates.channels.channel_catchup as cc
        from unittest import mock

        class _RateLimited(Exception):
            def __init__(self) -> None:
                super().__init__("ratelimited")
                self.response = {"headers": {"Retry-After": "0"}}

        class _RetryingClient(_FakeSlackClient):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.call_count = 0

            async def conversations_history(self, **kwargs: Any) -> dict:
                self.call_count += 1
                if self.call_count == 1:
                    raise _RateLimited()
                return await super().conversations_history(**kwargs)

        client = _RetryingClient(
            conversations=[{"id": "C1", "is_member": True}],
            history={"C1": [{"user": "U1", "text": "ok", "ts": "1712345691.000000"}]},
        )
        pipeline = _FakePipeline()
        cu = _catchup(client=client, pipeline=pipeline, routings=_ROUTINGS,
                      watermarks={"C1": "1712345680.000000"})

        async def _noop_sleep(s: float) -> None:
            pass

        with mock.patch.object(cc, "_sleep", _noop_sleep):
            await cu.run()

        assert client.call_count == 2
        assert len(pipeline.handled) == 1
