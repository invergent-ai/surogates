from surogates.db.models import AmbientScheduleRow


def test_table_and_columns():
    t = AmbientScheduleRow.__table__
    assert t.name == "ambient_schedules"
    cols = set(t.columns.keys())
    assert {
        "id", "org_id", "agent_id", "platform", "channel_id",
        "ambient_session_id", "cadence_seconds", "status",
        "next_run_at", "locked_by", "locked_until",
        "created_at", "updated_at",
    } <= cols
