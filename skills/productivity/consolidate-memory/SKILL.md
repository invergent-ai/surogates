---
name: consolidate-memory
description: "Reflective pass over your memory — merge duplicates, fix stale facts, prune what no longer matters. Use when the user asks to clean up or review what you remember, or when memory has grown cluttered or contradictory."
requires_tools:
  - memory
---

# Memory Consolidation

You're doing a reflective pass over what you've learned about this user and their work. The goal: a future session should be able to orient quickly — who they work with, what they're focused on, how they like things done — without re-asking.

Memory is two lists of entries, edited with the `memory` tool: `user` (who the user is, their preferences) and `memory` (your notes on their projects, conventions and workflows). Both are injected into every turn of every future session, so every entry costs context forever.

## Phase 1 — Take stock

- Read both lists as your system prompt shows them. That is the state when this session started; account for anything you wrote since.
- Note which entries overlap, which look stale, which are thin, and which sit in the wrong target.

## Phase 2 — Consolidate

**Separate the durable from the dated.** Preferences, working style, key relationships, and recurring workflows are durable — keep and sharpen them. Specific projects, deadlines, and one-off tasks are dated — if the date has passed or the work is done, remove the entry or fold the lasting takeaway (e.g. "user prefers X format for launch docs") into a durable one.

**Merge overlaps.** If two entries describe the same person, project, or preference, `replace` the richer one with the merged text and `remove` the other.

**Fix time references.** Convert "next week", "this quarter", "by Friday" to absolute dates so they stay readable later.

**Drop what's easy to re-find.** Task progress, session outcomes, and completed-work logs don't belong in memory — `session_search` recalls those from past transcripts. Cut entries that restate something you could pull from the user's files or connected tools on demand. Keep what's hard to re-derive: stated preferences, context behind a decision, who to go to for what.

**Keep the user's word.** Never drop or soften a preference the user stated unless a newer statement of theirs contradicts it; when two contradict, keep the newer and say so in the summary.

**One fact per entry, written compactly.** `replace` identifies an entry by a short unique substring (`old_text`) — pick one that appears in that entry only.

Finish with a short summary: how many entries you merged, rewrote, and removed, and what changed.
