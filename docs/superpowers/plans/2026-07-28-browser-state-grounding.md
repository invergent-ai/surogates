# Browser State Grounding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the browser agent a JavaScript execution tool and replace `browser_get_state`'s duplicated JSON dump with deduplicated markdown.

**Architecture:** The page snapshot is produced by a JavaScript string (`_SNAPSHOT_SCRIPT`) that `KernelBrowserClient` POSTs to the browser pod's `/playwright/execute` endpoint. That script gains two fields per node — `text_block` (coherent text, emitted once at the deepest node that owns it) and `heading_level`. A new pure module renders the resulting node list as markdown. A new `browser_evaluate` tool wraps the same `/playwright/execute` transport to run agent-supplied JavaScript in page context.

**Tech Stack:** Python 3.12, async httpx, pytest (async tests, no `asyncio_mode` decorator needed — see existing browser tests), Playwright-in-pod via kernel-images REST.

**Spec:** `docs/superpowers/specs/2026-07-28-browser-state-grounding-design.md`

## Global Constraints

- Branch is `feat/browser-state-grounding`. Do not create another.
- **Do not run `uv run`** in this repo — it reinstalls the pinned `surogates` wheel and clobbers the local dev install. Run `pytest` directly.
- Conventional Commits for every commit (`type(scope): subject`).
- No `Co-Authored-By` trailer.
- Never reference plan/task/phase/step numbers in code comments or commit messages.
- No legacy fallbacks: when replacing a path, delete the old one rather than branching on both.
- Markdown result cap: **500 nodes**. Evaluate result cap: **20 000 characters**.
- All tests run from the repo root: `cd /work/surogates`.

---

### Task 1: Markdown serializer

Pure function, no I/O. Written first so later tasks have a stable target.

**Files:**
- Create: `surogates/browser/serialize.py`
- Test: `tests/test_browser_serialize.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `render_markdown(state: dict[str, Any]) -> str` and the module
  constant `MAX_MARKDOWN_NODES: int = 500`.

  `state` is the dict `KernelBrowserClient.get_state` builds:
  ```python
  {
      "url": str,
      "title": str,
      "viewport": {"width": int, "height": int},
      "tree": [
          {
              "ref": "@e1",            # always present
              "role": "button",        # always present
              "name": "Search",        # always present, may be ""
              "x": 120, "y": 44,       # always present (centre point)
              "depth": 12,             # optional
              "intent": "accept_consent",  # optional
              "text_block": "£128 per night",  # optional, "" when not a text block
              "heading_level": 2,      # optional, only on role == "heading"
          },
      ],
  }
  ```
  Task 2 populates `text_block` and `heading_level`; Task 3 calls
  `render_markdown`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_browser_serialize.py`:

