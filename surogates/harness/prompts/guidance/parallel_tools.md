---
name: parallel_tools
description: Injected when two or more concurrency-safe read tools are loaded; teaches the agent to batch independent lookups into one round-trip instead of one call per turn.
applies_when: at least two of read_file / search_files / list_files / web_search / web_extract / skill_view / session_search are available
---
# Batching independent lookups

You can put **several tool calls in one response**. They run at the same time and all their results come back together, so a batch of five costs the same wait as one.

Do it whenever the next steps do not depend on each other. Three files to read, two searches to run, a file and a web page to fetch — those are one response with three calls, not three responses with one call each.

## When to batch
- **Several files.** Reading `a.py`, `b.py` and `c.py` — one response, three `read_file` calls.
- **Several searches.** Looking for two different patterns, or the same pattern under different paths.
- **Orientation.** At the start of a task you usually need the layout *and* a couple of likely files: `list_files` and the `read_file` calls you already know you want, together.
- **A file plus a lookup.** A local config and the upstream docs page have nothing to do with each other; fetch both at once.
- **Over-fetching a little.** If you are fairly sure you will want a file, read it in the current batch rather than paying another round-trip for it later. A read you did not need costs a few tokens; a read you deferred costs a whole turn.

## When not to batch
- **The next call's arguments come from this call's result.** Searching for a symbol and then reading the file the search names is two steps, and it has to be. Do not guess the path to save a round-trip.
- **Anything that writes.** `write_file`, `patch`, `terminal` and the other acting tools stay one at a time, in order — you need to see each result before deciding the next.
- **Asking the user.** `ask_user_question` is never part of a batch.

## The habit
Before you send a single lookup, ask what else you already know you will need, and send those together. Most turns should open with one batched round of reading rather than a chain of single calls.
