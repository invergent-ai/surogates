"""Tests for surogates.browser.pool.BrowserPool."""

from __future__ import annotations

from datetime import datetime, timezone

from surogates.browser.base import (
    BrowserEndpoint,
    BrowserSpec,
    BrowserStatus,
    BrowserUnavailableError,
)
from surogates.browser.pool import BrowserPool, EnsureResult
from surogates.browser.registry import BrowserEntry


class FakeBackend:
    def __init__(self) -> None:
        self.provisions = 0
        self.destroys: list[str] = []
        self.status_overrides: dict[str, BrowserStatus] = {}
        self.fail_provision: BrowserUnavailableError | None = None
        self.provision_labels: list[tuple[str, str, str]] = []
        self.destroyed_sessions: list[str] = []

    async def provision(
        self,
        spec: BrowserSpec,
        *,
        session_id: str = "",
        org_id: str = "",
        user_id: str = "",
    ) -> tuple[str, BrowserEndpoint]:
        if self.fail_provision is not None:
            raise self.fail_provision
        self.provisions += 1
        self.provision_labels.append((session_id, org_id, user_id))
        bid = f"b{self.provisions}"
        return bid, BrowserEndpoint(
            rest_url=f"http://x:{30000 + self.provisions}",
            cdp_url=f"ws://x:{31000 + self.provisions}",
            live_view_url=f"ws://x:{32000 + self.provisions}",
        )

    async def status(self, browser_id: str) -> BrowserStatus:
        return self.status_overrides.get(browser_id, BrowserStatus.RUNNING)

    async def destroy(self, browser_id: str) -> None:
        self.destroys.append(browser_id)

    async def destroy_for_session(self, session_id: str) -> None:
        self.destroyed_sessions.append(session_id)


class FakeRegistry:
    def __init__(self) -> None:
        self.entries: dict[str, BrowserEntry] = {}

    async def set(self, entry: BrowserEntry) -> None:
        self.entries[entry.session_id] = entry

    async def get(self, session_id: str) -> BrowserEntry | None:
        return self.entries.get(session_id)

    async def delete(self, session_id: str) -> None:
        self.entries.pop(session_id, None)


class TestEnsure:
    async def test_first_call_provisions(self) -> None:
        backend = FakeBackend()
        registry = FakeRegistry()
        pool = BrowserPool(backend=backend, registry=registry)  # type: ignore[arg-type]

        result = await pool.ensure(
            session_id="sess-1",
            org_id="o",
            user_id="u",
            spec=BrowserSpec(),
        )
        assert isinstance(result, EnsureResult)
        assert result.newly_provisioned is True
        assert result.endpoint.rest_url == "http://x:30001"
        assert backend.provisions == 1
        assert backend.provision_labels == [("sess-1", "o", "u")]
        assert "sess-1" in registry.entries

    async def test_second_call_reuses(self) -> None:
        backend = FakeBackend()
        pool = BrowserPool(backend=backend, registry=FakeRegistry())  # type: ignore[arg-type]

        await pool.ensure("sess-1", "o", "u", BrowserSpec())
        result = await pool.ensure("sess-1", "o", "u", BrowserSpec())
        assert result.newly_provisioned is False
        assert backend.provisions == 1

    async def test_stale_status_reprovisions(self) -> None:
        backend = FakeBackend()
        pool = BrowserPool(backend=backend, registry=FakeRegistry())  # type: ignore[arg-type]

        await pool.ensure("sess-1", "o", "u", BrowserSpec())
        backend.status_overrides["b1"] = BrowserStatus.FAILED
        result = await pool.ensure("sess-1", "o", "u", BrowserSpec())
        assert result.newly_provisioned is True
        assert backend.provisions == 2
        assert backend.destroys == ["b1"]

    async def test_provision_failure_propagates(self) -> None:
        backend = FakeBackend()
        backend.fail_provision = BrowserUnavailableError("docker pull failed")
        pool = BrowserPool(backend=backend, registry=FakeRegistry())  # type: ignore[arg-type]
        try:
            await pool.ensure("sess-1", "o", "u", BrowserSpec())
        except BrowserUnavailableError:
            pass
        else:
            raise AssertionError("BrowserUnavailableError was not raised")


class TestDestroy:
    async def test_destroy_for_session(self) -> None:
        backend = FakeBackend()
        registry = FakeRegistry()
        pool = BrowserPool(backend=backend, registry=registry)  # type: ignore[arg-type]
        await pool.ensure("sess-1", "o", "u", BrowserSpec())

        await pool.destroy_for_session("sess-1")
        assert backend.destroys == ["b1"]
        assert "sess-1" not in registry.entries

    async def test_destroy_for_unknown_session_is_noop(self) -> None:
        pool = BrowserPool(backend=FakeBackend(), registry=FakeRegistry())  # type: ignore[arg-type]
        await pool.destroy_for_session("nope")

    async def test_destroy_for_missing_mapping_uses_backend_session_cleanup(
        self,
    ) -> None:
        backend = FakeBackend()
        registry = FakeRegistry()
        registry.entries["sess-1"] = BrowserEntry(
            session_id="sess-1",
            org_id="o",
            user_id="u",
            rest_url="http://x:30000",
            cdp_url="ws://x:31000",
            live_view_url="ws://x:32000",
            provisioned_at=datetime.now(timezone.utc),
        )
        pool = BrowserPool(backend=backend, registry=registry)  # type: ignore[arg-type]

        await pool.destroy_for_session("sess-1")

        assert backend.destroyed_sessions == ["sess-1"]
        assert "sess-1" not in registry.entries

    async def test_destroy_all(self) -> None:
        backend = FakeBackend()
        pool = BrowserPool(backend=backend, registry=FakeRegistry())  # type: ignore[arg-type]
        await pool.ensure("sess-1", "o", "u", BrowserSpec())
        await pool.ensure("sess-2", "o", "u", BrowserSpec())
        await pool.destroy_all()
        assert sorted(backend.destroys) == ["b1", "b2"]


