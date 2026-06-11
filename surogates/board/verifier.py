"""Admission pipeline for board notes.

Two layers, run in order by the share_note tool:

1. :func:`precheck_notes` — free deterministic checks.  Order matters:
   renewal detection MUST precede the claim cap, or a writer at the cap
   could never renew its own claims.
2. :func:`verify_notes_llm` — always-on LLM gate, FAIL-CLOSED: any
   verifier error rejects the batch with a retryable reason.  The
   board's value rests on the invariant that everything visible passed
   the gate, so there is deliberately no deterministic fallback.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable

from surogates.board.types import MAX_CONTENT_CHARS, NOTE_TYPES
from surogates.harness.prompt import PromptBuilder
from surogates.memory.store import scan_memory_content

logger = logging.getLogger(__name__)

_WS_RE = re.compile(r"\s+")

VERIFICATION_UNAVAILABLE = "verification unavailable — retry on a later turn"


def _norm(content: str) -> str:
    return _WS_RE.sub(" ", content).strip().lower()


@dataclass(slots=True)
class NoteDraft:
    """A candidate note that passed pre-checks (not yet verified)."""

    type: str
    content: str
    ref: dict[str, Any] | None = None


@dataclass(slots=True)
class PrecheckResult:
    accepted: list[NoteDraft] = field(default_factory=list)
    rejected: list[tuple[int, str]] = field(default_factory=list)
    # (existing_note_id, new_expires_at) pairs for own-claim renewals.
    renewals: list[tuple[int, datetime]] = field(default_factory=list)


def precheck_notes(
    raw_notes: Iterable[dict[str, Any]],
    *,
    active_notes: list[Any],
    writer_session_id: str,
    max_claims_per_writer: int,
    max_notes_per_group: int,
    claim_ttl_seconds: int,
    now: datetime,
) -> PrecheckResult:
    """Deterministic admission pre-checks over a share_note batch.

    ``active_notes`` is the group's current active rows (duck-typed:
    id, type, content, writer_session_id, status, expires_at).
    """
    result = PrecheckResult()
    active_by_key = {(n.type, _norm(n.content)): n for n in active_notes}
    writer_sid = str(writer_session_id)
    own_active_claims = sum(
        1 for n in active_notes
        if n.type == "CLAIM" and str(n.writer_session_id) == writer_sid
    )
    n_active = len(active_notes)
    batch_keys: set[tuple[str, str]] = set()
    net_new_claims = 0
    net_new_total = 0

    for idx, raw in enumerate(raw_notes):
        ntype = str(raw.get("type") or "").strip().upper()
        content = str(raw.get("content") or "")
        ref = raw.get("ref")

        if ntype not in NOTE_TYPES:
            result.rejected.append(
                (idx, f"invalid type {ntype!r}; must be one of {', '.join(NOTE_TYPES)}")
            )
            continue
        stripped = content.strip()
        if not stripped:
            result.rejected.append((idx, "empty content"))
            continue
        cap = MAX_CONTENT_CHARS[ntype]
        if len(stripped) > cap:
            result.rejected.append(
                (idx, f"content exceeds {cap} chars for {ntype} ({len(stripped)})")
            )
            continue
        if ref is not None and not isinstance(ref, dict):
            result.rejected.append((idx, "ref must be an object"))
            continue

        # Cross-session prompt input: same bars as memory entries.
        if PromptBuilder.scan_for_injection(stripped):
            result.rejected.append(
                (idx, "blocked: content matches prompt-injection patterns")
            )
            continue
        scan_error = scan_memory_content(stripped)
        if scan_error is not None:
            result.rejected.append((idx, f"blocked: {scan_error}"))
            continue

        key = (ntype, _norm(stripped))
        existing = active_by_key.get(key)
        if existing is not None:
            if (
                ntype == "CLAIM"
                and str(existing.writer_session_id) == writer_sid
            ):
                # Renewal: refresh TTL, bypass the cap (the claim
                # already holds one of the writer's slots).
                result.renewals.append(
                    (existing.id, now + timedelta(seconds=claim_ttl_seconds))
                )
            else:
                result.rejected.append(
                    (idx, f"duplicate of active note n{existing.id}")
                )
            continue
        if key in batch_keys:
            result.rejected.append((idx, "duplicate within this batch"))
            continue

        if ntype == "CLAIM":
            if own_active_claims + net_new_claims >= max_claims_per_writer:
                result.rejected.append(
                    (idx,
                     f"claim cap reached ({max_claims_per_writer} active); "
                     "let one expire or renew an existing claim")
                )
                continue

        if ntype != "RESULT" and n_active + net_new_total >= max_notes_per_group:
            result.rejected.append(
                (idx,
                 "board full — let claims expire or supersede a RESULT; "
                 "RESULT notes are still admitted")
            )
            continue

        batch_keys.add(key)
        net_new_total += 1
        if ntype == "CLAIM":
            net_new_claims += 1
        result.accepted.append(NoteDraft(type=ntype, content=stripped, ref=ref))

    return result


_VERIFIER_PROMPT = """\
You are the admission gate for a shared coordination board used by multiple \
AI agents working in parallel on one goal. Judge each candidate note against \
the bar for its type:

