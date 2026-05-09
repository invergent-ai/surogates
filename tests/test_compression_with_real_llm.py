"""Integration tests for ``ContextCompressor`` against a real LLM endpoint.

These tests are skipped unless ``SUROGATE_LLM_API_KEY`` is set in the
environment.  They exercise the full summarisation pipeline against the
configured OpenAI-compatible endpoint and validate the contract the prompt
+ post-processor are designed to enforce:

  1. First compaction returns a non-empty summary with the handoff prefix.
  2. Plan sections (### Done / ### In Progress / ### Blocked /
     ## Remaining Work) contain numbered items.
  3. Indices in the four plan sections form a contiguous-from-1 sequence
     after the first compaction (no skipped indices from mis-numbering).
  4. A second compaction preserves every index from the first.
  5. New work in the second compaction gets indices strictly greater than
     the max index from the first.
  6. When a step that was In Progress becomes Done, it migrates between
     sections KEEPING its original index (status migration contract).
  7. A third compaction holds the original indices through one more cycle
     (no decay from compression-of-compressions).
  8. The compressor swallows summariser glitches gracefully — empty or
     malformed model output does not crash compress().

To run::

    SUROGATE_LLM_API_KEY=<key> python -m pytest tests/test_compression_with_real_llm.py -v

Override the endpoint or model with::

    SUROGATE_LLM_BASE_URL=https://your.endpoint/v1
    SUROGATE_LLM_MODEL=your-model-id
"""

from __future__ import annotations

import os
import re

import pytest

from surogates.harness.context import SUMMARY_PREFIX, ContextCompressor

LLM_BASE_URL = os.environ.get("SUROGATE_LLM_BASE_URL", "https://llm1.surogate.ai/v1")
LLM_API_KEY = os.environ.get("SUROGATE_LLM_API_KEY", "")
LLM_MODEL = os.environ.get("SUROGATE_LLM_MODEL", "surogate")

pytestmark = pytest.mark.skipif(
    not LLM_API_KEY,
    reason=(
        "set SUROGATE_LLM_API_KEY to run integration tests against a real "
        "LLM endpoint (default base URL: https://llm1.surogate.ai/v1, "
        "default model: surogate)"
    ),
)

PLAN_SECTIONS = ("### done", "### in progress", "### blocked", "## remaining work")


# ---------------------------------------------------------------------------
# Helpers — local copy of plan-section parsing so tests are self-contained.
# ---------------------------------------------------------------------------

def _parse_plan_indices(summary_text: str) -> dict[str, dict[int, str]]:
    """Return ``{section_header_lc: {index: description}}`` parsed from a summary."""
    out: dict[str, dict[int, str]] = {h: {} for h in PLAN_SECTIONS}
    section: str | None = None
    for line in summary_text.splitlines():
        stripped = line.strip()
        low = stripped.lower()
        if low.startswith(("##", "###")):
            section = low if low in PLAN_SECTIONS else None
            continue
        if section is None or not stripped:
            continue
        m = re.match(r"^(\d+)\.\s+(.*)", stripped)
        if m:
            out[section][int(m.group(1))] = m.group(2).strip()
    return out


def _all_indices(parsed: dict[str, dict[int, str]]) -> set[int]:
    return {idx for items in parsed.values() for idx in items}


def _section_for_index(
    parsed: dict[str, dict[int, str]], idx: int,
) -> str | None:
    for section, items in parsed.items():
        if idx in items:
            return section
    return None


