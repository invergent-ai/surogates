# Office Load Tool and Capacity Measurement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure how many concurrent editing sessions one document server holds while every save meets the bar,
what breaks first and what moves it, with a load tool kept in the repo.

**Architecture:**
- `benchmarks/office_load` (package `oload`) is a Python asyncio tool. It drives runtime-office load clients over
  their HTTP API (open, edit, flush, close), and plays the harness's part toward the document server:
  - it serves the input files;
  - it signs the editor config;
  - it sends forcesave;
  - it receives callbacks and checks each saved file for its turn's marker.
- It ramps concurrency step by step, judges each step against the spec's bar, bisects to the knee and samples the
  office namespace's metrics. Each run gets a report.
- The measurement tasks run it against the load release from the ops plan.

**Tech Stack:**
- Python 3.12, aiohttp (client and server), PyJWT;
- python-docx, openpyxl, python-pptx and pypdf (marker checks);
- pytest and pytest-asyncio;
- kubectl, for metrics and for discovering the client pods.

**Spec:** `docs/superpowers/specs/2026-10-18-document-server-capacity-design.md`

**Depends on:** the ops plan `2026-10-18-document-server-capacity-1-load-release.md` in surogate-ops: the
`office-load` release, the `office-load-client` fleet and the `office-load-s3` store. Tasks 1–6 need none of it. Task
7 and after need it installed.

## Global Constraints

- **Public repository.** No hosts, IPs, bucket names, secrets or kubeconfig paths in committed files. Everything
  site-specific comes from the environment, or from a git-ignored `.env`:
  - `OLOAD_DS_URL`: the document server as the pages reach it, in the cluster;
  - `OLOAD_COMMAND_URL`: the document server as this host reaches it;
  - `OLOAD_JWT_SECRET`;
  - `OLOAD_CLIENT_TOKEN`;
  - `OLOAD_LISTEN`: host:port the tool binds;
  - `OLOAD_PUBLIC_BASE`: the base URL the document server uses to reach the tool;
  - `OLOAD_NAMESPACE`;
  - `KUBECONFIG`.
- **The bar** (spec, verbatim values). A step holds when, through its hold period:
  - no lost save;
  - the forcesave `/command` reply has p99 under 5 s and none over 10 s;
  - the save callback after the turn's end has p95 at most 10 s and p99 at most 30 s;
  - every flush is acknowledged within 10 s;
  - a document opens with p95 at most 15 s and no failures;
  - there are no editor errors, disconnections or connection-limit messages.
- **Steps:**
  - 5, 10, 20, 40, 80, 160, then doubling;
  - each ramps up over 60 s and holds for 600 s;
  - the ramp stops at the first failing step, then bisects between the last holding and first failing step until the
    gap is at most 10% of the holding value, or 2 sessions.
- **Workloads** (spec):
  - **rollout:** one agent page; turns back to back; the edit count per turn drawn from the recorded distribution;
    calls about 10 s apart;
  - **chat:** the same turns plus a watching editor page per document; 30–90 s idle between turns.
- **Document mix:** 35% Word, 25% spreadsheets, 30% decks, 10% PDFs, from `runtime-office/test/fixtures/sample.{docx,xlsx,pptx,pdf}`.
- **Key prefix:** every document key starts `load-`, then the run id, the session index and a nonce.
- **Never load DEV's own office stack.**
  - Before every measurement run, check that no DEV benchmark run or runtime-office integration test is going.
  - Never point the tool at DEV's document server.
- **Tests:** in its own venv:
  `cd benchmarks/office_load && uv venv -q --python 3.12 .venv && uv pip install -q -p .venv/bin/python -e '.[dev]'`,
  then `.venv/bin/pytest -q -p no:cacheprovider`. Never `uv run` for surogates itself.
- **Conventional Commits:**
  - `git add` named files only;
  - no Co-Authored-By trailer;
  - never mention plans, tasks or reviews in code, comments or commits.