- FACT: concrete, reusable knowledge anchored to specifics (file, symbol, \
endpoint, error class, config key). Reject vague progress statements.
- FAIL: a dead end actually hit — what was tried and the observed reason. \
Reject speculation about what might fail.
- CLAIM: names one concrete work target the writer is taking on.
- RESULT: `outcome=…|evidence=…|risk=…` where the evidence describes a check \
that was ACTUALLY RUN with a concrete observed outcome (test ids + pass \
counts, command + output). Reject promises ("should work", "will verify", \
"TBD", "looks correct") and missing evidence.

Candidates:
{listing}

Reply with ONLY a JSON array, one object per candidate index, no prose:
[{{"index": 0, "keep": true, "reason": ""}}, …]
Set keep=false with a short reason whenever the note misses its bar.
"""


async def verify_notes_llm(
    drafts: list[NoteDraft],
    *,
    llm_client: Any,
    model: str,
    timeout_seconds: float,
) -> tuple[list[NoteDraft], list[tuple[int, str]]]:
    """LLM verification over pre-checked drafts.  FAIL-CLOSED.

    Returns ``(kept_drafts, rejected)`` where rejected pairs are
    ``(index_into_drafts, reason)``.  On any verifier failure every
    draft is rejected with :data:`VERIFICATION_UNAVAILABLE`.
    """
    if not drafts:
        return [], []

    listing = "\n".join(
        f"{i}: [{d.type}] {d.content}" for i, d in enumerate(drafts)
    )
    prompt = _VERIFIER_PROMPT.format(listing=listing)

    try:
        response = await asyncio.wait_for(
            llm_client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=1024,
            ),
            timeout=timeout_seconds,
        )
        text = (response.choices[0].message.content or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        verdicts = json.loads(text)
        if not isinstance(verdicts, list):
            raise ValueError("verifier reply is not a JSON array")
        by_index: dict[int, dict[str, Any]] = {}
        for v in verdicts:
            if isinstance(v, dict) and isinstance(v.get("index"), int):
                by_index[v["index"]] = v
    except Exception:
        logger.exception("board verifier call failed; rejecting batch (fail-closed)")
        return [], [(i, VERIFICATION_UNAVAILABLE) for i in range(len(drafts))]

    kept: list[NoteDraft] = []
    rejected: list[tuple[int, str]] = []
    for i, draft in enumerate(drafts):
        verdict = by_index.get(i)
        if verdict is None:
            # Fail-closed per note: no verdict means no admission.
            rejected.append((i, VERIFICATION_UNAVAILABLE))
        elif verdict.get("keep") is True:
            kept.append(draft)
        else:
            rejected.append(
                (i, str(verdict.get("reason") or "rejected by verifier"))
            )
    return kept, rejected
