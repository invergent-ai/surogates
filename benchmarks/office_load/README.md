# Office load

Measures how many concurrent editing sessions one office document server holds while every save meets the bar, what
breaks first, and how many pages one load client holds.

`oload` drives runtime-office load clients over their HTTP API and plays the harness's part toward the document
server: it serves the input files, signs the editor configs, sends forcesave through `/command`, receives the
callbacks and downloads each saved file.

**Never point it at a document server people use.** It loads its target to the point of losing saves. Run it against
a release of its own, and only while nothing else that shares its database server or its storage is under test.

## What it measures

A virtual session stands for one agent editing one document. It opens a copy of one of runtime-office's sample
documents (35% Word, 25% spreadsheets, 30% decks, 10% PDFs) and works turn by turn. Each edit writes a marker of its
own. At each turn's end it saves as the harness does: flush the page, forcesave, wait 30 s for the save's callback,
then check that the saved file holds the turn's last marker. A save that does not call back in that time, or whose
file lacks the marker, is a lost save.

| Workload | Pages per document | Pace |
| --- | --- | --- |
| `rollout` | one agent page | turns back to back; the edits per turn and the time between them drawn from a recorded benchmark run (median 1 edit, about 10 s apart) |
| `chat` | the agent page and a watching editor, as a chat's file pane holds one | the same turns, with 30–90 s idle between them |

A **step** is a number of concurrent sessions: they start spread over 60 s, then hold 600 s from the last start. The
ramp runs 5, 10, 20, 40, 80, 160, … sessions until a step breaks the bar, then bisects between the last step that held
and the first that broke until they are within a tenth of the holding step, or 2 sessions.

- **The capacity** is the highest step that held.
- **The knee** is the lowest step that broke the bar on the document server's side; its first broken row is the
  first finding.
- **A lower bound (client budget):** a step that broke only on the load clients' own failures, or that the clients had
  no room for, ends the ramp. It says nothing of the document server: the capacity is at least the last holding step.
- **An invalid step:** a request of the tool's own was refused (a wrong token, a malformed edit). Nothing it measured
  stands, and the ramp stops.

Each step also samples the office namespace every 15 s: CPU and memory per container, RabbitMQ's queue depths,
docservice's and the converters' error lines, and the document server database's connections.

It answers the document server as the runtime does: `{"error": 0}` once a save is kept; 400, 403, 404 or 502
otherwise, after which the document server retries the callback. Every forcesave and every download gets a client of
its own, as the harness's do.

One difference makes its numbers slightly optimistic. Its `/files` and its callbacks answer as soon as their own work
is done: reading the input from memory, fetching the saved file. The runtime also reads and writes storage and the
database first. Open times, and how long the document server holds each callback, are a little shorter here than in
production.

## The bar

A step holds when, through its hold period:

| What | Bar |
| --- | --- |
| Lost saves | none: every turn's save calls back within 30 s and holds the turn's last marker |
| Forcesave reply (`/command`) | p99 under 5 s, none over 10 s |
| Save callback after the turn's end | p95 at most 10 s, p99 at most 30 s |
| Flush | every one acknowledged within 10 s |
| Document open | p95 at most 15 s, no failures (judged over the whole step: every open comes in its ramp-up) |
| Editor errors | none: no error, disconnection or connection-limit message |

## Settings

Everything site-specific comes from the environment, or from a git-ignored `.env` in this directory:

| Name | What |
| --- | --- |
| `OLOAD_DS_URL` | the document server as the pages reach it, in the cluster |
| `OLOAD_COMMAND_URL` | the document server as this host reaches it |
| `OLOAD_JWT_SECRET` | the document server's JWT secret |
| `OLOAD_CLIENT_TOKEN` | the load clients' bearer token |
| `OLOAD_LISTEN` | `host:port` the tool binds |
| `OLOAD_PUBLIC_BASE` | the base URL the document server reaches the tool at |
| `OLOAD_NAMESPACE` | the office namespace: the load clients' pods and the metrics |
| `KUBECONFIG` | the cluster, for kubectl |

The host runs `kubectl` and reaches the load clients' pods by IP; the document server reaches the tool at
`OLOAD_PUBLIC_BASE`. The load clients are the ready pods labelled `app=office-load-client` in `OLOAD_NAMESPACE`; each
holds its `RUNTIME_OFFICE_MAX_PAGES` pages.

## Commands

