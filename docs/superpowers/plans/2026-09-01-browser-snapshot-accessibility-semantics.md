# Browser Snapshot Accessibility Semantics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the snapshot's hand-rolled tag→role guesses and flat name cascade with real ARIA role mapping and name-from-content discipline, so the page the model reads carries list/table/landmark structure and controls carry their actual labels.

**Architecture:** All of the change lives in the JS collector inside `KernelBrowserClient._SNAPSHOT_SCRIPT`, plus one mirrored constant in `serialize.py`. The collector already runs per frame and returns node dicts; only `roleOf`, `nameOf` and the per-element skip rules change. Nothing downstream of `_build_tree_and_cache` needs to know.

**Tech Stack:** Python 3.12, JS-in-a-string executed via Playwright `frame.evaluate`, pytest with the opt-in `browser_e2e` marker (Docker + `ghcr.io/invergent-ai/surogates-agent-browser`).

**Spec:** None written; this plan is the spec. It ports the semantics of `/work/agenticbrowser/package/ego-linux/src/snapshot.mjs` (MIT, CitroLabs) — the role table at lines 32-160, `roleOf` at 247-265 and `accessibleName` at 287-330. Port, do not vendor: that file walks a `DOMSnapshot` document array, we walk live DOM, and several of its ~20-line helpers collapse into one native DOM call here.

## Global Constraints

- **Branch per change**, conventional commits, no `Co-Authored-By` trailer.
- **Do not use `uv run` in this repo.** Run tests as `/work/surogates/.venv/bin/python -m pytest ...` from `/work/surogates`.
- Unit suite: `.venv/bin/python -m pytest tests/ -k browser -q` — must stay green (350 tests as of `92370163`).
- E2E suite: `.venv/bin/python -m pytest -m browser_e2e tests/integration/test_browser_e2e.py -q` — needs Docker; pull the image once with `docker pull ghcr.io/invergent-ai/surogates-agent-browser:latest`.
- The collector runs inside `frame.evaluate`, so it **must not close over anything outside itself**. Every helper and constant stays within the `__surogatesCollect` body.
- `window.getComputedStyle` is called **exactly once per element** into the `__style` map, and `tests/test_browser_client.py::TestSnapshotScriptShape::test_script_computes_style_once_per_element` enforces it. Never add a second call site.
- Every task ends with `node --check` passing, which `TestSnapshotScriptShape::test_injected_js_parses` runs automatically.

---

## Why this is worth doing

