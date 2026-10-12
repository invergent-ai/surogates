"""Every foreign key into ``events`` or ``sessions`` leads an index.

Deleting an agent or a session removes its events and sessions in bulk, and
Postgres checks each removed row against every table that references it. An
unindexed referencing column turns each check into a sequential scan of that
table: deleting a 240k-event agent scanned ``delivery_outbox`` 240k times
(3.6 billion rows read) before ``delivery_outbox.event_id`` was indexed.
"""

from __future__ import annotations

from surogates.db.models import Base

BULK_DELETED = {"events", "sessions"}


def _leading_columns(table) -> set[str]:
    leading = {next(iter(index.columns)).name for index in table.indexes}
    leading |= {
        next(iter(constraint.columns)).name
        for constraint in table.constraints
        if constraint.__class__.__name__ in ("PrimaryKeyConstraint", "UniqueConstraint")
        and len(constraint.columns)
    }
    leading |= {column.name for column in table.columns if column.index or column.unique}
    return leading


def test_foreign_keys_into_bulk_deleted_tables_are_indexed():
    unindexed = [
        f"{table.name}.{fk.column_keys[0]} -> {fk.referred_table.name}"
        for table in Base.metadata.tables.values()
        for fk in table.foreign_key_constraints
        if fk.referred_table.name in BULK_DELETED
        and fk.column_keys[0] not in _leading_columns(table)
    ]
    assert unindexed == []


def test_existing_databases_get_the_same_indexes():
    # create_all never adds an index to a table that already exists, so each
    # model index on these columns is retrofitted by observability.sql.
    from surogates.db.engine import OBSERVABILITY_SQL_PATH

    ddl = OBSERVABILITY_SQL_PATH.read_text()
    for name in (
        "idx_delivery_outbox_event",
        "idx_delivery_outbox_session",
        "idx_board_notes_writer_session",
        "idx_workstream_history_thread",
    ):
        assert f"CREATE INDEX IF NOT EXISTS {name}" in ddl, name
