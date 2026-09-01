"""Async HTTP client for kernel-images REST API."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import unicodedata
from typing import Any

import httpx


class KernelBrowserClient:
    """HTTP client for one kernel-images browser REST endpoint."""

    _INTERACTIVE_ROLES: frozenset[str] = frozenset(
        {
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
            "option",
            "file-input",
        }
    )
    # Casual / xdotool key names a model is likely to emit, mapped to the
    # Playwright vocabulary used by keyboard.press(). Applied per key before
    # the keys are joined into a chord. Unlisted keys (letters, "Enter",
    # "Shift", "ArrowUp", "F5", …) already match Playwright and pass through.
    _KEY_ALIASES: dict[str, str] = {
        "ctrl": "Control",
        "control": "Control",
        "cmd": "Meta",
        "command": "Meta",
        "super": "Meta",
        "win": "Meta",
        "opt": "Alt",
        "option": "Alt",
        "esc": "Escape",
        "del": "Delete",
        "return": "Enter",
        "space": " ",
    }
    _CONSENT_ACTION_RE = re.compile(
        r"^("
        r"accept(?:\s+(?:all|toate|cookies|all\s+cookies))?|"
        r"accepta(?:ti)?(?:\s+(?:toate|cookie-uri|cookies))?|"
        r"agree|i\s+agree|allow\s+all(?:\s+cookies)?|"
        r"ok|got\s+it|continue|continua|"
        r"sunt\s+de\s+acord|de\s+acord"
        r")[.!]?$",
        re.IGNORECASE,
    )
    _CONSENT_SETTINGS_RE = re.compile(
        r"\b("
        r"settings|setari|setarile|preferences|preferinte|"
        r"modify|modific|customize|parteneri|partners|vendors"
        r")\b",
        re.IGNORECASE,
    )

    _SNAPSHOT_SCRIPT = """
