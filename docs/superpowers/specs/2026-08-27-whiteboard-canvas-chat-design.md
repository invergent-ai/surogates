# Whiteboard canvas chat

## Problem

Every conversation surface we ship is a message list. That shape loses
anything whose meaning is spatial: handwritten mathematics, a sketched
system diagram, an arrow drawn from one paragraph to another meaning
"explain this bit", a graph the user wants annotated in place. Users
photograph paper and paste it in, and the answer comes back as prose
that has to be mentally re-registered onto the original.

PenEcho (`study/penecho`, AGPL-3.0-only, vendored read-only) solves this
with a canvas the model both reads and writes. This spec adds that
surface to Surogates as a third chat type, alongside the message thread
and the browser pane, reusing PenEcho's proven protocol and the parts of
its client that are actually portable.

### What PenEcho is, distilled

44,700 lines of vanilla JS. The mechanism underneath is six steps:

1. Infinite canvas (20,000 x 20,000 logical, sparse 512x512 raster
   tiles), pen / eraser / pan / zoom.
2. After a stroke settles, crop a white-background PNG around the
   newest ink.
3. POST that image plus geometry metadata (`sourceRect`, `imageScale`,
   `latestInput.imageRect`, an 8x8 hotspot grid of the pen trajectory,
   an optional magnified focus inset, typed-text ground truth) to one
   vision model behind a ~4 KB system prompt.
4. The model returns strict JSON: `{intent, observedText, message,
   commands[]}`.
5. Commands land in an unconfirmed draft layer, each item individually
   movable, resizable, acceptable, discardable.
6. Accepting commits into the tiles.

The command vocabulary is the product surface:

| Command | Effect | Our equivalent |
| --- | --- | --- |
| `write_text` | text at x,y with `maxWidth`/`fontSize` | new |
| `draw_formula` | LaTeX rendered to SVG | `@streamdown/math` (existing SDK dep) |
| `plot_function` | evaluates `f(x)`, renders a plot | `chart` artifact |
| `draw` | line/smooth/rect/ellipse/circle/arc primitives | vendored `draw.js` |
| `erase` | rect or freehand-path erase | new |
| `html_widget` / `diagram_source` | sandboxed HTML / mermaid, dot, vega-lite | `create_artifact` (`html`, `svg`) |
| `animate_scene` | declarative Canvas2D animation | out of scope for v1 |
| `widget_patch` | unified-diff refine of a widget | out of scope for v1 |

### What is and is not portable

Directly copyable, zero-dependency UMD modules:

| File | Lines | Role |
| --- | --- | --- |
| `public/draw.js` | 287 | validates and renders every `draw` primitive; computes the union bounding box including cubic and arc extrema, stroke padding, arrowheads |
| `public/mixed-text.js` | 256 | segments a text body into markdown and bare-LaTeX runs |
| `public/selection.js` | 192 | closes a freehand lasso path and clips to it |
| `public/animation.js` | 337 | normalises and renders `animate_scene` (deferred, not v1) |

Not portable as a module: the canvas itself.
`src/client/app/{core,canvas-runtime,persistence,ai-runtime,ui-bootstrap}.js`
are five *fragments of a single IIFE* concatenated by
`scripts/build-client.js` — `core.js` opens `(() => {`, `ui-bootstrap.js`
closes `})();`. None of them parses standalone. `core.js` binds the DOM at
init (`document.querySelector("#screen")`), and the five together make 322
selector calls against 235 element ids in a 680-line `index.html`. It is an
application, not a library.

Within `canvas-runtime.js` (4,656 lines), 944 lines are the widget and
Refine system, which `place_artifact` replaces outright. The tile,
render and navigation core is comparatively small.

The single most valuable asset is not code at all: the system prompt at
`src/server/main.js:673-706`, roughly 4 KB covering handwriting and CJK
transcription discipline, spatial-gesture reading (a drawn box selects,
an arrow points at where the answer goes, a label near the arrow is an
instruction and not content), response-language selection, and text
layout rules. It transfers verbatim.

Both projects are AGPL-3.0-only, so copying is clean with attribution
preserved.

## Design

### Session shape

A whiteboard session is an ordinary session with
`config.surface == "whiteboard"`.

Deliberately **not** a new `channel` value. `channel` is server-pinned
(`"web"` for the agent web app, `"studio"` for the ops Work console) and
governs six unrelated frozensets in `surogates/channels/constants.py` —
end-user enrollment, inbox-item retirement, delivery adapters,
interactive-prompt rendering, service-account authentication shape,
direct-UI streaming. A new channel value would have to be added to or
deliberately excluded from each, and the failure mode of getting one
wrong is silent (see `INTERACTIVE_PROMPT_CHANNELS`: a channel omitted
from it makes `ask_user_question` write no outbox row at all, so the
session parks for 30 minutes waiting for an answer nobody was asked
for). A whiteboard is a *surface within* the web and studio channels,
not a new delivery platform.