Install it, then run `.venv/bin/oload <command>` from this directory:

```bash
uv venv -q --python 3.12 .venv && uv pip install -q -p .venv/bin/python -e '.[dev]'
```

Each run goes to `runs/<run-id>/`, and a run id names one run: a second run under it is refused.

### `oload preflight-clients --pages 1,2,4,8,16,32`

How many pages one load client holds. The first load client alone, at the rollout pace, opens each page count in turn,
every page on that client, until one breaks the bar. The client's limit is the first count that breaks with the
clients' own failures, or at which its CPU sits at its limit; set the clients' pages to half the last count that held.
The pods must hold at least the largest count.

### `oload preflight-cap --editors 25,50`

Whether that many editors at once can each still edit and save. The editors open spread over the fleet; once all of
them have opened, each makes one edit and one save. It reports, for each count, how many opened, flushed true and
saved their marker; every one must.

### `oload ramp --workload rollout|chat --profile <name> --run-id <id>`

The ramp to the knee, as above. `--profile` names the document server's resource profile in the report.
`--steps 4,8`, `--ramp-s` and `--hold-s` replace the steps and their durations, for smoke runs; the defaults are the
measurement's. `--pages-per-client` places fewer pages on each client than the pods hold.

### `oload report <run-id>`

Writes the run's `report.md` again and prints it. The other commands write it at their end; this one also reports a run
that was stopped, from the steps that finished.

The commands that run steps also take `--pg-namespace`, `--pg-cluster` and `--pg-database`: the CNPG cluster whose
primary holds the document server's database (default `surogate-pg` in `default`, database `docs_load`).

## A worked example

```bash
cd benchmarks/office_load
cat > .env <<'EOF'
OLOAD_DS_URL=http://office-docs.<office namespace>.svc
OLOAD_COMMAND_URL=http://<the document server, from this host>
OLOAD_JWT_SECRET=<the document server's JWT secret>
OLOAD_CLIENT_TOKEN=<the load clients' token>
OLOAD_LISTEN=0.0.0.0:8099
OLOAD_PUBLIC_BASE=http://<this host, from the cluster>:8099
OLOAD_NAMESPACE=<office namespace>
KUBECONFIG=<the cluster's kubeconfig>
EOF

# One session of each kind, to check the whole path.
.venv/bin/oload ramp --workload rollout --profile prod --run-id smoke-1 --steps 4 --hold-s 120

# The two checks before the ramp.
.venv/bin/oload preflight-clients --pages 1,2,4,8,16,32 --run-id clients-1
.venv/bin/oload preflight-cap --editors 25,50 --run-id editors-1

# The measurement.
.venv/bin/oload ramp --workload rollout --profile prod --run-id prod-rollout-1
```

The ramp prints a line as each step starts and one as it ends. A run's ends might read:

```text
step 5: holds
step 10: holds
step 20: holds
step 40: holds
step 80: breaks: callback: p95 14.20 s, over 10 s
step 60: holds
step 70: breaks: callback: p95 11.05 s, over 10 s
step 65: holds
report: runs/prod-rollout-1/report.md
```

That run's capacity is 65 sessions and its knee 70, where the save callbacks came late first. The report gives each
step's times against the bar, its edits and forcesaves per minute, and the namespace's peaks at the knee: the busiest
containers, the deepest RabbitMQ queue, the database connections and the first error lines.

## What a run leaves

```text
runs/<run-id>/
  run.json            what ran: the command, workload, profile, pages per client, clients, settings (no secret), git sha
  step-<n>/           one per step
    events.jsonl      every open, edit, flush, forcesave, callback and marker check, as it came
    step.json         the step judged: its verdict against the bar, its stats, its leaked client slots
    metrics.jsonl     the namespace's samples
  editors-<n>/        one per editors count (preflight-cap): events.jsonl, metrics.jsonl, cap.json
  report.md
```

The metrics come from `kubectl`: `kubectl top pods --containers` (it needs the cluster's metrics-server),
`rabbitmqctl list_queues` in the `app=office-rabbitmq` pod, the logs of the `app=office-docs` docservice and
`app=office-converter` converter containers since the step's start (a line logged at `ERROR` or `FATAL` is an error
line), and `pg_stat_activity` on the CNPG primary. A probe that fails is recorded in its sample with why, and the step
goes on.

## Tests

```bash
.venv/bin/pytest -q -p no:cacheprovider
```
