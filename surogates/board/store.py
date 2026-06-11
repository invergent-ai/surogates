"""DB operations for the coordination board.

Admission flow (``admit``) deliberately splits across two short
transactions with the LLM verification in between, so no DB connection
is held during the (seconds-long) verifier call:

  txn A: load active rows  →  precheck (pure)  →  LLM verify (no txn)
  txn B: apply renewals + supersedes + inserts

The duplicate-race window this opens (two writers admitting the same
content concurrently) is benign: render-time dedupe collapses it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from uuid import UUID

from sqlalchemy import delete, func, select, update

from surogates.board.types import (
    STATUS_ACTIVE,
    STATUS_EXPIRED,
    STATUS_SUPERSEDED,
)
from surogates.board.verifier import NoteDraft, precheck_notes
from surogates.db.models import (
    BoardNote,
    Session as SessionRow,
    board_note_seq,
)

logger = logging.getLogger(__name__)

# Async callable applying the LLM gate: drafts -> (kept, rejected).
Verifier = Callable[
    [list[NoteDraft]],
    Awaitable[tuple[list[NoteDraft], list[tuple[int, str]]]],
]

# Keep in sync with surogates/jobs/inbox_expire.py — the platform's
# notion of a session that will never wake again.
_TERMINAL_SESSION_STATUSES = ("completed", "failed", "archived")


@dataclass(slots=True)
class AdmitResult:
    admitted: list[BoardNote] = field(default_factory=list)
    rejected: list[tuple[int, str]] = field(default_factory=list)
    renewed: list[int] = field(default_factory=list)


class BoardStore:
    def __init__(self, session_factory: Any) -> None:
        self._sf = session_factory

    async def admit(
        self,
        *,
        raw_notes: list[dict[str, Any]],
        org_id: UUID,
        group_id: UUID,
        writer_session_id: UUID,
        writer_label: str,
        verifier: Verifier,
        max_claims_per_writer: int,
        max_notes_per_group: int,
        claim_ttl_seconds: int,
    ) -> AdmitResult:
        """Run the full admission pipeline for one share_note batch."""
        now = datetime.now(timezone.utc)
        active = await self.active_notes(group_id)

        pre = precheck_notes(
            raw_notes,
            active_notes=active,
            writer_session_id=str(writer_session_id),
            max_claims_per_writer=max_claims_per_writer,
            max_notes_per_group=max_notes_per_group,
            claim_ttl_seconds=claim_ttl_seconds,
            now=now,
        )

        kept, llm_rejected = await verifier(pre.accepted)
        # ``llm_rejected`` indexes refer to positions in ``pre.accepted``,
        # which differ from the caller's raw batch indexes after precheck
        # filtering — so the reason embeds a content snippet the model
        # can correlate.
        rejected = list(pre.rejected) + [
            (idx, f"{reason} (note: {pre.accepted[idx].content[:80]!r})")
            for idx, reason in llm_rejected
        ]

        result = AdmitResult(rejected=rejected)

        if not kept and not pre.renewals:
            return result

        async with self._sf() as db:
            for note_id, new_expiry in pre.renewals:
                await db.execute(
                    update(BoardNote)
                    .where(BoardNote.id == note_id,
                           BoardNote.status == STATUS_ACTIVE)
                    .values(
                        expires_at=new_expiry,
                        seq=board_note_seq.next_value(),
                        updated_at=func.now(),
                    )
                )
                result.renewed.append(note_id)

            if any(d.type == "RESULT" for d in kept):
                await db.execute(
                    update(BoardNote)
                    .where(
                        BoardNote.group_id == group_id,
                        BoardNote.writer_session_id == writer_session_id,
                        BoardNote.type == "RESULT",
                        BoardNote.status == STATUS_ACTIVE,
                    )
                    .values(
                        status=STATUS_SUPERSEDED,
                        seq=board_note_seq.next_value(),
                        updated_at=func.now(),
                    )
                )

            rows: list[BoardNote] = []
            for draft in kept:
                rows.append(BoardNote(
                    org_id=org_id,
                    group_id=group_id,
                    writer_session_id=writer_session_id,
                    writer_label=writer_label,
                    type=draft.type,
                    content=draft.content,
                    ref=draft.ref,
                    expires_at=(
                        now + timedelta(seconds=claim_ttl_seconds)
                        if draft.type == "CLAIM" else None
                    ),
                ))
            db.add_all(rows)
            await db.commit()
            for row in rows:
                await db.refresh(row)
            result.admitted = rows

        return result

    async def active_notes(self, group_id: UUID) -> list[BoardNote]:
        async with self._sf() as db:
            rows = (await db.execute(
                select(BoardNote)
                .where(BoardNote.group_id == group_id,
                       BoardNote.status == STATUS_ACTIVE)
                .order_by(BoardNote.id.asc())
            )).scalars().all()
        return list(rows)

    async def changes_since(self, group_id: UUID, cursor: int) -> list[BoardNote]:
        async with self._sf() as db:
            rows = (await db.execute(
                select(BoardNote)
                .where(BoardNote.group_id == group_id,
                       BoardNote.seq > cursor)
                .order_by(BoardNote.seq.asc())
            )).scalars().all()
        return list(rows)

    async def max_seq(self, group_id: UUID) -> int:
        async with self._sf() as db:
            value = await db.scalar(
                select(func.max(BoardNote.seq))
                .where(BoardNote.group_id == group_id)
            )
        return int(value or 0)

    async def get_note(self, note_id: int) -> BoardNote | None:
        async with self._sf() as db:
            return await db.get(BoardNote, note_id)

    async def expire_due_claims(self) -> int:
        """Flip lapsed CLAIMs to expired (seq bumped so deltas report it)."""
        async with self._sf() as db:
            result = await db.execute(
                update(BoardNote)
                .where(
                    BoardNote.type == "CLAIM",
                    BoardNote.status == STATUS_ACTIVE,
                    BoardNote.expires_at.isnot(None),
                    BoardNote.expires_at <= func.now(),
                )
                .values(
                    status=STATUS_EXPIRED,
                    seq=board_note_seq.next_value(),
                    updated_at=func.now(),
                )
                .returning(BoardNote.id)
            )
            ids = list(result.scalars().all())
            await db.commit()
        return len(ids)

    async def purge_terminal_root_groups(self, *, older_than_days: int) -> int:
        """Purge clause 1: all notes of groups whose root session has
        been terminal for longer than the window."""
        # ``sessions.updated_at`` is a naive-UTC TIMESTAMP column —
        # compare with a naive cutoff (asyncpg rejects mixed awareness).
        cutoff = (
            datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(days=older_than_days)
        )
        async with self._sf() as db:
            terminal_roots = (
                select(SessionRow.id)
                .where(
                    SessionRow.status.in_(_TERMINAL_SESSION_STATUSES),
                    SessionRow.updated_at < cutoff,
                )
            )
            result = await db.execute(
                delete(BoardNote)
                .where(BoardNote.group_id.in_(terminal_roots))
                .returning(BoardNote.id)
            )
            ids = list(result.scalars().all())
            await db.commit()
        return len(ids)

    async def purge_stale_rows(self, *, older_than_days: int) -> int:
        """Purge clause 2: aged superseded/expired rows, any group."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
        async with self._sf() as db:
            result = await db.execute(
                delete(BoardNote)
                .where(
                    BoardNote.status.in_((STATUS_SUPERSEDED, STATUS_EXPIRED)),
                    BoardNote.updated_at < cutoff,
                )
                .returning(BoardNote.id)
            )
            ids = list(result.scalars().all())
            await db.commit()
        return len(ids)

    async def purge_orphaned_groups(self) -> int:
        """Purge clause 3: notes whose group_id matches no session row."""
        async with self._sf() as db:
            result = await db.execute(
                delete(BoardNote)
                .where(
                    ~BoardNote.group_id.in_(select(SessionRow.id))
                )
                .returning(BoardNote.id)
            )
            ids = list(result.scalars().all())
            await db.commit()
        return len(ids)
