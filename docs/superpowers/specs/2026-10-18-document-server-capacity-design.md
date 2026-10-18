# Document server capacity: measure one, then scale

## Why

Agents edit Word documents, spreadsheets, decks and PDFs live, through runtime-office (a Node service that drives
editor pages in Chromium) and the document server (our Euro-Office build). One document server serves a whole
deployment.

Under light extra load it already loses work. Integration tests run beside a benchmark run made the document server
leave forcesaves unanswered and send saves late, and four benchmark sessions lost their work. So the benchmark runs
one session at a time, and a full run takes three hours.

Production users, parallel benchmark runs and RL rollouts all need many editing sessions at once. Before designing
how to scale the office stack, we measure:
- how many concurrent editing sessions one document server holds;
- what breaks first;
- what moves that limit.

## Goals

1. **A capacity number:** the most concurrent editing sessions one document server holds while every save meets the
   bar below. "One document server" is one docservice and its converters, with the cluster's other services as
   production has them.
2. **The first bottleneck** at that number, with the evidence: the resource, queue or log line.
3. **What moves the limit:** sweeps of the knobs that are cheap to change.
4. **A load tool kept in the repo,** for re-runs after every change and as the base for running benchmark sessions
   and RL rollouts in parallel.
5. **A separate number for one runtime-office instance:** how many documents it holds open at an agent's pace.

## Non-goals

- Changing PROD, or the production architecture (several docservice instances, routing by document key, several
  runtime-office instances). That is the next design, written from these numbers.
- Model behaviour. No model takes part: agent sessions are simulated.
- People's editing experience in the browser, beyond what the bar covers.

## Anti-goals

- **A number set by the load clients.** If runtime-office saturates first, the number describes runtime-office, not
  the document server.
- **Loading DEV's own document server.** Saves were lost that way before. This work never touches it.
- **A step reported as holding while a save silently did not land.** Every save's content is checked.

## The bar: what "holding" means

A concurrency level holds when, through a steady period at that level, all of these are true:

| What | Bar | Why this value |
| --- | --- | --- |
| Lost saves | none | Every turn's forcesave produces a callback whose file contains that turn's last edit. |
| Forcesave reply (the `/command` call) | p99 under 5 s, none over 10 s | The harness gives up at 10 s and never retries. |
| Save callback after the turn ends | p95 at most 10 s, p99 at most 30 s | The harness waits 30 s. |
| Flush (every edit has reached the document server) | always acknowledged within 10 s | Otherwise the harness sends no forcesave. |
| Document open | p95 at most 15 s, no failures | |
| Editor errors | none | No error, no disconnection, no connection-limit message. |

**The capacity** is the highest concurrency where every row holds. **The knee** is the first concurrency where any
row breaks; what broke is the first finding.

## How: what is decided

### Where it runs

- **An isolated office release.** It runs on the DEV cluster, in its own namespace, built from the same chart and the
  same image DEV runs, with its own RabbitMQ, its own database and its own storage cache.
  - DEV's document server, and the benchmark runs that use it, carry no load from this work.
- **Two resource profiles** (my call).
  - DEV's requests.
  - The chart's production defaults.

  For the test, both profiles cap CPU at their requests. Without a cap, a document server alone on a node takes every
  free core, and both profiles would measure the same thing. The cap is a switch in the load release's values only.
  One further run uses no CPU limits, as production has it today. The number is quoted for the production profile.
- **The results say how DEV differs from PROD** wherever it could set the knee:
  - the link to object storage;
  - the single Postgres instance on network storage;
  - the CPU model.

  The storage sweep separates the first of these.
- **Storage.**
  - Baseline: object storage over the internet, as production has it, in its own cache bucket or prefix.
  - A later sweep moves storage into the cluster, to show what storage costs.
