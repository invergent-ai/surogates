# Office-Load Results

How many concurrent agent editing sessions one office document server holds while every save meets the bar, what
breaks first, and what moves the limit. "One document server" is our Euro-Office build as DEV runs it: one docservice,
its converters and its RabbitMQ, in a release of its own on the DEV cluster, on a node that carries nothing else of
DEV and no load clients. The load clients are runtime-office pods on other nodes. The first measurement ran on
2026-10-18; the re-measurement, with the converter bug fixed, on 2026-10-19.

With the document server's cache on an object store in the cluster, one document server now holds:

- with 2 converters, at least 40 rollout sessions and at least 120 chat sessions (lower bounds: every step held);
- with 4 converters, 75 rollout sessions; with 8, 112.

The converters are the limit. One converter worker does about 47 saves a minute, at about 1.7 core-seconds a save; 8
converters on one node did only 1.35 times what 4 did. Nothing else in the document server came near a limit.

The runs' folders (`runs/<run-id>/`) are not committed. Every number here was computed from them: `step.json` for the
verdicts, `events.jsonl` for times and rates, `metrics.jsonl` for CPU, memory, queues, connections and error lines.

## Re-measurement, 2026-10-19: the converter fixed

Two things changed since the first measurement. Both are on master and in the images these runs used.