`config` is already a client-supplied JSONB dict forwarded verbatim by
both hosts, so this needs no new field anywhere.

Naming: `surogates/board/` is taken by the multi-agent coordination
board. The new package is `surogates/whiteboard/`.

### Canvas data model

Vector objects in a z-ordered list — array position is z-order. No
raster tiles.

```ts
type WbObject =
  | { id, kind: "ink",      pts: number[], width, color, pressure? }
  | { id, kind: "draw",     origin, types, items, width?, tension?,
                            closed?, fill?, arrows? }
  | { id, kind: "text",     x, y, text, fontSize, maxWidth, lineHeight }
  | { id, kind: "formula",  x, y, latex, fontSize }
  | { id, kind: "artifact", x, y, w, h, artifactId, version }
  | { id, kind: "erase",    mode: "rect" | "path", ... }
```

Dropping PenEcho's tiles is what makes the rest cheap. With every
element an object rather than committed pixels:

* move, resize and delete are one implementation covering user ink,
  agent output and placed artifacts alike;
* undo/redo is list history, not per-tile bitmap snapshots;
* the agent's output can simply *arrive as the active selection* —
  drag to reposition, handles to resize, Delete or Cmd+Z to reject —
  which replaces PenEcho's entire unconfirmed-draft state machine and
  its accept/discard chrome with no loss of capability;
* the document serialises as plain JSON.

The accepted cost is a bounded object count. PenEcho's tiles exist to
support unlimited ink and raster erase; we cap objects per canvas
instead and treat `erase` as an object that clips those beneath it.

### Persistence: single writer

The **client is the sole writer** of the canvas document. It PUTs
`_whiteboard/canvas.json` on a debounce through the existing
`POST /sessions/{id}/workspace/upload`. The `_` prefix marks the
directory server-internal, matching `_artifacts/`, so the workspace file
browser hides it.

The agent never writes that file. Its output is a command list in the
event log; the client folds those commands into its in-memory document
and includes them in the next PUT. One writer, so no race and no sync
protocol.

The document carries `lastEventId`. On load the client reads the file
and then replays any `whiteboard_draw` tool calls with a higher event
id, so a tab closed between an agent reply and the next debounce flush
loses nothing — the event log is the recovery tail, not a second source
of truth.

### The turn: what goes up

On an explicit **Ask** (there is no auto-fire on stroke settle; see
*Rejected alternatives*), the client builds a white-background PNG atlas
cropped to content near the latest input, capped at 2048x1536 —
PenEcho's numbers — and sends:

```ts
adapter.sendMessage({
  text,                     // typed question; may be empty
  images: [atlas],
  metadata: { whiteboard: {
    sourceRect,             // full-resolution global rect the image covers
    imageScale,             // global units -> image pixels
    latestInput,            // authoritative attention rectangle
    hotspots,               // 8x8 grid of the current pen trajectory
    viewport,
    selection?,             // lasso rect, when the user selected before asking
    typedInput?,            // exact typed text, as transcription ground truth
    canvasSize,
    mode: "sketch" | "deep",
  }},
})
```

`SendMessageRequest.metadata` is already free-form and documented as
"the harness only reads keys it understands and ignores the rest", so
this needs no API schema change. The harness renders the geometry into a
transient system note for the turn, exactly as `view_context` is
rendered today by `_view_context_note_from_metadata`
(`harness/loop_messages.py:55`).

Two guards:

* `metadata` is currently unbounded. `metadata.whiteboard` gets a
  server-side byte cap in the send-message route — it is client-supplied
  data crossing a trust boundary into the event log.
* Canvas snapshots are **cumulative**: snapshot N contains everything
  snapshot N-1 did. Replaying all of them is pure waste and would
  dominate context within a dozen turns. Context replay keeps only the
  newest whiteboard image and replaces older ones with a short
  placeholder — the same shape as
  `ContextManager.prune_stale_browser_states` (`harness/context.py:338`),
  with `keep_last` fixed at 1 because, unlike browser state, there is no
  case for holding two.

### The turn: what comes down

One new harness tool, `whiteboard_draw`, registered under
`toolset="whiteboard"`:

```json
{"commands": [
  {"tool": "write_text",    "x": 0, "y": 0, "text": "",
                            "fontSize": 0, "maxWidth": 0, "lineHeight": 1.35},
  {"tool": "draw_formula",  "x": 0, "y": 0, "latex": "", "fontSize": 0},
  {"tool": "draw",          "origin": [0, 0], "types": [], "items": []},
  {"tool": "erase",         "mode": "rect", "x": 0, "y": 0, "w": 0, "h": 0},
  {"tool": "place_artifact","artifact_id": "", "x": 0, "y": 0, "w": 0, "h": 0}
]}
```