- **Placement** (Flavius's call).
  - **The document server under test:** docservice, converters and RabbitMQ go on the node that hosts no virtual
    machines and nothing else of DEV. It carries no load clients.
  - **The load clients** go on the two nodes that host the other project's virtual machines and the internal gateway.
    - They have hard CPU and memory limits: at most 24 cores and 200 GB per node together, so the machines and the
      gateway keep plenty.
    - The DEV host node carries none.
  - **If the client budget runs out before the knee,** the result is reported as a lower bound, with the budget that
    capped it.

### The load clients

runtime-office instances drive the editors, because their pages are the real clients the document server sees:
- they run as pods, as many as a step needs;
- each holds a bounded number of pages, set by the client-limit check below, so no instance is ever near its own
  limit.

### The load tool

A new Python (asyncio) tool in `benchmarks/office_load/`. It drives runtime-office's HTTP API directly: open, edit,
flush, close. No harness, no model, no sessions database.

**It plays the harness's part toward the document server:**
- It serves the input files the document server fetches.
- It signs the editor config with the release's JWT secret.
- It sends forcesave through the document server's `/command` at each turn's end, as the harness does.
- It receives the callbacks (status 1, 2, 4, 6, 3 and 7), downloads each saved file, and checks it.

**A virtual session** opens one document from the benchmark's fixtures and holds it for the whole step. Each edit
has the shape agents send and carries a unique marker. At the end of each turn it flushes, forcesaves, waits for the
callback, and checks that the saved file holds the turn's last marker.

The document mix follows the benchmark's: about 35% Word, 25% spreadsheets, 30% decks, 10% PDFs.

There are two workloads (my call). The capacity is reported for each.

| Workload | Who it stands for | Pages per document | Pace |
| --- | --- | --- | --- |
| **Rollout** | Benchmark sessions and RL rollouts | One agent page | Turns back to back. Each turn's edit count is drawn from the recorded distribution (median 1, p90 6, max 28 `office_edit` calls a session), about 10 s apart, with no idle between turns. |
| **Chat** | A production Work chat | The agent page plus a watching editor, as the chat's file pane holds one, receiving every change | The same turns, with 30–90 s idle between them, for a person reading and replying. |

The pace is configurable. Each step also reports edits and forcesaves per minute, so the number can be read
independently of the pace.

**Steps:**
- Concurrency of 5, 10, 20, 40, 80, 160 sessions, and up from there.
- Each step ramps up over a minute and is held for 10 minutes, which covers two autosave cycles.
- The ramp stops at the first step that breaks the bar, then bisects between the last step that held and the first
  that broke.

**What it records:**
- **Every event:** open, edit acknowledged, flush, forcesave reply, callback, download, marker check.
- **A summary per step.**
- **The office namespace's metrics:**
  - CPU and memory per container;
  - RabbitMQ queue depths;
  - docservice and converter errors from their logs;
  - database connections.
- **One report per run,** in the style of the office benchmark's report.

### Two checks before the ramp

1. **The client limit.** One runtime-office instance alone opens 1, 2, 4, … pages on the DEV-profile release, at the
   session pace, until its own bar breaks (Chromium CPU or memory). That sets the pages per instance for the ramp,
   with a wide margin. It is also goal 5's number.
2. **The connection cap.**
   - Upstream's community edition limits how many editors may connect, and our build's editor code still handles that
     limit. Open more editors than that cap, for example 25 and then 50, and check that each one can still edit and
     save: the flush comes back true and the saved file holds the marker.
   - If the cap holds, that is the first finding, and a fix to our build comes before the ramp.

### Sweeps after the baseline

Each sweep changes one thing from the baseline, then re-runs the steps around the knee. In this order:

1. **Converters:** more workers per pod, and more pods.
2. **Docservice:** CPU and memory.
3. **Storage:** in the cluster, against over the internet.
4. **RabbitMQ:** its high idle CPU (likely its schedulers busy-waiting) is fixed, to check it is not part of the
   limit.

## Deliverables

1. **The load tool,** `benchmarks/office_load/`, with its tests and a README on how to run it.
2. **A results document** with:
   - both profiles' numbers;
   - every sweep's numbers;
   - the first bottleneck and its evidence;
   - what the scaling design should do about it.
3. **Whatever chart changes the ops repository needs** to run an isolated release, such as a namespace, cache bucket,
   database name or resources. Also manifests for running runtime-office as load-client pods.

## Constraints

- **Public repository.** No hosts, IP addresses, bucket names or keys go into committed files.
- **DEV cluster only.** PROD is never touched.
- **DEV's own office stack** (its document server, runtime-office and benchmark runs) is never loaded by this work.
- **The document server's database** stays on the shared Postgres. The release gets its own database there, with no
  connection pooler, as production has it.
- **Load runs are kept apart from DEV's own office use** (my call). They share Postgres and the internet link with
  DEV, so a run starts only when no DEV benchmark run or runtime-office integration test is going. The builder runs
  both of those, and checks before each run.
- **Nothing is tainted or cordoned.** Placement uses node affinity on the release's and the load clients' pods only,
  so nothing else on the cluster moves.
- **The session driver is an importable module** (my call), not only a script. The parallel benchmark that comes
  later reuses it and the load-client fleet.

## Left to the builder

- The edit shapes per document type, taken from the office skill's references, and the marker scheme.
- How the namespace's metrics are collected.
- Where the load tool runs: in the cluster or beside it. The document server must be able to reach it for files and
  callbacks.
- The tool's internals, its concurrency control, and the report format.
- How the load-client pods get runtime-office: the existing image, or a build from the repo.
