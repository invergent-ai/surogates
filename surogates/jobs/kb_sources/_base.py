"""Shared types for KB source runners."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


@dataclass
class SourceContext:
    """Per-source state passed from :mod:`surogates.jobs.kb_ingest` to a
    runner. Resolved by joining ``kb_source`` to its parent ``kb`` row
    so the runner has everything it needs in one struct.
    """

    id: UUID                        # kb_source.id
    kb_id: UUID                     # kb_source.kb_id
    kb_org_id: UUID | None          # kb.org_id (None for platform KBs)
    kb_name: str                    # kb.name
    kind: str                       # kb_source.kind
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class IngestResult:
    """Per-run summary returned from a runner.

    Counts: how many docs were newly inserted, updated (content
    changed), unchanged (content_sha matched, no work done), and
    skipped (e.g. unreadable file, unsupported format).
    """

    docs_added: int = 0
    docs_updated: int = 0
    docs_unchanged: int = 0
    docs_skipped: int = 0
    bytes_written: int = 0

    @property
    def total(self) -> int:
        return (
            self.docs_added
            + self.docs_updated
            + self.docs_unchanged
            + self.docs_skipped
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "docs_added": self.docs_added,
            "docs_updated": self.docs_updated,
            "docs_unchanged": self.docs_unchanged,
            "docs_skipped": self.docs_skipped,
            "bytes_written": self.bytes_written,
            "total": self.total,
        }