- DEV restarts and installs of the load stack need no asking. Merging the PR is automatic.

## Review Focus

1. **A callback whose saved file lacks the turn's last marker**, a stale or earlier save, counts as a lost save, never
   as the turn's save. Test in Task 4.
2. **A callback for an earlier turn arriving after a later turn's forcesave** satisfies neither turn wrongly. Callbacks
   are matched by `userdata` (the turn id), and the marker is checked against that turn. Test in Task 4.
3. **The saved file's download URL** names the document server's in-cluster host. The tool rewrites its scheme and
   host to `OLOAD_COMMAND_URL`'s before downloading, as the harness does with `internal_url`. Test in Task 1.
4. **A load-client pod dying or answering 503 mid-step:**
   - its sessions count as failures with cause `client`;
   - a step whose only failures are `client` is reported as client-limited, not as the document server's knee.

   Test in Task 5.
5. **Forcesave answered with error 4** ("nothing to save") after a turn that edited is a failure: the edits never
   reached the server. After a turn with no edit it is fine. Test in Task 4.

---

### Task 1: The document server's side of the tool

**Files:**
- Create: `benchmarks/office_load/pyproject.toml`, `.gitignore` (`.venv/`, `.env`, `runs/`), `README.md` (stub),
  `oload/__init__.py`, `oload/settings.py`, `oload/editor.py`, `oload/docserver.py`
- Test: `benchmarks/office_load/tests/test_docserver.py`

**Interfaces:**
- Produces:
  - **`Settings`** (frozen dataclass), read from the env names above by `Settings.from_env()`.
  - **`editor_config(settings, *, doc_key: str, kind: str, user_id: str, user_name: str, file_url: str, callback_url: str) -> dict`**
    - The same shape as `surogates/office/editor_config.py:build_editor_config`: document, editorConfig with `mode:
      edit`, `coEditing: {mode: fast, change: false}`, `customization.forcesave: true`.
    - Signed HS256 with `OLOAD_JWT_SECRET` into `config["token"]`.
    - `kind` is one of `docx|xlsx|pptx|pdf`, mapped to `fileType` and `documentType` as surogates' `OfficeKind` does.
  - **`DocServerSide`**, an aiohttp app factory `make_app(state: CallbackState) -> web.Application`:
    - `GET /files/{doc_key}` returns the document's current input bytes.
    - `POST /callback/{doc_key}`:
      - verifies the body's `token` (HS256);
      - records `Callback(doc_key, status, userdata, url, at)`;
      - for status 2 or 6, downloads the file (URL rewritten), stores the bytes on the callback and answers `{"error": 0}`.
    - Anything unverifiable answers `{"error": 1}` and is recorded as an error.
  - **`CallbackState`:** `add_document(doc_key, kind, data)`, `wait_for(doc_key, userdata, timeout) -> Callback | None`,
    and `callbacks` (all, in arrival order).
  - **`async forcesave(session: aiohttp.ClientSession, settings, doc_key, userdata) -> ForceSave(error: int | None, seconds: float)`**
    - `POST {OLOAD_COMMAND_URL}/command` with `{"c": "forcesave", "key", "userdata", "token"}`, with a 10 s timeout
      as the harness has.
    - A timeout gives `error=None`.

- [ ] **Step 1: Write the failing tests:**
  - `test_editor_config_is_signed_and_shaped`: decode `token` with the secret, then assert `document.key`,
    `document.fileType == "docx"`, `documentType == "word"`, `editorConfig.callbackUrl` and
    `customization.forcesave is True`.
  - `test_callback_rejects_a_bad_token`: answers `{"error": 1}` and records an error.
  - `test_callback_status_2_downloads_through_the_command_host`: a fake document server on a test port. The callback's
    `url` names `http://office-docs.example.svc/cache/x`, and the download hits the fake at the same path.
  - `test_wait_for_matches_userdata`: two callbacks with userdata `t1` and `t2`; `wait_for(key, "t2")` returns the
    second.
  - `test_forcesave_timeout_is_none`: the fake answers after 11 s, giving `error is None` and `seconds >= 10`.