const __surogatesSelector = __SUROGATES_SELECTOR__;
const __surogatesCollect = ({selector, base}) => {
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

// Roles whose accessible name comes from their own text content.  Everything
// else -- main, nav, form, region, list, generic -- is a container: naming it
// by its contents swallows the page into one string, which then poisons the
// role+name ref healing that reads these names back.  Roles that merely
// CARRY text (cell, listitem, paragraph) are deliberately absent: their text
// reaches the model through text_block, and naming them too would restore the
// per-element innerText layout cost this set exists to avoid.
const __NAME_FROM_CONTENT = new Set(['button','link','heading','option','tab',
  'menuitem','menuitemcheckbox','menuitemradio','switch','label','legend']);

function clean240(s) {
  return String(s || '').replace(/\\s+/g, ' ').trim().slice(0, 240);
}

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
      const named = clean240(
        labels.map((l) => l.innerText || l.textContent || '').join(' ')
      );
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

function depthOf(el) {
  let d = 0, cur = el;
  while (cur && cur.parentElement) { d++; cur = cur.parentElement; }
  return d;
}

function isBlockLevel(el) {
  // Reads the precomputed __style map -- getComputedStyle here would run once
  // per scanned descendant and force layout each time.
  const s = __style.get(el);
  const d = s ? s.display : 'block';
  return d !== 'inline' && d !== 'inline-block' && d !== 'contents' && d !== 'none';
}

const __INTERACTIVE = new Set(['button','link','textbox','combobox','checkbox',
  'radio','menuitem','tab','switch','searchbox','slider','spinbutton',
  'option','file-input']);

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
  // Text nodes that are direct children of el, i.e. the runs that belong to no
  // descendant element.  Used for elements that are NOT text blocks: their
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

const out = [];
const root = selector === null ? document : document.querySelector(selector);
if (!root) throw new Error('selector matched no element');
const covered = new Set();
const __els = Array.from(root.querySelectorAll('*'));
const __style = new Map();
for (const el of __els) __style.set(el, window.getComputedStyle(el));
for (const el of __els) {
  const style = __style.get(el);
  if (style.visibility === 'hidden' || style.display === 'none') continue;
  // aria-hidden marks a subtree decorative: icon glyphs, duplicated mobile
  // nav, screen-reader spacers.  closest() covers the subtree, since the
  // attribute applies to every descendant in the accessibility tree.
  if (el.closest('[aria-hidden="true"]')) continue;
  const bbox = el.getBoundingClientRect();
  if (!bbox || bbox.width <= 0 || bbox.height <= 0) continue;
  let role = roleOf(el);
  // input[type=hidden] carries no box, but an explicit role="hidden" does.
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
  const idx = base + out.length;
  el.setAttribute('data-sg-i', String(idx));
  let textBlock = '';
  if (__INTERACTIVE.has(role)) {
    // Text inside a control belongs to the control: nameOf already carries it
    // into the "- role @eN name" line.  Cover the subtree so a block-level
    // child (<a><div>Label</div></a>, ubiquitous in nav menus and card links)
    // cannot emit the same label a second time as a stray text line.
    for (const d of Array.from(el.querySelectorAll('*'))) covered.add(d);
  } else if (covered.has(el)) {
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
    name: nameOf(el, role),
    x: Math.round(bbox.x),
    y: Math.round(bbox.y),
    width: Math.round(bbox.width),
    height: Math.round(bbox.height),
    depth: depthOf(el),
    children_count: el.children ? el.children.length : 0,
    idx: idx,
    text_block: textBlock,
  };
  if (role === 'heading') entry.heading_level = headingLevelOf(el);
  out.push(entry);
}
return {
  viewport: {width: window.innerWidth, height: window.innerHeight},
  nodes: out,
};
};
// One collector run per frame.  Every frame measures in its own viewport, so
// each result carries the frame's origin in root space -- page.mouse and the
// screenshot overlay only ever speak root coordinates.  A selector narrows the
// main document to one subtree, so that mode stays single-frame.
const __mainFrame = page.mainFrame();
const __targets = __surogatesSelector === null ? page.frames() : [__mainFrame];
const __frames = [];
let __viewport = null;
let __base = 0;
for (const __f of __targets) {
  let __ox = 0, __oy = 0;
  if (__f !== __mainFrame) {
    // frameElement() is a Playwright-side lookup, so it resolves across
    // origins where window.frameElement would be blocked, and boundingBox()
    // already accumulates the offsets of every ancestor frame.
    let __box = null;
    try {
      const __fe = await __f.frameElement();
      __box = __fe ? await __fe.boundingBox() : null;
    } catch (e) { __box = null; }
    // Detached, display:none or zero-size frame: nothing clickable inside.
    if (!__box) continue;
    __ox = Math.round(__box.x); __oy = Math.round(__box.y);
  }
  let __r = null;
  try {
    __r = await __f.evaluate(__surogatesCollect, {
      selector: __f === __mainFrame ? __surogatesSelector : null,
      base: __base,
    });
  } catch (e) { continue; }
  if (__f === __mainFrame) __viewport = __r.viewport;
  __base += __r.nodes.length;
  __frames.push({x: __ox, y: __oy, nodes: __r.nodes});
}
const __cdp = await page.context().newCDPSession(page);
const __doc = await __cdp.send('DOM.getDocument', {depth: -1, pierce: true});
const __map = {};
(function walk(n){ if(!n) return; const a=n.attributes||[]; for(let i=0;i<a.length;i+=2){ if(a[i]==='data-sg-i'){ __map[a[i+1]] = n.backendNodeId; } } for(const c of (n.children||[])) walk(c); if(n.contentDocument) walk(n.contentDocument); })(__doc.root);
for (const __fr of __frames) { for (const node of __fr.nodes) { node.backend_node_id = (node.idx!=null && __map[String(node.idx)]!=null) ? __map[String(node.idx)] : null; } }
return {
  url: page.url(),
  title: await page.title(),
  viewport: page.viewportSize() || __viewport || {width: 0, height: 0},
  frames: __frames,
};
"""

    _DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0)

    def __init__(
        self,
        rest_url: str,
        *,
        timeout: float | httpx.Timeout | None = None,
        snapshot_cache: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.rest_url = rest_url.rstrip("/")
        if timeout is None:
            self._timeout: float | httpx.Timeout = self._DEFAULT_TIMEOUT
        else:
            self._timeout = timeout
        self._http: httpx.AsyncClient = httpx.AsyncClient(
            base_url=self.rest_url,
            timeout=self._timeout,
        )
        self._closed = False
        self._snapshot_cache = snapshot_cache if snapshot_cache is not None else {}

    async def close(self) -> None:
        """Close the underlying HTTP client."""

        if self._closed:
            return
        await self._http.aclose()
        self._closed = True

    async def __aenter__(self) -> "KernelBrowserClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def navigate(self, url: str, *, wait_until: str = "load") -> dict[str, Any]:
        """Navigate to a URL and return the final URL and title."""

        code = (
            "await page.goto({url!r}, {{waitUntil: {wait_until!r}}});\n"
            "return {{ url: page.url(), title: await page.title() }};"
        ).format(url=url, wait_until=wait_until)
        result = await self._playwright_execute(code)
        self._invalidate_snapshot_cache()
        return result

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

    async def storage_state(self) -> dict[str, Any]:
        """Export the live context's cookies + per-origin localStorage."""

        code = "return await page.context().storageState();"
        result = await self._playwright_execute(code)
        return result or {"cookies": [], "origins": []}

    async def apply_storage_state(self, state: dict[str, Any]) -> None:
        """Inject cookies (and best-effort localStorage) into the live context.

        Cookies are applied first, in one ``addCookies`` call — they are the
        primary auth carrier and land atomically. localStorage can only be
        written while the page is on the matching origin, and a fresh context
        can't be created on an already-running browser, so each origin is seeded
        by navigating to it and writing its items.

        Those per-origin navigations run **sequentially** inside the single
        ``_playwright_execute`` call, which shares one 60s budget — a profile
        spanning many origins can approach that timeout. Cookies are already in
        place by then, so a partial localStorage seed degrades gracefully rather
        than losing the session; per-origin failures (origins that block
        navigation) are swallowed for the same reason.
        """

        cookies_json = json.dumps(state.get("cookies", []) or [])
        origins_json = json.dumps(state.get("origins", []) or [])
        code = (
            # The kernel-images execute wrapper already binds ``context`` in
            # scope; redeclaring it is a SyntaxError ("Identifier 'context' has
            # already been declared") that aborts the whole injection. Use a
            # local name instead.
            "const ctx = page.context();\n"
            f"await ctx.addCookies({cookies_json});\n"
            f"for (const o of {origins_json}) {{\n"
            "  try {\n"
            "    await page.goto(o.origin, {waitUntil: 'domcontentloaded'});\n"
            "    await page.evaluate((items) => {\n"
            "      for (const it of items) localStorage.setItem(it.name, it.value);\n"
            "    }, o.localStorage || []);\n"
            "  } catch (e) { /* best-effort per origin */ }\n"
            "}\n"
            "return true;"
        )
        await self._playwright_execute(code)
        self._invalidate_snapshot_cache()

    async def get_state(
        self,
        *,
        interactive_only: bool = False,
        max_depth: int | None = None,
        selector: str | None = None,
    ) -> dict[str, Any]:
        """Return a DOM-derived page tree with stable refs and cached centers."""

        raw = await self._playwright_execute(self._snapshot_script(selector))
        nodes = self._merge_frame_nodes(raw.get("frames", []))
        full_tree, new_cache = self._build_tree_and_cache(nodes)
        tree = [
            entry
            for entry in full_tree
            if self._state_entry_visible(
                entry,
                interactive_only=interactive_only,
                max_depth=max_depth,
            )
        ]
        tree = self._prioritize_state_entries(tree)

        self._snapshot_cache.clear()
        self._snapshot_cache.update(new_cache)

        return {
            "url": raw.get("url", ""),
            "title": raw.get("title", ""),
            "viewport": raw.get("viewport", {"width": 0, "height": 0}),
            "tree": tree,
        }

    async def click_at(
        self,
        x: int,
        y: int,
        *,
        button: str = "left",
        click_type: str = "click",
        num_clicks: int = 1,
    ) -> None:
        """Click at viewport coordinates."""

        options: dict[str, Any] = {"button": button}
        if num_clicks != 1:
            options["clickCount"] = num_clicks
        if click_type == "click":
            code = (
                "let __reqSeen = false;\n"
                "const __reqHandler = () => { __reqSeen = true; };\n"
                "page.on('request', __reqHandler);\n"
                "try {\n"
                f"  await page.mouse.click({int(x)}, {int(y)}, "
                f"{json.dumps(options)});\n"
                "  await page.waitForTimeout(150);\n"
                "  if (__reqSeen) {\n"
                "    await page.waitForLoadState('networkidle', "
                "{timeout: 5000}).catch(() => null);\n"
                "  }\n"
                "} finally {\n"
                "  page.off('request', __reqHandler);\n"
                "}\n"
                "return true;"
            )
        elif click_type == "down":
            code = (
                f"await page.mouse.move({int(x)}, {int(y)});\n"
                f"await page.mouse.down({json.dumps({'button': button})});\n"
                "return true;"
            )
        elif click_type == "up":
            code = (
                f"await page.mouse.move({int(x)}, {int(y)});\n"
                f"await page.mouse.up({json.dumps({'button': button})});\n"
                "return true;"
            )
        else:
            raise ValueError(f"unsupported click_type: {click_type}")
        await self._playwright_execute(code)

    async def click_ref(self, ref: str, **kwargs: Any) -> None:
        """Re-locate a `browser_get_state` ref and click its live center.

        Refs are two-tier: a CDP backend node id is tried first, then a
        role/name/nth lookup in the accessibility tree heals the ref when the
        DOM has re-rendered. The element's center is recomputed at action time,
        so the click lands correctly on dynamic pages.
        """

        entry = self._resolve_ref(ref)
        await self._act_click_on_entry(
            entry,
            button=kwargs.get("button", "left"),
            num_clicks=kwargs.get("num_clicks", 1),
        )

    async def type_text(self, text: str, *, delay_ms: int = 0) -> None:
        """Type text into the currently focused element.

        Goes through Playwright's ``keyboard.type`` rather than the kernel
        ``/computer/type`` endpoint: that endpoint sends OS-level (xdotool)
        keystrokes that never reach contenteditable rich-text editors — e.g.
        x.com's Draft.js composer silently drops them while the endpoint still
        returns 200, so every ``typed: true`` was a lie. CDP key events are
        delivered to the focused element and trigger its input handling, so
        the text actually lands.
        """

        opts = {"delay": delay_ms} if delay_ms else {}
        code = (
            f"await page.keyboard.type({json.dumps(text)}, {json.dumps(opts)});\n"
            "return true;"
        )
        await self._playwright_execute(code)

    async def type_into_ref(self, ref: str, text: str, **kwargs: Any) -> None:
        """Re-locate a ref, click it to focus, then type text."""

        await self._act_click_on_entry(self._resolve_ref(ref))
        await self.type_text(text, **kwargs)

    async def press_key(self, *keys: str, duration_ms: int = 0) -> None:
        """Press one key or key chord (e.g. ``Enter`` or ``Control+a``).

        Like :meth:`type_text`, uses Playwright's ``keyboard.press`` instead of
        the kernel ``/computer/press_key`` endpoint so the key reaches the
        focused DOM element rather than the OS-level X display. Multiple keys
        are normalized to the Playwright vocabulary and joined into one chord
        (``"Ctrl", "a"`` -> ``"Control+a"``).
        """

        if not keys:
            raise ValueError("press_key requires at least one key")
        chord = "+".join(self._KEY_ALIASES.get(k.lower(), k) for k in keys)
        opts = {"delay": duration_ms} if duration_ms else {}
        code = (
            f"await page.keyboard.press({json.dumps(chord)}, {json.dumps(opts)});\n"
            "return true;"
        )
        await self._playwright_execute(code)

    async def scroll_at(
        self, x: int, y: int, *, delta_x: int = 0, delta_y: int = 0
    ) -> dict[str, Any]:
        """Scroll at viewport coordinates; deltas are pixels.

        Goes through Playwright's ``mouse.wheel`` rather than the
        kernel ``/computer/scroll`` endpoint: that endpoint's deltas
        are xdotool wheel *ticks*, so a pixel-sized value (the unit
        every caller passes) overshoots by two orders of magnitude and
        slams the page to the bottom in one call. Returns the
        post-scroll position so callers can tell whether the page
        actually moved.
        """

        code = f"""
await page.mouse.move({int(x)}, {int(y)});
await page.mouse.wheel({int(delta_x)}, {int(delta_y)});
await page.waitForTimeout(150);
return await page.evaluate(() => ({{
  scroll_x: Math.round(window.scrollX),
  scroll_y: Math.round(window.scrollY),
  page_height: Math.round(document.documentElement.scrollHeight),
  viewport_height: Math.round(window.innerHeight),
}}));
"""
        result = await self._playwright_execute(code)
        # Scrolling only moves the viewport; the elements (and their backend
        # node ids) persist, and a ref's click center is recomputed at action
        # time — so refs survive a scroll and need no re-snapshot.
        return result if isinstance(result, dict) else {}

    async def drag(self, path: list[tuple[int, int]], *, button: str = "left") -> None:
        """Drag the mouse along a path of viewport coordinates.

        Goes through Playwright's mouse rather than the kernel
        ``/computer/drag_mouse`` endpoint: that endpoint's xdotool coordinates
        don't map onto the page's CSS-pixel viewport — a drag aimed at y=400
        lands near y=224 — so it grabs the wrong element. Playwright's mouse is
        pixel-accurate against the viewport, matching ``click_at``/``scroll_at``.
        """

        if len(path) < 2:
            raise ValueError("drag path must contain at least two points")
        button_lit = json.dumps(button)
        steps = [f"await page.mouse.move({int(path[0][0])}, {int(path[0][1])});"]
        steps.append(f"await page.mouse.down({{button: {button_lit}}});")
        for x, y in path[1:]:
            steps.append(f"await page.mouse.move({int(x)}, {int(y)});")
        steps.append(f"await page.mouse.up({{button: {button_lit}}});")
        steps.append("return true;")
        # Two-tier refs re-resolve at action time, so a drag — like click and
        # scroll — does not invalidate the snapshot cache.
        await self._playwright_execute("\n".join(steps))

    async def wait(self, ms: int) -> None:
        """Sleep without changing browser state or cached refs."""

        await asyncio.sleep(max(0, ms) / 1000.0)

    async def screenshot(
        self,
        *,
        region: dict[str, int] | None = None,
        annotate: bool = False,
        save_path: str | None = None,
    ) -> dict[str, Any]:
        """Capture a PNG screenshot, optionally with numbered ref overlays.

        Always captures through Playwright's ``page.screenshot`` rather than
        the kernel ``/computer/screenshot`` endpoint: that endpoint grabs the
        whole X framebuffer (e.g. 3840x2160) instead of the page viewport
        (e.g. 1905x1984), so the bytes are the wrong size and any ref-overlay
        annotations — positioned in CSS pixels — would not line up.
        """

        annotations: list[dict[str, Any]] | None = None
        if annotate:
            if not self._snapshot_cache:
                await self.get_state(interactive_only=True)
            annotations = self._build_annotations()
            await self._inject_overlay(annotations)

        try:
            options: dict[str, Any] = {}
            if save_path is not None:
                options["path"] = save_path
            if region is not None:
                options["clip"] = {
                    "x": region["x"],
                    "y": region["y"],
                    "width": region["width"],
                    "height": region["height"],
                }
            encoded = await self._playwright_execute(
                "const options = "
                + json.dumps(options)
                + ";\n"
                + "const data = await page.screenshot(options);\n"
                + "return data.toString('base64');"
            )
            result: dict[str, Any] = {"png_bytes": base64.b64decode(encoded)}
            if annotations is not None:
                result["annotations"] = annotations
            return result
        finally:
            if annotate:
                await self._remove_overlay()

    async def _playwright_execute(
        self,
        code: str,
        *,
        timeout_sec: int = 60,
    ) -> Any:
        """POST to /playwright/execute and unwrap kernel-images' envelope."""

        response = await self._http.post(
            "/playwright/execute",
            json={"code": code, "timeout_sec": timeout_sec},
        )
        response.raise_for_status()
        body = response.json()
        if not body.get("success", False):
            raise RuntimeError(body.get("error") or "playwright execute failed")
        return body.get("result")

    def _invalidate_snapshot_cache(self) -> None:
        self._snapshot_cache.clear()

    def _resolve_ref(self, ref: str) -> dict[str, Any]:
        entry = self._snapshot_cache.get(ref)
        if entry is None:
            raise KeyError(
                f"Unknown ref {ref!r}; call browser_get_state to refresh refs"
            )
        return entry

    async def _act_click_on_entry(
        self,
        entry: dict[str, Any],
        *,
        button: str = "left",
        num_clicks: int = 1,
    ) -> None:
        """Resolve a cache entry two-tier and click its live center."""

        code = self._build_ref_click_js(entry, button, num_clicks)
        await self._playwright_execute(code)

    def _build_ref_click_js(
        self,
        entry: dict[str, Any],
        button: str,
        num_clicks: int,
    ) -> str:
        """Build the JS that re-locates a cached ref and clicks it.

        Tier one is a CDP ``DOM.resolveNode`` on the snapshot's backend node id;
        tier two heals the ref by matching role + normalized name + nth in the
        accessibility tree. The clicked center is computed at action time, after
        ``scrollIntoView``, with a covering-element guard.
        """

        backend_node_id = entry.get("backend_node_id")
        bid_lit = json.dumps(backend_node_id)
        role_lit = json.dumps(str(entry.get("role", "")))
        name_lit = json.dumps(str(entry.get("name", "")))
        nth_lit = json.dumps(int(entry.get("nth", 0)))
        ref_lit = json.dumps(str(entry.get("ref", "")))
        button_lit = json.dumps(button)
        click_opts = (
            f"{{button: {button_lit}, clickCount: {int(num_clicks)}}}"
            if num_clicks != 1
            else f"{{button: {button_lit}}}"
        )
        return f"""
const cdp = await page.context().newCDPSession(page);
let objectId = null;
const __bid = {bid_lit};
if (__bid !== null) {{
  try {{
    const r = await cdp.send('DOM.resolveNode', {{backendNodeId: __bid}});
    objectId = (r && r.object && r.object.objectId) ? r.object.objectId : null;
  }} catch (e) {{ objectId = null; }}
}}
if (!objectId) {{
  const ax = await cdp.send('Accessibility.getFullAXTree');
  const target = {role_lit};
  const wantName = {name_lit};
  const matches = [];
  for (const n of (ax.nodes || [])) {{
    if (n.ignored) continue;
    if (!n.role || n.role.value !== target) continue;
    const nm = (n.name && n.name.value != null) ? String(n.name.value) : '';
    const norm = nm.replace(/\\s+/g, ' ').trim().slice(0, 240);
    if (norm !== wantName) continue;
    if (n.backendDOMNodeId == null) continue;
    matches.push(n.backendDOMNodeId);
  }}
  const pick = matches[{nth_lit}] != null ? matches[{nth_lit}] : matches[0];
  if (pick != null) {{
    try {{
      const r = await cdp.send('DOM.resolveNode', {{backendNodeId: pick}});
      objectId = (r && r.object && r.object.objectId) ? r.object.objectId : null;
    }} catch (e) {{ objectId = null; }}
  }}
}}
if (!objectId) {{
  throw new Error("Unknown ref " + {ref_lit} + "; the element is gone — call browser_get_state to refresh refs");
}}
const probe = await cdp.send('Runtime.callFunctionOn', {{
  objectId: objectId,
  returnByValue: true,
  functionDeclaration: function () {{
    this.scrollIntoView({{block: 'center', inline: 'center'}});
    const r = this.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return {{ok: false}};
    const cx = Math.round(r.left + r.width / 2);
    const cy = Math.round(r.top + r.height / 2);
    let cover = null;
    const hit = document.elementFromPoint(cx, cy);
    if (hit && hit !== this && !this.contains(hit) && !hit.contains(this)) {{
      const cls = (hit.className && typeof hit.className === 'string')
        ? hit.className.split(/\\s+/)[0] : '';
      cover = hit.tagName.toLowerCase()
        + (hit.id ? '#' + hit.id : '')
        + (cls ? '.' + cls : '');
    }}
    return {{ok: true, cover: cover}};
  }}.toString(),
}});
const res = (probe && probe.result && probe.result.value) ? probe.result.value : {{ok: false}};
if (!res.ok) {{
  throw new Error("ref element not visible; call browser_get_state to refresh refs");
}}
if (res.cover) {{
  throw new Error("ref click blocked: covered by <" + res.cover + ">. Dismiss that element, then browser_get_state and retry.");
}}
// The probe measured inside the node's own frame, which is right for
// elementFromPoint and wrong for the mouse: DOM.resolveNode on a node in an
// iframe resolves into that frame's context, so its rect is frame-relative
// while page.mouse dispatches in root space. getBoxModel returns the quad
// already in root coordinates. Read it after scrollIntoView, not before.
let box = null;
try {{
  box = await cdp.send('DOM.getBoxModel', {{objectId: objectId}});
}} catch (e) {{ box = null; }}
const quad = (box && box.model && box.model.content) ? box.model.content : null;
if (!quad) {{
  throw new Error("ref element not measurable; call browser_get_state to refresh refs");
}}
const cx = Math.round((quad[0] + quad[2] + quad[4] + quad[6]) / 4);
const cy = Math.round((quad[1] + quad[3] + quad[5] + quad[7]) / 4);
await page.mouse.click(cx, cy, {click_opts});
await page.waitForTimeout(150);
return true;
"""

    @staticmethod
    def _merge_frame_nodes(
        frames: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Flatten per-frame node lists into one root-space list.

        Each frame measured its nodes against its own viewport and reports the
        origin it sits at in the root frame; adding the two makes every
        coordinate comparable, and clickable, because ``page.mouse`` dispatches
        in root space only.

        ponytail: ``depth`` stays frame-local, so ``max_depth`` treats each
        frame as its own root and under-filters iframe content. Carry the
        iframe element's own depth as a base if that ever matters.
        """

        merged: list[dict[str, Any]] = []
        for frame in frames:
            origin_x = int(frame.get("x", 0))
            origin_y = int(frame.get("y", 0))
            for node in frame.get("nodes", []):
                merged.append({
                    **node,
                    "x": int(node.get("x", 0)) + origin_x,
                    "y": int(node.get("y", 0)) + origin_y,
                })
        return merged

    def _build_tree_and_cache(
        self,
        nodes: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        tree: list[dict[str, Any]] = []
        cache: dict[str, dict[str, Any]] = {}
        # 0-based occurrence counter per (role, name) so a cache entry can be
        # re-located in the accessibility tree even when its backend node id is
        # gone — the nth match disambiguates duplicate (role, name) pairs.
        nth_counts: dict[tuple[str, str], int] = {}

        for index, node in enumerate(nodes, start=1):
            ref = f"@e{index}"
            x = int(node.get("x", 0))
            y = int(node.get("y", 0))
            width = int(node.get("width", 0))
            height = int(node.get("height", 0))
            center_x = x + width // 2
            center_y = y + height // 2
            role = str(node.get("role", ""))
            name = str(node.get("name", ""))
            backend_node_id = node.get("backend_node_id")
            nth = nth_counts.get((role, name), 0)
            nth_counts[(role, name)] = nth + 1

            entry: dict[str, Any] = {
                "ref": ref,
                "role": role,
                "name": name,
                "x": center_x,
                "y": center_y,
            }
            intent = self._state_entry_intent(role, name)
            if intent is not None:
                entry["intent"] = intent
            depth = node.get("depth")
            if depth is not None:
                entry["depth"] = int(depth)
            entry["text_block"] = str(node.get("text_block") or "")
            heading_level = node.get("heading_level")
            if heading_level is not None:
                entry["heading_level"] = int(heading_level)
            tree.append(entry)
            cache_entry: dict[str, Any] = {
                "x": center_x,
                "y": center_y,
                "role": role,
                "name": name,
                "backend_node_id": backend_node_id,
                "nth": nth,
            }
            if intent is not None:
                cache_entry["intent"] = intent
            cache[ref] = cache_entry

        return tree, cache

    def _state_entry_visible(
        self,
        entry: dict[str, Any],
        *,
        interactive_only: bool,
        max_depth: int | None,
    ) -> bool:
        role = str(entry.get("role", ""))
        if interactive_only and role not in self._INTERACTIVE_ROLES:
            return False
        if entry.get("intent") == "accept_consent":
            return True
        if max_depth is not None and int(entry.get("depth", 0)) > max_depth:
            return False
        return True

    def _state_entry_intent(self, role: str, name: str) -> str | None:
        if role not in {"button", "link"}:
            return None
        normalized = self._normalize_state_name(name)
        if not normalized:
            return None
        if self._CONSENT_SETTINGS_RE.search(normalized):
            return None
        if self._CONSENT_ACTION_RE.search(normalized):
            return "accept_consent"
        return None

    def _normalize_state_name(self, name: str) -> str:
        collapsed = " ".join(name.split()).lower()
        return (
            unicodedata.normalize("NFKD", collapsed)
            .encode("ascii", "ignore")
            .decode("ascii")
        )

    def _prioritize_state_entries(
        self,
        entries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return sorted(
            entries,
            key=lambda entry: (
                0 if entry.get("intent") == "accept_consent" else 1,
                int(str(entry.get("ref", "@e0"))[2:] or "0"),
            ),
        )

    def _snapshot_script(self, selector: str | None) -> str:
        return self._SNAPSHOT_SCRIPT.replace(
            "__SUROGATES_SELECTOR__",
            json.dumps(selector),
        )

    def _build_annotations(self) -> list[dict[str, Any]]:
        annotations: list[dict[str, Any]] = []
        for label, (ref, entry) in enumerate(
            sorted(self._snapshot_cache.items(), key=lambda item: int(item[0][2:])),
            start=1,
        ):
            annotations.append(
                {
                    "ref": ref,
                    "label": label,
                    "role": entry.get("role", ""),
                    "name": entry.get("name", ""),
                }
            )
        return annotations

    async def _inject_overlay(self, annotations: list[dict[str, Any]]) -> None:
        overlay_data = [
            {"label": annotation["label"], **self._snapshot_cache[annotation["ref"]]}
            for annotation in annotations
        ]
        overlay_json = json.dumps(overlay_data)
        code = f"""
await page.evaluate((items) => {{
  document.getElementById('surogates-overlay')?.remove();
  const c = document.createElement('canvas');
  c.id = 'surogates-overlay';
  c.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483647';
  c.width = window.innerWidth;
  c.height = window.innerHeight;
  document.documentElement.appendChild(c);
  const g = c.getContext('2d');
  g.font = 'bold 14px sans-serif';
  for (const item of items) {{
    g.fillStyle = 'rgba(255,215,0,0.9)';
    g.fillRect(item.x - 12, item.y - 10, 24, 20);
    g.fillStyle = 'black';
    g.textAlign = 'center';
    g.textBaseline = 'middle';
    g.fillText(String(item.label), item.x, item.y);
  }}
}}, {overlay_json});
return true;
"""
        await self._playwright_execute(code)

    async def _remove_overlay(self) -> None:
        await self._playwright_execute(
            """
await page.evaluate(() => {
  document.getElementById('surogates-overlay')?.remove();
});
return true;
"""
        )