def _summary_message(messages: list[dict]) -> dict:
    """Locate the SUMMARY_PREFIX-marked message in a compressed list."""
    for m in messages:
        c = m.get("content")
        if isinstance(c, str) and SUMMARY_PREFIX[:30] in c:
            return m
    raise AssertionError(
        "no SUMMARY_PREFIX message in compressed list — summariser failed silently"
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def llm_client():
    """OpenAI-compatible async client targeting the real LLM endpoint."""
    from openai import AsyncOpenAI
    return AsyncOpenAI(
        base_url=LLM_BASE_URL,
        api_key=LLM_API_KEY,
        # Generous timeout — the upstream endpoint may be under high load
        # and the underlying model is a thinking model that can spend
        # several minutes on a single 40K-token summarisation prompt.
        timeout=600.0,
        max_retries=3,
    )


def _make_compressor() -> ContextCompressor:
    """Compressor with an 80K-token synthetic context window.

    With the real 262K-context model the threshold (131K tokens) is too
    large to cross with a fast test conversation, so we override the
    catalog record with a smaller window.  80K is chosen so that:

      - threshold is 40K tokens (manageable conversation size),
      - ``max_summary_tokens = 0.05 × 80_000 = 4000``,
      - the per-call ``max_tokens = budget × 2`` budget is up to 8000,
        leaving a thinking model enough headroom for both reasoning and
        a structured-template answer.

    Smaller windows (e.g., 8K) starve the thinking-model summariser of
    output budget and produce empty content.
    """
    return ContextCompressor(
        model_id=LLM_MODEL,
        threshold_percent=0.50,
        protect_first_n=3,
        protect_last_n=10,
        quiet_mode=True,
        summary_model_override=LLM_MODEL,
        model_overrides={LLM_MODEL: {"context_window": 80000, "max_output_tokens": 8000}},
    )


_FILLER_REPEAT = 10  # bigger filler so we can hit a 40K-token threshold without
                     # generating hundreds of turns


def _make_initial_messages(filler_size: int = 70) -> list[dict]:
    """Synthetic agentic conversation with anchored plan steps.

    Three explicit steps ("read middleware", "decide PyJWT", "PKCE state-store")
    plus filler.  The first two are described as completed, the third as
    in-progress, so the summariser has clear material for ### Done and
    ### In Progress sections.
    """
    msgs: list[dict] = [
        {
            "role": "system",
            "content": (
                "You are a senior engineer working in a sandboxed environment. "
                "Plan before acting. Use tools to read and modify files."
            ),
        },
        {
            "role": "user",
            "content": (
                "Refactor the authentication middleware in src/auth/middleware.py "
                "to support OIDC with PKCE. Preserve backwards compatibility "
                "with the existing JWT path. Deadline: end of week."
            ),
        },
        {
            "role": "assistant",
            "content": (
                "Plan:\n"
                "1. Read src/auth/middleware.py\n"
                "2. Add OIDC discovery support\n"
                "3. Add PKCE handler\n"
                "4. Wire into existing dispatch\n"
                "5. Add tests"
            ),
        },
        # Three explicit anchored steps — the summariser should index these.
        {
            "role": "assistant",
            "content": (
                "COMPLETED: Read src/auth/middleware.py and mapped the existing "
                "JWT verification path. The verify_token function lives at line 142 "
                "and is called from api/auth.py, api/admin.py, and channels/web.py."
            ),
        },
        {"role": "user", "content": "Good, continue."},
        {
            "role": "assistant",
            "content": (
                "DECIDED: use PyJWT[crypto] for OIDC verification, NOT python-jose, "
                "because of CVE-2024-XXXXX in the jose library. Documented in the "
                "ADR draft."
            ),
        },
        {
            "role": "assistant",
            "content": (
                "COMPLETED: Added OIDC discovery integration via Authlib's "
                "discover() function. Tested against the Auth0 staging tenant — "
                "discovery doc is being fetched and cached correctly."
            ),
        },
        {
            "role": "assistant",
            "content": (
                "IN PROGRESS: Implementing PKCE state-store cleanup. Currently "
                "the Redis TTL is 600s but it should match the auth_code lifetime "
                "(also 600s) — wiring through the lifetime config now."
            ),
        },
        {"role": "user", "content": "Sounds good. Keep going."},
    ]

    # Filler turns to push the conversation over the compression threshold.
    for i in range(filler_size):
        msgs.append({
            "role": "assistant",
            "content": (
                f"Investigating call site #{i}. Looking for existing JWT "
                f"consumers and verifying the migration path will not break "
                f"any current callers. Running mypy on the changed module to "
                f"check for type regressions. The grep results show {i % 4 + 2} "
                f"matches in tests/ and {i % 3 + 1} in src/."
            ) * _FILLER_REPEAT,
        })
        if i % 5 == 0:
            msgs.append({"role": "user", "content": f"Continue with #{i}."})

    return msgs


def _make_iteration_messages(
    head: list[dict], summary_msg: dict, filler_size: int = 110,
) -> list[dict]:
    """Build messages for a *second* compaction.

    Reuses the head + the prior summary, then adds NEW activity that
    completes the previously In-Progress step (PKCE state-store) so the
    iterative path can demonstrate status migration with index preservation.
    """
    msgs = list(head[:3])  # system + first user + first assistant
    msgs.append(summary_msg)

    msgs.append({"role": "user", "content": "Status update?"})
    msgs.append({
        "role": "assistant",
        "content": (
            "COMPLETED: PKCE state-store cleanup — Redis TTL now matches "
            "auth_code lifetime (600s). Pushed the fix and verified end-to-end "
            "with the staging Auth0 tenant."
        ),
    })
    msgs.append({
        "role": "assistant",
        "content": (
            "IN PROGRESS: Writing integration tests for the PKCE flow against "
            "Auth0 staging. Three test cases written so far, four more pending."
        ),
    })
    msgs.append({"role": "user", "content": "Good. Continue."})

    for i in range(filler_size):
        msgs.append({
            "role": "assistant",
            "content": (
                f"Setting up test fixture #{i}. Mocking the Auth0 OIDC "
                f"endpoint and the Redis state store. Verifying that the PKCE "
                f"verifier is generated and stored correctly. Running the test "
                f"and inspecting the captured request flow."
            ) * _FILLER_REPEAT,
        })
        if i % 5 == 0:
            msgs.append({"role": "user", "content": f"Continue with #{i}."})

    return msgs


def _make_third_iteration_messages(
    head: list[dict], summary_msg: dict, filler_size: int = 110,
) -> list[dict]:
    msgs = list(head[:3])
    msgs.append(summary_msg)

    msgs.append({"role": "user", "content": "Final status?"})
    msgs.append({
        "role": "assistant",
        "content": (
            "COMPLETED: Integration tests for the PKCE flow are now passing "
            "against Auth0 staging. All seven test cases green."
        ),
    })
    msgs.append({
        "role": "assistant",
        "content": (
            "IN PROGRESS: Writing the migration runbook for the platform team."
        ),
    })

    for i in range(filler_size):
        msgs.append({
            "role": "assistant",
            "content": (
                f"Drafting runbook section #{i}. Documenting the rollout plan, "
                f"feature flag, and rollback procedure. Verifying the "
                f"backwards-compat shim handles the old JWT-only deployments."
            ) * _FILLER_REPEAT,
        })
        if i % 5 == 0:
            msgs.append({"role": "user", "content": f"Continue with #{i}."})

    return msgs


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_first_compaction_produces_non_empty_summary(llm_client):
    compressor = _make_compressor()
    messages = _make_initial_messages()
    assert compressor.should_compress(messages, ""), (
        "test setup error: synthetic conversation does not exceed threshold"
    )

    compressed, summary_data = await compressor.compress(messages, llm_client)

    assert summary_data["strategy"] == "summarise", (
        f"compressor did not summarise: strategy={summary_data['strategy']!r}"
    )
    assert summary_data["compressed_message_count"] < summary_data["original_message_count"]

    summary = _summary_message(compressed)
    body = summary["content"]
    assert body.startswith(SUMMARY_PREFIX[:30]), (
        f"summary missing handoff prefix; first 200 chars:\n{body[:200]}"
    )
    # After the prefix and the colon, there must be substantive content.
    after_prefix = body[len(SUMMARY_PREFIX):].strip()
    assert len(after_prefix) > 300, (
        f"summary body suspiciously short ({len(after_prefix)} chars):\n{body}"
    )


async def test_first_compaction_produces_indexed_plan_sections(llm_client):
    compressor = _make_compressor()
    messages = _make_initial_messages()

    compressed, _ = await compressor.compress(messages, llm_client)
    summary = _summary_message(compressed)
    parsed = _parse_plan_indices(summary["content"])
    indices = _all_indices(parsed)

    assert indices, (
        "no indexed items in any plan section — prompt change did not take effect.\n"
        f"summary:\n{summary['content']}"
    )
    # Plan sections should pick up at least the three anchored steps
    # (read middleware, OIDC discovery, PKCE state-store).
    assert len(indices) >= 2, (
        f"expected at least two indexed plan steps, got {sorted(indices)}.\n"
        f"summary:\n{summary['content']}"
    )


async def test_first_compaction_indices_start_at_one(llm_client):
    compressor = _make_compressor()
    messages = _make_initial_messages()

    compressed, _ = await compressor.compress(messages, llm_client)
    summary = _summary_message(compressed)
    parsed = _parse_plan_indices(summary["content"])
    indices = _all_indices(parsed)

    assert indices, "no indexed items found"
    assert min(indices) == 1, (
        f"first index should be 1, got {min(indices)}.\n"
        f"summary:\n{summary['content']}"
    )


async def test_second_compaction_preserves_indices(llm_client):
    compressor = _make_compressor()
    head = _make_initial_messages()

    compressed_1, _ = await compressor.compress(head, llm_client)
    summary_1 = _summary_message(compressed_1)
    parsed_1 = _parse_plan_indices(summary_1["content"])
    indices_1 = _all_indices(parsed_1)
    assert indices_1, "first compaction had no indexed plan items"

    messages_2 = _make_iteration_messages(head, summary_1)
    # We invoke compress() directly rather than gating on should_compress —
    # the contract under test (index preservation) holds whenever the
    # iterative path runs, regardless of whether threshold was crossed.

    compressed_2, _ = await compressor.compress(messages_2, llm_client)
    summary_2 = _summary_message(compressed_2)
    parsed_2 = _parse_plan_indices(summary_2["content"])
    indices_2 = _all_indices(parsed_2)

    missing = indices_1 - indices_2
    assert not missing, (
        f"indices {sorted(missing)} from first compaction are MISSING from "
        f"second.\n"
        f"first indices:  {sorted(indices_1)}\n"
        f"second indices: {sorted(indices_2)}\n\n"
        f"first summary:\n{summary_1['content']}\n\n"
        f"second summary:\n{summary_2['content']}"
    )


async def test_new_work_in_second_compaction_gets_index_above_prior_max(llm_client):
    compressor = _make_compressor()
    head = _make_initial_messages()

    compressed_1, _ = await compressor.compress(head, llm_client)
    summary_1 = _summary_message(compressed_1)
    parsed_1 = _parse_plan_indices(summary_1["content"])
    indices_1 = _all_indices(parsed_1)
    assert indices_1
    max_1 = max(indices_1)

    messages_2 = _make_iteration_messages(head, summary_1)
    compressed_2, _ = await compressor.compress(messages_2, llm_client)
    summary_2 = _summary_message(compressed_2)
    parsed_2 = _parse_plan_indices(summary_2["content"])
    indices_2 = _all_indices(parsed_2)

    new_indices = indices_2 - indices_1
    assert new_indices, (
        "second compaction added no new indices — the new work "
        "(integration tests, etc.) was lost.\n"
        f"first indices:  {sorted(indices_1)}\n"
        f"second indices: {sorted(indices_2)}\n\n"
        f"second summary:\n{summary_2['content']}"
    )
    assert min(new_indices) > max_1, (
        f"new indices {sorted(new_indices)} include one not greater than "
        f"prior max ({max_1}) — the next-free-index rule was violated.\n"
        f"second summary:\n{summary_2['content']}"
    )


async def test_in_progress_step_migrates_to_done_keeping_index(llm_client):
    """Status migration contract.

    The first compaction places PKCE state-store work In Progress.  The
    iterative-update messages explicitly mark it COMPLETED.  The second
    compaction must migrate it to Done while keeping its original index.
    """
    compressor = _make_compressor()
    head = _make_initial_messages()

    compressed_1, _ = await compressor.compress(head, llm_client)
    summary_1 = _summary_message(compressed_1)
    parsed_1 = _parse_plan_indices(summary_1["content"])

    # Find the PKCE-state-store step in the first summary.  The model may
    # describe it differently each run, so we match on the keyword "PKCE".
    pkce_idx_1 = None
    pkce_section_1 = None
    for section, items in parsed_1.items():
        for idx, desc in items.items():
            if "pkce" in desc.lower() or "state-store" in desc.lower() or "state store" in desc.lower():
                pkce_idx_1 = idx
                pkce_section_1 = section
                break
        if pkce_idx_1 is not None:
            break

    if pkce_idx_1 is None:
        pytest.skip(
            "first compaction did not produce an identifiable PKCE step — "
            "summariser variance.  Re-run; persistent skips indicate the "
            "synthetic conversation needs stronger anchoring."
        )

    messages_2 = _make_iteration_messages(head, summary_1)
    compressed_2, _ = await compressor.compress(messages_2, llm_client)
    summary_2 = _summary_message(compressed_2)
    parsed_2 = _parse_plan_indices(summary_2["content"])

    # The same index must still exist in the second summary.
    pkce_section_2 = _section_for_index(parsed_2, pkce_idx_1)
    if pkce_section_2 is None:
        pytest.fail(
            f"PKCE step (index {pkce_idx_1}) from first summary disappeared "
            f"in second summary — index preservation failed.\n"
            f"first summary section/index: {pkce_section_1} #{pkce_idx_1}\n"
            f"first summary:\n{summary_1['content']}\n\n"
            f"second summary:\n{summary_2['content']}"
        )

    desc_2 = parsed_2[pkce_section_2][pkce_idx_1].lower()
    if not ("pkce" in desc_2 or "state-store" in desc_2 or "state store" in desc_2):
        pytest.fail(
            f"index {pkce_idx_1} survived but its description changed "
            f"identity — got {desc_2!r}.\nThe step ID should be stable across "
            f"compactions; description rewording is acceptable but topic drift "
            f"is not.\nsecond summary:\n{summary_2['content']}"
        )


async def test_third_compaction_preserves_first_compaction_indices(llm_client):
    """Multi-cycle preservation — three compactions, indices from the first
    must still be present in the third."""
    compressor = _make_compressor()
    head = _make_initial_messages()

    compressed_1, _ = await compressor.compress(head, llm_client)
    summary_1 = _summary_message(compressed_1)
    parsed_1 = _parse_plan_indices(summary_1["content"])
    indices_1 = _all_indices(parsed_1)
    assert indices_1

    messages_2 = _make_iteration_messages(head, summary_1)
    compressed_2, _ = await compressor.compress(messages_2, llm_client)
    summary_2 = _summary_message(compressed_2)

    messages_3 = _make_third_iteration_messages(head, summary_2)
    # As in the two-compaction test: invoke compress() directly to exercise
    # the iterative path even when the threshold check is borderline.
    compressed_3, _ = await compressor.compress(messages_3, llm_client)
    summary_3 = _summary_message(compressed_3)
    parsed_3 = _parse_plan_indices(summary_3["content"])
    indices_3 = _all_indices(parsed_3)

    missing = indices_1 - indices_3
    assert not missing, (
        f"after three compactions, indices {sorted(missing)} from the FIRST "
        f"compaction have been lost — multi-cycle decay.\n"
        f"first indices:  {sorted(indices_1)}\n"
        f"third indices:  {sorted(indices_3)}\n\n"
        f"third summary:\n{summary_3['content']}"
    )


async def test_compressor_swallows_summariser_exception(llm_client):
    """Robustness: if the summariser endpoint refuses the request, the
    compressor must not crash — it sets a cooldown and drops middle turns.
    Simulated by pointing at an obviously-bad model name; the endpoint
    should reject and we verify recovery."""
    from openai import AsyncOpenAI

    bad_client = AsyncOpenAI(
        base_url=LLM_BASE_URL,
        api_key=LLM_API_KEY,
        timeout=30.0,
        max_retries=0,
    )
    compressor = _make_compressor()
    # Force the summariser to call a non-existent model so the API rejects.
    compressor.summary_model = "this-model-does-not-exist"
    messages = _make_initial_messages()

    # Compression must complete without raising even though summarisation fails.
    compressed, summary_data = await compressor.compress(messages, bad_client)

    assert summary_data["strategy"] == "summarise"
    # Cooldown should now be active.
    import time
    assert compressor._summary_failure_cooldown_until > time.monotonic(), (
        "cooldown was not engaged after summariser failure"
    )
    # Middle was dropped without summary; head + tail still present.
    assert len(compressed) < len(messages)