```python
"""Tests for surogates.browser.serialize."""

from __future__ import annotations

from typing import Any

from surogates.browser.serialize import MAX_MARKDOWN_NODES, render_markdown


def _state(tree: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    state = {
        "url": "https://example.com/",
        "title": "Example",
        "viewport": {"width": 1280, "height": 800},
        "tree": tree,
    }
    state.update(overrides)
    return state


class TestHeader:
    def test_emits_title_url_and_viewport(self) -> None:
        out = render_markdown(_state([]))
        assert out.splitlines()[:3] == [
            "# Example",
            "https://example.com/",
            "viewport 1280x800",
        ]

    def test_survives_missing_fields(self) -> None:
        out = render_markdown({"tree": []})
        assert out.splitlines()[:3] == ["# ", "", "viewport 0x0"]


class TestHeadings:
    def test_heading_level_maps_to_hashes(self) -> None:
        out = render_markdown(_state([
            {"ref": "@e1", "role": "heading", "name": "Results",
             "x": 0, "y": 0, "heading_level": 2, "text_block": "Results"},
        ]))
        assert "## Results" in out

    def test_level_is_clamped_to_two_through_six(self) -> None:
        tree = [
            {"ref": "@e1", "role": "heading", "name": "Top", "x": 0, "y": 0,
             "heading_level": 1, "text_block": "Top"},
            {"ref": "@e2", "role": "heading", "name": "Deep", "x": 0, "y": 0,
             "heading_level": 9, "text_block": "Deep"},
        ]
        out = render_markdown(_state(tree))
        assert "## Top" in out
        assert "###### Deep" in out

    def test_missing_level_defaults_to_two(self) -> None:
        out = render_markdown(_state([
            {"ref": "@e1", "role": "heading", "name": "H",
             "x": 0, "y": 0, "text_block": "H"},
        ]))
        assert "## H" in out


class TestInteractiveNodes:
    def test_rendered_as_ref_lines(self) -> None:
        out = render_markdown(_state([
            {"ref": "@e7", "role": "button", "name": "Search", "x": 0, "y": 0},
        ]))
        assert '- button @e7 "Search"' in out

    def test_rendered_even_without_text_block(self) -> None:
        out = render_markdown(_state([
            {"ref": "@e7", "role": "checkbox", "name": "Free cancellation",
             "x": 0, "y": 0, "text_block": ""},
        ]))
        assert '- checkbox @e7 "Free cancellation"' in out

    def test_unnamed_interactive_still_addressable(self) -> None:
        out = render_markdown(_state([
            {"ref": "@e9", "role": "button", "name": "", "x": 0, "y": 0},
        ]))
        assert '- button @e9 ""' in out


class TestTextBlocks:
    def test_text_block_emitted_as_plain_line(self) -> None:
        out = render_markdown(_state([
            {"ref": "@e3", "role": "paragraph", "name": "ignored",
             "x": 0, "y": 0, "text_block": "£128 per night"},
        ]))
        assert "£128 per night" in out
        assert "ignored" not in out

    def test_container_owning_no_text_contributes_nothing(self) -> None:
        out = render_markdown(_state([
            {"ref": "@e2", "role": "generic", "name": "whole page text",
             "x": 0, "y": 0, "text_block": ""},
        ]))
        assert "whole page text" not in out

    def test_generic_without_text_block_key_contributes_nothing(self) -> None:
        out = render_markdown(_state([
            {"ref": "@e2", "role": "generic", "name": "leaked", "x": 0, "y": 0},
        ]))
        assert "leaked" not in out


class TestMotivatingCases:
    """The two shapes that drove the text-block rule.

    Both use the node lists the snapshot script produces for the markup in the
    docstrings, so they pin the contract between derivation and rendering.
    """

    def test_price_split_across_spans_reads_as_one_line(self) -> None:
        # <div class="price"><span>£</span><span>128</span></div>
        # The div is a text block; both spans are covered by it.
        tree = [
            {"ref": "@e1", "role": "generic", "name": "£128",
             "x": 0, "y": 0, "text_block": "£128"},
            {"ref": "@e2", "role": "generic", "name": "£",
             "x": 0, "y": 0, "text_block": ""},
            {"ref": "@e3", "role": "generic", "name": "128",
             "x": 0, "y": 0, "text_block": ""},
        ]
        out = render_markdown(_state(tree))
        body = out.split("viewport 1280x800\n")[1]
        assert body.strip() == "£128"

    def test_sentence_around_a_link_keeps_both_parts(self) -> None:
        # <p>Read our <a href="…">privacy policy</a> for details</p>
        # The p is not a text block (interactive descendant), so it emits its
        # own text runs; the link renders as its own control line.
        tree = [
            {"ref": "@e1", "role": "paragraph", "name": "Read our privacy policy for details",
             "x": 0, "y": 0, "text_block": "Read our for details"},
            {"ref": "@e2", "role": "link", "name": "privacy policy",
             "x": 0, "y": 0, "text_block": ""},
        ]
        out = render_markdown(_state(tree))
        assert "Read our for details" in out
        assert '- link @e2 "privacy policy"' in out


class TestOrdering:
    def test_consent_intent_sorts_first(self) -> None:
        tree = [
            {"ref": "@e5", "role": "link", "name": "Home", "x": 0, "y": 0},
            {"ref": "@e9", "role": "button", "name": "Accept all",
             "x": 0, "y": 0, "intent": "accept_consent"},
        ]
        out = render_markdown(_state(tree))
        assert out.index("@e9") < out.index("@e5")

    def test_otherwise_document_order_is_preserved(self) -> None:
        tree = [
            {"ref": "@e1", "role": "link", "name": "First", "x": 0, "y": 0},
            {"ref": "@e2", "role": "link", "name": "Second", "x": 0, "y": 0},
        ]
        out = render_markdown(_state(tree))
        assert out.index("@e1") < out.index("@e2")


class TestNodeCap:
    def test_caps_emitted_nodes_and_reports_truncation(self) -> None:
        tree = [
            {"ref": f"@e{i}", "role": "link", "name": f"L{i}", "x": 0, "y": 0}
            for i in range(1, MAX_MARKDOWN_NODES + 51)
        ]
        out = render_markdown(_state(tree))
        assert out.count('- link @e') == MAX_MARKDOWN_NODES
        assert f"[truncated: {MAX_MARKDOWN_NODES} of {MAX_MARKDOWN_NODES + 50} nodes shown" in out
        assert "browser_evaluate" in out

    def test_no_truncation_line_when_under_cap(self) -> None:
        out = render_markdown(_state([
            {"ref": "@e1", "role": "link", "name": "Only", "x": 0, "y": 0},
        ]))
        assert "truncated" not in out


class TestEmptyPage:
    def test_empty_tree_yields_header_only(self) -> None:
        out = render_markdown(_state([]))
        assert "no visible elements" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /work/surogates && python -m pytest tests/test_browser_serialize.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'surogates.browser.serialize'`

- [ ] **Step 3: Write the serializer**

Create `surogates/browser/serialize.py`:

