"""Async HTTP client for kernel-images REST API."""

from __future__ import annotations

from typing import Any

import httpx


class KernelBrowserClient:
    """HTTP client for one kernel-images browser REST endpoint."""

    _SNAPSHOT_SCRIPT = """
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
for (const el of Array.from(document.querySelectorAll('*'))) {
  const style = window.getComputedStyle(el);
  if (style.visibility === 'hidden' || style.display === 'none') continue;
  const bbox = el.getBoundingClientRect();
  if (!bbox || bbox.width <= 0 || bbox.height <= 0) continue;
  out.push({
    role: roleOf(el),
    name: nameOf(el),
    x: Math.round(bbox.x),
    y: Math.round(bbox.y),
    width: Math.round(bbox.width),
    height: Math.round(bbox.height),
    depth: depthOf(el),
    children_count: el.children ? el.children.length : 0,
  });
}
return {
  url: page.url(),
  title: await page.title(),
  viewport: page.viewportSize() || {width: 0, height: 0},
  nodes: out,
};
"""

    def __init__(
        self,
        rest_url: str,
        *,
        timeout: float = 30.0,
        snapshot_cache: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.rest_url = rest_url.rstrip("/")
        self._timeout = timeout
        self._http: httpx.AsyncClient = httpx.AsyncClient(
            base_url=self.rest_url,
            timeout=timeout,
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

    async def get_state(self) -> dict[str, Any]:
        """Return a DOM-derived page tree with stable refs and cached centers."""

        raw = await self._playwright_execute(self._SNAPSHOT_SCRIPT)
        nodes = raw.get("nodes", [])
        tree, new_cache = self._build_tree_and_cache(nodes)

        self._snapshot_cache.clear()
        self._snapshot_cache.update(new_cache)

        return {
            "url": raw.get("url", ""),
            "title": raw.get("title", ""),
            "viewport": raw.get("viewport", {"width": 0, "height": 0}),
            "tree": tree,
        }

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

    def _build_tree_and_cache(
        self,
        nodes: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        tree: list[dict[str, Any]] = []
        cache: dict[str, dict[str, Any]] = {}

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

            tree.append({"ref": ref, "role": role, "name": name, "x": center_x, "y": center_y})
            cache[ref] = {"x": center_x, "y": center_y, "role": role, "name": name}

        return tree, cache
