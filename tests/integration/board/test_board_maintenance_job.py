"""board_maintenance job: one pass runs expiry + all three purge clauses."""
import pytest

from surogates.jobs.board_maintenance import board_maintenance_pass


@pytest.mark.asyncio(loop_scope="session")
async def test_maintenance_pass_runs_all_clauses(session_factory):
    stats = await board_maintenance_pass(session_factory, purge_after_days=7)
    assert set(stats) == {
        "claims_expired",
        "purged_terminal_root",
        "purged_stale_rows",
        "purged_orphaned",
    }
    assert all(isinstance(v, int) for v in stats.values())
