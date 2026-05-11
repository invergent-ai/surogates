"""Tests for browser pause notice injection in the harness."""

from __future__ import annotations

from types import SimpleNamespace

from surogates.harness.loop import _initial_system_message, maybe_inject_browser_pause


class TestBrowserPauseInjection:
    async def test_injects_when_held_and_not_yet_injected(self) -> None:
        session = SimpleNamespace(id="sess-1", config={})

        async def held_by(_session_id: str) -> str | None:
            return "user-1"

        msg = await maybe_inject_browser_pause(
            session=session,
            browser_control=SimpleNamespace(held_by=held_by),
        )

        assert msg is not None
        assert "user has taken control" in msg.lower()
        assert session.config["browser_pause_msg_injected"] is True

    async def test_no_inject_when_already_injected(self) -> None:
        session = SimpleNamespace(
            id="sess-1",
            config={"browser_pause_msg_injected": True},
        )

        async def held_by(_session_id: str) -> str | None:
            return "user-1"

        msg = await maybe_inject_browser_pause(
            session=session,
            browser_control=SimpleNamespace(held_by=held_by),
        )

        assert msg is None

    async def test_clears_flag_when_not_held(self) -> None:
        session = SimpleNamespace(
            id="sess-1",
            config={"browser_pause_msg_injected": True},
        )

        async def held_by(_session_id: str) -> str | None:
            return None

        msg = await maybe_inject_browser_pause(
            session=session,
            browser_control=SimpleNamespace(held_by=held_by),
        )

        assert msg is None
        assert session.config["browser_pause_msg_injected"] is False

    async def test_no_inject_without_control_store(self) -> None:
        session = SimpleNamespace(id="sess-1", config={})

        msg = await maybe_inject_browser_pause(
            session=session,
            browser_control=None,
        )

        assert msg is None

    def test_pause_notice_is_folded_into_first_system_message(self) -> None:
        msg = _initial_system_message(
            "Base system prompt.",
            "The user has taken control of the browser.",
        )

        assert msg["role"] == "system"
        assert msg["content"].startswith("Base system prompt.")
        assert "The user has taken control of the browser." in msg["content"]
