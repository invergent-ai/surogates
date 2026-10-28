"""The scheduled sweep goes on past a step that fails."""

from surogates.scheduled.materialize import recover_stalled_loops


class _Store:
    def __init__(self):
        self.ran = []

    async def plan_unscheduled(self, *, limit):
        raise ValueError("a row nobody can plan")

    async def expire_active_schedules(self, *, limit):
        self.ran.append("expire")
        return []

    async def find_retryable_stalled_dynamic_loop_runs(self, *, stale_seconds, limit):
        raise ConnectionError("lost the database")

    async def recover_stalled_dynamic_loops(self, *, stale_seconds, limit):
        self.ran.append("recover_terminal")
        return []


async def test_a_failing_step_does_not_skip_the_rest():
    store = _Store()

    await recover_stalled_loops(scheduled_store=store, redis=None)

    assert store.ran == ["expire", "recover_terminal"]