- [ ] **Step 2: Run them.** Expected: FAIL (no package).
- [ ] **Step 3: Implement** `settings.py`, `editor.py` and `docserver.py`. In `pyproject.toml`: aiohttp, pyjwt,
  python-docx, openpyxl, python-pptx, pypdf; dev: pytest, pytest-asyncio; and the script `oload = "oload.cli:main"`
  (`cli.py` comes in Task 6).
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat(bench): an office load tool's document-server side"`

### Task 2: The load clients

**Files:**
- Create: `oload/clients.py`
- Test: `tests/test_clients.py`

**Interfaces:**
- Produces:
  - **`RuntimeOffice(base_url: str, token: str, session: aiohttp.ClientSession)`**, with these calls:
    - `open(doc_key, session_id, config, document_server_url) -> Timed[str]` (the page id);
    - `edit(page, ops) -> Timed[dict]`;
    - `flush(page) -> Timed[bool]`;
    - `close(page) -> None`.

    `Timed` is a generic dataclass `(value, seconds)`. HTTP 503 raises `ClientBusy` and a connection error raises
    `ClientDown`, both with `cause = "client"`.
  - **`Fleet.discover(namespace) -> Fleet`** reads the pod IPs of `app=office-load-client` from `kubectl get pods -o json`.
    `Fleet.place(pages: int) -> RuntimeOffice` hands out clients round-robin. No client gets more than
    `pages_per_client` pages, and `FleetFull` is raised past the total.

- [ ] **Step 1: Write the failing tests** against a fake runtime-office (aiohttp test server):
  - the request paths and bearer header;
  - 503 gives `ClientBusy`;
  - a refused connection gives `ClientDown`;
  - `place` spreads 10 pages over 3 clients as 4/3/3 with `pages_per_client=4`;
  - the 13th page raises `FleetFull`;
  - discovery parses a recorded `kubectl` JSON fixture.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat(bench): talk to a fleet of runtime-office load clients"`

### Task 3: Workloads, edits and markers

**Files:**
- Create: `oload/workloads.py`, `oload/pace.py`, `oload/markers.py`
- Test: `tests/test_workloads.py`, `tests/test_markers.py`

**Interfaces:**
- Produces:
  - **`edit_ops(kind: str, marker: str, n: int) -> list`**: one valid `office_edit` op for the kind, carrying
    `marker`:
    - **docx:** an `insert` at `{"end": true}` with a paragraph;
    - **xlsx:** a `set` of one cell on the fixture's first sheet, in row `n + 2` of a free column;
    - **pptx:** a text change that the client image's version accepts, on a slide the fixture has;
    - **pdf:** a rect-placed note on page 1.

    Each shape is checked against the client image's runtime-office version: the released `surogates-office`
    pinned in the ops chart. Read that tag's references.
  - **`has_marker(kind: str, data: bytes, marker: str) -> bool`**: reads the saved file (python-docx, openpyxl,
    python-pptx, pypdf annotations) for the marker text.
  - **`Pace(workload: Literal["rollout", "chat"], rng: random.Random)`**:
    - `edits_in_turn() -> int`: drawn from `RECORDED_EDITS`;
    - `gap_s() -> float`: about 10 s, from `RECORDED_GAPS`;
    - `idle_s() -> float`: 0 for rollout, uniform 30–90 s for chat.
  - **`RECORDED_EDITS`** (a tuple of ints) and **`RECORDED_GAPS`** (a tuple of floats) in `pace.py`, with a comment
    naming the source: the office benchmark's final run on the templates build, `office_edit` calls per session and
    seconds between tool calls.
    - They are extracted once from that run's `events.jsonl` (in the templates worktree's `runs/full-005`).
    - Only the numbers are committed.
  - **`pick_kind(i: int) -> str`**: the 35/25/30/10 mix, deterministic per session index.

