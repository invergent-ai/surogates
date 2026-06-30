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
        lock=None,  # no-op until the lock wiring is added
    )
    cu._resolve_platform = lambda app: platform        # inject the fake platform (sync)
    cu._watermark = _wm(watermarks)                    # inject watermarks (async)
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
