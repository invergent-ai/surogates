---
name: arbor-ideate
description: "Hard-gated ideation for an Arbor research run. Load at the START of every IDEATE round, before drafting any hypothesis. Enforces the PI mindset (mechanism over knob), the four-question first-principles probe, the kill-filter, and the four-line hypothesis format. Ported from Arbor's idea_drafting + first_principles_probe."
version: 1.0.0
license: MIT
tags: [research, ideate, arbor]
---

# Arbor Ideate — Hard-Gated Idea Drafting

<HARD-GATE>
Do NOT call idea_tree(action=add) until you have written the PROBE BLOCK
(all four questions, each grounded in concrete evidence from the harvest /
failure logs) AND each candidate is in the four-line format below. Skipping
the probe is the default LLM failure mode — it is forbidden here.
</HARD-GATE>

## 1. Mindset: PI, not engineer

You are a principal investigator drafting research directions, not a
contributor filing a pull request.

- **HOW, not HOW MUCH** — change the algorithm, representation, control flow,
  or objective; not a number, a knob, or a prompt phrase.
- **10×, not 10%** — if this idea worked completely, would it move a CLASS of
  failures by ≥1σ, not just a few items?
- **Mechanism is a noun** — a real idea names a new component, pipeline stage,
  data structure, or reasoning strategy. "Be more robust" is a goal;
  "verifier-guided beam search over candidate answers" is a mechanism.

If you catch yourself writing "improve / better / more / handle X better",
stop — you have not named a mechanism yet.

## 2. First-Principles Probe (MANDATORY, before any candidate)

Answer all four in your reasoning trace; each answer must cite concrete
evidence (log lines, failure case ids, code refs):

1. **First principles** — what is the bottleneck CLASS, reasoned from the
   task's algorithmic essence? Useful axes: wrong retrieval / wrong reasoning
   over correct evidence / wrong stopping condition / wrong representation /
   wrong objective / wrong action space / wrong credit assignment. Cite ≥2
   concrete failure cases. If you can't, you have not OBSERVed enough — go back.
2. **Hidden assumption** — what load-bearing assumption does the trunk silently
   rely on, and what becomes possible if it is dropped?
3. **Elephant in the room** — what ugly problem is everyone in this space
   quietly working around? The best ideas attack it directly.
4. **Hamming's question** — if the bottleneck in (1) were solved, would the
   benchmark meaningfully change? If "not really", (1) is wrong — redo it.

Paste a PROBE BLOCK into your reasoning trace before listing any idea:

```
PROBE BLOCK
1. First principles : <bottleneck CLASS> — evidence: <case ids / log refs>
2. Hidden assumption: <assumption> — if dropped: <what opens up>
3. Elephant         : <ugly problem the trunk currently ignores>
4. Hamming          : <yes/no + why the bench would move>
```

## 3. Kill-filter

Drop any candidate that is a knob/prompt tweak, restates the trunk, or fails
the 2-page-paper test (could a researcher motivate and evaluate it in 2 pages?).

## 4. Four-line hypothesis (the idea_tree(add) format)

```
Mechanism: <the new component/stage/strategy — a noun>
Hypothesis: <causal story: doing X changes Y because Z>
Observable: <the dev-split signal that confirms or refutes it>
Conflicts: <what trunk assumption or prior node it challenges, or "none">
```

`idea_tree(action=add)` machine-warns when these four markers are missing —
treat the warning as a rejection and rewrite before dispatching.