- [ ] **Step 1: Write the failing tests:**
  - for each kind, `has_marker` is true on a file saved with the op applied (build it with the same library), and
    false on the untouched fixture;
  - `pick_kind` over 1000 indices lands within ±2% of each share;
  - `Pace("rollout").idle_s() == 0`;
  - chat's idle stays in [30, 90];
  - `edits_in_turn` draws only values in `RECORDED_EDITS`;
  - `RECORDED_EDITS` has median 1 and p90 6, as recorded.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** Extract the recorded numbers with a throwaway script; do not commit it.
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat(bench): office load workloads, edits and the marker check"`

### Task 4: The virtual session

**Files:**
- Create: `oload/session.py`, `oload/events.py`
- Test: `tests/test_session.py`

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces:
  - **`Event`** (dataclass): `session: int`, `kind`, `what`, `ok: bool`, `seconds: float`, `cause: str | None`, `at:
    float`. `what` is one of `open | edit | flush | forcesave | callback | marker | error`. `cause` is one of
    `client | server | lost | timeout`.
  - **`EventLog`** collects events and writes `events.jsonl`.
  - **`VirtualSession(index, workload, fleet, settings, state, log, rng)`** with `async run(until: float)`:
    - It opens the document, plus a watching page for chat, with its own `session_id` and user.
    - It loops turns until `until`. Each turn makes N edits, each with a new marker and the gap between them, then:
      1. flush;
      2. forcesave with `userdata` = the turn id;
      3. `wait_for` that turn's callback for 30 s;
      4. `has_marker` on its bytes for the turn's last marker;
      5. the idle time.
    - It closes its pages at the end.

    The forcesave rules:
    - error 4 after a turn that edited is logged `ok=False, cause="lost"`; after a turn with no edit it is fine;
    - a callback whose file lacks the turn's last marker is logged `marker ok=False, cause="lost"`;
    - no callback within 30 s is logged `callback ok=False, cause="timeout"`.

- [ ] **Step 1: Write the failing tests** with fakes for the fleet and the document server:
  - `test_a_turn_logs_open_edits_flush_forcesave_callback_marker`: the order and `ok` values.
  - `test_stale_callback_is_a_lost_save`: the callback's bytes carry the previous turn's marker, giving `marker` with
    `cause == "lost"`.
  - `test_late_earlier_callback_does_not_satisfy_a_later_turn`.
  - `test_forcesave_4_after_edits_is_lost_but_after_no_edit_is_fine`.
  - `test_chat_opens_a_watching_page`: 2 opens for one document, with different session ids.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat(bench): a simulated agent session that checks every save"`

### Task 5: Steps, the bar and the knee

**Files:**
- Create: `oload/bar.py`, `oload/ramp.py`
- Test: `tests/test_bar.py`, `tests/test_ramp.py`

**Interfaces:**
- Produces:
  - **`judge(events: list[Event], hold_from: float) -> Verdict`.** `Verdict(holds: bool, broken: list[str],
    client_limited: bool, stats: dict)`.
    - It applies the bar to the hold period only.
    - `stats` holds the p50/p95/p99/max of each timed `what`, plus counts of lost, timed-out and error events.
    - `client_limited` is true when every failure has `cause == "client"`.
  - **`steps() -> Iterator[int]`** yields 5, 10, 20, 40, 80, 160, 320, …
  - **`bisect(good: int, bad: int) -> int | None`** returns the midpoint, or `None` once
    `bad - good <= max(2, good // 10)`.
  - **`async run_step(n, workload, …) -> StepResult`.** It starts `n` sessions spread over 60 s and holds 600 s from
    the last start (both durations are arguments, for the tests). It then judges.
  - **`async ramp(workload, …) -> list[StepResult]`.**
    - It stops at the first failing step that is not client-limited, then bisects.
    - At a client-limited step, or at `FleetFull`, it stops and marks the result as a lower bound.

