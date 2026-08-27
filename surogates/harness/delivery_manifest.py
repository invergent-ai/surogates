"""Deterministic reconciliation of what a turn actually delivered.

The turn summarizer used to hand every touched file to the base model
and ask which one was the user's deliverable. That is a guess on the
delivery path, paid for on every turn, and it is blind to the three ways
a delivery goes wrong without looking wrong:

* the file is **scaffolding** -- written by the agent and then executed
  by it, i.e. a generator script rather than the thing generated;
* the file was written but is **empty**;
* the file is **stale** -- a same-named leftover from an earlier turn,
  where this turn's write failed;
* nothing was written **at all**, yet the closing message says a report
  was delivered. There is no candidate to reject, so a model choosing
  among candidates cannot see it.

Candidates were already collected deterministically -- from ``write_file``
/ ``patch`` / ``create_artifact`` calls plus a workspace scan. Only the
selection was a guess, so only the selection is replaced here.

Artifacts (``kind="artifact"``) are not reconciled: they are created
through ``create_artifact``, which either succeeds or raises, so there is
no filesystem to disagree with.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Literal

from surogates.harness.turn_summarizer import TurnArtifact

#: Why a candidate was not counted as delivered.
RejectionReason = Literal["missing", "empty", "stale", "scaffolding"]


@dataclass(frozen=True)
class RejectedDelivery:
    """A candidate the workspace does not support as a real deliverable."""

    ref: str
    reason: RejectionReason


@dataclass(frozen=True)
class DeliveryManifest:
    """What the turn actually produced, and what it only appeared to."""

    delivered: list[TurnArtifact]
    rejected: list[RejectedDelivery]
    #: Set when the closing message asserts a delivery the manifest does
    #: not support. Advisory -- see :func:`check_terminal_claim`.
    unsupported_claim: str | None = None

    @property
    def has_delivery(self) -> bool:
        return bool(self.delivered)


def reconcile(
    candidates: Iterable[TurnArtifact],
    *,
    entries_by_path: dict[str, dict[str, Any]],
    turn_start: datetime,
) -> DeliveryManifest:
    """Keep the candidates the workspace actually supports.

    ``entries_by_path`` is the bulk listing the candidate scan already
    fetches, keyed by workspace-relative path; each entry carries at
    least ``size`` and ``modified``. A file candidate survives only if it
    is present, non-empty, and was written during this turn.

    A candidate whose entry is absent from the listing is **kept**, not
    rejected: the listing can lag a just-written object, and dropping a
    real deliverable is worse than showing one. Only positive evidence of
    a problem -- present-but-empty, or present-but-old -- rejects.
    """
    delivered: list[TurnArtifact] = []
    rejected: list[RejectedDelivery] = []

    for cand in candidates:
        if cand.kind != "file":
            delivered.append(cand)
            continue

        if (cand.meta or {}).get("executed_by_terminal"):
            # The agent wrote it and then ran it: a generator script,
            # not the thing generated. This was the summariser prompt's
            # strongest rule and it never needed a model -- the tool
            # stream already says whether a file was executed.
            rejected.append(
                RejectedDelivery(ref=cand.ref, reason="scaffolding"),
            )
            continue

        entry = entries_by_path.get(cand.ref)
        if entry is None:
            # Not observable either way -- see the docstring.
            delivered.append(cand)
            continue

        size = entry.get("size")
        if isinstance(size, int) and size <= 0:
            rejected.append(RejectedDelivery(ref=cand.ref, reason="empty"))
            continue

        modified = entry.get("modified")
        if isinstance(modified, datetime) and modified < turn_start:
            # Same name, older content: this turn's write did not land.
            rejected.append(RejectedDelivery(ref=cand.ref, reason="stale"))
            continue

        delivered.append(cand)

    return DeliveryManifest(delivered=delivered, rejected=rejected)


# A closing message that names a file and asserts it was produced. Kept
# narrow on purpose: this only ever adds a warning, and a warning that
# wrongly tells someone their work failed is worse than staying quiet.
_CLAIM_VERBS = (
    r"saved|wrote|written|created|generated|exported|produced|"
    r"attached|delivered"
)
_FILE_TOKEN = r"[\w./-]+\.[A-Za-z0-9]{1,8}"
_CLAIM_RE = re.compile(
    rf"\b(?:{_CLAIM_VERBS})\b[^.\n]{{0,80}}?({_FILE_TOKEN})",
    re.IGNORECASE,
)


def check_terminal_claim(
    manifest: DeliveryManifest, final_message: str | None,
) -> DeliveryManifest:
    """Flag a closing message that claims a delivery the manifest lacks.

    This is the case no amount of candidate curation can reach: with
    nothing written there is no candidate, so nothing reaches the model
    to be rejected, and "here is your report" reads as success.

    Only fires when the manifest delivered **nothing** -- a turn that
    produced one file while mentioning another is ordinary prose, not a
    false claim, and flagging it would train people to ignore the
    warning.
    """
    if manifest.has_delivery or not final_message:
        return manifest

    match = _CLAIM_RE.search(final_message)
    if match is None:
        return manifest

    return DeliveryManifest(
        delivered=manifest.delivered,
        rejected=manifest.rejected,
        unsupported_claim=match.group(1),
    )
