"""Inspect ContextCompressor behaviour against real or synthetic conversations.

Walks a conversation through the compressor and prints, at each compression
event, what survived verbatim, what got summarised, and what was dropped.
Designed to expose the post-compression message list a fine-tuned model
will actually see during 8-hour autonomous sessions.

Companion to docs/architecture/compression-characterization.md.

Usage:
    # Synthetic 200-turn conversation, default mock summariser, single compaction
    python scripts/inspect_compression.py --synthetic 200

    # 600 turns to force re-compression cycles
    python scripts/inspect_compression.py --synthetic 600

    # Real conversation log (JSON list of OpenAI-shaped messages)
    python scripts/inspect_compression.py --conversation logs/run42.json

    # Hit a real OpenAI-compatible summariser (gpt-4o-mini by default)
    python scripts/inspect_compression.py --synthetic 400 \\
        --real-summariser --base-url https://api.openai.com/v1 \\
        --api-key $OPENAI_API_KEY --summariser-model gpt-4o-mini

    # See what the as-shipped (broken) compressor does — will crash with NameError
    python scripts/inspect_compression.py --synthetic 200 --no-patch-name-error
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

# Surogates package import — assumes script is run from repo root with installed deps.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from surogates.harness import context as compressor_module  # noqa: E402
from surogates.harness.context import (  # noqa: E402
    SUMMARY_PREFIX,
    ContextCompressor,
    _estimate_messages_tokens_rough,
)


# ---------------------------------------------------------------------------
# As-shipped compressor has a NameError on `get_model_info` at line 438.
# We patch the symbol into the module's globals (without modifying source)
# so the full algorithm can run. Disable with --no-patch-name-error to
# observe the production crash.
# ---------------------------------------------------------------------------

def patch_name_error() -> None:
    from surogates.harness.model_metadata import resolve_model_info as _r
    compressor_module.get_model_info = _r  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Mock summariser — emulates the LLM by producing a structured summary
# template with stable, identifiable section bodies. Not faithful to a real
# model's output, but lets the user see the algorithm's behaviour without
# hitting an API. Returns (summary_text, usage_dict).
# ---------------------------------------------------------------------------

class MockSummariserClient:
    """Mimics the OpenAI-style chat.completions.create interface."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.chat = self  # so client.chat.completions.create works
        self.completions = self

    async def create(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
    ) -> Any:
        prompt = messages[0]["content"]
        is_iterative = "PREVIOUS SUMMARY:" in prompt
        self.calls.append(
            {"model": model, "max_tokens": max_tokens, "iterative": is_iterative}
        )

        # Heuristically pull out the conversation slice the compressor sent.
        marker = "TURNS TO SUMMARIZE:" if not is_iterative else "NEW TURNS TO INCORPORATE:"
        slice_text = prompt.split(marker, 1)[-1].split("Use this exact structure:", 1)[0]

        # Mimic what a real summariser would do: extract sentences carrying
        # anchor-style markers (decisions, completions, evidence, dead-ends).
        # This is what makes anchor survival meaningful when running against
        # the mock — a real model will hit roughly the same patterns.
        decisions: list[str] = []
        completions: list[str] = []
        in_progress: list[str] = []
        dead_ends: list[str] = []
        evidence: list[str] = []
        files_seen: set[str] = set()

        import re as _re
        path_re = _re.compile(r"[\w/\.\-]+\.(?:py|ts|tsx|js|md|yaml|json|toml|sql)")

        for block in slice_text.split("\n\n"):
            for sent in block.split(". "):
                s = sent.strip()
                if not s:
                    continue
                up = s.upper()
                if "DECIDED" in up or "DECISION" in up:
                    decisions.append(s[:300])
                elif "COMPLETED" in up or "DONE:" in up:
                    completions.append(s[:300])
                elif "IN PROGRESS" in up:
                    in_progress.append(s[:300])
                elif "DEAD-END" in up or "ABANDONED" in up:
                    dead_ends.append(s[:300])
                elif "FOUND" in up or "GREP RESULT" in up or "ERROR" in up:
                    evidence.append(s[:300])
            for p in path_re.findall(block):
                files_seen.add(p)

        n_blocks = sum(1 for b in slice_text.split("\n\n") if b.strip())

        # Plan-step indexing: parse the PREVIOUS SUMMARY (if iterative) so the
        # mock reproduces the index-preservation behaviour the new prompt
        # asks for. A real summariser is expected to do the same; the mock
        # mirrors it here so the harness can verify end-to-end.
        prev_summary_block = ""
        if is_iterative:
            prev_summary_block = (
                prompt.split("PREVIOUS SUMMARY:", 1)[-1]
                .split("NEW TURNS TO INCORPORATE:", 1)[0]
            )

        # 1. Reserve indices that already appeared anywhere in the previous
        #    summary's plan sections (Done / In Progress / Blocked / Remaining).
        existing: dict[str, int] = {}  # description-key -> index
        plan_section = None
        for line in prev_summary_block.splitlines():
            stripped = line.strip()
            low = stripped.lower()
            if low.startswith("###") or low.startswith("##"):
                if low in ("### done", "### in progress", "### blocked",
                          "## remaining work"):
                    plan_section = low
                else:
                    plan_section = None
                continue
            if plan_section is None or not stripped:
                continue
            m = _re.match(r"^(\d+)\.\s+(.*)", stripped)
            if not m:
                continue
            idx = int(m.group(1))
            # Strip status tag for description-key lookup.
            desc = _re.sub(r"\s*\[[^\]]*\]\s*$", "", m.group(2)).strip().lower()
            if desc:
                existing[desc[:60]] = idx

        next_free = max(existing.values(), default=0) + 1

        def _index_for(desc: str) -> int:
            nonlocal next_free
            key = desc.strip().lower()[:60]
            if key in existing:
                return existing[key]
            chosen = next_free
            next_free += 1
            existing[key] = chosen
            return chosen

        # 2. Build indexed plan sections. To exercise status migration:
        #    - things in completions[] go to ### Done (with status tag)
        #    - things in in_progress[] go to ### In Progress
        #    - if an item appeared in In Progress in the previous summary
        #      AND it is now in completions[], it migrates to Done with the
        #      SAME index — verifying the preservation contract.
        prev_in_progress_keys: set[str] = set()
        for line in prev_summary_block.splitlines():
            m = _re.match(r"^\s*(\d+)\.\s+(.*)", line)
            if m and "[In Progress]" in line:
                key = _re.sub(r"\s*\[[^\]]*\]\s*$", "", m.group(2)).strip().lower()[:60]
                prev_in_progress_keys.add(key)

        done_lines: list[str] = []
        in_prog_lines: list[str] = []
        # Items completed *now*: items that either match a previous in-progress
        # entry (migrate) or are net-new completions.
        for c in completions[:8]:
            idx = _index_for(c)
            done_lines.append(f"{idx}. {c} [Done]")
        # In-progress items that did NOT just complete.
        for ip in in_progress[:6]:
            key = ip.strip().lower()[:60]
            if key in {c.strip().lower()[:60] for c in completions[:8]}:
                continue  # it migrated to Done
            idx = _index_for(ip)
            in_prog_lines.append(f"{idx}. {ip} [In Progress]")

        # Carry forward previous Remaining Work items, untouched.
        remaining_lines: list[str] = []
        plan_section = None
        for line in prev_summary_block.splitlines():
            stripped = line.strip()
            low = stripped.lower()
            if low.startswith(("##", "###")):
                plan_section = low if low == "## remaining work" else None
                continue
            if plan_section == "## remaining work" and stripped:
                m = _re.match(r"^\d+\.\s+", stripped)
                if m:
                    remaining_lines.append(stripped)

        # Add a new remaining-work item on iterative calls so we can verify
        # next-free-index assignment.
        if is_iterative:
            new_desc = "Validate against staging tenant"
            remaining_lines.append(f"{_index_for(new_desc)}. {new_desc}")
        elif decisions:
            # First compaction: seed Remaining with one item so it is non-empty.
            seed_desc = "Carry remaining decisions through to implementation"
            remaining_lines.append(f"{_index_for(seed_desc)}. {seed_desc}")

        body = textwrap.dedent(
            f"""\
            ## Goal
            (mock) Continue the user's task from the conversation context.

            ## Constraints & Preferences
            (mock) None explicitly captured.

            ## Progress
            ### Done
            {chr(10).join(done_lines) or '(mock) (no completion markers found)'}
            ### In Progress
            {chr(10).join(in_prog_lines) or '(mock) (no in-progress markers found)'}
            ### Blocked
            (mock) None recorded.

            ## Key Decisions
            {chr(10).join('- ' + d for d in decisions[:8]) or '(mock) (no decision markers found)'}

            ## Resolved Questions
            (mock) Earlier user questions assumed answered.

            ## Pending User Asks
            None.

            ## Relevant Files
            {chr(10).join('- ' + f for f in sorted(files_seen)[:30]) or '(mock) (no file paths found)'}

            ## Remaining Work
            {chr(10).join(remaining_lines) or '(mock) (no remaining work tracked)'}

            ## Critical Context
            Iterative summary update: {is_iterative}. {n_blocks} prior blocks compressed.
            {chr(10).join('- ' + e for e in evidence[:6])}
            Dead-ends to avoid:
            {chr(10).join('- ' + d for d in dead_ends[:6]) or '(mock) (none found)'}

            ## Tools & Patterns
            (mock) Tool calls in the summarised range condensed to this summary.
            """
        )

        @dataclass
        class _Msg:
            content: str

        @dataclass
        class _Choice:
            message: _Msg

        @dataclass
        class _Resp:
            choices: list[_Choice]

        return _Resp(choices=[_Choice(message=_Msg(content=body))])


