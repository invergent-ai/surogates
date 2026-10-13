"""The office worker job: off unless office editing is configured."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from surogates.config import OfficeSettings
from surogates.jobs import office_saves


async def test_does_nothing_when_office_is_off(monkeypatch):
    monkeypatch.delenv("SUROGATES_OFFICE_PUBLIC_URL", raising=False)

    async def called(**kwargs):
        raise AssertionError("the job ran with office editing off")

    monkeypatch.setattr(office_saves, "land_pending_saves", called)
    monkeypatch.setattr(office_saves, "reap", called)
    await asyncio.wait_for(
        office_saves.run_office_loop(session_factory=None, session_store=None, storage=None, redis=None,
                                     settings=SimpleNamespace(office=OfficeSettings())),
        timeout=1,
    )
