from datetime import datetime, timezone
from itertools import islice

from surogates.office.saves import conflict_copy_paths

WHEN = datetime(2026, 10, 13, 14, 32, tzinfo=timezone.utc)


def test_conflict_copy_names():
    names = list(islice(conflict_copy_paths("docs/Q3 plan.docx", WHEN), 3))
    assert names == [
        "docs/Q3 plan (edited 2026-10-13 14.32 UTC).docx",
        "docs/Q3 plan (edited 2026-10-13 14.32 UTC) 2.docx",
        "docs/Q3 plan (edited 2026-10-13 14.32 UTC) 3.docx",
    ]


def test_conflict_copy_at_the_root():
    assert next(conflict_copy_paths("Plan.xlsx", WHEN)) == "Plan (edited 2026-10-13 14.32 UTC).xlsx"
