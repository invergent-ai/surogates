# Relational placement

Why the whiteboard is moving off absolute coordinates, and the contract
that replaces them.

## The problem, from production sessions

The original design hands the model a PNG plus a geometry note and asks
it to answer with absolute canvas coordinates. That quietly makes the
model four engines it is bad at being:

| engine | observed failure |
| --- | --- |
| OCR | `∫` read as `S`; `2x + 1` read as `2×4 ÷ 1` |
| geometry | image→canvas conversion done for x and skipped for y; an answer at row 17 of a 16-row grid |
| layout | one sentence wrapped into a 9-line, 877-unit tower; wrapped text printed through the next command |
| diff | its own `(e^x+C)²` answered as if the user had written it |

Each fix so far taught the model more geometry — an occupancy grid, a
cell-size formula, a handwriting height, four coordinate systems in one
note. The note grew; the failure class stayed. Models are weak at
exactly these jobs and strong at semantics, structure and relations —
which the old contract barely used.

## The inversion

**The model never computes a coordinate. It names a thing and a
relation; the client owns geometry.**

A person at a whiteboard says "put the 3 after the equals sign", not
"place glyphs at (700, 740)". The tool now speaks that language:

```json
{"tool": "draw_formula", "latex": "3", "anchor": "latest", "side": "right"}
{"tool": "write_text",  "text": "Diverges: e^x grows without bound.",
 "anchor": "latest", "side": "below"}
{"tool": "draw_formula", "latex": "= e^2 + 1", "replaces": "toolu_01X"}
```

- `anchor` — what the answer relates to: `latest` (the user's newest
  ink), `selection` (their lasso), or the id of one of the agent's own
  earlier draw calls (the turn note lists them).
- `side` — `right` (default), `below`, `above`, `left`.
- `replaces` without coordinates — the revision inherits the replaced
  object's place.
- Absolute `x`/`y` remain valid and always win when present: they are
  the escape hatch, not the norm.

## Resolution (client, deterministic)

`layout.ts` resolves anchored commands into absolute ones at **fold
time**, against the board **as it is then**:

- target rect: `replaces`/call-id → union bounds of that origin's
  surviving objects; `latest`/`selection` → the rect in the turn's
  user-message metadata (surfaced on `AgentChatMessage.metadata`, so
  replay from the event log resolves identically).
- sizing: a formula or short answer matches the anchor's height; prose
  gets a readable size and a width that makes it read across, from real
  measurement — the tower and the wrap collision become unconstructible
  rather than validated against.
- position: beside the target with a gap proportional to its height;
  nudged downward while it overlaps existing objects.

Because resolution happens at fold time, a user who drags things around
mid-turn no longer corrupts placement: "after the equals sign" follows
the equals sign.

## What this retires

Immediately: the model-side wrap/tower failure modes, the image→canvas
conversion for placement, and coordinate staleness. Progressively (see
staging): the occupancy grid, the cell-size formula, and most of the
geometry note.

## Staging (all three built)

1. **Relational placement** — beside the absolute path, on the existing
   `whiteboard_draw` tool. No new commands: two optional fields.
2. **Ink clustering + labelled marks** — user ink grouped into
   addressable clusters (`A1, A2, …`), the agent's objects labelled
   (`B1, B2, …`), the same ids drawn on the atlas and canvas, listed in
   the note, and accepted as `anchor`/`replaces`.
3. **Persistent transcription** — the model returns `readings:
   [{mark, text}]` with its draw; each is stored against the exact
   strokes it covers (`doc.readings`, keyed by stroke ids, so added ink
   reads as new) and handed back as text on every later turn. The user
   sees each reading under its ink and corrects it in place; a user
   correction outranks any later agent reading. The confirmed pairs
   accumulate as training data.

No new models at any stage: clustering and layout are algorithms; the
one model-shaped job (reading new ink, once) stays on the session VLM.

## Slots and intent

Three production sessions showed the model inferring three things at
once from spatial convention alone — *where* the result goes, *what*
to do, and whether to *perform* it or talk about it — and each failed
once: `( ln|x|+C )²` explained instead of squared; `H _ USE` asked
about instead of filled; the trailing `=` the only one that worked,
because maths carries its conventions and prose and drawing do not.

**Slots** make *where* explicit and domain-blind. A slot is an empty
box the user reserves for the result, placed with the **Answer** tool
(robot icon): a click drops an answer-sized box at the pen tip, a drag
sizes one. Gesture recognition was tried first — a drawn box, then a
spiral — and dropped: nobody draws a box in one stroke, box-like and
loop-like strokes are everywhere in real writing, and a deliberate
placement has no false positives. Slots are labelled `S1, S2, …` beside
the ink and agent marks. It is
the one gesture with the same meaning everywhere: the missing letter,
the value after `=`, the paragraph under a heading, the cat in an empty
frame. The model fills it with `anchor: "S1", side: "in"`; the client
fits the content into the box (text shrinks to fit, artifacts take the
box, sketches are drawn in a 1000×1000 local space and scaled in) and
the slot disappears. A call that leaves a slot empty is rejected by the
tool before the client folds anything, so the retry cannot land beside
a miss. The user can attach a hint (what belongs here) when drawing it.

**Intent** makes *what* inspectable: every call declares `fill`,
`continue`, `transform` or `respond`. And the guidance settles *act*:
if you can produce the thing, produce it on the board; ask only when
you cannot. PenEcho (`study/penecho`) has the model-declared intent and
an `observedText` transcription, and an action menu for the user; it
has no slot or placeholder mechanism.
