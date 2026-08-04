"""Reading an inbox item: what a receipt settles, and what it must not.

Two processes write this table -- the harness store and the ops control
plane -- so the rule lives in one function they both call rather than a
copy each with a comment warning them not to drift.
"""

from types import SimpleNamespace

import pytest

from surogates.session.inbox_payload import apply_read_receipt


def _item(*, kind: str, status: str = "pending", read_at=None) -> SimpleNamespace:
    return SimpleNamespace(
        kind=kind,
        status=status,
        read_at=read_at,
        responded_at=None,
        updated_at=None,
    )


@pytest.mark.parametrize("kind", ["task_complete", "progress_checkin"])
def test_reading_an_update_retires_it(kind):
    item = _item(kind=kind)

    assert apply_read_receipt(item) is True
    assert item.read_at is not None
    assert item.status == "acknowledged"
    assert item.responded_at is not None


@pytest.mark.parametrize(
    "kind", ["input_required", "action_required", "governance_gate"]
)
def test_reading_something_answerable_leaves_it_pending(kind):
    # A question is not answered by being looked at.
    item = _item(kind=kind)

    assert apply_read_receipt(item) is True
    assert item.read_at is not None
    assert item.status == "pending"
    assert item.responded_at is None


def test_an_already_read_item_is_left_alone():
    item = _item(kind="task_complete", status="expired", read_at="2026-08-04T00:00:00Z")

    # Nothing to write, and no transition attempted out of a terminal
    # status — which the store would reject.
    assert apply_read_receipt(item) is False
    assert item.status == "expired"


def test_a_resolved_but_unread_item_only_gets_the_receipt():
    """A row can be terminal without ever having been opened: the chat
    surfaces retire informational items when the session is opened."""
    item = _item(kind="task_complete", status="expired")

    assert apply_read_receipt(item) is True
    assert item.read_at is not None
    assert item.status == "expired"
