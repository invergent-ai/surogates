"""Opt-in end-to-end browser smoke tests.

Run explicitly with:

    uv run pytest -m browser_e2e tests/integration/test_browser_e2e.py -v

Requires Docker and the kernel-images Chromium image.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from urllib.parse import quote

import pytest
import websockets

from surogates.api.routes.browser import MAX_CDP_FRAME, _cdp_browser_ws_url
from surogates.browser.base import BrowserSpec
from surogates.browser.cdp import CdpClient
from surogates.browser.client import KernelBrowserClient
from surogates.browser.process import ProcessBrowserBackend
from surogates.browser.shell import ShellSession


pytestmark = pytest.mark.browser_e2e

E2E_IMAGE = os.environ.get(
    "BROWSER_E2E_IMAGE",
    "ghcr.io/invergent-ai/surogates-agent-browser:latest",
)


@pytest.fixture()
async def backend():
    browser_backend = ProcessBrowserBackend(
        image=E2E_IMAGE,
        rest_port_base=39000,
        cdp_port_base=39100,
        live_view_port_base=39200,
    )
    yield browser_backend


@pytest.fixture()
async def browser(backend):
    browser_id, endpoint = await backend.provision(
        BrowserSpec(image=E2E_IMAGE, pod_ready_timeout=60)
    )
    try:
        yield browser_id, endpoint
    finally:
        await backend.destroy(browser_id)


async def test_navigate_and_get_state(browser) -> None:
    _browser_id, endpoint = browser
    async with KernelBrowserClient(rest_url=endpoint.rest_url) as client:
        result = await client.navigate("https://example.com")
        assert "Example" in result["title"]

        state = await client.get_state(interactive_only=True)
        assert any(node["role"] == "link" for node in state["tree"])


async def test_screenshot_returns_png(browser) -> None:
    _browser_id, endpoint = browser
    async with KernelBrowserClient(rest_url=endpoint.rest_url) as client:
        await client.navigate("https://example.com")
        result = await client.screenshot()
        assert result["png_bytes"].startswith(b"\x89PNG")
        assert len(result["png_bytes"]) > 1000


# A control inside an iframe offset 300px down and 100px across -- the shape of
# every embedded payment field and consent frame.  The frame reports the button
# at its own (30, 20); only the frame's origin makes that clickable.
IFRAME_PAGE = (
    "<body style='margin:0'>"
    "<h1 style='margin:0;height:100px'>Checkout</h1>"
    "<iframe style='position:absolute;top:300px;left:100px;width:400px;"
    "height:200px;border:0' srcdoc=\""
    "<body style='margin:0'>"
    "<button style='position:absolute;top:20px;left:30px;width:100px;"
    "height:40px' onclick='this.textContent=&quot;Paid&quot;'>Pay now</button>"
    "</body>\"></iframe>"
    "</body>"
)


async def test_get_state_reaches_into_iframes(browser) -> None:
    _browser_id, endpoint = browser
    async with KernelBrowserClient(rest_url=endpoint.rest_url) as client:
        await client.navigate("data:text/html," + quote(IFRAME_PAGE))

        state = await client.get_state()
        button = next(
            (n for n in state["tree"] if n["name"] == "Pay now"), None
        )
        assert button is not None, "iframe content missing from the snapshot"
        # Root-space centre: frame origin (100, 300) + local (30, 20) + half
        # the 100x40 button.  Nothing here should be off by the frame origin.
        assert button["x"] == pytest.approx(180, abs=2)
        assert button["y"] == pytest.approx(340, abs=2)


async def test_click_ref_lands_inside_an_iframe(browser) -> None:
    _browser_id, endpoint = browser
    async with KernelBrowserClient(rest_url=endpoint.rest_url) as client:
        await client.navigate("data:text/html," + quote(IFRAME_PAGE))

        state = await client.get_state()
        ref = next(n["ref"] for n in state["tree"] if n["name"] == "Pay now")
        await client.click_ref(ref)

        # The button rewrites its own label, so a click measured in the frame's
        # coordinates instead of the root's misses it and the label stands.
        after = await client.get_state()
        assert any(n["name"] == "Paid" for n in after["tree"])


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
        texts = [n.get("text_block", "") for n in state["tree"]]

        assert any("Real content" in t for t in texts)
        # aria-hidden hides the whole subtree, not just the marked element, so
        # neither the button nor the paragraph inside it becomes a node.
        # Asserted per node rather than across every name: until Task 3 lands,
        # <body> is still named by its own innerText and carries every string
        # on the page, which would mask this.
        assert not any(
            n["role"] == "button" and "Ghost" in n.get("name", "")
            for n in state["tree"]
        )
        assert not any(t.strip() == "Decorative duplicate" for t in texts)


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

        tree = (await client.get_state())["tree"]
        roles = {n["role"] for n in tree}
        assert {"navigation", "main", "table", "row", "columnheader",
                "cell", "list", "listitem"} <= roles
        # <summary> behaves as a button; contenteditable is a textbox.
        assert "button" in roles
        assert "textbox" in roles
        assert "file-input" in roles
        # "generic" must no longer be the answer for a table header cell.
        assert not any(
            n["role"] == "generic" and n.get("text_block") == "Plan"
            for n in tree
        )


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
        by_role: dict[str, list] = {}
        for node in (await client.get_state())["tree"]:
            by_role.setdefault(node["role"], []).append(node)

        names = [n["name"] for n in by_role["textbox"]]
        # <label for>, a wrapping <label>, and aria-labelledby all win over
        # the placeholder, which is only the last resort.
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
        # A wrapper swallowing its subtree's text as a "name" is what poisons
        # tier-2 ref healing, which re-locates a lost ref by role + name.
        generics = [n for n in state["tree"] if n["role"] == "generic"]
        assert generics, "fixture should produce at least one generic container"
        assert all("Paragraph one" not in n["name"] for n in generics)


CLICKABLE_PAGE = (
    "<body style='margin:0'>"
    "<div onclick='void 0' style='width:80px;height:30px'>Add to cart</div>"
    "<span tabindex='0' style='display:block;width:80px;height:30px'>Menu</span>"
    "<div style='cursor:pointer;width:80px;height:30px'>Dismiss</div>"
    "<div style='width:80px;height:30px'>Just a label</div>"
    "<a href='/x' style='display:block;width:80px;height:30px'>"
    "<span>Inside a link</span></a>"
    "<div tabindex='-1' style='width:80px;height:30px'>Scroll target</div>"
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
        # A plain div stays plain -- promoting everything floods the tree.
        assert "Just a label" not in names


async def test_promotion_does_not_leak_into_links_or_scroll_targets(
    browser,
) -> None:
    _browser_id, endpoint = browser
    async with KernelBrowserClient(rest_url=endpoint.rest_url) as client:
        await client.navigate("data:text/html," + quote(CLICKABLE_PAGE))

        buttons = [
            n for n in (await client.get_state())["tree"]
            if n["role"] == "button"
        ]
        names = [n["name"] for n in buttons]
        # cursor:pointer inherits, so every span inside every link looked
        # clickable: this turned 13 Wikipedia buttons into 618 and every one
        # of Hacker News' 59 promotions was a span inside an anchor.
        assert "Inside a link" not in names
        # A negative tabindex means focusable by script but deliberately NOT
        # reachable by the user -- how pages mark scroll targets.
        assert "Scroll target" not in names


# --- browser shell -----------------------------------------------------------


class RecordingShellClient:
    """Stands in for the viewer's WebSocket: keeps frames and JSON messages."""

    def __init__(self) -> None:
        self.binary: list[bytes] = []
        self.text: list[dict] = []

    async def send_bytes(self, payload: bytes) -> None:
        self.binary.append(payload)

    async def send_text(self, payload: str) -> None:
        self.text.append(json.loads(payload))