- [ ] **Step 1: Write the failing tests:**
  - `judge` on synthetic events, one test per row of the bar, each at and just past its threshold:
    - forcesave p99 4.9 s holds, 5.1 s breaks;
    - one forcesave at 10.1 s breaks;
    - callback p95 10.0 s holds, 10.1 s breaks;
    - callback p99 30.1 s breaks;
    - one flush past 10 s breaks;
    - open p95 15.1 s breaks;
    - one open failure, one lost save, or one editor error breaks.
  - Only `client` failures give `client_limited=True` and `holds=False`.
  - `steps` gives the first 8 values.
  - `bisect(40, 80)` gives 60, and `bisect(40, 44)` gives `None`.
  - `ramp` with a fake step function that fails from 50 up returns holding steps 5, 10, 20, 40, failing 80, then
    bisects 60, then 50, then 45, and stops.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat(bench): ramp office load to the knee against the save bar"`

### Task 6: Metrics, report and CLI

**Files:**
- Create: `oload/metrics.py`, `oload/report.py`, `oload/cli.py`; complete `README.md`
- Test: `tests/test_metrics.py`, `tests/test_report.py`, `tests/test_cli.py`

**Interfaces:**
- Produces:
  - **`Sampler(namespace, every_s=15)`**, an async context manager. Each 15 s it records:
    - `kubectl top pods --containers -n <ns>` as CPU in millicores and memory in MiB per container;
    - the RabbitMQ queue depths, from `kubectl exec <rabbitmq pod> -- rabbitmqctl list_queues name messages`;
    - the count of `error` lines from docservice and the converters since the step's start (`kubectl logs --since-time`);
    - the database's connections, from `psql` `SELECT count(*) FROM pg_stat_activity WHERE datname = 'docs_load'`
      on the CNPG primary, found by label.

    The samples are written to `metrics.jsonl`.
  - **`render(run_dir) -> str`** writes `report.md`:
    - the run's settings (workload, profile, pages per client, clients);
    - a table per step: n, holds, broken rows, the stats, and the peak CPU and memory per container;
    - the capacity, the knee and the first broken row with its evidence: the metric peaks and error lines at the knee;
    - "lower bound (client budget)" where it applies.
  - **CLI** `oload`:
    - `preflight-clients --pages 1,2,4,8,…`: one client, the session pace, every page on that client;
    - `preflight-cap --editors 25,50`: that many editors, each with one edit and one save;
    - `ramp --workload rollout|chat --profile <name> --run-id <id> [--steps 4,8,…] [--ramp-s 60] [--hold-s 600]`
      (the overrides are for smoke runs; the defaults are the spec's);
    - `report <run-id>`.

    Each run goes to `runs/<run-id>/`.

- [ ] **Step 1: Write the failing tests:**
  - parse recorded `kubectl top` and `rabbitmqctl` outputs (fixtures);
  - `render` on a synthetic run dir gives the step table, the capacity line and the lower-bound wording;
  - the CLI's `--help` lists the four commands.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** The README covers what the tool measures, the bar, the env names, each command, and
  "never point it at a document server people use".
- [ ] **Step 4: Run the whole suite.** Expected: PASS.
- [ ] **Step 5: Commit.** `git commit -m "feat(bench): office load metrics, report and command line"`

### Task 7: End to end against the load release

**Interfaces:**
- Consumes: the ops plan's release and fleet (one client), and Tasks 1–6.

- [ ] **Step 1: Prepare.**
  - Check that no DEV benchmark run or integration test is going.
  - Write the git-ignored `.env`. `OLOAD_PUBLIC_BASE` is this host as the cluster reaches it.
- [ ] **Step 2: Run one session of each kind.** `oload ramp --workload rollout --profile prod --run-id smoke-1 --steps 4 --hold-s 120`:
  one docx, one xlsx, one pptx, one pdf.
- [ ] **Step 3: Check that** every turn's marker is found, the forcesave errors are 0, the callbacks came, and
  `report.md` renders. Any edit shape the client rejects is fixed in `workloads.py` with a test, then re-run.
- [ ] **Step 4: Commit any fixes.** `git commit -m "fix(bench): …"`

### Task 8: Preflight — the client limit and the connection cap

- [ ] **Step 1: Find the client limit.**
  - Use the DEV-profile release and one client at `resources` 8 CPU / 32 Gi.
  - Run `oload preflight-clients --pages 1,2,4,8,16,32` (rollout pace).
  - The client knee is the first page count where the bar breaks with `cause=client`, or the client's CPU sits at its
    limit.
  - `pagesPerClient` = half the last holding count.
- [ ] **Step 2: Test the connection cap.**
  - Run `oload preflight-cap --editors 25,50`, with the fleet scaled so the editors fit.
  - Every editor must flush true and save its marker.
  - If any editor is refused or stops editing, record it, stop, and fix our build of the document server first. That
    is a new plan, not this one.
- [ ] **Step 3: Write the results.** Start `benchmarks/office_load/RESULTS.md` with both results: the client knee, the
  `pagesPerClient` chosen, and the cap verdict.
  `git commit -m "docs(bench): the office load clients' limit and the connection cap"`

### Task 9: Baselines

- [ ] **Step 1: Run the four production-profile ramps.**
  - `rollout` and `chat` on the production profile (CPU capped): the headline numbers.
  - The fleet is scaled to cover each step within the budget (the chart refuses more).
  - Each ramp gets its own run id: `prod-rollout-1`, `prod-chat-1`.
- [ ] **Step 2: Repeat on the other two profiles.** The same two workloads on the DEV profile, then on the uncapped
  profile, upgrading the release between profiles. Wait for the pods to be ready, and run nothing else meanwhile.
- [ ] **Step 3: Add to `RESULTS.md`**, for each run:
  - the capacity, the knee and the first broken row with its evidence;
  - edits per minute and forcesaves per minute at the knee;
  - whether it is a lower bound.

  Commit: `docs(bench): what one document server holds, by workload and profile`

### Task 10: Sweeps

Each sweep starts from the production-profile baseline and changes one thing. It re-runs only the steps around that
workload's knee: last holding, knee, and one step above. Rollout runs first, then chat if time allows.

- [ ] **Step 1: Converters.** `converterReplicas` at 4, then 8. Workers per pod are fixed in our build's entrypoint
  (`maxprocesscount`), so pods are the knob. Say so in the results.
- [ ] **Step 2: Docservice.** CPU 4, then 8, with memory doubled. The CPU limit equals the request.
- [ ] **Step 3: Storage.** Install the in-cluster store, and point the release's `storage` at it.
- [ ] **Step 4: RabbitMQ.** Set `rabbitmq.erlArgs` to `+sbwt none +sbwtdcpu none +sbwtdio none`. Record idle CPU before
  and after, and the knee.
- [ ] **Step 5: Add each sweep to `RESULTS.md`** with what moved the knee and by how much. Commit:
  `docs(bench): what moves one document server's limit`

### Task 11: Results and the pull request

- [ ] **Step 1: Finish `RESULTS.md`** with:
  - the headline numbers;
  - the first bottleneck and its evidence;
  - how DEV differs from PROD where it could set the knee: the storage link, Postgres on network storage, the CPU
    model;
  - what the scaling design should take from it.
- [ ] **Step 2: Run the suite once on the branch head.**
- [ ] **Step 3: Push and open the PR,** which carries the spec, both plans' surogates side and the tool. Merge it
  (automatic).
- [ ] **Step 4: Tear down.** Uninstall the load release, the fleet and the store, and drop `docs_load`, as the ops
  README says. Leave DEV's `office` untouched.