# ---------------------------------------------------------------------------
# Synthetic conversation generator. Emits "anchor" facts at known positions
# so the user can audit what survives compression.
# ---------------------------------------------------------------------------

@dataclass
class Anchor:
    turn_index: int
    role: str
    label: str            # short tag like "INITIAL_TASK_SPEC"
    content: str

    def __str__(self) -> str:
        return f"[anchor #{self.turn_index} {self.role}/{self.label}]"


@dataclass
class SyntheticConversation:
    messages: list[dict[str, Any]] = field(default_factory=list)
    anchors: list[Anchor] = field(default_factory=list)


def make_synthetic(n_turns: int, *, seed: int = 42) -> SyntheticConversation:
    """Generate a deterministic synthetic agentic conversation with anchored facts.

    Anchors are placed early, mid, and just before the typical tail boundary
    so the user can read the post-compression output and check survival.
    """
    rng = random.Random(seed)
    conv = SyntheticConversation()

    def emit(role: str, content: str, *, tool_calls: list[dict] | None = None,
             tool_call_id: str | None = None, anchor: str | None = None) -> None:
        msg: dict[str, Any] = {"role": role, "content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        if tool_call_id:
            msg["tool_call_id"] = tool_call_id
        if anchor:
            conv.anchors.append(Anchor(len(conv.messages), role, anchor, content))
        conv.messages.append(msg)

    # System prompt (head, always preserved).
    emit("system",
         "You are a senior software engineer working in a sandboxed environment. "
         "Use tools to read and modify files. Plan before acting.",
         anchor="SYSTEM_PROMPT")

    # Initial task spec — first user message, in protect_first_n.
    emit("user",
         "I need you to refactor the authentication middleware to support "
         "OIDC with PKCE. The token verification function lives in "
         "src/auth/middleware.py. Preserve backwards compatibility with the "
         "existing JWT path. The deadline is end of week.",
         anchor="INITIAL_TASK_SPEC")

    # First assistant ack.
    emit("assistant",
         "I'll start by reading the current middleware and mapping out the "
         "JWT verification path. Plan: (1) read existing code, (2) add OIDC "
         "discovery, (3) add PKCE handler, (4) wire into existing dispatch.",
         anchor="INITIAL_PLAN")

    # Now generate body turns, planting anchors at strategic depths.
    anchor_plan = {
        15: ("decision", "DECIDED to use PyJWT[crypto] for OIDC, NOT python-jose, because of CVE-2024-XXXXX in jose"),
        25: ("dead_end", "DEAD-END: tried using authlib's OIDC client; it requires Flask, we are FastAPI. Abandoned."),
        45: ("tool_evidence", "Found that token expiry is hardcoded to 3600s in line 142 of middleware.py — the user wanted configurable"),
        70: ("subgoal_done", "COMPLETED: OIDC discovery endpoint integration, tested against Auth0 staging tenant."),
        90: ("subgoal_open", "IN PROGRESS: PKCE state-store cleanup. The Redis TTL is set to 600s but should match auth_code lifetime."),
        110: ("decision", "DECIDED to keep the existing JWT cache but invalidate on OIDC refresh — see ADR draft."),
        130: ("tool_evidence", "GREP RESULT: only 3 callers of verify_token in the codebase: api/auth.py, api/admin.py, channels/web.py"),
        160: ("subgoal_done", "COMPLETED: backwards-compat shim for legacy JWT-only deployments."),
    }

    for i in range(4, n_turns):
        anchor_label = None
        anchor_content = None
        if i in anchor_plan:
            kind, txt = anchor_plan[i]
            anchor_label = f"{kind.upper()}_at_{i}"
            anchor_content = txt

        kind = rng.choice(["assistant_text", "assistant_tool", "tool_result", "user"])
        if kind == "user" and rng.random() < 0.85:
            kind = "assistant_text"  # users rarely speak in agentic loops

        if kind == "assistant_text":
            content = anchor_content or rng.choice([
                "Reading the file now to confirm structure.",
                "The grep returned 14 matches, all in tests.",
                "I'll patch the imports and re-run the type checker.",
                "Let me search for any other callers of this function.",
                "The implementation looks correct; proceeding to add tests.",
                "Running mypy on the changed module.",
                "I see the issue — the iterator was being consumed twice.",
            ]) + " " + ("filler " * rng.randint(20, 80))
            emit("assistant", content, anchor=anchor_label)

        elif kind == "assistant_tool":
            call_id = f"call_{uuid4().hex[:12]}"
            tool_name = rng.choice(["read_file", "search_files", "patch", "terminal", "list_files"])
            args = json.dumps(rng.choice([
                {"path": f"src/auth/{rng.choice(['middleware.py', 'oidc.py', 'pkce.py'])}"},
                {"pattern": "verify_token", "path": "src/"},
                {"command": rng.choice(["pytest tests/auth -x", "mypy src/auth", "ruff check"])},
            ]))
            tc = [{"id": call_id, "type": "function",
                   "function": {"name": tool_name, "arguments": args}}]
            emit("assistant", anchor_content or "Calling tool.",
                 tool_calls=tc, anchor=anchor_label)

            # Synthesise a tool result on the next turn.
            payload = anchor_content or (
                f"Tool {tool_name} ran successfully.\n" +
                ("output line " * rng.randint(50, 250))
            )
            emit("tool", payload, tool_call_id=call_id)

        elif kind == "tool_result":
            # Orphan tool results are sanitised; produce a paired call instead.
            call_id = f"call_{uuid4().hex[:12]}"
            tc = [{"id": call_id, "type": "function",
                   "function": {"name": "read_file",
                                "arguments": json.dumps({"path": "src/main.py"})}}]
            emit("assistant", "Reading the file.", tool_calls=tc)
            emit("tool",
                 anchor_content or ("file content " * rng.randint(20, 80)),
                 tool_call_id=call_id, anchor=anchor_label)

        else:  # user clarification
            emit("user", anchor_content or "Looks good, please continue.",
                 anchor=anchor_label)

    return conv


# ---------------------------------------------------------------------------
# Diff renderer — pretty-prints what changed across a compression event.
# ---------------------------------------------------------------------------

def short(content: Any, *, width: int = 90) -> str:
    if not isinstance(content, str):
        content = str(content)
    content = content.replace("\n", " ⏎ ")
    if len(content) <= width:
        return content
    return content[: width - 3] + "..."


def role_tag(msg: dict[str, Any]) -> str:
    role = msg.get("role", "?")
    if role == "assistant" and msg.get("tool_calls"):
        names = [tc.get("function", {}).get("name", "?") for tc in msg["tool_calls"]]
        return f"assistant({','.join(names)})"
    if role == "tool":
        return f"tool[{msg.get('tool_call_id', '')[:8]}]"
    return role


def is_summary_message(msg: dict[str, Any]) -> bool:
    content = msg.get("content") or ""
    return isinstance(content, str) and SUMMARY_PREFIX[:30] in content


def survival_report(
    pre: list[dict[str, Any]],
    post: list[dict[str, Any]],
    anchors: list[Anchor],
) -> str:
    """For each anchor in pre, report whether its content survives in post."""
    pre_to_anchor = {}
    for a in anchors:
        if a.turn_index < len(pre):
            pre_to_anchor[a.turn_index] = a

    # Find which post messages are head, summary, or tail (by content match).
    summary_text = ""
    for msg in post:
        if is_summary_message(msg):
            summary_text = msg.get("content") or ""
            break

    surviving_contents = []
    for msg in post:
        if is_summary_message(msg):
            continue
        c = msg.get("content")
        if isinstance(c, str):
            surviving_contents.append(c)

    lines = []
    for a in anchors:
        probe = a.content[:30]
        # Try verbatim survival first
        verbatim = any(a.content[:60] in c for c in surviving_contents)
        # Then check whether the summariser preserved it (shorter probe to
        # tolerate sentence-boundary truncation by summarisers).
        in_summary = probe in summary_text or a.label in summary_text
        if verbatim:
            verdict = "VERBATIM (in head or tail)"
        elif in_summary:
            verdict = "IN SUMMARY"
        else:
            verdict = "DROPPED"
        lines.append(
            f"  turn={a.turn_index:>4} {a.label:<28} {verdict:<26} preview={short(a.content, width=60)}"
        )
    return "\n".join(lines)


PLAN_SECTION_HEADERS = ("### done", "### in progress", "### blocked", "## remaining work")


def parse_plan_indices(summary_text: str) -> dict[str, dict[int, str]]:
    """Return ``{section_header: {index: description}}`` parsed from a summary.

    Used to verify that the new prompt + post-processor produces
    indexed plan sections, and that indices are preserved between
    compaction events.
    """
    import re as _re
    out: dict[str, dict[int, str]] = {h: {} for h in PLAN_SECTION_HEADERS}
    section: str | None = None
    for line in summary_text.splitlines():
        stripped = line.strip()
        low = stripped.lower()
        if low.startswith(("##", "###")):
            section = low if low in PLAN_SECTION_HEADERS else None
            continue
        if section is None or not stripped:
            continue
        m = _re.match(r"^(\d+)\.\s+(.*)", stripped)
        if m:
            out[section][int(m.group(1))] = m.group(2).strip()
    return out


def render_plan_indices(parsed: dict[str, dict[int, str]]) -> None:
    print()
    print("  PLAN-SECTION INDICES (post-normalisation):")
    any_indexed = False
    for header in PLAN_SECTION_HEADERS:
        items = parsed[header]
        if not items:
            print(f"    {header:<22} (empty)")
            continue
        any_indexed = True
        for idx in sorted(items):
            preview = items[idx][:80]
            print(f"    {header:<22} {idx:>2}. {preview}")
    if not any_indexed:
        print("    ⚠ no indexed items found in any plan section — "
              "format change did not take effect")


def diff_plan_indices(
    before: dict[str, dict[int, str]] | None,
    after: dict[str, dict[int, str]],
) -> None:
    """Compare two summaries' plan indices and report preservation."""
    if before is None:
        return
    before_all: dict[int, str] = {}
    for items in before.values():
        before_all.update(items)
    after_all: dict[int, str] = {}
    for items in after.values():
        after_all.update(items)

    preserved = sorted(set(before_all) & set(after_all))
    dropped = sorted(set(before_all) - set(after_all))
    added = sorted(set(after_all) - set(before_all))

    print()
    print("  INDEX DIFF vs PREVIOUS COMPACTION:")
    if preserved:
        print(f"    preserved indices: {preserved}")
    else:
        print("    ⚠ NO indices preserved — index identity broke across compaction")
    if added:
        max_before = max(before_all, default=0)
        all_above = all(i > max_before for i in added)
        marker = "✓" if all_above else "⚠ NOT all > previous max"
        print(f"    new indices: {added}  ({marker} previous max was {max_before})")
    if dropped:
        print(f"    dropped indices: {dropped}")

    # Section migration detection.
    section_changes: list[str] = []
    before_section = {idx: sec for sec, items in before.items() for idx in items}
    after_section = {idx: sec for sec, items in after.items() for idx in items}
    for idx in preserved:
        b, a = before_section.get(idx), after_section.get(idx)
        if b != a:
            section_changes.append(f"{idx}: {b} → {a}")
    if section_changes:
        print("    section migrations: " + "; ".join(section_changes))


def render_event(
    ev: int,
    pre: list[dict[str, Any]],
    post: list[dict[str, Any]],
    summary_data: dict[str, Any],
    anchors: list[Anchor],
    *,
    previous_plan: dict[str, dict[int, str]] | None = None,
) -> dict[str, dict[int, str]] | None:
    print()
    print("=" * 100)
    print(f"COMPRESSION EVENT #{ev}")
    print("=" * 100)
    print(
        f"  pre:  {summary_data.get('original_message_count')} msgs / "
        f"~{summary_data.get('original_token_estimate')} tokens (rough)"
    )
    print(
        f"  post: {summary_data.get('compressed_message_count')} msgs / "
        f"~{summary_data.get('compressed_token_estimate')} tokens (rough)"
    )
    print(f"  strategy: {summary_data.get('strategy')}")

    summary_msg_idx = next(
        (i for i, m in enumerate(post) if is_summary_message(m)), None
    )
    if summary_msg_idx is None:
        print("  ⚠ NO SUMMARY MESSAGE in post — middle turns dropped without replacement.")
    else:
        print(
            f"  summary inserted at post[{summary_msg_idx}] as "
            f"role={post[summary_msg_idx]['role']!r} "
            f"({len(post[summary_msg_idx].get('content') or '')} chars)"
        )

    # Show post message list (compact).
    print()
    print("  POST MESSAGE LIST (role tags):")
    for i, msg in enumerate(post):
        marker = "  ★ SUMMARY ★ " if is_summary_message(msg) else "             "
        print(f"    {marker}post[{i:>3}] {role_tag(msg):<30} {short(msg.get('content') or '', width=60)}")

    # Show the actual summary text (one of the highest-leverage things to read).
    if summary_msg_idx is not None:
        print()
        print("  SUMMARY TEXT (verbatim, the model will see this):")
        print()
        for line in (post[summary_msg_idx]["content"] or "").splitlines():
            print(f"    │ {line}")

    # Anchor survival report.
    if anchors:
        print()
        print("  ANCHOR SURVIVAL (was the planted fact preserved?):")
        print(survival_report(pre, post, anchors))

    # Plan-section indices: prove the indexed-list format and index
    # preservation across compactions are working.
    parsed: dict[str, dict[int, str]] | None = None
    if summary_msg_idx is not None:
        parsed = parse_plan_indices(post[summary_msg_idx]["content"] or "")
        render_plan_indices(parsed)
        diff_plan_indices(previous_plan, parsed)
    return parsed


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

async def run_inspection(args: argparse.Namespace) -> int:
    if args.synthetic:
        conv = make_synthetic(args.synthetic, seed=args.seed)
        print(f"[generated synthetic conversation: {len(conv.messages)} messages, "
              f"{len(conv.anchors)} anchors, "
              f"~{_estimate_messages_tokens_rough(conv.messages)} tokens rough]")
    elif args.conversation:
        path = Path(args.conversation)
        raw = json.loads(path.read_text())
        if isinstance(raw, dict) and "messages" in raw:
            raw = raw["messages"]
        conv = SyntheticConversation(messages=raw, anchors=[])
        print(f"[loaded conversation: {len(conv.messages)} messages from {path}]")
    else:
        print("error: --synthetic N or --conversation PATH required", file=sys.stderr)
        return 2

    if not args.no_patch_name_error:
        patch_name_error()
    else:
        print("⚠ --no-patch-name-error: running as-shipped compressor; "
              "expect NameError on first compression.")

    # Build summariser client.
    if args.real_summariser:
        try:
            from openai import AsyncOpenAI
        except ImportError:
            print("error: install openai (`uv add openai`) for --real-summariser", file=sys.stderr)
            return 2
        client = AsyncOpenAI(base_url=args.base_url, api_key=args.api_key or os.environ.get("OPENAI_API_KEY", ""))
    else:
        client = MockSummariserClient()

    # Build compressor.
    compressor = ContextCompressor(
        model_id=args.model,
        threshold_percent=args.threshold,
        protect_first_n=args.head_n,
        protect_last_n=args.tail_n,
        summary_target_ratio=args.summary_ratio,
        quiet_mode=False,
        summary_model_override=args.summariser_model or None,
    )

    print(f"[compressor: model={args.model} ctx={compressor.context_length} "
          f"threshold={compressor.threshold_tokens} ({args.threshold * 100:.0f}%) "
          f"tail_budget={compressor.tail_token_budget}]")

    messages = list(conv.messages)
    event = 0
    previous_plan: dict[str, dict[int, str]] | None = None
    while True:
        if not compressor.should_compress(messages, ""):
            print(f"[no further compression needed — "
                  f"~{_estimate_messages_tokens_rough(messages)} tokens "
                  f"< {compressor.threshold_tokens} threshold]")
            break

        if event >= args.max_events:
            print(f"[reached --max-events {args.max_events}, stopping]")
            break

        event += 1
        pre = [m.copy() for m in messages]
        try:
            post, summary_data = await compressor.compress(messages, client)
        except Exception as e:
            print(f"\n‼ compression event #{event} CRASHED: "
                  f"{type(e).__name__}: {e}")
            if args.no_patch_name_error:
                print("  (this is the production behaviour — see "
                      "docs/architecture/compression-characterization.md §7.1)")
            return 1

        previous_plan = render_event(
            event, pre, post, summary_data, conv.anchors,
            previous_plan=previous_plan,
        )
        messages = post

        # If user wants only one event, stop here.
        if args.events == 1:
            break

        # To force re-compression, append synthetic bulk turns.
        if event < args.max_events and compressor.should_compress(messages, ""):
            continue
        if event < args.events:
            print("\n[appending bulk filler turns to force re-compression…]")
            # Append enough to clearly cross the threshold again. 400 turns
            # of synthetic body run ~80K tokens at default sizing.
            filler = make_synthetic(400, seed=args.seed + event).messages[3:]
            messages.extend(filler)

    print("\n[done]")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Inspect ContextCompressor behaviour.")

    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--synthetic", type=int, metavar="N",
                     help="generate a synthetic N-turn conversation with planted anchors")
    src.add_argument("--conversation", type=Path, metavar="PATH",
                     help="path to a JSON file (list of OpenAI-shaped messages, "
                          "or {messages: […]})")

    p.add_argument("--model", default="gpt-4o",
                   help="model id whose context window we compress against (default: gpt-4o, ~128K)")
    p.add_argument("--threshold", type=float, default=0.50,
                   help="threshold_percent (default 0.50)")
    p.add_argument("--head-n", type=int, default=3, dest="head_n",
                   help="protect_first_n (default 3)")
    p.add_argument("--tail-n", type=int, default=20, dest="tail_n",
                   help="protect_last_n (default 20)")
    p.add_argument("--summary-ratio", type=float, default=0.20, dest="summary_ratio",
                   help="summary_target_ratio (default 0.20)")

    p.add_argument("--seed", type=int, default=42,
                   help="RNG seed for synthetic conversation (default 42)")
    p.add_argument("--events", type=int, default=99,
                   help="max compression events to run before stopping. Set 1 to "
                        "see only the first event; default 99 lets the run go "
                        "until no further compression is needed.")
    p.add_argument("--max-events", type=int, default=10, dest="max_events",
                   help="hard cap on compression events (default 10)")

    p.add_argument("--real-summariser", action="store_true",
                   help="use a real OpenAI-compatible API instead of the mock")
    p.add_argument("--base-url", default="https://api.openai.com/v1",
                   help="OpenAI-compatible base URL (with --real-summariser)")
    p.add_argument("--api-key", default="",
                   help="API key (with --real-summariser; falls back to OPENAI_API_KEY env)")
    p.add_argument("--summariser-model", default="",
                   help="override summariser model (default gpt-4o-mini in the compressor)")

    p.add_argument("--no-patch-name-error", action="store_true",
                   help="do NOT patch the get_model_info NameError; observe production crash behaviour")

    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return asyncio.run(run_inspection(args))


if __name__ == "__main__":
    sys.exit(main())
