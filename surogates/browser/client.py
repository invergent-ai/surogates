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
const __surogatesSnapshot = await page.evaluate((selector) => {
function roleOf(el) {
  const explicit = el.getAttribute('role');
  if (explicit) return explicit;
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || '').toLowerCase();
  if (tag === 'button') return 'button';
  if (tag === 'a' && el.hasAttribute('href')) return 'link';
  if (tag === 'textarea') return 'textbox';
  if (tag === 'select') return 'combobox';
  if (tag === 'input') {
    if (type === 'checkbox') return 'checkbox';
    if (type === 'radio') return 'radio';
    if (type === 'range') return 'slider';
    if (type === 'number') return 'spinbutton';
    if (type === 'search') return 'searchbox';
    return 'textbox';
  }
  if (/^h[1-6]$/.test(tag)) return 'heading';
  if (tag === 'img') return 'img';
  if (tag === 'p') return 'paragraph';
  return 'generic';
}

function nameOf(el) {
  const direct = el.getAttribute('aria-label')
    || el.getAttribute('title')
    || el.getAttribute('alt')
    || el.getAttribute('placeholder')
    || el.value
    || el.innerText
    || el.textContent
    || '';
  return String(direct).replace(/\\s+/g, ' ').trim().slice(0, 240);
}

function depthOf(el) {
  let d = 0, cur = el;
  while (cur && cur.parentElement) { d++; cur = cur.parentElement; }
  return d;
}

const out = [];
const root = selector === null ? document : document.querySelector(selector);
if (!root) throw new Error('selector matched no element');
for (const el of Array.from(root.querySelectorAll('*'))) {
  const style = window.getComputedStyle(el);
  if (style.visibility === 'hidden' || style.display === 'none') continue;
  const bbox = el.getBoundingClientRect();
  if (!bbox || bbox.width <= 0 || bbox.height <= 0) continue;
  el.setAttribute('data-sg-i', String(out.length));
  out.push({
    role: roleOf(el),
    name: nameOf(el),
    x: Math.round(bbox.x),
    y: Math.round(bbox.y),
    width: Math.round(bbox.width),
    height: Math.round(bbox.height),
    depth: depthOf(el),
    children_count: el.children ? el.children.length : 0,
    idx: out.length,
  });
}
return {
  viewport: {width: window.innerWidth, height: window.innerHeight},
  nodes: out,
};
}, __surogatesSelector);
const __cdp = await page.context().newCDPSession(page);
const __doc = await __cdp.send('DOM.getDocument', {depth: -1, pierce: true});
const __map = {};
(function walk(n){ if(!n) return; const a=n.attributes||[]; for(let i=0;i<a.length;i+=2){ if(a[i]==='data-sg-i'){ __map[a[i+1]] = n.backendNodeId; } } for(const c of (n.children||[])) walk(c); if(n.contentDocument) walk(n.contentDocument); })(__doc.root);
for (const node of __surogatesSnapshot.nodes) { node.backend_node_id = (node.idx!=null && __map[String(node.idx)]!=null) ? __map[String(node.idx)] : null; }
return {
  url: page.url(),
  title: await page.title(),
  viewport: page.viewportSize() || __surogatesSnapshot.viewport,
  nodes: __surogatesSnapshot.nodes,
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
        compact: bool = False,
        max_depth: int | None = None,
        selector: str | None = None,
    ) -> dict[str, Any]:
        """Return a DOM-derived page tree with stable refs and cached centers."""

        raw = await self._playwright_execute(self._snapshot_script(selector))
        nodes = raw.get("nodes", [])
        full_tree, new_cache = self._build_tree_and_cache(nodes)
        tree = [
            entry
            for entry in full_tree
            if self._state_entry_visible(
                entry,
                interactive_only=interactive_only,
                compact=compact,
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
        """Drag the mouse along a path of viewport coordinates."""

        if len(path) < 2:
            raise ValueError("drag path must contain at least two points")
        body: dict[str, Any] = {
            "path": [list(point) for point in path],
            "smooth": False,
        }
        if button != "left":
            body["button"] = button
        response = await self._http.post("/computer/drag_mouse", json=body)
        response.raise_for_status()
        self._invalidate_snapshot_cache()

    async def wait(self, ms: int) -> None:
        """Sleep without changing browser state or cached refs."""

        await asyncio.sleep(max(0, ms) / 1000.0)

    async def screenshot(
        self,
        *,
        region: dict[str, int] | None = None,
        annotate: bool = False,
        save_path: str | None = None,
        viewport_only: bool = False,
    ) -> dict[str, Any]:
        """Capture a PNG screenshot, optionally with numbered ref overlays."""

        annotations: list[dict[str, Any]] | None = None
        if annotate:
            if not self._snapshot_cache:
                await self.get_state(interactive_only=True)
            annotations = self._build_annotations()
            await self._inject_overlay(annotations)

        try:
            if save_path is not None or viewport_only:
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
            else:
                response = await self._http.post(
                    "/computer/screenshot",
                    json={} if region is None else {"region": region},
                )
                response.raise_for_status()
                result = {"png_bytes": response.content}
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
    return {{ok: true, cx: cx, cy: cy, cover: cover}};
  }}.toString(),
}});
const res = (probe && probe.result && probe.result.value) ? probe.result.value : {{ok: false}};
if (!res.ok) {{
  throw new Error("ref element not visible; call browser_get_state to refresh refs");
}}
if (res.cover) {{
  throw new Error("ref click blocked: covered by <" + res.cover + ">. Dismiss that element, then browser_get_state and retry.");
}}
await page.mouse.click(res.cx, res.cy, {click_opts});
await page.waitForTimeout(150);
return true;
"""

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
        compact: bool,
        max_depth: int | None,
    ) -> bool:
        role = str(entry.get("role", ""))
        if interactive_only and role not in self._INTERACTIVE_ROLES:
            return False
        if compact and not entry.get("name") and role not in self._INTERACTIVE_ROLES:
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