class TestEvents:
    async def test_ensure_emits_browser_provisioned_via_callback(self) -> None:
        events: list[tuple[str, dict]] = []

        async def emitter(session_id: str, event_type: str, data: dict) -> None:
            events.append((event_type, data))

        backend = FakeBackend()
        pool = BrowserPool(
            backend=backend,
            registry=FakeRegistry(),  # type: ignore[arg-type]
            event_emitter=emitter,
        )
        await pool.ensure("sess-1", "o", "u", BrowserSpec())
        await pool.ensure("sess-1", "o", "u", BrowserSpec())

        types = [event_type for event_type, _ in events]
        assert types == ["browser.provisioned"]
        assert events[0][1]["session_id"] == "sess-1"
        assert events[0][1]["browser_id"] == "b1"

    async def test_destroy_emits_browser_destroyed(self) -> None:
        events: list[tuple[str, dict]] = []

        async def emitter(session_id: str, event_type: str, data: dict) -> None:
            events.append((event_type, data))

        backend = FakeBackend()
        pool = BrowserPool(
            backend=backend,
            registry=FakeRegistry(),  # type: ignore[arg-type]
            event_emitter=emitter,
        )
        await pool.ensure("sess-1", "o", "u", BrowserSpec())
        await pool.destroy_for_session("sess-1")

        types = [event_type for event_type, _ in events]
        assert "browser.destroyed" in types

    async def test_destroy_with_cold_mapping_still_emits_destroyed(
        self,
    ) -> None:
        # Simulates a worker restart: registry still has the entry from
        # a previous worker but the in-memory mapping is empty. The SDK
        # must still learn that the browser is gone.
        events: list[tuple[str, dict]] = []

        async def emitter(session_id: str, event_type: str, data: dict) -> None:
            events.append((event_type, data))

        backend = FakeBackend()
        registry = FakeRegistry()
        registry.entries["sess-1"] = BrowserEntry(
            session_id="sess-1",
            org_id="o",
            user_id="u",
            rest_url="http://x:30000",
            cdp_url="ws://x:31000",
            live_view_url="ws://x:32000",
            provisioned_at=datetime.now(timezone.utc),
        )
        pool = BrowserPool(
            backend=backend,
            registry=registry,  # type: ignore[arg-type]
            event_emitter=emitter,
        )

        await pool.destroy_for_session("sess-1")

        types = [event_type for event_type, _ in events]
        assert "browser.destroyed" in types
        # The browser_id is unknown on the cold path; consumers should
        # treat the event as authoritative regardless of payload.
        destroy_event = next(e for e in events if e[0] == "browser.destroyed")
        assert destroy_event[1]["session_id"] == "sess-1"
        assert destroy_event[1]["browser_id"] is None

    async def test_destroy_with_cold_mapping_and_empty_registry_skips_emit(
        self,
    ) -> None:
        # If neither the mapping nor the registry knew about the session
        # there's nothing to announce — don't emit a phantom destroy.
        events: list[tuple[str, dict]] = []

        async def emitter(session_id: str, event_type: str, data: dict) -> None:
            events.append((event_type, data))

        pool = BrowserPool(
            backend=FakeBackend(),
            registry=FakeRegistry(),  # type: ignore[arg-type]
            event_emitter=emitter,
        )
        await pool.destroy_for_session("nope")
        assert events == []

    async def test_reprovision_emits_destroyed_then_provisioned(self) -> None:
        # When ensure() finds a stale slot it tears the old pod down and
        # provisions a fresh one. Consumers must see a clean
        # destroyed→provisioned pair instead of two provisioneds against
        # different pod ids.
        events: list[tuple[str, dict]] = []

        async def emitter(session_id: str, event_type: str, data: dict) -> None:
            events.append((event_type, data))

        backend = FakeBackend()
        pool = BrowserPool(
            backend=backend,
            registry=FakeRegistry(),  # type: ignore[arg-type]
            event_emitter=emitter,
        )

        await pool.ensure("sess-1", "o", "u", BrowserSpec())
        backend.status_overrides["b1"] = BrowserStatus.FAILED
        await pool.ensure("sess-1", "o", "u", BrowserSpec())

        types = [event_type for event_type, _ in events]
        assert types == [
            "browser.provisioned",
            "browser.destroyed",
            "browser.provisioned",
        ]
        # The destroyed event carries the stale browser_id.
        destroy_event = next(e for e in events if e[0] == "browser.destroyed")
        assert destroy_event[1]["browser_id"] == "b1"
        # The new provisioned event carries the fresh browser_id.
        assert events[-1][1]["browser_id"] == "b2"
