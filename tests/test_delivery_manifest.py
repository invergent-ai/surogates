"""What a turn delivered is decided by the workspace, not by a guess.

The summarizer used to hand every touched file to the base model and ask
which was the user's deliverable. These are the cases that guess cannot
see, because a candidate that is empty, stale, or absent looks exactly
like a real one once it is a line in a prompt.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from surogates.harness.delivery_manifest import (
    check_terminal_claim,
    reconcile,
)
from surogates.harness.turn_summarizer import TurnArtifact

T0 = datetime(2026, 8, 27, 12, 0, 0)


def _entry(size: int, offset_s: float) -> dict:
    return {"size": size, "modified": T0 + timedelta(seconds=offset_s)}


class TestReconcile:
    def test_real_deliverable_survives(self):
        m = reconcile(
            [TurnArtifact("file", "report.pdf", "report.pdf")],
            entries_by_path={"report.pdf": _entry(1200, 30)},
            turn_start=T0,
        )
        assert [a.ref for a in m.delivered] == ["report.pdf"]
        assert m.rejected == []
        assert m.has_delivery

    def test_empty_file_is_not_a_delivery(self):
        """A zero-byte file reads as success in a prompt."""
        m = reconcile(
            [TurnArtifact("file", "out.csv", "out.csv")],
            entries_by_path={"out.csv": _entry(0, 30)},
            turn_start=T0,
        )
        assert m.delivered == []
        assert [(r.ref, r.reason) for r in m.rejected] == [("out.csv", "empty")]

    def test_stale_same_named_file_is_not_a_delivery(self):
        """The write failed; last turn's file is still sitting there."""
        m = reconcile(
            [TurnArtifact("file", "report.pdf", "report.pdf")],
            entries_by_path={"report.pdf": _entry(5000, -7200)},
            turn_start=T0,
        )
        assert m.delivered == []
        assert m.rejected[0].reason == "stale"

    def test_executed_script_is_scaffolding(self):
        """A file the agent wrote and then ran generated the deliverable."""
        m = reconcile(
            [
                TurnArtifact("file", "report.pdf", "report.pdf"),
                TurnArtifact(
                    "file", "make.py", "make.py",
                    {"executed_by_terminal": True},
                ),
            ],
            entries_by_path={
                "report.pdf": _entry(900, 5), "make.py": _entry(400, 3),
            },
            turn_start=T0,
        )
        assert [a.ref for a in m.delivered] == ["report.pdf"]
        assert m.rejected[0].reason == "scaffolding"

    def test_unlisted_candidate_is_kept(self):
        """A listing that lags a just-written file must not lose it.

        Only positive evidence of a problem rejects -- dropping a real
        deliverable is worse than showing an extra one.
        """
        m = reconcile(
            [TurnArtifact("file", "fresh.md", "fresh.md")],
            entries_by_path={},
            turn_start=T0,
        )
        assert [a.ref for a in m.delivered] == ["fresh.md"]

    def test_artifacts_bypass_reconciliation(self):
        """create_artifact either succeeded or raised; no file to check."""
        m = reconcile(
            [TurnArtifact("artifact", "chart", "art-1")],
            entries_by_path={},
            turn_start=T0,
        )
        assert [a.ref for a in m.delivered] == ["art-1"]


class TestTerminalClaim:
    """The failure a candidate-curating model structurally cannot see.

    With nothing written there is no candidate, so nothing reaches the
    model to reject, and "here is your report" reads as success.
    """

    @staticmethod
    def _empty():
        return reconcile([], entries_by_path={}, turn_start=T0)

    @pytest.mark.parametrize("message,expected", [
        ("I saved the analysis to report.pdf for you.", "report.pdf"),
        ("I have written summary.md with the findings.", "summary.md"),
        ("Generated output.xlsx as requested.", "output.xlsx"),
    ])
    def test_claim_with_nothing_delivered_is_flagged(self, message, expected):
        assert check_terminal_claim(self._empty(), message).unsupported_claim == expected

    @pytest.mark.parametrize("message", [
        "Done. Let me know if you need anything else.",
        "The config lives in settings.yaml, but I did not change it.",
        "",
    ])
    def test_ordinary_prose_is_not_flagged(self, message):
        """A false warning is worse than none -- it trains people to ignore it."""
        assert check_terminal_claim(self._empty(), message).unsupported_claim is None

    def test_a_real_delivery_silences_the_check(self):
        """Mentioning another file while delivering one is normal prose."""
        m = reconcile(
            [TurnArtifact("file", "report.pdf", "report.pdf")],
            entries_by_path={"report.pdf": _entry(900, 5)},
            turn_start=T0,
        )
        assert check_terminal_claim(m, "Saved to notes.md too").unsupported_claim is None
