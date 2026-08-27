# Vendored from PenEcho

These files are copied **verbatim** from [PenEcho](https://penecho.ai)
([github.com/penecho/penecho](https://github.com/penecho/penecho)),
licensed **AGPL-3.0-only** — the same licence as this project.

Copied at upstream `d580110` (Release PenEcho 1.0.1, #40).

| File | Upstream path | Role |
| --- | --- | --- |
| `draw.js` | `public/draw.js` | Validates and renders every `draw` primitive; computes the union bounding box including cubic and arc extrema, stroke padding and arrowheads |
| `mixed-text.js` | `public/mixed-text.js` | Segments a text body into markdown and bare-LaTeX runs |
| `selection.js` | `public/selection.js` | Closes a freehand lasso path and clips to it |

They are zero-dependency UMD modules and are intentionally **not
modified**. Fixes belong upstream; re-copy to update.

`tests/vendor-draw.test.ts` is a port of PenEcho's own `test/draw.test.js`,
kept so a re-copy that changes behaviour fails loudly here rather than
silently at render time.

`index.ts` is the only place that asserts these modules' shapes, so a
re-copy that changes an export surfaces in one file instead of at every
call site.

The whiteboard system prompt at
`surogates/harness/prompts/guidance/whiteboard.md` is also adapted from
PenEcho and carries its own attribution.
