from types import SimpleNamespace
from contextlib import asynccontextmanager

import pytest

from surogates.ambient.tasks_probe import recent_task_changes, summarize_tasks


def _task(**kw):
    base = dict(goal="deploy", status="done", blocked_reason=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_summarize_done_blocked_failed():
    out = summarize_tasks([
        _task(goal="deploy", status="done"),
        _task(goal="migrate", status="blocked", blocked_reason="needs secret"),
        _task(goal="build", status="failed"),
    ])
    joined = " | ".join(out)
    assert "deploy" in joined and "done" in joined
    assert "migrate" in joined and "blocked" in joined and "needs secret" in joined
    assert "build" in joined and "failed" in joined


def test_summarize_empty():
    assert summarize_tasks([]) == []


@pytest.mark.asyncio
async def test_recent_task_changes_queries_and_summarizes(monkeypatch):
    import uuid

    captured = {}

    class _Result:
        def scalars(self): return self
        def all(self): return [_task(goal="x", status="done")]

    class _DB:
        async def execute(self, stmt):
            captured["queried"] = True
            return _Result()

    @asynccontextmanager
    async def _factory():
        yield _DB()

    out = await recent_task_changes(
        _factory, org_id=uuid.uuid4(), source_session_id=uuid.uuid4(),
    )
    assert captured.get("queried") is True
    assert out == ["Task 'x' is now done"]