A tool rather than JSON parsed out of the assistant's text, for three
reasons: the tool schema *is* the structured-output contract, so
malformed output is a provider-level retry instead of a parse failure;
it emits a `tool.call` event the SDK already streams and reconciles; and
it participates in the existing per-session tool filtering.

The handler validates the command list — bounds, per-command counts,
canvas limits, all ported from PenEcho's server-side validators — and
returns a compact acknowledgement. The SDK renders from the *call
arguments* on the `tool.call` event, so drawing begins as the call
streams rather than after the result lands.

`TOOL_LOCATIONS["whiteboard_draw"] = ToolLocation.HARNESS` in
`tools/router.py` is required, not optional: an unlisted tool defaults
to the sandbox executor and fails as `Unknown tool`.

`whiteboard_draw` is dropped from the schema set for non-whiteboard
sessions through the existing `drop_unusable_tools` mechanism
(`harness/tool_schemas.py`), which already gates KB, channel and cron
tools the same way.

#### `place_artifact` absorbs the plugin layer

PenEcho's `html_widget`, `diagram_source` and `plot_function` all become
`create_artifact` plus `place_artifact`. Our artifacts subsystem already
provides `markdown`, `table`, `chart` (Chart.js), `html` (sandboxed
iframe), `svg`, `image` and `video`, each with a renderer in the SDK,
versioned storage, a fetch API and a 500 KB cap. Charts, mermaid
diagrams, tables and interactive HTML widgets on the canvas therefore
cost one new command rather than a plugin runtime, a widget host, a
capture bridge and a diff-based refine protocol.

`place_artifact` positions an artifact that already exists; it does not
create one. Producing a new one requires `create_artifact`, which is not
in the `sketch` filter — so authoring a chart or an HTML widget is a
`deep`-mode capability by construction. `sketch` mode can still place or
reposition an artifact created earlier in the session. This is the
intended split, not an oversight: a widget worth building is worth a
full turn.

### Two speeds

The `mode` field on the turn selects between:

* `sketch` — the tool filter narrows to `{whiteboard_draw}` and the turn
  runs on the base tier sentinel. One model round-trip, so ink gets
  answered at something close to PenEcho's latency.
* `deep` — the full tool catalogue and the pro tier sentinel. The agent
  can search, compute, call `create_artifact`, then draw.

Both tiers resolve through the proxy's model sentinels, so this selects
a tier and never a literal model id.

`_tool_filter_for_session` is already evaluated per turn inside the loop
(`harness/loop.py:1638`); it gains the turn's mode as an argument.
`/deep-research` is the existing precedent for a user-triggered per-turn
escalation.

This is the one place the design adds a hook to the harness loop rather
than reusing one. It is confined to reading a single enum off the
current turn's metadata and narrowing a set that is already computed
per turn.

### SDK surface

`<AgentWhiteboard>` is a sibling of `<AgentChat>`, not a fork. Both
drive the already-exported `useAgentChatRuntime` hook — SSE stream,
event reducer, and the independent poll that reconciles against the DB
event log — so sessions, streaming, reconnection, billing, inbox and
title updates work unchanged.

Layout: the canvas fills the surface; a thin tool rail carries pen,
eraser, text, lasso, pan, colour and width; an **Ask** button with a
"think harder" modifier selects `sketch` versus `deep`; a collapsible
transcript drawer renders the ordinary chat thread so the agent's prose,
reasoning and tool activity remain readable.

### Prompting

`guidance/whiteboard.md` carries the ported PenEcho system prompt,
loaded only for `config.surface == "whiteboard"` sessions. Its
substance:

* the attached image is a clean rendering of canvas content around the
  newest input; `latestInput.imageRect` is the authoritative attention
  region; transcribe only the newest ink into `observedText`;
* treat the canvas as a document to *extend*, never to reproduce — given
  `3+2=` place only `5` after the equals sign;
* interpret spatial gestures as instructions: a box or circle selects,
  an arrow connects source to destination, a label near an arrow
  ("more", "why", "explain") requests elaboration and must not be echoed
  into the answer; follow an arrow chain to its final head and place the
  answer in the clear space beyond it;
* choose the response language from the newest substantive user content,
  never from the interface language;
* the model is responsible for layout: every `write_text` must choose
  `x`, `y` and `maxWidth` explicitly, in a blank region near the
  referenced content, never at the canvas origin merely because it is
  empty.

## Rejected alternatives

