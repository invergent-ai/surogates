# Offline experiments

Evaluations that run against stored run traces — no agent, no browser, no
benchmark execution. A trace is a paid artefact; asking new questions of one
should be free.

## doubter_eval.py

Would a critic model, shown the agent's action history before it answered,
flag the runs that turned out wrong — and leave the correct ones alone?

`runs/<id>/outcomes.json` gives the labels, `trajectory.md` the history. Both
halves of the question matter: a doubter that catches every failure by
objecting to everything costs a rerun on every task and makes the agent worse.

```bash
python experiments/doubter_eval.py dev-022                        # summary model
python experiments/doubter_eval.py dev-022 --model google/gemini-3.1-pro-preview
```

Writes `runs/<id>/doubter_verdicts.json` so score thresholds can be swept
without paying for the pass again.

### Result on dev-022 (110 trajectories, 68 pass / 42 fail)

| model | best precision | detection there | false positives | passed / failed median score |
| --- | --- | --- | --- | --- |
| deepseek-v4-flash (summary preset) | 59% @ score<=0 | 40% | 18% | 5.0 / 1.0 |
| gemini-3.1-pro | **68% @ score<=3** | 64% | 19% | **10.0** / 2.0 |

**The doubter needs a strong model.** Cross-family alone does not carry it:
deepseek is a different family from the executor (claude-sonnet-5) and still
scored *correct* runs a median 5 — it could not tell good work from bad. The
pro model scores them 10 against 2 for failures.

Against a 38% base rate, deepseek's 59% precision is barely better than
chance. Do not wire in a cheap doubter on the theory that criticism is easier
than generation; it was measured and it is not.

Still untested: whether a rerun actually fixes a caught failure. The 68%
precision figure is the ceiling on value, not the value.
