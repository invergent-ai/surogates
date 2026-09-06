# ECBench Results

Scored on upstream's primary metric: end-of-horizon **total assets**
(bank + platform wallet + pending settlement), and its multiple of the
¥100,000 opening stake. Deterministic settle, no judge. Not comparable
to the public ECBench leaderboard (different scaffold by design — see
README); rows are only comparable to each other, at the same PIN, the
same `--max-days`, and the same served model. "Model served" is the
model that actually answered the calls — tier sentinels resolve per
request, so check the traces, not the config.

Every counted run records two things: its row, and its **per-episode
table** copied from the run's `report.md` — a mean without the episode
spread and failure reasons is just a number.

| Date | Run | Where | Model served | Size | Mean assets | Multiple |
| --- | --- | --- | --- | --- | --- | --- |

*(no counted runs yet — the first full-horizon run appends here)*

### Smaller runs

Pilots and pipeline checks (`--max-days` below 365). Too small to read
as a score — they exist to prove the protocol and estimate cost.

| Date | Run | Model served | Size | Mean assets | Multiple |
| --- | --- | --- | --- | --- | --- |

## Method notes

- One config change per run, recorded in the row's notes. `--max-days`,
  the PIN, and the NPC renderer mode all define the economy — never
  compare across them.
- The world is deterministic; episode variance is entirely the agent.
  Counted runs use ≥3 episodes; report mean and range.
- Reference points from the public leaderboard (different scaffold —
  context, not targets): top agent ¥1,431,425 (14.3× stake), best
  open-weight ¥416,252 (4.2×), 10 of 90 published episodes ended in
  bankruptcy.