**Auto-fire after each stroke, like PenEcho.** PenEcho can do this
because each request is one stateless vision call costing a few seconds.
Ours is a session turn: full agent loop, tool catalogue, memory, and
per-turn billing. Firing on every settled stroke would bill a turn per
stroke and expose a latency gap the interaction cannot absorb. The
trigger is explicit; auto-fire may return later as an opt-in toggle once
the `sketch` path's real latency is measured.

**Vendoring PenEcho's client wholesale in an iframe.** Maximum reuse and
everything works on day one — tiles, pressure ink, lasso, undo, the text
editor, MathJax, PNG export. Rejected because it is a visually foreign
island inside our app: no shared theme or Tailwind, postMessage-only
integration, their own i18n and settings and tour and cloud UI to strip,
and we would own 14,500 lines of vanilla JS bound to a 235-id HTML page.
It also reinstates the draft-layer model this design deliberately
replaces.

**Surgically stripping their IIFE into a web component.** Keeps their
canvas code nearly verbatim without the iframe, but the surgery is on a
14,500-line closure with no module boundaries and no test seam.

**A separate stateless `/api/whiteboard/command` endpoint** mirroring
PenEcho's server. Fast and predictable, but it is a parallel system next
to the agent with no memory, tools, skills or history — which is not "a
new chat type", and would need its own auth, billing and rate limiting.

**Storing the canvas as an artifact kind.** Artifacts are agent-authored
and capped at 500 KB; the canvas is primarily *user*-authored and a page
of ink can approach that cap. The workspace file keeps the single-writer
property that makes the sync story trivial.

## Testing

* `draw.js`, `mixed-text.js` and `selection.js` arrive with PenEcho's
  own test files (`test/draw.test.js`, `test/mixed-text.test.js`,
  `test/selection.test.js`) ported to vitest — they are the regression
  suite for the vendored geometry, and vendored code with no test is
  vendored code nobody can safely touch.
* Command validation gets a Python test covering the bounds, count and
  canvas-limit rejections, including the cases PenEcho's validators
  reject (mismatched `types`/`items` lengths, out-of-canvas geometry,
  non-integer coordinates).
* One routing regression test asserting `whiteboard_draw` resolves to
  `ToolLocation.HARNESS` — the failure mode is otherwise a runtime
  `Unknown tool` with no static signal.
* One test that context replay keeps exactly one whiteboard image
  across a multi-turn session.
* One test that `metadata.whiteboard` over the byte cap is rejected by
  the send-message route.
* One test that `whiteboard_draw` is absent from the schema set for a
  session without `config.surface == "whiteboard"`.

## Out of scope for v1

Animations (`animation.js` is vendorable later at 337 lines), widget
Refine via unified diff, the plugin marketplace, cloud publishing,
real-time multi-user collaboration on one canvas, PNG import, desktop
and mobile packaging, Chinese localisation.

## Files

**surogates** (base `master`)

| Path | Change |
| --- | --- |
| `surogates/whiteboard/commands.py` | new — command schema and validators |
| `surogates/tools/builtin/whiteboard.py` | new — the `whiteboard_draw` tool |
| `surogates/tools/router.py` | `whiteboard_draw` -> `HARNESS` |
| `surogates/harness/tool_schemas.py` | `_WHITEBOARD_TOOLS` drop set |
| `surogates/harness/prompt.py` | load `guidance/whiteboard.md` for the surface |
| `surogates/harness/loop_messages.py` | render the whiteboard geometry note |
| `surogates/harness/loop_context_replay.py` | keep only the newest canvas image |
| `surogates/harness/loop.py` | per-turn `mode` into the tool filter |
| `surogates/api/routes/sessions.py` | cap `metadata.whiteboard` |
| `surogates/harness/prompts/guidance/whiteboard.md` | new — ported system prompt |
| `sdk/agent-chat-react/src/components/whiteboard/*` | new — canvas, tool rail, renderers |
| `sdk/agent-chat-react/src/vendor/penecho/*` | vendored UMD modules + attribution |
| `sdk/agent-chat-react/src/index.ts` | export `AgentWhiteboard` |
| `web/src/features/whiteboard/*` | new route and page |

**surogate-ops** (base `main`)

| Path | Change |
| --- | --- |
| `frontend/src/features/work/work-agent-whiteboard-page.tsx` | new |
| `frontend/src/features/work/work-agent-tabs.ts` | tab wiring |
| agent settings | `whiteboard_enabled` capability toggle |
| runtime-config passthrough | carry the capability to the harness |

Session `config` already forwards through `create_live_session`
(`surogate_ops/server/routes/sessions.py:780`), so ops needs no backend
session work.

## Attribution

Vendored files retain PenEcho's copyright headers, and
`sdk/agent-chat-react/src/vendor/penecho/README.md` records the upstream
project, commit, licence (AGPL-3.0-only) and the list of files taken.
The ported system prompt carries the same attribution in
`guidance/whiteboard.md`.