@asynccontextmanager
async def shell_for(endpoint, *, lease: bool = True):
    """Yield a started ShellSession against a real pod, plus a probe session.

    The probe is a SECOND flat session on the same target, used to read the
    page back without going through the shell -- the shell deliberately has no
    verb that could.
    """

    url = await _cdp_browser_ws_url(endpoint.cdp_url)
    state = {"lease": lease}

    async def lease_held() -> bool:
        return state["lease"]

    async with websockets.connect(url, max_size=MAX_CDP_FRAME) as socket:
        async with CdpClient(socket) as cdp:
            pages = await cdp.targets()
            probe = await cdp.attach_page(pages[0]["targetId"])
            client = RecordingShellClient()
            session = ShellSession(cdp, client, lease_held=lease_held)
            await session.start()
            try:
                yield session, cdp, client, probe, state
            finally:
                await session.close()


async def _read(cdp, probe: str, expression: str):
    result = await cdp.call(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True},
        session=probe,
    )
    return result.get("result", {}).get("value")


# A button at 20% across and 40% down, 200x60. A click sent as (0.25, 0.43)
# is inside it only if the server scaled the normalized point by the live
# viewport; getting that wrong misses and the label stays "miss".
SHELL_BUTTON = (
    "document.body.innerHTML = \"<button id='b' style='position:absolute;"
    "left:20%;top:40%;width:200px;height:60px' "
    "onclick='this.textContent=\\\"HIT\\\"'>miss</button>\""
)