```python
"""Markdown rendering of a browser page snapshot.

The page tree from ``KernelBrowserClient.get_state`` is a flat, document-ordered
node list.  This module renders it as markdown: interactive nodes become
addressable ``@eN`` lines, text blocks become plain lines, and heading roles
provide the structure.  Pure -- no I/O, no client, no page access -- so it is
testable in isolation and cannot fail on a slow page.
"""

from __future__ import annotations

from typing import Any

# Interactive roles get a ``- role @eN "name"`` line whether or not they own
# text.  Mirrors ``KernelBrowserClient._INTERACTIVE_ROLES``; kept as a separate
# constant so the serializer does not import the HTTP client.
INTERACTIVE_ROLES: frozenset[str] = frozenset({
    "button",
    "link",
    "textbox",
    "combobox",
    "checkbox",
    "radio",
    "menuitem",
    "tab",
    "switch",
    "searchbox",
    "slider",
    "spinbutton",
})

# Cap on emitted nodes.  Deliberately not a tool parameter: ``browser_evaluate``
# is the escape hatch for reading past it, and extracting 1200 rows in one call
# beats re-requesting a bigger tree.
MAX_MARKDOWN_NODES: int = 500

_MIN_HEADING_LEVEL: int = 2
_MAX_HEADING_LEVEL: int = 6


def render_markdown(state: dict[str, Any]) -> str:
    """Render a ``get_state`` result as markdown."""

    viewport = state.get("viewport") or {}
    lines: list[str] = [
        f"# {state.get('title', '')}",
        str(state.get("url", "")),
        f"viewport {int(viewport.get('width', 0))}x{int(viewport.get('height', 0))}",
        "",
    ]

    tree = state.get("tree") or []
    emitted = 0
    for entry in tree:
        if emitted >= MAX_MARKDOWN_NODES:
            break
        line = _render_entry(entry)
        if line is None:
            continue
        lines.append(line)
        emitted += 1

    if not emitted:
        lines.append("(no visible elements)")
    elif len(tree) > emitted:
        lines.append("")
        lines.append(
            f"[truncated: {emitted} of {len(tree)} nodes shown — narrow with "
            f"selector, or read the rest with browser_evaluate]"
        )

    return "\n".join(lines)


def _render_entry(entry: dict[str, Any]) -> str | None:
    """Return the markdown line for one node, or ``None`` to skip it."""

    role = str(entry.get("role", ""))
    if role == "heading":
        text = str(entry.get("text_block") or "").strip()
        if not text:
            return None
        return f"{'#' * _heading_level(entry)} {text}"

    if role in INTERACTIVE_ROLES:
        return f'- {role} {entry.get("ref", "")} "{entry.get("name", "")}"'

    text = str(entry.get("text_block") or "").strip()
    return text or None


def _heading_level(entry: dict[str, Any]) -> int:
    try:
        level = int(entry.get("heading_level", _MIN_HEADING_LEVEL))
    except (TypeError, ValueError):
        level = _MIN_HEADING_LEVEL
    return max(_MIN_HEADING_LEVEL, min(_MAX_HEADING_LEVEL, level))
```

Note the consent-ordering tests pass without sorting here: `get_state` already
runs `_prioritize_state_entries` before the serializer sees the tree, and the
test builds its tree pre-sorted. Do **not** add a second sort.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /work/surogates && python -m pytest tests/test_browser_serialize.py -v`
Expected: PASS, all tests.

If `test_consent_intent_sorts_first` fails, the fixture is out of document order
— fix the fixture, not the serializer.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add surogates/browser/serialize.py tests/test_browser_serialize.py
git commit -m "feat(browser): add markdown serializer for page snapshots"
```

---

### Task 2: `text_block` and `heading_level` derivation

Adds the two fields to the in-page snapshot script and carries them through the
tree builder.

**Files:**
- Modify: `surogates/browser/client.py` — `_SNAPSHOT_SCRIPT` (lines 70-151) and `_build_tree_and_cache` (lines 632-683)
- Test: `tests/test_browser_client.py`

