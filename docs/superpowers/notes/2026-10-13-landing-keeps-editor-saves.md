# How a landing treats a project file saved from the editor

**Date:** 2026-10-13. Verify item 1 of `docs/superpowers/specs/2026-10-13-office-editors-design.md`, cloud projects only.
**Code read:** surogates `origin/master` at `e44c0a105`, worktree `.worktrees/office-editors-platform`. Every `file:line` below is from that commit.

**Editing project files from the editor is safe.**

## What the editor writes

The editor route gets the session's files from `workspace_files` → `session_files`. For a cloud session, that is a
`StorageWorkspaceIO` on `boundary_workspace_prefix(...)` (`surogates/session/files.py:111-114`). A project session's
boundary is `workstream:<id>`, so its prefix is `boundaries/workstream:<id>/workspace/` (`surogates/storage/tenant.py:161-168`).
That prefix holds the project's real files, the same files a thread's pod mounts at `/project`.

So an editor save, like an upload today (`surogates/api/routes/workspace.py:747`, also through `workspace_files`),
writes a project's real file in the bucket directly. It does not go through a thread or a history step.
`BucketHistory` (`surogates/workstreams/bucket.py`) is only used for landings you start yourself (Restore, `workstreams/undo.py`).
The upload route and the plan's `write_if` do not use it.

## Q1: Does `pickup` commit every real-file change since `main`, as "you"?

**Yes.**

- The landing's first step is `pickup` with `author=you` (`surogates/harness/landing.py:821`). Here
  `you = {"name": session.user_id, "email": "user:<user_id>@surogate"}` (`landing.py:776`). A project has a
  single owner (`Workstream.user_id`, `surogates/db/models.py:2373-2395`), so "you" is the user who did the editing.
- `History.pickup` (`surogates/sandbox/history.py:439-481`) reads `main` from the durable history (`:454-458`).
  It then builds the index from the real files as they are now (`_read_real(on)`, `:461`). This step tells geesefs
  to drop its cache for each of `main`'s folders (`:415-418`), and it reads every file whose size or time changed
  (`_add_all` → `_read_at_once`, `:1804-1895`). If that tree is different from `main`'s, it commits it on `main`
  with `author` as both author and committer, titled "Your changes" (`:467-473`). Every changed path is listed in
  `picked_up` (`:474-475`).
- The pickup doesn't check who wrote a file. Whatever in the real files differs from `main` gets picked up: an API
  write, an upload, or an editor save. A later save in the same second as the read also gets caught: the index is
  re-dated (`:463-466`) and `_keep_index` marks the file to be read again (`:1732-1760`).
- If the thread's pod opened after the save, its open already committed the save to the pod's local `main` as
  "Your changes" (`history.py:356-357`). That commit becomes the copy's base, but it is never pushed. The landing's
  pickup compares against the durable `main` (`:454-458`), so it still records the save.

## Q2: Does the thread's `commit` step write only the files its copy changed since `refs/bases/<id>`?

**Yes.** In fact the commit step writes no real file. Of the landing's steps, only `apply` writes, and only for paths in
`commit`'s `changes`:

- `commit` maps to `History.commit_turn` (`surogates/sandbox/executor_server.py:77`). The set of candidate files is
  `self._diff(base, turn)` (`history.py:569`), where `base = refs/bases/<id>` (`:537`). That diff is by content
  (`git diff --raw`, `:1776-1791`). A file the thread's copy didn't change is not in `versions`, so it can't be in
  `changes` (`:584-590`) or in `overlapped`.
- The landing runs one `apply` per entry in `changes` and nothing else (`landing.py:832-838`). Each `apply` writes
  only when the real file still matches the version `commit` read (`history.py:618-622`). Otherwise it raises
  `HistoryConflict`, and the landing is rolled back.
- `record` builds the landing's tree from `main_tip = pickup or main` (`history.py:679`). That tree is the pickup's
  tree, editor save included, with only the applied paths replaced (`_landed`, `:695-715`, `read-tree main_tip` at
  `:700`). It isn't a three-way merge, so a stale copy of an untouched file in the thread can't get in. The landing's
  first parent is the pickup, and the turn is its second parent (`:683`).
- If the landing is rolled back, `unapply` only touches applied paths, and only where the real file is still the
  landing's version (`history.py:630-650`). A file saved after that is a conflict, and it isn't overwritten.
- At the end of the turn, the pod's copy still holds the old version. The pod is deleted at the end of every turn.
  `record` moves the branch and its base to the landing (`:687-689`), so the next pod's copy opens from a tree that
  contains the save (`:373-374`).

The same is true for a file the thread **did** change. If the real file is neither the base's version nor the turn's
(`history.py:574`), the file is held back: it is listed in `overlapped`, with reason `changed` and `by: you`
(`:591-608`). The newer real file stays, as described in `landing.py:12-14`.

The API-side landing (a Restore, through `BucketHistory`) is safe as well. Its pickup records only the paths it is
about to write (`bucket.py:455-515`). Its `apply` is a conditional bucket write and fails with a conflict if
the file changed (`bucket.py:517-539`).

## Checked by running it

I added two throwaway tests on top of `tests/test_durable_history.py` (its `project`, `a_pod` and `land` helpers),
run against this commit. Both pass:

1. The pod opens, then `Report.docx` is written straight into the real files, and the thread changes only `notes.txt`.
   The pickup lists exactly `Report.docx`, authored `user:u1@surogate`. `changes` is `["notes.txt"]` and nothing is
   overlapped. After the landing, the real file is still the saved bytes, the landing's tree holds the saved blob, and
   the landing's first parent is the pickup.
2. Same setup, but the thread also changes `Report.docx`. It is overlapped as `("changed", {"kind": "you"})`. The real
   file and the landing's tree both keep the saved bytes.

The existing tests `test_your_edits_are_picked_up_on_main_by_you_and_land_as_the_landings_first_parent` and
`test_your_save_over_another_threads_landed_file_is_by_you` also pass.

## What the code alone can't confirm, and what is outside this question

- **geesefs freshness.** The tests use a local folder, not geesefs. To see an API write, the pod relies on
  `.invalidate` (`history.py:2294-2297`, called at `:418` and `:2033`) and on S3 reporting the new size and time.
  Uploads depend on exactly the same mechanism today. The plan's end-to-end run on DEV (one save in a cloud project,
  then a thread landing that changes some other file) would settle it.
- **A save that lands later than the turn.** If a turn changes nothing (`turn["commit"] is None`), its pickup is never
  pushed (`landing.py:855-857`). The save stays in the real files, and the next landing that does push records it.
  Nothing is reverted in the meantime.
- **A file both sides change in the same turn (not this question).** The pod's `apply` checks the real file and then
  writes it through geesefs without a condition (`history.py:618-627`, `_put` `:2052-2085`). An editor save that
  lands in the milliseconds between the check and the write is overwritten. This race exists for uploads today. It
  closes with the sandbox refusing writes to an open document, which the spec puts in a later phase. It doesn't apply
  to files the thread didn't change.