Today `roleOf` ([client.py:73-94](../../../surogates/browser/client.py#L73-L94)) recognises ten things: button, `a[href]`, textarea, select, five input types, h1-h6, img, p. Everything else — every list, table, row, cell, nav, dialog, `<summary>` — is `generic`. A pricing table reaches the model as an undifferentiated run of text lines with no indication that it is a table.

And `nameOf` is a flat cascade ending in `el.innerText || el.textContent`, applied to **every** element. Two consequences:

1. **Containers are named by their contents.** A wrapper `<div>` gets the whole page's text as its `name`, truncated at 240 chars. That name is stored in the ref cache and is what the tier-2 ref healing matches on, so healing a lost ref can match the wrong node.
2. **`el.innerText` forces layout, once per element.** On a page with a few thousand nodes that is the single most expensive thing the collector does — and for containers the result is thrown away by the serializer anyway, since `_render_entry` only prints `name` for interactive roles.

Name-from-content discipline fixes both: subtree text is computed only for the roles whose name genuinely comes from their content.

## What is explicitly out of scope

- **Rendering table/list structure in the markdown.** Adding roles is backward-compatible for `serialize._render_entry` — unknown roles fall through to the text-line branch, exactly as today. Emitting real `| cell | cell |` rows is a separate feature with its own risk against `MAX_MARKDOWN_NODES = 500`, and nobody has asked for it. Do not build it here.
- **Extracting the JS to a `.js` asset file.** Considered and rejected: it would need package-data wiring in `pyproject.toml`, and the `node --check` guard already parses the generated string, so extraction buys nothing today.
- **Off-screen addressability** (ego's "addressable ≠ emitted" split, snapshot.mjs:426-437). Our collector already refs everything with a non-zero box regardless of viewport. Different design, not a defect.
- **Shadow DOM.** Neither implementation handles it. Out of scope, no regression either way.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `surogates/browser/client.py` | The collector JS + Python post-processing | `_SNAPSHOT_SCRIPT` role/name/skip logic; `_INTERACTIVE_ROLES` gains `option`, `file-input` |
| `surogates/browser/serialize.py` | Markdown rendering | `INTERACTIVE_ROLES` mirrors the client set (it says so in a comment; keep it true) |
| `tests/integration/test_browser_e2e.py` | Real-Chromium verification | One fixture page + assertions per task |
| `tests/test_browser_client.py` | Guards on the JS string, Python-side logic | Add the `innerText` call-site guard |
| `tests/test_browser_serialize.py` | Serializer behaviour | Extend for the widened interactive set |

**Testing note, read before Task 1.** The collector is a JS string; Python cannot unit-test its behaviour. The only honest verification is the `browser_e2e` suite against real Chromium, which is the pattern already established by `test_get_state_reaches_into_iframes`. Fixtures are `data:text/html,` + `urllib.parse.quote(...)` pages — deterministic, no network. String assertions in `TestSnapshotScriptShape` are a weak second line and are used only for properties that have no observable behaviour (like the `innerText` call-site count).

---

### Task 1: Skip decorative and hidden nodes

`aria-hidden="true"` marks a subtree as decorative — icon fonts, duplicated mobile nav, screen-reader-only spacers. We currently emit all of it, and it is a meaningful share of the node budget on a real page. `input[type=hidden]` has no box so it is already filtered, but the role map in Task 2 introduces a `hidden` role and the skip belongs here with its sibling.

**Files:**
- Modify: `surogates/browser/client.py` — the per-element loop in `_SNAPSHOT_SCRIPT`, currently at the `style.visibility === 'hidden'` check
- Test: `tests/integration/test_browser_e2e.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: nothing later tasks depend on. Independent; may be done in any order relative to Tasks 2-4.

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_browser_e2e.py`:

```python
ARIA_HIDDEN_PAGE = (
    "<body style='margin:0'>"
    "<p>Real content</p>"
    "<div aria-hidden='true'><p>Decorative duplicate</p>"
    "<button>Ghost button</button></div>"
    "</body>"
)


async def test_aria_hidden_subtrees_are_skipped(browser) -> None:
    _browser_id, endpoint = browser
    async with KernelBrowserClient(rest_url=endpoint.rest_url) as client:
        await client.navigate("data:text/html," + quote(ARIA_HIDDEN_PAGE))

        state = await client.get_state()
        names = [n.get("name", "") for n in state["tree"]]
        texts = [n.get("text_block", "") for n in state["tree"]]

        assert any("Real content" in t for t in texts)
        # aria-hidden hides the whole subtree, not just the marked element.
        assert not any("Ghost button" in n for n in names)
        assert not any("Decorative duplicate" in t for t in texts)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest -m browser_e2e tests/integration/test_browser_e2e.py -k aria_hidden -q`

Expected: FAIL — `Ghost button` is present, because nothing consults `aria-hidden`.

- [ ] **Step 3: Write minimal implementation**

In `_SNAPSHOT_SCRIPT`, extend the skip block at the top of the per-element loop. It currently reads:

```js
for (const el of __els) {
  const style = __style.get(el);
  if (style.visibility === 'hidden' || style.display === 'none') continue;
```

Change to:

```js
for (const el of __els) {
  const style = __style.get(el);
  if (style.visibility === 'hidden' || style.display === 'none') continue;
  // aria-hidden marks a subtree decorative: icon glyphs, duplicated mobile
  // nav, screen-reader spacers.  closest() covers the subtree, since the
  // attribute is inherited by every descendant in the a11y tree.
  if (el.closest('[aria-hidden="true"]')) continue;
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest -m browser_e2e tests/integration/test_browser_e2e.py -k aria_hidden -q`
Expected: PASS

Then the full guard: `.venv/bin/python -m pytest tests/ -k browser -q` — expected 350 passed.

- [ ] **Step 5: Commit**

```bash
git add surogates/browser/client.py tests/integration/test_browser_e2e.py
git commit -m "fix(browser): drop aria-hidden subtrees from the snapshot"
```

---

### Task 2: Real role mapping

**Files:**
- Modify: `surogates/browser/client.py` — `roleOf` inside `_SNAPSHOT_SCRIPT` (currently lines 73-94)
- Test: `tests/integration/test_browser_e2e.py`

**Interfaces:**
- Consumes: nothing.
- Produces: the role vocabulary Task 3 branches on (`NAME_FROM_CONTENT` membership) and Task 5 mirrors. Exact new role strings introduced: `list`, `listitem`, `term`, `definition`, `table`, `row`, `cell`, `columnheader`, `rowgroup`, `navigation`, `main`, `banner`, `contentinfo`, `complementary`, `article`, `region`, `form`, `dialog`, `option`, `group`, `label`, `legend`, `iframe`, `video`, `audio`, `progressbar`, `separator`, `file-input`, `hidden`.

- [ ] **Step 1: Write the failing test**

```python
ROLES_PAGE = (
    "<body style='margin:0'>"
    "<nav><a href='/x'>Home</a></nav>"
    "<main>"
    "<table><tr><th>Plan</th><td>Pro</td></tr></table>"
    "<ul><li>First</li></ul>"
    "<details><summary>More</summary><p>Body</p></details>"
    "<div contenteditable='true'>Notes</div>"
    "<input type='file'>"
    "</main>"
    "</body>"
)


async def test_structural_tags_get_real_roles(browser) -> None:
    _browser_id, endpoint = browser
    async with KernelBrowserClient(rest_url=endpoint.rest_url) as client:
        await client.navigate("data:text/html," + quote(ROLES_PAGE))

        roles = {n["role"] for n in (await client.get_state())["tree"]}
        assert {"navigation", "main", "table", "row", "columnheader",
                "cell", "list", "listitem"} <= roles
        # <summary> behaves as a button; contenteditable is a textbox.
        assert "button" in roles
        assert "textbox" in roles
        assert "file-input" in roles
        # "generic" must no longer be the answer for a table row.
        assert not any(
            n["role"] == "generic" and n.get("text_block") == "Plan"
            for n in (await client.get_state())["tree"]
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest -m browser_e2e tests/integration/test_browser_e2e.py -k structural_tags -q`
Expected: FAIL on the first `<=` assertion — the set is missing every structural role, since `roleOf` returns `generic` for all of them.

- [ ] **Step 3: Write minimal implementation**

Replace `roleOf` in `_SNAPSHOT_SCRIPT` in its entirety:

```js
const __ROLE_BY_TAG = {
  a: 'link', button: 'button', textarea: 'textbox', select: 'combobox',
  h1: 'heading', h2: 'heading', h3: 'heading', h4: 'heading',
  h5: 'heading', h6: 'heading',
  img: 'img', p: 'paragraph',
  ul: 'list', ol: 'list', dl: 'list', li: 'listitem',
  dt: 'term', dd: 'definition',
  table: 'table', tr: 'row', td: 'cell', th: 'columnheader',
  thead: 'rowgroup', tbody: 'rowgroup', tfoot: 'rowgroup',
  nav: 'navigation', main: 'main', aside: 'complementary',
  article: 'article', section: 'region', form: 'form', dialog: 'dialog',
  option: 'option', summary: 'button', details: 'group',
  fieldset: 'group', label: 'label', legend: 'legend',
  iframe: 'iframe', video: 'video', audio: 'audio',
  progress: 'progressbar', hr: 'separator',
};

const __ROLE_BY_INPUT_TYPE = {
  button: 'button', submit: 'button', reset: 'button', image: 'button',
  checkbox: 'checkbox', radio: 'radio', range: 'slider',
  file: 'file-input', hidden: 'hidden',
  search: 'searchbox', number: 'spinbutton',
};

function roleOf(el) {
  // An explicit role wins, and the attribute is a space-separated fallback
  // list of which only the first token applies.
  const explicit = el.getAttribute('role');
  if (explicit) {
    const first = explicit.trim().split(/\\s+/)[0];
    if (first) return first;
  }
  const tag = el.tagName.toLowerCase();
  if (tag === 'input') {
    const type = (el.getAttribute('type') || 'text').toLowerCase();
    return __ROLE_BY_INPUT_TYPE[type] || 'textbox';
  }
  // A bare anchor is a jump target, not a link.
  if (tag === 'a') return el.hasAttribute('href') ? 'link' : 'generic';
  const editable = el.getAttribute('contenteditable');
  if (editable === '' || editable === 'true') return 'textbox';
  // header/footer are only landmarks at the top level; inside an article or
  // section they are that section's own header, not the page banner.
  if (tag === 'header' || tag === 'footer') {
    return el.closest('article, section, aside, nav')
      ? 'generic'
      : (tag === 'header' ? 'banner' : 'contentinfo');
  }
  return __ROLE_BY_TAG[tag] || 'generic';
}
```

Note the `\\s` — `_SNAPSHOT_SCRIPT` is a normal Python string, so a JS `\s` is written `\\s`. The existing `nameOf` and `clean` already do this; copy the convention or the regex silently becomes a literal `s`.

Then skip the `hidden` role next to the Task 1 skip, inside the per-element loop after `const role = roleOf(el);` is computed — **move that line above the skip block** so the role is available:

```js
  const role = roleOf(el);
  if (role === 'hidden') continue;
```

Add `file-input` and `option` to `__INTERACTIVE` in the same script:

```js
const __INTERACTIVE = new Set(['button','link','textbox','combobox','checkbox',
  'radio','menuitem','tab','switch','searchbox','slider','spinbutton',
  'option','file-input']);
```

And to the Python mirror `KernelBrowserClient._INTERACTIVE_ROLES` (client.py:18-33), which drives `interactive_only` filtering:

```python
            "spinbutton",
            "option",
            "file-input",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest -m browser_e2e tests/integration/test_browser_e2e.py -k structural_tags -q`
Expected: PASS

Run: `.venv/bin/python -m pytest tests/ -k browser -q`
Expected: 350 passed. If `test_interactive_only_drops_structural_nodes` fails, its fixture uses a role that is now interactive — update the fixture, not the filter.

- [ ] **Step 5: Commit**

```bash
git add surogates/browser/client.py tests/integration/test_browser_e2e.py
git commit -m "feat(browser): map real ARIA roles in the page snapshot"
```

---

### Task 3: Accessible name, computed the way ARIA says

The heart of the change. Today every element's name is `aria-label || title || alt || placeholder || value || innerText || textContent`. After this task, a name comes from content **only** for roles whose name genuinely does, form controls resolve their real `<label>`, and containers are named only by an explicit label.

**Files:**
- Modify: `surogates/browser/client.py` — `nameOf` inside `_SNAPSHOT_SCRIPT` (currently lines 96-106)
- Test: `tests/integration/test_browser_e2e.py`, `tests/test_browser_client.py`

**Interfaces:**
- Consumes: the role vocabulary from Task 2 — `nameOf(el, role)` now takes the already-computed role as its second argument, so the per-element loop must pass it.
- Produces: `nameOf(el, role) -> string`. Callers: the single `name: nameOf(el, role)` site in the entry object.

- [ ] **Step 1: Write the failing test**

```python
NAMES_PAGE = (
    "<body style='margin:0'>"
    "<label for='em'>Email address</label>"
    "<input id='em' placeholder='you@example.com'>"
    "<label>Postcode <input id='pc'></label>"
    "<span id='lbl'>Delivery notes</span>"
    "<textarea aria-labelledby='lbl'></textarea>"
    "<div id='wrap'><p>Paragraph one</p><p>Paragraph two</p></div>"
    "<img src='data:image/gif;base64,R0lGODlhAQABAAAAACw=' alt='Logo'"
    " style='width:20px;height:20px'>"
    "</body>"
)


async def test_controls_take_their_label_as_name(browser) -> None:
    _browser_id, endpoint = browser
    async with KernelBrowserClient(rest_url=endpoint.rest_url) as client:
        await client.navigate("data:text/html," + quote(NAMES_PAGE))
        by_role = {}
        for node in (await client.get_state())["tree"]:
            by_role.setdefault(node["role"], []).append(node)

        names = [n["name"] for n in by_role["textbox"]]
        # <label for>, wrapping <label>, and aria-labelledby all win over the
        # placeholder, which is only the last resort.
        assert "Email address" in names
        assert any(n.startswith("Postcode") for n in names)
        assert "Delivery notes" in names
        assert "you@example.com" not in names
        assert by_role["img"][0]["name"] == "Logo"


async def test_containers_are_not_named_by_their_contents(browser) -> None:
    _browser_id, endpoint = browser
    async with KernelBrowserClient(rest_url=endpoint.rest_url) as client:
        await client.navigate("data:text/html," + quote(NAMES_PAGE))

        state = await client.get_state()
        # The wrapper div swallowing its subtree's text as a "name" is what
        # poisons tier-2 ref healing, which matches on role + name.
        generics = [n for n in state["tree"] if n["role"] == "generic"]
        assert generics, "fixture should produce at least one generic container"
        assert all("Paragraph one" not in n["name"] for n in generics)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest -m browser_e2e tests/integration/test_browser_e2e.py -k "label_as_name or not_named_by" -q`

Expected: both FAIL. The first because the input's name is the placeholder (`nameOf` reaches `placeholder` before it ever looks at a label, and never looks at `<label>` at all). The second because the wrapper div's name is its `innerText`.

- [ ] **Step 3: Write minimal implementation**

Replace `nameOf` in `_SNAPSHOT_SCRIPT`:

```js
// Roles whose accessible name comes from their own text content.  Everything
// else -- main, nav, form, region, list, generic -- is a container: naming it
// by its contents swallows the page into one string, which then poisons the
// role+name ref healing that reads these names back.  Roles that merely CARRY
// text (cell, listitem, paragraph) are deliberately absent: their text already
// reaches the model through text_block, and naming them too would restore the
// per-element innerText layout cost this set exists to avoid.
const __NAME_FROM_CONTENT = new Set(['button','link','heading','option','tab',
  'menuitem','menuitemcheckbox','menuitemradio','switch','label','legend']);

function nameOf(el, role) {
  const aria = el.getAttribute('aria-label');
  if (aria) return clean240(aria);
  const labelledby = el.getAttribute('aria-labelledby');
  if (labelledby) {
    const parts = [];
    for (const id of labelledby.trim().split(/\\s+/)) {
      const target = document.getElementById(id);
      if (target) parts.push(target.innerText || target.textContent || '');
    }
    const joined = clean240(parts.join(' '));
    if (joined) return joined;
  }
  const tag = el.tagName.toLowerCase();
  if (tag === 'img') return clean240(el.getAttribute('alt') || '');
  if (tag === 'iframe') {
    return clean240(el.getAttribute('title') || el.getAttribute('name') || '');
  }
  if (tag === 'input' || tag === 'textarea' || tag === 'select') {
    // el.labels is the platform's own answer for both <label for=...> and a
    // wrapping <label>, so there is nothing to walk by hand.
    const labels = el.labels ? Array.from(el.labels) : [];
    if (labels.length) {
      const text = labels.map((l) => l.innerText || l.textContent || '').join(' ');
      const named = clean240(text);
      if (named) return named;
    }
    return clean240(el.getAttribute('placeholder')
      || el.getAttribute('title')
      || el.getAttribute('name')
      || '');
  }
  if (__NAME_FROM_CONTENT.has(role)) {
    return clean240(el.innerText || el.textContent || '');
  }
  // Containers: an explicit label only, never their contents.
  return clean240(el.getAttribute('title') || '');
}

function clean240(s) {
  return String(s || '').replace(/\\s+/g, ' ').trim().slice(0, 240);
}
```

Update the single call site in the entry object from `name: nameOf(el)` to `name: nameOf(el, role)`. `role` is already in scope there.

Delete the old `clean240`-equivalent tail from the removed `nameOf`; the existing `clean` helper (2000-char limit, used for `text_block`) stays as it is — the two limits are different on purpose.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest -m browser_e2e tests/integration/test_browser_e2e.py -q`
Expected: all pass, including the Task 1 and 2 cases and the two iframe cases from `92370163`.

Run: `.venv/bin/python -m pytest tests/ -k browser -q`
Expected: 350 passed.

- [ ] **Step 5: Add the layout-cost guard**

The point of name-from-content is that `innerText` — which forces layout — is no longer called for every element. Nothing enforces that, and the obvious "improvement" of adding a content fallback for containers would silently undo it. Add to `tests/test_browser_client.py::TestSnapshotScriptShape`:

```python
    def test_name_from_content_is_not_a_universal_fallback(self) -> None:
        from surogates.browser.client import KernelBrowserClient

        script = KernelBrowserClient._SNAPSHOT_SCRIPT
        # innerText forces layout.  It is affordable for the handful of roles
        # ARIA names from content, for aria-labelledby targets, for the label
        # of a form control, and for text_block derivation -- and nowhere
        # else.  A container fallback would restore the per-element cost this
        # change exists to remove.
        assert "__NAME_FROM_CONTENT.has(role)" in script
        assert script.count("el.innerText") <= 2
```

Run: `.venv/bin/python -m pytest tests/test_browser_client.py -k name_from_content -q`
Expected: PASS. If the count assertion fails, count the real call sites before raising the bound — the number is the point of the test.

- [ ] **Step 6: Commit**

```bash
git add surogates/browser/client.py tests/integration/test_browser_e2e.py tests/test_browser_client.py
git commit -m "feat(browser): compute accessible names the way ARIA defines them"
```

---

### Task 4: Interactivity that is not a role

`<div onclick=...>`, `<span tabindex="0">` and `cursor:pointer` cards are how a large share of real applications build their controls. They are `generic` to us and get no ref, so the agent can only reach them by coordinate. Ego treats `clickable`, `onclick` and `tabindex` as interactivity signals independent of role (snapshot.mjs:419-423); this is the same idea using what live DOM gives us.

**Files:**
- Modify: `surogates/browser/client.py` — the per-element loop in `_SNAPSHOT_SCRIPT`
- Test: `tests/integration/test_browser_e2e.py`

**Interfaces:**
- Consumes: `roleOf` from Task 2, `__style` (already populated).
- Produces: node dicts may now carry `role: "button"` for elements whose tag says otherwise. No new field.

- [ ] **Step 1: Write the failing test**

```python
CLICKABLE_PAGE = (
    "<body style='margin:0'>"
    "<div onclick='void 0' style='width:80px;height:30px'>Add to cart</div>"
    "<span tabindex='0' style='display:block;width:80px;height:30px'>Menu</span>"
    "<div style='cursor:pointer;width:80px;height:30px'>Dismiss</div>"
    "<div style='width:80px;height:30px'>Just a label</div>"
    "</body>"
)


async def test_clickable_divs_become_addressable(browser) -> None:
    _browser_id, endpoint = browser
    async with KernelBrowserClient(rest_url=endpoint.rest_url) as client:
        await client.navigate("data:text/html," + quote(CLICKABLE_PAGE))

        state = await client.get_state(interactive_only=True)
        names = [n["name"] for n in state["tree"]]
        assert "Add to cart" in names
        assert "Menu" in names
        assert "Dismiss" in names
        # A plain div stays plain -- promoting everything would flood the tree.
        assert "Just a label" not in names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest -m browser_e2e tests/integration/test_browser_e2e.py -k clickable_divs -q`
Expected: FAIL — `interactive_only=True` returns an empty tree, because all four divs are `generic`.

- [ ] **Step 3: Write minimal implementation**

In the per-element loop, immediately after the `hidden` skip from Task 2:

```js
  let role = roleOf(el);
  if (role === 'hidden') continue;
  // A generic element the page has wired for clicking is a control in every
  // way that matters to the agent, and needs a ref.  Three bounds, each of
  // which cost real over-promotion when it was missing:
  //   - generic only, so an onclick on a <section> cannot demote a landmark;
  //   - not already covered, i.e. not inside a control that was emitted
  //     earlier in document order.  Both cursor:pointer and the pointer
  //     cursor's inheritance make every <span> inside every <a> look
  //     clickable, which is what turned 13 Wikipedia buttons into 618;
  //   - tabindex >= 0.  A negative tabindex means focusable by script but
  //     deliberately NOT reachable by the user, which is the opposite of
  //     interactive, and it is how pages mark scroll targets and headings.
  if (role === 'generic' && !covered.has(el)) {
    const tabindex = Number(el.getAttribute('tabindex'));
    if (el.hasAttribute('onclick')
        || (el.hasAttribute('tabindex') && Number.isFinite(tabindex) && tabindex >= 0)
        || (style && style.cursor === 'pointer' && isTextBlock(el))) {
      role = 'button';
    }
  }
```

Note `role` becomes `let` rather than `const`.

The `covered` bound is free: the set is already populated with every descendant of an interactive element, and document order guarantees an ancestor is processed before its descendants. Without all three bounds the measured result was 618 Wikipedia buttons against a baseline of 13, and all 59 Hacker News promotions were `<span>`s inside anchors. With them: 57 and 0.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest -m browser_e2e tests/integration/test_browser_e2e.py -q`
Expected: all pass.

Run: `.venv/bin/python -m pytest tests/ -k browser -q`
Expected: 350 passed.

- [ ] **Step 5: Sanity-check the node budget on a real page**

This task can only add nodes, and `MAX_MARKDOWN_NODES` is 500. Check a heavy real page before committing:

```bash
.venv/bin/python -m pytest -m browser_e2e tests/integration/test_browser_e2e.py -k navigate_and_get_state -q
```

Then manually, against a provisioned browser, compare `len(state["tree"])` for `https://en.wikipedia.org/wiki/Kubernetes` before and after this task. A rise of more than ~20% means the `cursor: pointer` clause is over-promoting; tighten it to `onclick`/`tabindex` only and note the omission.

- [ ] **Step 6: Commit**

```bash
git add surogates/browser/client.py tests/integration/test_browser_e2e.py
git commit -m "feat(browser): give clickable non-semantic elements a ref"
```

---

### Task 5: Keep the serializer's role set honest

`serialize.INTERACTIVE_ROLES` carries the comment *"Mirrors `KernelBrowserClient._INTERACTIVE_ROLES`"*. Tasks 2 and 4 make that false, and a role missing here renders as a bare text line with no `@eN`, so the model sees the label and cannot click it.

**Files:**
- Modify: `surogates/browser/serialize.py:17-30`
- Test: `tests/test_browser_serialize.py`

**Interfaces:**
- Consumes: `KernelBrowserClient._INTERACTIVE_ROLES` as extended in Task 2.
- Produces: nothing further.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_browser_serialize.py`:

```python
def test_interactive_roles_match_the_client() -> None:
    from surogates.browser.client import KernelBrowserClient
    from surogates.browser.serialize import INTERACTIVE_ROLES

    # The serializer keeps its own copy so it need not import the HTTP client.
    # A role in one set and not the other renders a control as plain text with
    # no @eN, so the model can read it and cannot click it.
    assert INTERACTIVE_ROLES == KernelBrowserClient._INTERACTIVE_ROLES


def test_option_role_renders_addressable() -> None:
    from surogates.browser.serialize import render_markdown

    markdown = render_markdown({
        "title": "t",
        "url": "u",
        "viewport": {"width": 100, "height": 100},
        "tree": [{"role": "option", "ref": "@e1", "name": "Standard delivery"}],
    })
    assert '- option @e1 "Standard delivery"' in markdown
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_browser_serialize.py -k "match_the_client or option_role" -q`
Expected: both FAIL — the sets differ by `option` and `file-input`, and the option renders as an empty text line (`_render_entry` falls through to `text_block`, which is absent, and returns `None`).

- [ ] **Step 3: Write minimal implementation**

In `surogates/browser/serialize.py`, add the two roles:

```python
    "spinbutton",
    "option",
    "file-input",
})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_browser_serialize.py -q`
Expected: PASS

Run: `.venv/bin/python -m pytest tests/ -k browser -q`
Expected: 350 passed.

- [ ] **Step 5: Commit**

```bash
git add surogates/browser/serialize.py tests/test_browser_serialize.py
git commit -m "fix(browser): keep the serializer's interactive roles in sync"
```

---

### Task 6: Verify against real pages, then open the PR

Unit and fixture tests confirm the rules; they cannot tell you whether the page a model reads got better or worse. This task is the judgement call, and it is the reason the plan ends here rather than at Task 5.

**Files:** none modified unless a regression turns up.

- [ ] **Step 1: Capture before/after markdown for three real pages**

Provision a browser (the `browser` fixture pattern in `tests/integration/test_browser_e2e.py` is the shortest path), then for each of `https://en.wikipedia.org/wiki/Kubernetes`, `https://news.ycombinator.com`, and a page with a real form — `https://github.com/login` — capture `render_markdown(await client.get_state())` on `master` and on the branch.

- [ ] **Step 2: Compare on three axes**

1. **Node count** — the branch should be *lower* on content-heavy pages (aria-hidden skips) and modestly higher where clickable divs exist. A large rise means Task 4 is over-promoting.
2. **Truncation** — if `[truncated: ...]` appears on the branch where it did not on master, the budget regressed; tighten Task 4 before shipping.
3. **Control labels** — every form control on the GitHub login page should carry its label text, not its placeholder.

- [ ] **Step 3: Open the PR**

```bash
git push -u origin feat/browser-snapshot-a11y-semantics
gh pr create --base master \
  --title "feat(browser): real ARIA roles and accessible names in the snapshot"
```

The body must state the before/after node counts from Step 2, and must say plainly that behaviour is verified by the `browser_e2e` suite against real Chromium and **not** by CI — this repo runs no checks on branches.

---

## Self-Review

**Spec coverage.** The two things named in the ask — the role table and the name-from-content rule — are Tasks 2 and 3. Tasks 1, 4 and 5 are consequences: `aria-hidden` and clickable-div promotion came out of the same read of `snapshot.mjs` and are cheap alongside it; Task 5 repairs an invariant Task 2 breaks. Task 6 exists because no automated check can tell you the snapshot got *better*.

**Ordering.** Task 1 is independent. Task 3 depends on Task 2 for the role vocabulary it branches on. Task 4 depends on Task 2 for `roleOf`. Task 5 depends on Task 2. Task 6 is last. Doing 2 → 3 → 4 → 5 in order is required; 1 can go anywhere.

**Type consistency.** `nameOf` gains a second parameter in Task 3 and its only call site is updated in the same step. `role` changes from `const` to `let` in Task 4, in the loop where Task 2 moved its declaration — Task 2's step 3 must therefore leave `const role = roleOf(el);` at the top of the loop body, which it does.

**Risk I could not remove.** Task 3 changes what `name` holds for a large fraction of nodes, and `name` feeds the tier-2 ref healing (`role + normalized name + nth`). Healing gets *more* reliable for controls and containers, because the names stop being page-length text blobs — but any cached ref taken before an upgrade and healed after it will match differently. Refs are per-snapshot and invalidated on navigation, so the window is one page; not worth a migration, worth knowing.