async def test_shell_click_lands_at_a_normalized_coordinate(browser) -> None:
    _browser_id, endpoint = browser
    async with shell_for(endpoint) as (session, cdp, _client, probe, _state):
        await session.handle(json.dumps({"t": "navigate", "url": "https://example.com/"}))
        await asyncio.sleep(3.0)
        await cdp.call("Runtime.evaluate", {"expression": SHELL_BUTTON}, session=probe)

        await session.handle(json.dumps({"t": "click", "x": 0.25, "y": 0.43}))
        await asyncio.sleep(1.5)

        assert await _read(cdp, probe, "document.getElementById('b').textContent") == "HIT"


async def test_shell_drops_commands_without_the_lease(browser) -> None:
    _browser_id, endpoint = browser
    async with shell_for(endpoint) as (session, cdp, _client, probe, state):
        await session.handle(json.dumps({"t": "navigate", "url": "https://example.com/"}))
        await asyncio.sleep(3.0)
        await cdp.call("Runtime.evaluate", {"expression": SHELL_BUTTON}, session=probe)

        state["lease"] = False
        await session.handle(json.dumps({"t": "click", "x": 0.25, "y": 0.43}))
        await asyncio.sleep(1.5)

        # Watching is never gated; acting is.
        assert await _read(cdp, probe, "document.getElementById('b').textContent") == "miss"


async def test_shell_navigate_rejects_file_scheme(browser) -> None:
    _browser_id, endpoint = browser
    async with shell_for(endpoint) as (session, cdp, _client, probe, _state):
        await session.handle(json.dumps({"t": "navigate", "url": "https://example.com/"}))
        await asyncio.sleep(3.0)

        # The unit test proves translate() raises; this proves nothing
        # downstream re-admits it.
        await session.handle(json.dumps({"t": "navigate", "url": "file:///etc/passwd"}))
        await asyncio.sleep(1.5)

        assert await _read(cdp, probe, "location.protocol") == "https:"


async def test_shell_streams_frames_while_the_page_changes(browser) -> None:
    _browser_id, endpoint = browser
    async with shell_for(endpoint) as (session, cdp, client, probe, _state):
        await session.handle(json.dumps({"t": "navigate", "url": "https://example.com/"}))
        await asyncio.sleep(3.0)
        assert client.binary, "no screencast frames reached the viewer"
        assert all(frame.startswith(b"\xff\xd8") for frame in client.binary), (
            "frames must be decoded JPEG, not base64 text"
        )
        assert any(message.get("t") == "nav" for message in client.text)


async def test_shell_switch_tab_drains_the_previous_stream(browser) -> None:
    _browser_id, endpoint = browser
    async with shell_for(endpoint) as (session, cdp, client, _probe, _state):
        created = await cdp.call(
            "Target.createTarget", {"url": "https://example.com/"}
        )
        await asyncio.sleep(2.0)

        client.binary.clear()
        await session.handle(
            json.dumps({"t": "switch_tab", "id": created["targetId"]})
        )
        await asyncio.sleep(2.0)

        tabs = [m for m in client.text if m.get("t") == "tabs"]
        assert tabs, "switching tabs must push a fresh tab list"
        active = [t for t in tabs[-1]["tabs"] if t["active"]]
        assert active and active[0]["id"] == created["targetId"]
