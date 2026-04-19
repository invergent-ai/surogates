"""Pure-Python unit tests for :mod:`surogates.jobs.training_collector` helpers.

Kept out of ``tests/integration/`` because these do not require the
PostgreSQL testcontainer.
"""

from __future__ import annotations

from surogates.jobs.training_collector import _strip_skill_prefix


def test_strip_skill_prefix_removes_leading_slash_command():
    assert _strip_skill_prefix("/sql find users", "sql") == "find users"


def test_strip_skill_prefix_strips_multiple_spaces():
    assert _strip_skill_prefix("/sql   find  users", "sql") == "find  users"


def test_strip_skill_prefix_passthrough_when_no_slash():
    assert _strip_skill_prefix("find users", "sql") == "find users"


def test_strip_skill_prefix_keeps_raw_when_prefix_is_all_we_have():
    # Don't hand back an empty string — caller needs something to decide on.
    assert _strip_skill_prefix("/sql", "sql") == "/sql"