**Interfaces:**
- Consumes: nothing (Task 1's serializer reads these fields but is not imported here).
- Produces: every entry in `get_state()["tree"]` may carry `text_block: str` and,
  when `role == "heading"`, `heading_level: int`. Absent keys mean "no text" and
  "no level" respectively.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_browser_client.py`:

```python
class TestTextBlockDerivation:
    """The snapshot script derives text at the text-block level.

    A text block is the deepest node whose subtree holds no interactive and no
    block-level element.  These tests assert the Python side carries the fields
    through; the in-page derivation itself is exercised in the e2e suite.
    """

    async def test_tree_carries_text_block_and_heading_level(self) -> None:
        from surogates.browser.client import KernelBrowserClient

        client = KernelBrowserClient("http://browser:30000")
        nodes = [
            {"role": "heading", "name": "Results", "x": 0, "y": 0,
             "width": 100, "height": 20, "depth": 3, "children_count": 0,
             "idx": 0, "backend_node_id": 11,
             "text_block": "Results", "heading_level": 2},
            {"role": "generic", "name": "Results £128", "x": 0, "y": 0,
             "width": 100, "height": 20, "depth": 3, "children_count": 2,
             "idx": 1, "backend_node_id": 12, "text_block": ""},
        ]
        tree, cache = client._build_tree_and_cache(nodes)

        assert tree[0]["text_block"] == "Results"
        assert tree[0]["heading_level"] == 2
        assert tree[1]["text_block"] == ""
        assert "heading_level" not in tree[1]
        # Refs and centres are unchanged by the new fields.
        assert tree[0]["ref"] == "@e1"
        assert cache["@e1"]["backend_node_id"] == 11

    async def test_missing_fields_are_omitted_not_defaulted(self) -> None:
        from surogates.browser.client import KernelBrowserClient

        client = KernelBrowserClient("http://browser:30000")
        nodes = [
            {"role": "link", "name": "Home", "x": 0, "y": 0,
             "width": 10, "height": 10, "depth": 2, "children_count": 0,
             "idx": 0, "backend_node_id": 5},
        ]
        tree, _ = client._build_tree_and_cache(nodes)

        assert "heading_level" not in tree[0]
        assert tree[0].get("text_block", "") == ""


class TestSnapshotScriptShape:
    """Guards on the injected JS -- it is a string, so nothing else type-checks it."""

    def test_script_derives_text_block_and_heading_level(self) -> None:
        from surogates.browser.client import KernelBrowserClient

        script = KernelBrowserClient._SNAPSHOT_SCRIPT
        assert "text_block" in script
        assert "heading_level" in script
        # The text-block test must consider both interactive and block-level
        # descendants; a check for only one of them is the bug this guards.
        assert "isTextBlock" in script

    def test_script_guards_against_both_duplication_and_text_loss(self) -> None:
        from surogates.browser.client import KernelBrowserClient

        script = KernelBrowserClient._SNAPSHOT_SCRIPT
        # Without the covered set, nested pure-inline elements each emit the
        # same text ("£128", then "£", then "128").
        assert "covered.add" in script
        # Without the own-text branch, bare text runs beside an interactive
        # child are lost -- querySelectorAll('*') returns elements only.
        assert "ownTextOf" in script

    def test_script_reuses_the_existing_computed_style_call(self) -> None:
        from surogates.browser.client import KernelBrowserClient

        # One getComputedStyle per element in the main walk -- the visibility
        # check.  A second per-element call would double snapshot cost.
        assert KernelBrowserClient._SNAPSHOT_SCRIPT.count(
            "window.getComputedStyle(el)"
        ) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /work/surogates && python -m pytest tests/test_browser_client.py -k "TextBlock or SnapshotScript" -v`
Expected: FAIL — `KeyError: 'text_block'` / `assert 'text_block' in script`

- [ ] **Step 3: Add the derivation to the snapshot script**

In `surogates/browser/client.py`, inside `_SNAPSHOT_SCRIPT`, add these two
helpers immediately after the existing `depthOf` function:

```javascript
function isBlockLevel(el) {
  const d = window.getComputedStyle(el).display;
  return d !== 'inline' && d !== 'inline-block' && d !== 'contents' && d !== 'none';
}

const __INTERACTIVE = new Set(['button','link','textbox','combobox','checkbox',
  'radio','menuitem','tab','switch','searchbox','slider','spinbutton']);

function isTextBlock(el) {
  // A text block is an element whose subtree holds no interactive element and
  // no block-level element -- i.e. pure inline markup, so its innerText reads
  // as one coherent run.
  for (const child of Array.from(el.querySelectorAll('*'))) {
    if (__INTERACTIVE.has(roleOf(child))) return false;
    if (isBlockLevel(child)) return false;
  }
  return true;
}

function ownTextOf(el) {
  // Text nodes that are direct children of *el*, i.e. the runs that belong to
  // no descendant element.  Used for elements that are NOT text blocks: their
  // descendants' text is emitted separately, but these loose runs would
  // otherwise be lost, since querySelectorAll('*') returns elements only.
  let out = '';
  for (const node of Array.from(el.childNodes)) {
    if (node.nodeType === 3) out += node.nodeValue;
  }
  return out;
}

function headingLevelOf(el) {
  const tag = el.tagName.toLowerCase();
  if (/^h[1-6]$/.test(tag)) return Number(tag.slice(1));
  const aria = Number(el.getAttribute('aria-level'));
  return Number.isFinite(aria) && aria > 0 ? aria : 2;
}

function clean(s) {
  return String(s || '').replace(/\\s+/g, ' ').trim().slice(0, 2000);
}
```

Declare the covered-set immediately before the main walk, beside `const out = []`:

```javascript
const covered = new Set();
```

Then in the main walk, replace the `out.push({...})` call with:

```javascript
  const role = roleOf(el);
  let textBlock = '';
  if (covered.has(el)) {
    // An ancestor text block already emitted this element's text.
    textBlock = '';
  } else if (isTextBlock(el)) {
    textBlock = clean(el.innerText || el.textContent);
    for (const d of Array.from(el.querySelectorAll('*'))) covered.add(d);
  } else {
    textBlock = clean(ownTextOf(el));
  }
  const entry = {
    role: role,
    name: nameOf(el),
    x: Math.round(bbox.x),
    y: Math.round(bbox.y),
    width: Math.round(bbox.width),
    height: Math.round(bbox.height),
    depth: depthOf(el),
    children_count: el.children ? el.children.length : 0,
    idx: out.length,
    text_block: textBlock,
  };
  if (role === 'heading') entry.heading_level = headingLevelOf(el);
  out.push(entry);
```

The `\\s` escaping in `clean` is required — this JavaScript lives inside a
Python string literal, exactly as the existing `nameOf` body does.

**Why the covered-set and the own-text branch both exist.** Without the covered
set, `<div class="price"><span>£</span><span>128</span></div>` emits three
times: the div is a text block (`£128`) and so is each span (`£`, `128`).
Marking the subtree covered when a text block fires means the shallowest pure-
inline element wins and its descendants stay silent.

Without the own-text branch, `<p>Read our <a>privacy policy</a> for details</p>`
loses text: the `<p>` is not a text block (it contains an interactive `<a>`),
and the runs `Read our` and `for details` are bare text nodes, which
`querySelectorAll('*')` never returns. The own-text branch catches exactly the
runs no descendant element owns.

Together they emit every text node exactly once: either inside the innerText of
the shallowest text-block ancestor, or as a direct-child run of the nearest
non-text-block ancestor.

- [ ] **Step 4: Carry the fields through the tree builder**

In `_build_tree_and_cache`, after the existing `depth` block:

```python
            depth = node.get("depth")
            if depth is not None:
                entry["depth"] = int(depth)
            entry["text_block"] = str(node.get("text_block") or "")
            heading_level = node.get("heading_level")
            if heading_level is not None:
                entry["heading_level"] = int(heading_level)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /work/surogates && python -m pytest tests/test_browser_client.py -v`
Expected: PASS — the new tests and every pre-existing client test.

- [ ] **Step 6: Commit**

```bash
cd /work/surogates
git add surogates/browser/client.py tests/test_browser_client.py
git commit -m "feat(browser): derive text blocks and heading levels in page snapshots"
```

---

### Task 3: `get_state` markdown output

Wires the serializer in, adds `format`, deletes `compact`, documents every
parameter.

**Files:**
- Modify: `surogates/browser/client.py` — `get_state` (lines 245-278), `_state_entry_visible` (lines 685-702)
- Modify: `surogates/tools/builtin/browser.py` — `GET_STATE_SCHEMA`, `_browser_get_state_handler`, the `browser_get_state` registration
- Test: `tests/test_browser_tools.py`

**Interfaces:**
- Consumes: `render_markdown` from Task 1; `text_block` / `heading_level` from Task 2.
- Produces: `browser_get_state` returns a **raw markdown string** by default and
  a JSON object when called with `format="json"`. Errors stay JSON in both modes.
  `KernelBrowserClient.get_state` keeps returning a dict; the handler renders.

- [ ] **Step 1: Write the failing tests**

In `tests/test_browser_tools.py`, extend `FakeClient.get_state` to return a
populated tree, then add the test class. Replace the existing `FakeClient.get_state`
body with:

```python
    async def get_state(self, **kwargs: Any) -> dict[str, Any]:
        self.get_state_kwargs = kwargs
        return {
            "url": "http://example.com/",
            "title": "Test",
            "viewport": {"width": 1280, "height": 800},
            "tree": [
                {"ref": "@e1", "role": "heading", "name": "Results",
                 "x": 0, "y": 0, "text_block": "Results", "heading_level": 2},
                {"ref": "@e2", "role": "button", "name": "Search",
                 "x": 10, "y": 20, "text_block": ""},
            ],
        }
```

Add to the same file:

```python
class TestGetStateFormat:
    async def test_defaults_to_markdown(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_get_state_handler

        result = await _browser_get_state_handler(
            {},
            tenant=tenant,
            session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            _client_factory=lambda endpoint: FakeClient(),
        )
        assert result.startswith("# Test")
        assert "## Results" in result
        assert '- button @e2 "Search"' in result

    async def test_json_format_returns_the_tree(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_get_state_handler

        result = await _browser_get_state_handler(
            {"format": "json"},
            tenant=tenant,
            session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            _client_factory=lambda endpoint: FakeClient(),
        )
        body = json.loads(result)
        assert body["tree"][0]["text_block"] == "Results"
        assert body["tree"][0]["heading_level"] == 2
        assert body["tree"][1]["x"] == 10

    async def test_errors_stay_json_in_markdown_mode(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_get_state_handler

        result = await _browser_get_state_handler(
            {},
            tenant=tenant,
            session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            _client_factory=lambda endpoint: FailingStateClient(),
        )
        assert json.loads(result)["error"] == "get_state_failed"


class TestGetStateSchema:
    def test_compact_is_gone(self) -> None:
        from surogates.tools.builtin.browser import GET_STATE_SCHEMA

        assert "compact" not in GET_STATE_SCHEMA["properties"]

    def test_every_parameter_is_documented(self) -> None:
        from surogates.tools.builtin.browser import GET_STATE_SCHEMA

        for name, prop in GET_STATE_SCHEMA["properties"].items():
            assert prop.get("description"), f"{name} has no description"

    def test_format_enumerates_both_modes(self) -> None:
        from surogates.tools.builtin.browser import GET_STATE_SCHEMA

        assert GET_STATE_SCHEMA["properties"]["format"]["enum"] == ["markdown", "json"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /work/surogates && python -m pytest tests/test_browser_tools.py -k "GetStateFormat or GetStateSchema" -v`
Expected: FAIL — `AssertionError: assert 'compact' not in {...}` and the markdown
test failing because the handler still returns JSON.

- [ ] **Step 3: Drop `compact` from the client**

In `surogates/browser/client.py`, remove the `compact` parameter from
`get_state`'s signature and its forwarding into `_state_entry_visible`, and
delete this branch from `_state_entry_visible`:

```python
        if compact and not entry.get("name") and role not in self._INTERACTIVE_ROLES:
            return False
```

`_state_entry_visible`'s signature becomes:

```python
    def _state_entry_visible(
        self,
        entry: dict[str, Any],
        *,
        interactive_only: bool,
        max_depth: int | None,
    ) -> bool:
```

- [ ] **Step 4: Update the schema and handler**

In `surogates/tools/builtin/browser.py`, replace `GET_STATE_SCHEMA`:

```python
GET_STATE_SCHEMA = {
    "type": "object",
    "properties": {
        "format": {
            "type": "string",
            "enum": ["markdown", "json"],
            "default": "markdown",
            "description": (
                "Output shape. 'markdown' (default) is a compact page outline: "
                "headings for structure, '- role @eN \"name\"' lines for "
                "clickable elements, plain lines for text. 'json' returns the "
                "raw node tree with viewport coordinates — only needed when you "
                "must click by coordinate rather than by ref."
            ),
        },
        "interactive_only": {
            "type": "boolean",
            "default": False,
            "description": (
                "Return only clickable and typeable elements, dropping all page "
                "text. Use when you know what you are looking for and only need "
                "its ref."
            ),
        },
        "max_depth": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "Drop elements nested deeper than this in the DOM. Rarely "
                "useful — prefer 'selector' to scope by region."
            ),
        },
        "selector": {
            "type": "string",
            "description": (
                "CSS selector limiting the snapshot to one subtree, e.g. "
                "'#search-results'. The best way to keep a large page under the "
                "node cap."
            ),
        },
    },
    "additionalProperties": False,
}
```

Then in `_browser_get_state_handler`, drop the `compact` argument and render:

```python
    _browser_id, endpoint, snapshot_cache = preflight
    client = _make_client(_client_factory, endpoint, snapshot_cache)
    async with client:
        try:
            state = await client.get_state(
                interactive_only=arguments.get("interactive_only", False),
                max_depth=arguments.get("max_depth"),
                selector=arguments.get("selector"),
            )
        except RuntimeError as exc:
            return json.dumps({"error": "get_state_failed", "detail": str(exc)})
    if arguments.get("format", "markdown") == "json":
        return json.dumps(state)
    return render_markdown(state)
```

Add the import at the top of the file:

```python
from surogates.browser.serialize import render_markdown
```

Finally update the tool description in `register`:

```python
            description=(
                "Return the current page as a markdown outline with @eN refs "
                "for browser_click and browser_type. Pass format='json' for the "
                "raw node tree with coordinates."
            ),
```

- [ ] **Step 5: Run the full browser suite**

Run: `cd /work/surogates && python -m pytest tests/test_browser_tools.py tests/test_browser_client.py tests/test_browser_serialize.py -v`
Expected: PASS. `TestGetStateHandler::test_returns_tree` will fail — it asserts
`"tree" in body` against what is now markdown. Update it to pass
`{"format": "json"}`, since it is testing the tree path.

- [ ] **Step 6: Commit**

```bash
cd /work/surogates
git add surogates/browser/client.py surogates/tools/builtin/browser.py tests/test_browser_tools.py
git commit -m "feat(browser): return page state as markdown by default"
```

---

### Task 4: `browser_evaluate`

**Files:**
- Modify: `surogates/browser/client.py` — new `evaluate` method beside `navigate`
- Modify: `surogates/tools/builtin/browser.py` — schema, handler, registration
- Modify: `surogates/tools/router.py` — `TOOL_LOCATIONS` (lines 76-86)
- Modify: `surogates/harness/tool_guardrails.py` — `MUTATING_TOOL_NAMES` (lines 23-38)
- Test: `tests/test_browser_tools.py`, `tests/test_browser_client.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `KernelBrowserClient.evaluate(code: str) -> Any` and tool
  `browser_evaluate` taking `{"code": str}`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_browser_tools.py`:

```python
class FakeEvaluateClient(FakeClient):
    def __init__(self, result: Any = None) -> None:
        super().__init__()
        self.evaluated: list[str] = []
        self._result = result if result is not None else {"rows": 3}

    async def evaluate(self, code: str) -> Any:
        self.evaluated.append(code)
        return self._result


class FailingEvaluateClient(FakeClient):
    async def evaluate(self, code: str) -> Any:
        raise RuntimeError("SyntaxError: Unexpected token '}'")


class TestEvaluateHandler:
    async def test_returns_the_json_result(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_evaluate_handler

        result = await _browser_evaluate_handler(
            {"code": "return document.title;"},
            tenant=tenant,
            session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            _client_factory=lambda endpoint: FakeEvaluateClient({"title": "T"}),
        )
        assert json.loads(result) == {"title": "T"}

    async def test_returns_structured_error_on_js_failure(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_evaluate_handler

        result = await _browser_evaluate_handler(
            {"code": "return }"},
            tenant=tenant,
            session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            _client_factory=lambda endpoint: FailingEvaluateClient(),
        )
        body = json.loads(result)
        assert body["error"] == "evaluate_failed"
        assert "SyntaxError" in body["detail"]

    async def test_truncates_oversized_results(self, tenant) -> None:
        from surogates.tools.builtin.browser import (
            _MAX_EVALUATE_RESULT_CHARS,
            _browser_evaluate_handler,
        )

        huge = "x" * (_MAX_EVALUATE_RESULT_CHARS + 5000)
        result = await _browser_evaluate_handler(
            {"code": "return document.body.innerHTML;"},
            tenant=tenant,
            session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            _client_factory=lambda endpoint: FakeEvaluateClient(huge),
        )
        body = json.loads(result)
        assert body["truncated"] is True
        assert body["original_length"] > _MAX_EVALUATE_RESULT_CHARS
        assert len(body["result"]) == _MAX_EVALUATE_RESULT_CHARS

    async def test_rejects_missing_code(self, tenant) -> None:
        from surogates.tools.builtin.browser import _browser_evaluate_handler

        result = await _browser_evaluate_handler(
            {},
            tenant=tenant,
            session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            _client_factory=lambda endpoint: FakeEvaluateClient(),
        )
        assert json.loads(result)["error"] == "missing_code"
```

Add `"browser_evaluate"` to `BROWSER_TOOL_NAMES` and
`("_browser_evaluate_handler", {"code": "return 1;"})` to
`BROWSER_SUSPEND_HANDLERS` in the same file.

Add to `tests/test_browser_client.py`:

```python
class TestEvaluate:
    async def test_wraps_code_in_a_page_evaluate_callback(self) -> None:
        from surogates.browser.client import KernelBrowserClient

        client = KernelBrowserClient("http://browser:30000")
        sent: list[str] = []

        async def fake_execute(code: str, **kwargs: Any) -> Any:
            sent.append(code)
            return {"ok": True}

        client._playwright_execute = fake_execute  # type: ignore[assignment]
        result = await client.evaluate("return document.title;")

        assert result == {"ok": True}
        assert "await page.evaluate(async () => {" in sent[0]
        assert "return document.title;" in sent[0]

    async def test_invalidates_the_snapshot_cache(self) -> None:
        from surogates.browser.client import KernelBrowserClient

        client = KernelBrowserClient("http://browser:30000")
        client._snapshot_cache["@e1"] = {"x": 1, "y": 2}

        async def fake_execute(code: str, **kwargs: Any) -> Any:
            return None

        client._playwright_execute = fake_execute  # type: ignore[assignment]
        await client.evaluate("document.body.innerHTML = '';")

        assert client._snapshot_cache == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /work/surogates && python -m pytest tests/test_browser_tools.py tests/test_browser_client.py -k "Evaluate or evaluate" -v`
Expected: FAIL — `ImportError: cannot import name '_browser_evaluate_handler'`

- [ ] **Step 3: Add the client method**

In `surogates/browser/client.py`, immediately after `navigate`:

```python
    async def evaluate(self, code: str) -> Any:
        """Run agent-supplied JavaScript in page context and return its value.

        *code* is a function **body**, not an expression: it must ``return`` a
        JSON-serializable value.  Raw interpolation is correct here -- the
        payload is code, and it sits in a function body with no surrounding
        literal to escape from.  The callback is ``async`` so the body may
        ``await``.

        The snapshot cache is cleared afterwards: JavaScript can move or replace
        elements, and a stale ``@eN`` that resolves to the wrong node is worse
        than one that fails to resolve.
        """

        wrapped = (
            "const __result = await page.evaluate(async () => {\n"
            f"{code}\n"
            "});\n"
            "return __result;"
        )
        result = await self._playwright_execute(wrapped)
        self._invalidate_snapshot_cache()
        return result
```

- [ ] **Step 4: Add the schema, handler, and registration**

In `surogates/tools/builtin/browser.py`, beside the other module constants:

```python
# Cap on a single evaluate result.  Matches ``_MAX_TOOL_RESULT_CHARS`` in
# ``surogates/tools/builtin/expert_loop.py``: a ``document.body.innerHTML``
# return would otherwise swallow the context window.
_MAX_EVALUATE_RESULT_CHARS: int = 20_000
```

```python
EVALUATE_SCHEMA = {
    "type": "object",
    "properties": {
        "code": {
            "type": "string",
            "description": (
                "JavaScript function body run inside the page. 'document' and "
                "'window' are available; Playwright's 'page' is not. You must "
                "'return' a JSON-serializable value — DOM nodes are not "
                "serializable, so return their properties instead. 'await' is "
                "allowed. Example: return [...document.querySelectorAll('tr')]"
                ".map(r => r.innerText);"
            ),
        },
    },
    "required": ["code"],
    "additionalProperties": False,
}


async def _browser_evaluate_handler(
    arguments: dict[str, Any],
    *,
    tenant: Any = None,
    session_id: UUID | str | None = None,
    browser_pool: BrowserPool | None = None,
    browser_control: BrowserControlStore | None = None,
    _client_factory: Callable[..., Any] = _default_client_factory,
    workspace_path: str | None = None,
    session_config: dict[str, Any] | None = None,
    **_: Any,
) -> str:
    code = arguments.get("code")
    if not code or not str(code).strip():
        return json.dumps({
            "error": "missing_code",
            "detail": "browser_evaluate requires a non-empty 'code' argument.",
        })

    preflight = await _resolve_session_browser(
        tenant=tenant,
        session_id=session_id,
        browser_pool=browser_pool,
        browser_control=browser_control,
        workspace_path=workspace_path,
        session_config=session_config,
    )
    if isinstance(preflight, str):
        return preflight

    _browser_id, endpoint, snapshot_cache = preflight
    client = _make_client(_client_factory, endpoint, snapshot_cache)
    async with client:
        try:
            result = await client.evaluate(str(code))
        except RuntimeError as exc:
            return json.dumps({"error": "evaluate_failed", "detail": str(exc)})

    serialized = json.dumps(result)
    if len(serialized) > _MAX_EVALUATE_RESULT_CHARS:
        kept = serialized[:_MAX_EVALUATE_RESULT_CHARS]
        return json.dumps({
            "truncated": True,
            "original_length": len(serialized),
            "result": kept,
        })
    return serialized
```

And in `register`:

```python
    registry.register(
        name="browser_evaluate",
        schema=ToolSchema(
            name="browser_evaluate",
            description=(
                "Run JavaScript in the page and return its value. Use this to "
                "read structured page state — full tables, hidden input values, "
                "every option of a select — in one call instead of many "
                "get_state and scroll round trips."
            ),
            parameters=EVALUATE_SCHEMA,
        ),
        handler=_browser_evaluate_handler,
        toolset="browser",
    )
```

- [ ] **Step 5: Wire routing and guardrails**

In `surogates/tools/router.py`, add to `TOOL_LOCATIONS` beside the other browser
entries:

```python
    "browser_evaluate": ToolLocation.HARNESS,
```

In `surogates/harness/tool_guardrails.py`, add to `MUTATING_TOOL_NAMES`:

```python
    "browser_evaluate",
```

JavaScript can mutate the DOM, so the tool belongs with the mutating set — the
no-progress guardrail would otherwise treat repeated evaluates as read-only
spinning.

- [ ] **Step 6: Run the full browser suite**

Run: `cd /work/surogates && python -m pytest tests/test_browser_tools.py tests/test_browser_client.py tests/test_browser_serialize.py -v`
Expected: PASS, including the parametrized `paused_by_user` test now covering
`browser_evaluate` and the `TestToolWiring` routing assertion.

- [ ] **Step 7: Commit**

```bash
cd /work/surogates
git add surogates/browser/client.py surogates/tools/builtin/browser.py \
        surogates/tools/router.py surogates/harness/tool_guardrails.py \
        tests/test_browser_tools.py tests/test_browser_client.py
git commit -m "feat(browser): add browser_evaluate for in-page JavaScript"
```

---

### Task 5: Guidance, fixtures, and callers

The behaviour change has to reach the prompt that tells the agent how to browse,
and the fixtures that claim to mirror real tool output.

**Files:**
- Modify: `surogates/harness/prompts/guidance/browser.md`
- Modify: `tests/test_context_prune.py`, `tests/test_context_prune_integration.py`
- Modify: `/work/surogate-ops/frontend/src/features/agents/builtin-tools.ts:27`

**Interfaces:**
- Consumes: the tool surface from Tasks 3 and 4.
- Produces: no code interfaces.

- [ ] **Step 1: Find every caller that assumes the old shape**

Run and read the output — do not skip this, it decides what else this task
touches:

```bash
cd /work/surogates
grep -rn "browser_get_state" --include=*.py --include=*.md --include=*.ts \
  . /work/surogate-ops/frontend/src | grep -v node_modules | grep -v __pycache__
```

Any bundled skill or prompt that describes the JSON tree must be updated here.

- [ ] **Step 2: Update the browser guidance prompt**

In `surogates/harness/prompts/guidance/browser.md`, replace the opening
paragraph with:

```markdown
Use `browser_get_state` before interacting with a page whenever you need a
target ref, and refresh refs after navigation, scrolling, modal dismissal, or
any large page change. It returns a markdown outline: `- role @eN "name"` lines
are the things you can click and type into.

Reach for `browser_evaluate` instead of repeated scroll-and-restate loops when
you need data rather than an action — every row of a table, all options of a
select, a hidden field's value. One evaluate that returns an array beats ten
snapshots. `browser_get_state` shows at most 500 elements and says so when it
truncates; scope it with `selector`, or read past the cap with
`browser_evaluate`.
```

Leave the cookie-and-consent section unchanged.

- [ ] **Step 3: Update the prune fixtures**

`tests/test_context_prune.py` and `tests/test_context_prune_integration.py`
build fixtures shaped like real `browser_get_state` output. Pruning is
format-agnostic, so behaviour does not change, but the fixtures should describe
reality. Replace the JSON-shaped fixture content with markdown, e.g.:

```python
def _browser_state_result(n: int) -> str:
    return (
        f"# Page {n}\n"
        f"https://example.com/{n}\n"
        "viewport 1280x800\n"
        "\n"
        "## Results\n"
        '- button @e1 "Search"\n'
        '- link @e2 "Next"\n'
    )
```

Update the module docstrings that describe the old shape at
`tests/test_context_prune.py:4` and `tests/test_context_prune_integration.py:7`.

- [ ] **Step 4: Register the new tool in the ops frontend**

In `/work/surogate-ops/frontend/src/features/agents/builtin-tools.ts`, add
`"browser_evaluate"` to the browser tool list beside `"browser_get_state"` on
line 27, so the tool appears in the agent tool picker.

- [ ] **Step 5: Run the whole affected suite**

```bash
cd /work/surogates
python -m pytest tests/test_browser_serialize.py tests/test_browser_tools.py \
  tests/test_browser_client.py tests/test_context_prune.py \
  tests/test_context_prune_integration.py -v
```

Expected: PASS.

- [ ] **Step 6: Run the full test suite to catch anything this missed**

```bash
cd /work/surogates
python -m pytest tests/ -x -q
```

Expected: PASS. If an unrelated test fails, check whether it predates this
branch (`git stash && python -m pytest <test> -q`) before debugging it.

- [ ] **Step 7: Commit**

```bash
cd /work/surogates
git add surogates/harness/prompts/guidance/browser.md tests/test_context_prune.py \
        tests/test_context_prune_integration.py
git commit -m "docs(browser): teach the guidance prompt markdown state and evaluate"
```

The frontend file is in a different repo — commit it separately on its own
branch there:

```bash
cd /work/surogate-ops
git checkout -b feat/browser-evaluate-tool
git add frontend/src/features/agents/builtin-tools.ts
git commit -m "feat(agents): expose browser_evaluate in the tool picker"
```

---

## Verification

Before opening a PR:

- [ ] `cd /work/surogates && python -m pytest tests/ -q` passes.
- [ ] `grep -rn "compact" surogates/browser/client.py surogates/tools/builtin/browser.py` returns nothing — the parameter is gone, not deprecated.
- [ ] `grep -rn "own_text" surogates/` returns nothing — the superseded field name never entered the code.
- [ ] A manual smoke test against a real page, since the in-page derivation cannot be unit tested: point a session's browser at a content-heavy page (a shop listing or news article), call `browser_get_state`, and confirm prices and sentences read as whole lines rather than fragments, with no run of text appearing twice. This is the check the spec's `display`-based risk calls for.
- [ ] Confirm the audit trail is real, not assumed — the spec's security posture depends on it. After a `browser_evaluate` call in a live session, query the event log and confirm the complete JS source is present and readable:

  ```sql
  SELECT jsonb_pretty(data) FROM events
  WHERE session_id = '<sid>' AND type = 'tool.call'
    AND data->>'name' = 'browser_evaluate' ORDER BY id DESC LIMIT 1;
  ```

  If the source is absent or truncated, the "unrestricted execution with full audit" posture does not hold and needs fixing before merge.