- **The converter bug is fixed** in our document server build (`office-docs:acbdc99`; invergent-ai/office-server#1).
  The converter listed a save's storage folder by prefix without a trailing slash, so `…_57` also matched an earlier
  `…_571/` save folder, and the conversion failed at its start with EEXIST.
- **A step no longer ends with every session saving at once** (#540). A turn in progress at the step's deadline is
  abandoned, unsaved and not judged.

Five runs, all with the cache on the object store in the cluster and 12 load clients of 20 pages each (240 pages):
r2-rollout-2conv, r2-chat-2conv, r2-rollout-4conv, r2-rollout-8conv and r2-rollout-dev. Each step's sessions start
over 60 s and hold 600 s; after the first step that breaks, the ramp bisects to the knee.

### Capacity

| Workload | Converters | Profile | Capacity | Knee: what broke it | Saves/min at the knee | First measurement |
| --- | --- | --- | --- | --- | --- | --- |
| rollout | 2 | production | at least 40: lower bound, every step held | not reached; at 40 the callback p95 was 9.36 s | 93.3, at 40 | 15, knee 17 (the bug); 17 without it |
| chat | 2 | production | at least 120: lower bound, every step held and 120 filled the fleet's 240 pages | not reached | 87.4, at 120 | at least 60 (the load clients' room) |
| rollout | 4 | production | 75 | 82: 2 lost saves, sent in the hold's last 1.1 s, at the step's end (below); its times held, callback p95 8.48 s | 184.8 | none of 15, 30, 60 held (the bug); 30 without it |
| rollout | 8 | production | 112 | 120: callback p95 10.41 s; no lost save | 257.0 | 60 did not hold (the bug); 60 without it |
| rollout | 2, at 0.5 CPU each | DEV | 13 | 15: callback p95 13.25 s; no lost save | 28.0 | 8, knee 10 (one timeout in the step's last 30 s) |

Saves/min: forcesaves that called back within the hold, per minute. At these loads a rollout session saved 1.9–3.0
times a minute, fewer as saves slowed; a chat session about 0.73 times.

No step lost a save except at its end, every saved file held its turn's last edit, and no open, edit or flush failed.

### What limits now: the converters

With the bug gone, every knee comes from the converters' throughput: with 8 converters and in the DEV profile the
callback p95 broke the bar; with 4, the step's end broke a step that had saturated them (see "The step's end").

- **Saves stop growing with sessions.** 4 converters completed 184.8 saves a minute at 82 sessions and 188.8 at 90;
  8 converters 249.8, 248.8 and 257.0 at 105, 112 and 120. A session waits for its save before its next turn, so
  past the knee saves wait longer instead of coming faster: with 4 converters the callback median went from 1.50 s at
  60 sessions to 2.52, 5.16 and 6.87 s at 75, 82 and 90.
- **Each converter pod is at one worker's ceiling.** Through the hold at the knees each pod averaged 1.24–1.28 cores
  and peaked at 1.32–1.35 cores of its 2-core limit (1.40 at most in any step). In the DEV profile each pod averaged
  0.48 core against its 0.5 cap. Our build runs one conversion worker per pod.
- **The conversion queue grows.** Its median depth through the hold was 4 or less up to 60 sessions on 4 converters,
  14 at 75, 30 at 82 and 48 at 90; on 8 converters, 8 at 90 and 45–62 from 105 on. From about five minutes into a
  hold the queue also holds the document server's timed saves of the edited documents (the chart's autoAssembly,
  every 5 minutes), queued below forcesaves, so near the knees its depth overstates the forcesaves waiting.

| Converters | Profile | Sessions | Verdict | Saves/min | Per converter | Cores per pod, mean / peak | Core-seconds per save | Queue, median | Callback p50 / p95 / p99 (s) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2 | production | 40 | holds | 93.3 | 46.7 | 1.26 / 1.33 | 1.62 | 18 | 5.68 / 9.36 / 10.08 |
| 2 | production | 120 chat | holds | 87.4 | 43.7 | 1.15 / 1.33 | 1.58 | 4 | 2.56 / 7.00 / 11.39 |
| 4 | production | 60 | holds | 144.5 | 36.1 | 1.02 / 1.40 | 1.69 | 4 | 1.50 / 2.60 / 3.10 |
| 4 | production | 75 | holds | 168.9 | 42.2 | 1.20 / 1.35 | 1.71 | 14 | 2.52 / 5.15 / 5.75 |
| 4 | production | 82 | breaks: 2 lost at the step's end | 184.8 | 46.2 | 1.28 / 1.35 | 1.67 | 30 | 5.16 / 8.48 / 10.83 |
| 4 | production | 90 | breaks: 12 lost at the step's end | 188.8 | 47.2 | 1.28 / 1.35 | 1.63 | 48 | 6.87 / 9.66 / 10.50 |
| 8 | production | 60 | holds | 181.8 | 22.7 | 0.65 / 1.34 | 1.70 | 4 | 1.32 / 1.75 / 2.13 |
| 8 | production | 90 | holds | 226.8 | 28.4 | 1.04 / 1.34 | 2.19 | 8 | 1.90 / 3.87 / 5.32 |
| 8 | production | 105 | holds | 249.8 | 31.2 | 1.22 / 1.35 | 2.35 | 56 | 4.70 / 6.99 / 8.38 |
| 8 | production | 112 | holds | 248.8 | 31.1 | 1.23 / 1.33 | 2.36 | 62 | 4.99 / 9.01 / 10.05 |
| 8 | production | 120 | breaks: callback p95 | 257.0 | 32.1 | 1.24 / 1.32 | 2.31 | 45 | 6.74 / 10.41 / 11.14 |
| 2 at 0.5 CPU | DEV | 13 | holds | 29.4 | 14.7 | 0.42 / 0.50 | 1.73 | 2 | 4.89 / 9.63 / 10.82 |
| 2 at 0.5 CPU | DEV | 15 | breaks: callback p95 | 28.0 | 14.0 | 0.48 / 0.50 | 2.06 | 5 | 7.26 / 13.25 / 14.60 |

Core-seconds per save: the converters' mean CPU through the hold, times its 600 s, over the saves completed in it.

**About 47 saves a minute per worker, up to 4 converters.** 2 converters at 40 sessions, at their ceiling, completed
93.3 saves a minute: 46.7 each. 4 converters at 82 and 90 completed 184.8 and 188.8: 46.2 and 47.2 each. Doubling
the converters doubled the saves.

**8 converters did about 1.35 times what 4 did**: 249–257 saves a minute, 31–32 each. Their pods ran at the same
1.22–1.24 cores, but each save took 2.2–2.4 core-seconds of it, against 1.6–1.7 with 2 or 4 converters, and 1.70
with 8 at 60 sessions, when each was half busy. Busy, the 8 converters take about 10 cores of the document server's
node, beside RabbitMQ's 1.6–1.9; the runs did not measure what on the node slowed each conversion.

In the DEV profile a worker does 14–15 saves a minute at its 0.5-core cap, as the first measurement estimated. The
first measurement had a production worker at about 37 saves a minute, at about 1.9 core-seconds a save and about 1.2
cores a pod.

### Not the limit

- **docservice:** at most 0.31 core of its 2 (8 converters, 112 sessions, about 250 saves a minute); memory at most
  203 MiB. In the DEV profile at most 0.06 core of its 0.5.
- **The proxy** grows with editor connections, 3.5–5.8 millicores each at its peak: at most 0.64 core with 112
  rollout editors, 0.83 with 240 chat editors. It runs without a CPU limit.
- **The in-cluster object store:** at most 0.25 core and 482 MiB.
- **RabbitMQ:** 1.6–1.9 cores at the median of every step, with samples from 0.7 to 3.8 cores; memory at most
  344 MiB. The same at any load, as before.
- **The database:** at most 18 connections with 8 converters, 14 with 4, 12 with 2.
- **The bar's other rows:** forcesave replies p99 at most 0.21 s (the longest 1.01 s), flushes at most 0.18 s, opens
  p95 at most 6.54 s in the rollout steps and 9.97 s in the chat step at 120, where 240 pages opened within 60 s. No
  load-client failure and no leaked page slot.

### The converter bug is gone

No converter or docservice error line came in any of the 24 steps, and the log probe answered in every sample: no
EEXIST. The steps sent 28,199 forcesaves, each one conversion, besides the document server's own timed and closing
saves. At the first measurement's one in 750, about 38 would have been lost. The 28,185 that called back all held
their turn's last edit.

### The step's end

The forcesave burst is gone: in every step the forcesaves sent in the hold's last 30 s came at 0.3–1.4 times the
rate before them, against 1.5–3.6 times in the first measurement.

The step's end still loses saves at saturation, another way. All 14 lost saves of the re-measurement were forcesaves
sent in a hold's last 3 s, in the two 4-converter steps that broke. At the deadline every session whose turn was
abandoned closes its page with that turn's edits unsaved. 5 s after a document's last editor leaves, the document
server queues that document's save, at a higher priority than forcesaves (normal against low). The queue rose with
them, and forcesaves whose conversion had not started by then waited behind them past 30 s:

| Step | Sessions with an abandoned turn | Conversion queue at the deadline, then 6–15 s after | Last forcesave that called back: sent, called back (s into the hold) | Lost |
| --- | --- | --- | --- | --- |
| 4 converters, 82 | 58 | 67, then 104 | 598.3, 606.3 | 2, sent at 598.9 and 599.2 |
| 4 converters, 90 | 59 | 97, then 132 | 596.4, 606.3 | 12, sent from 597.0 to 599.7 |
| 8 converters, 120 | 95 | 85, then 160 | 599.4, 605.7 | 0 |
| 2 converters, 40 | 30 | 35, then 54 | 599.3, 606.0 | 0 |

Where the converters had started every waiting forcesave before the closing saves came, about 5 s after the
deadline, none was lost. So the 4-converter knee at 82 is the step's end, not the steady rate: on its times 82 held
(callback p95 8.48 s), and so did 90 (9.66 s), saturated at 185–189 saves a minute.

Production has the same priorities: a document whose last editor leaves with unsaved edits is saved ahead of every
waiting forcesave. Many editors closing at once while the converters are saturated would delay turn-end saves the
same way.

### Costs for sizing

- **Converters:** about 1.7 core-seconds a forcesave, 1.58–1.78 in every 2- and 4-converter step, rollout and chat
  alike. That includes the document server's timed saves, which production has too. With 8 converters busy on one
  node, 2.2–2.4. In the DEV profile 1.7–2.4. A converter pod used at most 392 MiB.
- **One worker at its ceiling does about 47 saves a minute.** At the tool's pace that is 19–20 rollout sessions per
  worker up to 4 converters (40 on 2, 75 on 4) and 14 with 8 on one node, or at least 60 chat sessions. None of it is
  room for bursts.
- **Load clients:** one page costs about 55 millicores on average (46–63 across the steps) and about 280 MiB
  (272–288) above its process's memory at the step's start, 150–220 MiB once the process had held pages (357 MiB in
  the day's first step, every process fresh at 75–78 MiB). About the same as in the first measurement.

| What | CPU | Memory |
| --- | --- | --- |
| One client, 10 rollout pages (8 converters, 120 sessions) | 0.60 core mean (0.49–0.70 across clients), 2.0 peak | 2.9 GiB mean, 3.2 GiB peak |
| One client, 20 pages: 10 chat sessions and their watching editors (120 chat sessions) | 1.13 core mean (0.86–1.40), 3.6 peak | 5.5 GiB mean, 6.0 GiB peak |

## First measurement, 2026-10-18

Kept as the record: its knees were set by the converter bug and by the step-end burst, both since fixed (above).

### Capacity

Sessions held at once while every save met the bar ([README](README.md#the-bar)). At the tool's pace a rollout
session saves 2.4–3.2 times a minute when saves are fast (in the cluster, production profile), fewer when they are
slow; a chat session about 0.8 times.

| Workload | Object storage | Converters | Profile | Capacity | Knee: what broke it | Runs |
| --- | --- | --- | --- | --- | --- | --- |
| rollout | over the internet | 2 | production | none stable | 3 and 5 broke on the callback p95 (11.30 and 13.71 s); 1, 2 and 4 held in 5-minute steps | prod-rollout-2, prod-rollout-3, clients-1 |
| rollout | in the cluster | 2 | production | 15 | 17: one lost save, the converter bug | s3-rollout-1 |
| chat | in the cluster | 2 | production | at least 60: lower bound (the load clients' room) | not reached | s3-chat-1, s3-chat-2 |
| rollout | in the cluster | 4 | production | none of 15, 30, 60 held | 15 and 30: the converter bug only (1 and 3 lost saves); 60: 24 callbacks timed out in the step's last 30 s and one save was lost to the bug, callback p95 10.73 s | conv4-rollout-1, -30, -60 |
| rollout | in the cluster | 8 | production | 60 did not hold | 60: 3 lost saves, all the converter bug; its times held | conv8-rollout-60 |
| rollout | in the cluster | 2, at 0.5 CPU each | DEV | 8 | 10: one callback timed out in the step's last 30 s | dev-s3-rollout-1 |

What the in-cluster rows say once the converter bug's losses are set aside:

| Converters | Profile | Rollout sessions that hold | First that breaks |
| --- | --- | --- | --- |
| 2 | production | 17 | 20: one timeout in its last 30 s |
| 4 | production | 30 | 60: saturated, 24 timeouts in its last 30 s |
| 8 | production | 60 | none run above 60 |
| 2 at 0.5 CPU | DEV | 8 | 10: one timeout in its last 30 s |

Every lost save in the in-cluster runs was either the converter bug or a timeout in a step's last 30 s, when every
session saves at once (see "How to read the runs"). Outside those, every in-cluster step held on its times; 4
converters at 60 sessions only just, with saves queueing and slowing through its second half.

### What breaks first

#### 1. The cache on object storage over the internet

With the document server's cache in object storage over DEV's internet link, as production has it, a save took 4–7 s
at the median and 6–17 s at p95, from one session to eight. The bar's p95 of 10 s sits inside that spread, so the
verdicts did not follow the load: 4 and 6 sessions held, 3 and 5 broke. With the cache moved to an object store in the
cluster (MinIO), the same saves took 1.3–1.7 s at the median.

| Run | Storage | Sessions | Hold | Callback p50 (s) | p95 | p99 | Forcesaves/min | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| clients-1 | internet | 1 | 5 min | 4.21 | 5.70 | 5.70 | 2.2 | holds |
| clients-1 | internet | 2 | 5 min | 5.68 | 8.88 | 8.88 | 3.5 | holds |
| prod-rollout-3 | internet | 3 | 10 min | 7.14 | 11.30 | 14.95 | 5.7 | breaks: p95 |
| clients-1 | internet | 4 | 5 min | 4.50 | 6.56 | 9.49 | 7.8 | holds |
| prod-rollout-2 | internet | 5 | 10 min | 6.09 | 13.71 | 18.29 | 9.7 | breaks: p95, 1 timeout |
| smoke-2 | internet | 6 | 2 min | 4.08 | 8.38 | 12.73 | 18.9 | holds |
| clients-1 | internet | 8 | 5 min | 5.93 | 17.18 | timed out | 19.1 | breaks: p95, 3 timeouts |
| s3-rollout-1 | in the cluster | 5 | 10 min | 1.48 | 2.14 | 2.55 | 15.1 | holds |
| s3-smoke-1 | in the cluster | 6 | 2 min | 1.42 | 2.57 | 2.60 | 24.7 | holds |

The converters did the same work either way: about 1.9 core-seconds a save over the internet (1.5–2.1) and in the
cluster (1.5–2.0). Over the internet they waited: each converter pod averaged at most 0.3 core through those steps. The
storage link is the first limit on DEV. Production's link to its object storage was not measured (see "How DEV differs
from PROD").

#### 2. Converter throughput

With storage in the cluster, saves go as fast as the converters convert them.

- **One conversion per converter pod at a time.** In the connection check (editors-1), 25 and then 50 editors saved
  at once on two converter pods; their callbacks came mostly in pairs, the pairs 1.2–2.0 s apart. No converter pod
  ever used more than 1.31 cores of its 2-core limit, and at 4 converters and 60 sessions each pod sat at about 1.2
  cores while up to 56 conversions waited. Our build's entrypoint sets one worker per pod, so pods are the knob today.
- **A save costs about 1.9 core-seconds** of converter CPU in the rollout steps (1.8–2.0), 1.5–1.7 in the chat steps.
  At about 1.2 cores, one worker takes about 1.6 s a save, which is the callback median seen (1.3–1.7 s).
- **One worker does about 37 saves a minute.** Four converters at 60 rollout sessions completed 128–159 saves a minute
  through the hold, about 146 on average, while the conversion queue grew and the median callback rose from about 2 s
  to about 9 s. That is the only step whose converters saturated outside its last 30 s. Two converters carried up to
  48 saves a minute and eight carried 160 without saturating, so their ceilings were not reached.
- **A converter's CPU limit sets each save's time.** In the DEV profile each converter is capped at 0.5 core: a save
  took 3.5–4.2 s at the median, about 2 core-seconds at half a core. That is about 15 saves a minute per pod, about 30
  for two (estimated; no step's steady rate reached it).

| Converters | Profile | Sessions | Forcesaves/min | Converter cores, all pods (mean) | Cores per pod (mean / peak) | Callback p50 (s) | Callback p99 outside the last 30 s (s) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2 | production | 15 | 43.5 | 1.33 | 0.67 / 1.22 | 1.55 | 2.68 |
| 2 | production | 20 | 47.8 | 1.53 | 0.76 / 1.29 | 1.71 | 4.38 |
| 4 | production | 30 | 82.2 | 2.51 | 0.63 / 1.24 | 1.55 | 3.41 |
| 4 | production | 60 | 144.5 | 4.50 | 1.13 / 1.28 | 2.18 | 9.18 |
| 8 | production | 60 | 159.6 | 5.38 | 0.67 / 1.21 | 1.58 | 2.40 |
| 2 | DEV | 8 | 16.6 | 0.56 | 0.28 / 0.50 | 4.24 | 8.06 |
| 2 | DEV | 10 | 17.4 | 0.57 | 0.29 / 0.50 | 4.17 | 7.23 |

**Bursts find this limit first.** Every callback timeout that was not the converter bug, 31 in all the runs' steps,
came in its step's last 30 s. There a rollout step's forcesave rate was 1.5 to 3.6 times the rest of the step (about
twice in most), the conversion queue grew, and saves sent in the last seconds waited behind it:

| Run, step | Forcesaves per 30 s, before the last 30 s | In the last 30 s | Callback p95 before (s) | Callback p95 in the last 30 s (s) | Timeouts in the last 30 s |
| --- | --- | --- | --- | --- | --- |
| clients-1, 8 (internet) | 10.0 | 15 | 11.63 | timed out | 3 |
| prod-rollout-2, 5 (internet) | 4.8 | 10 | 10.28 | timed out | 1 |
| s3-rollout-1, 15 | 21.3 | 45 | 2.35 | 18.58 | 0 |
| s3-rollout-1, 20 | 23.9 | 49 | 3.17 | 22.32 | 1 |
| conv4-rollout-60, 60 | 74.2 | 112 | 7.72 | timed out | 24 |
| conv8-rollout-60, 60 | 78.4 | 162 | 2.10 | 18.46 | 0 |
| dev-s3-rollout-1, 10 | 8.5 | 22 | 6.09 | 27.68 | 1 |

Where an in-cluster step's callback p99 reached 17–28 s, it came from these seconds: before them it stayed under
10 s.

#### 3. A converter bug loses a save

Now and then a conversion fails at its start, and that forcesave never calls back. The converter logs, for the
document's key:

```text
receiveTask Error: EEXIST: file already exists, mkdir '/tmp/ASC_CONVERT…/source'
```

13 such lines came in all the runs. Each was for a document whose forcesave had gone 0.1–3.8 s before, and each of
those forcesaves had no callback within the 30 s wait: a lost save. No such line came without a lost save. That is 13
in the 9,838 forcesaves of all the steps, about one in 750, on Word documents, spreadsheets and decks alike, at 5
sessions as at 60. None came in the 1,587 forcesaves of the chat steps or the 700 of the DEV-profile steps. At about
500 forcesaves a step (15 to 20 rollout sessions), about half the steps lose a save to it.

| Run, step | Forcesaves | Lost to the bug | Other lost saves |
| --- | --- | --- | --- |
| smoke-1, 6 (set aside) | 51 | 1 | 5, the deck flush (#538) |
| prod-rollout-1, 5 (set aside) | 115 | 1 | 1 timeout in the last 30 s |
| s3-rollout-1, 17 | 519 | 1 | 0 |
| s3-rollout-1, 20 | 566 | 2 | 1 timeout in the last 30 s |
| conv4-rollout-1, 15 | 491 | 1 | 0 |
| conv4-rollout-30, 30 | 900 | 3 | 0 |
| conv4-rollout-60, 60 | 1,633 | 1 | 24 timeouts in the last 30 s |
| conv8-rollout-60, 60 | 1,812 | 3 | 0 |

It set the knee of the 2-converter ramp (17), and it alone broke the 4-converter steps at 15 and 30 and the 8-converter
step at 60. It is a document server bug, not a capacity limit. Its cause has not been found.

#### Not the limit

- **docservice:** at most 0.15 core of its 2 (at 160 saves a minute), 0.17 while 50 editors opened at once; memory at
  most 195 MiB.
- **The proxy** in front of every editor connection: it grows with connections, about 4 millicores each. At most 0.24
  core with 60 rollout editors, 0.49 with 120 chat editors. It runs without a CPU limit, as in production.
- **RabbitMQ:** 1.6–1.9 cores at the median of every step, from one session to sixty, with samples from 0.7 to 3.6
  cores; memory at most 340 MiB. The same at any load, so likely its schedulers' busy-wait, not work.
- **The database:** at most 18 connections (at 8 converters and 60 sessions; at most 14 in every other step).
- **The in-cluster object store:** at most 0.82 core.
- **Editor connections:** no cap. 25 and 50 editors at once each opened, edited and flushed true, and the chat step at
  60 sessions held 120 editors. No step had a connection-limit message or any editor error.
- **The bar's other rows:** forcesave replies p99 at most 0.18 s (the longest 1.15 s), flushes at most 0.09 s, opens
  p95 at most 5.1 s. No open, edit or flush failed in any run.

### Load-client cost

At the agent pace, one runtime-office page costs about 50 millicores on average (46–57 across the ramp steps) and about
280 MiB (272–305) above an idle process of 130–200 MiB.

| What | CPU | Memory |
| --- | --- | --- |
| One client, 1 page (clients-1) | 0.08 core mean | 430 MiB |
| One client, 8 pages (clients-1) | 0.42 core mean, 1.0 peak | 2.4 GiB peak |
| One client, 20 pages: 10 chat sessions and their watching editors (s3-chat-2, 60 sessions) | 0.98 core mean, 3.6 peak | 5.5 GiB mean, 6.0 peak |

One client ran 8 pages with no failure of its own; that step broke on the document server, on internet storage, so the
client's own limit was not reached. In the ramps each client took up to 20 pages, round-robin (all six full in the chat
step at 60 sessions), and no step had a load-client failure or a leaked page slot.

For runtime-office this means its default of 6 pages per process (the README's estimate: 0.5–0.7 GB a page) is about
1.8 GiB and 0.3 core at this pace. These are the small sample documents with short edits; real documents and reads cost
more. runtime-office is one process with one Chromium: 60 chat sessions (120 pages) take 20 instances at 6 pages, 6
at 20.

### How DEV differs from PROD

Where the difference could move the knee:

- **The link to object storage.** On DEV it added about 3–6 s to the median save and broke the bar from 3 sessions.
  Production reaches its object storage over a link of its own, not measured here. Measure it before deciding where
  the cache goes: one session's save times are enough, since they changed little with load.
- **Postgres on network storage.** DEV's document server database lives on a single Postgres instance on network
  storage, shared with the rest of DEV. The document server held at most 18 connections, and no row of the bar
  pointed at the database, but the runs did not measure its query times.
- **The CPU model.** A save costs about 1.9 core-seconds on the document server's node. A faster or slower CPU moves
  each save's time and the saves a worker does per minute in proportion.

### What the scaling design should take from this

1. **Keep the cache near the document server.** Over DEV's internet link the median save took 4–7 s at any load; in
   the cluster, 1.3–1.7 s. Measure production's link first.
2. **Size converters by the forcesave rate.** About 1.9 core-seconds a save; one worker does about 37 saves a minute
   at about 1.2 cores. At this tool's pace that is 12–15 rollout sessions or about 45 chat sessions per worker, before
   room for bursts, which doubled or tripled the rate here. A worker uses at most 1.3 cores of a 2-core pod: run more
   workers per pod (our build's entrypoint fixes one) or more pods. Do not cap a converter far below that: the cap sets
   each save's time (0.5 core: about 4 s).
3. **Fix the converter's EEXIST bug.** It loses about one forcesave in 750, at any load.
4. **RabbitMQ's scheduler flags.** Set `+sbwt none +sbwtdcpu none +sbwtdio none` and check its CPU: 1.6–1.9 cores at any
   load today, not part of the limit.
5. **docservice is not the limit at these loads.** 120 editor connections and 160 saves a minute used at most 0.15
   core of its 2 and at most 18 database connections. Its own limit was not reached.
6. **runtime-office's page cap and single process.** A page costs about 50 millicores and 280 MiB at this pace; one
   process held 20 pages without a failure. At 6 pages per instance and one Chromium per process, many sessions mean
   many instances.

## How to read the runs

- **The bar.** A step holds when, through its hold: no lost save (every turn's save calls back within 30 s and holds
  the turn's last edit); forcesave replies p99 under 5 s and none over 10 s; callbacks p95 at most 10 s and p99 at most
  30 s after the turn's end; every flush acknowledged within 10 s; opens p95 at most 15 s with no failure; no editor
  error.
- **The workloads.** A rollout session works turns back to back at a recorded agent pace (median 1 edit a turn, about
  10 s between edits). A chat session also holds a watching editor on its document and idles 30–90 s between turns.
  The documents are runtime-office's samples: 35% Word, 25% spreadsheets, 30% decks, 10% PDFs.
- **The steps.** Each step's sessions start over 60 s and hold 600 s from the last start (the smoke runs 20 s and
  120 s, the client check 60 s and 300 s). Since prod-rollout-2, each step starts once the conversion queues have been
  empty for 30 s.
- **The sessions are simulated.** No model takes part. The tool drives runtime-office's pages and plays the harness
  toward the document server: it serves the files, sends forcesave at each turn's end, takes the callbacks and checks
  every saved file for the turn's last edit.
- **Slightly optimistic.** The tool answers file requests and callbacks as soon as its own work is done; the runtime
  also reads and writes storage and its database first. Opens and callbacks are a little faster here than in
  production.
- **The step's end was a burst.** In the first measurement's runs every session's turn was cut short at the step's
  end and saved at once, so a rollout step's last 30 s carry about twice its save rate. Production has no such
  instant, though anything that ends many turns together would. Read the timeouts in those seconds as what a burst
  does, not as the steady rate's limit. The tool no longer does this from #540 on: at a step's deadline a turn not yet
  saved is abandoned, unsaved and not judged, and the hold judged ends there. Every page still closes at the deadline,
  and the closing saves that sets off go ahead of the forcesaves still waiting (the re-measurement's "The step's end").
- **The profiles.** Production: the chart's defaults, docservice at 2 CPU and two converters at 2 CPU each, with CPU
  limits equal to the requests. DEV: docservice and each converter at 0.5 CPU. The proxy and RabbitMQ have no CPU
  limit in either, as in production. The 4- and 8-converter runs change only the converters' number.

| Run | What | Storage, profile | Result | Counted |
| --- | --- | --- | --- | --- |
| smoke-1 | 6 rollout sessions, one client | internet, production | broke: deck saves raced the edit | set aside: the deck flush, fixed in #538 |
| smoke-2 | the same, after the fix | internet, production | held; every save with its edit | a path check |
| clients-1 | one client, 1, 2, 4, 8 pages | internet, production | 8 broke on the document server | the client check |
| editors-1 | 25 and 50 editors at once | internet, production | all opened, edited and flushed; 17 saves within 30 s each time | the connection check |
| prod-rollout-1 | the ramp | internet, production | 5 broke (callback p95 19.52 s) | set aside: the document server was still saving the connection check's 50 closed documents when it began (its log has callback errors for their keys at the step's start); each step now settles first |
| prod-rollout-2 | the ramp | internet, production | 5 broke | counted |
| prod-rollout-3 | steps 3 and 4 | internet, production | 3 broke | counted |
| s3-smoke-1 | 6 rollout sessions | in the cluster, production | held | a path check |
| s3-rollout-1 | the ramp | in the cluster, production | capacity 15, knee 17 | counted |
| s3-chat-1 | the ramp | in the cluster, production | 5 to 40 held; no room for 80 | counted |
| s3-chat-2 | steps 50 and 60 | in the cluster, production | both held | counted |
| conv4-rollout-1 | steps 15, 30, 60 | in the cluster, 4 converters | 15 broke on the bug; the ramp stopped there | counted |
| conv4-rollout-30 | step 30 | in the cluster, 4 converters | broke on the bug | counted |
| conv4-rollout-60 | step 60 | in the cluster, 4 converters | broke: saturated, timeouts in the last 30 s | counted |
| conv8-rollout-60 | step 60 | in the cluster, 8 converters | broke on the bug | counted |
| dev-s3-rollout-1 | steps 5 and 10, then 7 and 8 | in the cluster, DEV | capacity 8, knee 10 | counted |
| r2-rollout-2conv | steps 10, 20, 30, 40 | in the cluster, production, converter fixed | all held | counted |
| r2-chat-2conv | steps 40, 60, 80, 120 | in the cluster, production, converter fixed | all held; 120 filled the fleet | counted |
| r2-rollout-4conv | steps 30, 45, 60, 90, then 75 and 82 | in the cluster, 4 converters, converter fixed | capacity 75, knee 82 | counted |
| r2-rollout-8conv | steps 60, 90, 120, then 105 and 112 | in the cluster, 8 converters, converter fixed | capacity 112, knee 120 | counted |
| r2-rollout-dev | steps 5, 10, 15, then 12 and 13 | in the cluster, DEV, converter fixed | capacity 13, knee 15 | counted |

## Findings fixed along the way

**A deck's last edit could miss the turn-end save (#538).** When the agent's editor is alone in a deck, the slide
editor's modified flag reads false from the commit until its autosave sends the change, 1–2 s later. So runtime-office's
flush answered "flushed" at once, and the turn-end forcesave raced the autosave: it got "nothing to save" or saved the
version before. In smoke-1, two forcesaves answered "nothing to save" after an edit and three saved decks lacked the
turn's last edit. The same path serves the harness at every turn's end. The fix waits for the editor's "all changes
sent" event after a slide edit, and flush applies the same rule for every format. smoke-2, on load clients built with
it, saved every turn's edit, decks included.

**The converter lost about one save in 750 (invergent-ai/office-server#1).** It listed a save's storage folder by
prefix without a trailing slash, so `…_57` also matched an earlier `…_571/` save folder, and the conversion failed at
its start with EEXIST. Our document server build `office-docs:acbdc99` carries the fix. The re-measurement's 24 steps
had no converter error line in 28,199 forcesaves.
