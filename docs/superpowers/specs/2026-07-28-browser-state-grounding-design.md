# Browser state grounding: `browser_evaluate` and markdown page state

**Date:** 2026-07-28
**Status:** Design
**Repo:** `surogates`

## Motivation

[StateAct (arXiv:2607.22798)](https://arxiv.org/html/2607.22798v1) argues that
computer-use agents should read program state directly instead of perceiving
rendered pixels. Its web component "navigates, executes JavaScript, serializes
the DOM to markdown, and clicks by CSS selector, grounding it in structured
state rather than a screenshot."

Our browser stack already follows the principle. `browser_get_state` derives a
role/name tree from the live DOM (`surogates/browser/client.py`), refs are
pinned to CDP backend node ids with a role/name/nth healing fallback, and
`browser_screenshot` deliberately does not inject the image into context. The
headline benefit the paper reports over screenshot-driven agents is already
banked.

Two gaps remain against that description:

1. **No JavaScript execution.** The agent has ten fixed verbs and no way to read
   structured page state. Reading a long table, a hidden input's value, a
   `<select>`'s full option list, or data the page already fetched is either
   impossible or costs many `scroll` + `get_state` round trips.

2. **Page state is a JSON firehose, not markdown.** The snapshot keeps every
   visible element from `querySelectorAll('*')`, and `nameOf` falls back to
   `innerText`/`textContent`, so each ancestor container re-emits its subtree's
   text. Page text is serialized roughly once per level of nesting. There is no
   node cap, and `interactive_only` / `compact` default to `False` with no
   `description` in the schema, so the model has no signal to narrow the
   request.

Historical bloat is already handled: `_SUPERSEDED_STATE_TOOLS` in
`surogates/harness/context.py` collapses every superseded `browser_get_state`
result to a placeholder. What is unaddressed is the **live** snapshot, which
stays in context and is re-paid on the wire at every call.

## Scope

**In scope**

- `browser_evaluate`: run JavaScript in the page and return its value.
- `browser_get_state`: markdown output by default, JSON opt-in, text
  deduplicated at the source, hard node cap, documented parameters.

**Out of scope — and why**

A dedicated web subagent (the paper's third component) was designed and
rejected. `ask_user_question` is on `_DELEGATION_ALWAYS_BLOCKED_TOOLS` in
`surogates/tools/builtin/delegate.py`, because a child session has no surface
for human input. Browser work is precisely where mid-task human input is
needed: expired logins, 2FA, CAPTCHAs, disambiguating results. Take-control
compounds it — `surogates/browser/control.py` keys the control flag by
`session_id` and every `browser_*` call returns `paused_by_user` while a user
holds it, so a delegated child would be paused on the parent's flag with no way
to ask the user anything and no option but to burn its iteration budget. The
paper scores **0.0% binary success on human-in-the-loop tasks**, its worst
category; routing our flakiest subsystem through the one context that cannot
talk to the user would import that failure mode.

The context saving that would justify the machinery is also largely
pre-empted: history is already pruned, screenshots never enter context, and
the two changes in this spec shrink the live snapshot and reduce how often it
is fetched.

**Revisit criteria.** After these changes ship, sample real browser sessions and
measure what share of main-loop context browser turns still occupy. If it
remains dominant, design delegation deliberately — including a human-handoff
path — against that number.

## Change 1: `browser_evaluate`

### Tool surface

Registered in `surogates/tools/builtin/browser.py` under `toolset="browser"`,
with one parameter:

| Param  | Type   | Required | Meaning |
| ------ | ------ | -------- | ------- |
| `code` | string | yes      | JavaScript **function body** evaluated in page context. Must `return` a JSON-serializable value. |

Description text must state three things the model will otherwise get wrong:
the code runs in page context (so `document` and `window` are available, but
Playwright's `page` is not), it must `return` its result, and the return value
must be JSON-serializable (DOM nodes are not — return their properties).

`timeout` is deliberately not exposed. `_playwright_execute` already defaults to
60s, which is the same budget every other browser tool gets.

### Implementation

New `KernelBrowserClient.evaluate(code)` wrapping the existing
`_playwright_execute` transport — the same path every browser operation already
uses. The agent's body is embedded in an async page-context callback:

```
const __result = await page.evaluate(async () => {
<code>
});
return __result;
```

Raw interpolation is correct here: the payload *is* code, and it is a function
body with no surrounding literal to break out of. `await` works because the
callback is async.

**Snapshot cache invalidation.** `_invalidate_snapshot_cache()` runs after every
evaluate, matching `navigate`. JavaScript can mutate the DOM, and a stale `@eN`
ref that resolves to a moved or replaced element is worse than a missing one —
`click_ref` would silently act on the wrong node.

**Result cap.** The serialized result is truncated at **20 000 characters** with
an explicit marker naming the original size, so a `document.body.innerHTML`
return cannot blow the context. The value matches
`surogates/tools/builtin/expert_loop.py`'s `_MAX_TOOL_RESULT_CHARS`, which caps
tool results for the same reason; the constant lives beside the other browser
constants.

**Errors.** `_playwright_execute` raises `RuntimeError` when the kernel envelope
reports failure. The handler catches it and returns
`{"error": "evaluate_failed", "detail": ...}`, matching the shape
`_browser_get_state_handler` already returns for `get_state_failed`. A syntax
error or a thrown exception in the agent's JS therefore surfaces as a readable
tool result rather than an exception in the harness.

### Routing and governance

`browser_evaluate` must be added to `TOOL_LOCATIONS` in
`surogates/tools/router.py` as `ToolLocation.HARNESS`, with a routing
regression test. Unlisted tools default to the sandbox executor and fail as
"Unknown tool".

Security posture is **unrestricted execution with full audit**. No pattern
restrictions: static blocklists for exfil-shaped JS are defeated by trivial
string concatenation while blocking legitimate same-site `fetch`. The tool is
governed at the allowlist level like any other, and the complete source lands
in the tool-call event and passes through `TransparencyInterceptor`.

The trust argument: enabling the browser toolset already grants the agent the
user's live authenticated browser. `browser_click` and `browser_type` can
already reach anything the user can reach. JavaScript makes that reach more
convenient, not broader.

## Change 2: markdown page state

### New module

`surogates/browser/serialize.py` — a pure function from snapshot nodes to
markdown. No I/O, independently testable, and it keeps `client.py` (796 lines)
from absorbing more. `get_state` calls it; nothing else changes in the request
path.

### Output format

`get_state` gains `format`: `"markdown"` (default) or `"json"`. JSON returns
today's structure so anything needing coordinates keeps working.

Markdown output is returned as a **raw string**, not wrapped in a JSON envelope
— JSON-escaping a markdown blob adds a backslash per newline for no benefit.
Errors remain JSON objects, consistent with the existing error shape.

```
# Booking.com — search results
https://booking.com/searchresults?dest=bucharest
viewport 1280x800

## Filters
- checkbox @e42 "Free cancellation"
- checkbox @e43 "Breakfast included"

## 1,204 properties found
### Hotel Cismigiu
£128 per night · 8.4 Very good
- link @e88 "See availability"

[truncated: 180 of 1204 nodes shown — narrow with selector, or read the rest
with browser_evaluate]
```

Rules:

- Header: title, url, viewport.
- Heading roles become markdown headings, level clamped to 2–6.
- Interactive nodes (`_INTERACTIVE_ROLES`) render as `- {role} @{ref} "{name}"`.
- Other nodes contribute a plain text line when they are a text block (below).
- Document order throughout — the same order that assigns `@eN` refs, so refs
  read in ascending order.
- No indentation. Structure comes from headings; indenting by raw DOM depth is
  noise, since real pages nest 20+ levels.

`intent: accept_consent` nodes keep their existing priority ordering so consent
banners stay at the top, matching the guidance in
`surogates/harness/prompts/guidance/browser.md`.

### Text deduplication

Fixed at the source rather than guessed at in the serializer. A serializer-side
rule cannot work reliably: `querySelectorAll('*')` returns document order, so
the ancestor carrying the whole page's text is always seen *before* the
descendants that own it, leaving nothing to deduplicate against.

The injected snapshot script gains one field per node, `text_block`, derived
from a **text-block** test:

> A node is a text block when its subtree contains no interactive element and
> no block-level element — that is, its content is pure inline markup.

- Text blocks carry their full `innerText`, correctly ordered.
- Non-text-block nodes carry an empty `text_block`; their content belongs to
  their text-block descendants.
- `nameOf` is left untouched, and interactive nodes keep using `name` — for a
  control, `aria-label` / `placeholder` / `value` are what identify it.

The serializer emits a text line only when `text_block` is non-empty, so a
container div that merely wraps its children contributes nothing. Text is
emitted exactly once, at the deepest node owning a coherent run.

Choosing the text block as the unit — rather than an element's direct child text
nodes — is what keeps sentences intact. Direct-child-text-nodes would make
`<div class="price"><span>£</span><span>128</span></div>` serialize as `£` and
`128` on separate lines, and mixed inline markup is the common case on content
pages, not an edge case. Fragmenting it would trade a token problem for a
comprehension problem.

`<p>Read our <a href="…">privacy policy</a> for details</p>` still splits, but
along the right seam: the `<a>` is interactive, so the paragraph is not a text
block, and the two inline runs around the link each become their own text block.
The link renders as a separately addressable control, which is what the agent
needs.

The test is cheap. The snapshot script already calls
`window.getComputedStyle(el)` per element for its visibility check, so the
`display` lookup rides on a call we are already paying for.

### Heading levels

`roleOf` currently collapses `h1`–`h6` to a single `heading` role, discarding the
level, so headings cannot nest. The snapshot script gains a second field,
`heading_level` (1–6, from the tag name, falling back to `aria-level`), set only
on heading nodes.

Both `text_block` and `heading_level` are additive to the JSON format: two new
fields, no removals.

### Node cap

A cap of **500 emitted nodes**, with a truncation line naming how many of how
many were shown and pointing at the two ways to get the rest. 500 keeps a dense
search-results page readable while bounding the result at a few thousand tokens;
it is a constant, tunable in one place once we see real pages.

The cap is not a parameter. `browser_evaluate` is now the escape hatch for
reading past it, and it is a better one — an agent that needs all 1 204 results
should extract them in a single call, not re-request a larger tree.

### Parameter changes

- `format` — new, described above.
- `interactive_only` — keep, applies to both formats, gains a description.
- `max_depth` — keep, applies to both formats, gains a description.
- `selector` — keep unchanged, gains a description. It scopes the snapshot to a
  subtree and is the primary way to stay under the node cap on a large page.
- `compact` — **delete**. It dropped unnamed non-interactive nodes, which
  `text_block` now does by construction in markdown and more precisely. Keeping
  it would leave two overlapping ways to express one intent.

The schema currently has no `description` on any property. All surviving
parameters get one.

### What does not change

Ref semantics are untouched. `@eN` refs come from the same snapshot pass in the
same order, the cache is populated identically, and `click_ref` / `type_into_ref`
resolve exactly as before. Markdown is a rendering of the tree, not a new
addressing scheme. `_SUPERSEDED_STATE_TOOLS` pruning continues to work — it
operates on string content and does not inspect the format.

## Testing

**`serialize.py` unit tests** (pure, no fixtures): heading nesting and clamping;
interactive line rendering; `text_block` emission and the
container-owns-nothing case; consent-intent ordering; node cap and truncation
line; empty page; nodes with missing or malformed fields.

**Text-block derivation tests** against the two cases that motivated the rule:
`<div class="price"><span>£</span><span>128</span></div>` yields the single line
`£128`, not two fragments; and `<p>Read our <a>privacy policy</a> for
details</p>` yields the surrounding inline runs as text plus the link as its own
control line. These assert the in-page derivation, so they need a DOM — either
the integration suite (`tests/integration/test_browser_e2e.py`) or a fake whose
nodes carry pre-derived `text_block` values, depending on how much of the
injected script we are willing to exercise in unit tests.

**`browser_evaluate` handler tests**, following the existing
`TestGetStateHandler` pattern in `tests/test_browser_tools.py` with
`_client_factory` fakes: successful value return; `evaluate_failed` on a
client `RuntimeError`; result truncation past the cap; snapshot cache cleared
after a call; `paused_by_user` when the control store reports user control.

**Routing regression:** `browser_evaluate` resolves to `ToolLocation.HARNESS`.

**`get_state` format tests:** markdown by default; `format="json"` returns
today's structure with the two new fields added and every existing key and the
node ordering unchanged.

**Fixture updates:** `tests/test_context_prune.py` and
`tests/test_context_prune_integration.py` build fixtures shaped like the real
`browser_get_state` result. Pruning is format-agnostic, but the fixtures should
be updated to markdown so they keep describing reality.

## Risks

**Markdown default is a behavior change for live agents.** Any agent prompt or
skill that assumes a JSON tree from `browser_get_state` will see markdown. The
built-in guidance file needs updating in the same change. Bundled skills that
mention the tool should be grepped before merge.

**`browser_evaluate` widens what a compromised or confused agent can do in one
call.** Not the blast radius — click and type already reach the same origins —
but the ease. The audit trail is the mitigation, and it only works if the JS
source is genuinely readable in the event log; verify that end to end rather
than assuming it.

**The node cap can hide relevant content.** Truncation is explicit and names the
escape hatch, but an agent that ignores the notice will reason over a partial
page. This is strictly better than today, where large pages are fully serialized
at ruinous cost, but it is a new failure mode to watch for.

**The text-block test rests on computed `display`.** Sites that restyle block
elements as inline, or build layout entirely from `display: contents`, can push
a text block boundary higher or lower than the visual structure suggests —
merging two visually separate runs onto one line, or splitting one. Text is
still emitted exactly once either way, so this degrades readability rather than
correctness. Worth checking against a few real pages during implementation
rather than reasoning about in the abstract.
