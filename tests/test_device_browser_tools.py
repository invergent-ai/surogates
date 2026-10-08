"""The browser tools of a session on the user's computer: the browser there, no cloud browser."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from surogates.browser.control import paused_by_user_result
from surogates.devices.browser import LOCATE, NO_BROWSER, SNAPSHOT
from surogates.devices.workspace import DeviceWorkspaceIO
from surogates.tools.builtin import browser
from surogates.tools.workspace_io import LocalWorkspaceIO
from surogates.session.events import EventType
from tests.fake_laptop import InProcessRunner

FRAMES = {
    "url": "https://example.com/",
    "title": "Example",
    "viewport": {"width": 1280, "height": 800},
    "frames": [{"x": 0, "y": 0, "nodes": [
        {"role": "button", "name": "Search", "x": 10, "y": 30, "width": 80, "height": 20, "depth": 4,
         "children_count": 0, "idx": 0, "text_block": "", "backend_node_id": 12},
    ]}],
}


class Laptop:
    """A computer in a test: its folder's operations run for real, its browser's are scripted."""

    def __init__(self, folder: str, *browsing: dict[str, Any]) -> None:
        self.files = InProcessRunner(LocalWorkspaceIO(folder))
        self.browsing = list(browsing)
        self.asked: list[tuple[str, dict[str, Any]]] = []

    async def run(self, kind: str, args: dict[str, Any], payload: bytes | None = None) -> dict[str, Any]:
        if not kind.startswith("browser."):
            return await self.files.run(kind, args, payload)
        self.asked.append((kind, json.loads(json.dumps(args))))
        return self.browsing.pop(0)


class NoPool:
    """A worker's browser pool, which a session on the user's computer never asks."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def ensure(self, **kwargs: Any) -> Any:
        self.asked.append("ensure")
        raise AssertionError("a session on the user's computer asked the cloud's browser pool")

    async def destroy_for_session(self, session_id: str) -> None:
        self.asked.append("destroy")


class Events:
    """The session's event log in a test: what each tool told the session's pane."""

    def __init__(self) -> None:
        self.emitted: list[tuple[UUID, EventType, dict[str, Any]]] = []

    async def emit_event(self, session_id: UUID, event_type: EventType, data: dict[str, Any]) -> int:
        self.emitted.append((session_id, event_type, data))
        return len(self.emitted)


@pytest.fixture()
def computer(tmp_path):
    folder = (tmp_path / "laptop").resolve()
    folder.mkdir()

    def on(*browsing: dict[str, Any]) -> SimpleNamespace:
        laptop = Laptop(str(folder), *browsing)
        return SimpleNamespace(
            laptop=laptop,
            folder=folder,
            kwargs={
                "tenant": SimpleNamespace(org_id=UUID(int=1), user_id=UUID(int=2)),
                "session_id": uuid4(),
                "browser_pool": NoPool(),
                "workspace_io": DeviceWorkspaceIO(laptop, root=str(folder)),
                "workspace_path": None,
                "session_config": {
                    "execution": {"kind": "device", "device_id": str(uuid4())},
                    "storage_bucket": "cloud-bucket",
                    "browser": {"profile_id": str(uuid4())},
                },
                "storage": SimpleNamespace(write=_no_cloud_write),
                "session_store": Events(),
            },
        )

    return on


async def _no_cloud_write(*args: Any) -> None:
    raise AssertionError("a session on the user's computer wrote to the cloud's storage")


async def test_a_navigation_opens_the_page_on_the_computer_and_reads_its_outline(computer) -> None:
    rig = computer({"ok": {"url": "https://example.com/", "title": "Example", "opened": True}}, {"ok": FRAMES})

    body = json.loads(await browser._browser_navigate_handler({"url": "https://example.com"}, **rig.kwargs))

    assert body["title"] == "Example"
    assert "@e1" in body["snapshot"] and "Search" in body["snapshot"]
    assert rig.laptop.asked == [
        ("browser.navigate", {"url": "https://example.com", "wait_until": "load"}),
        ("browser.observe", {"script": SNAPSHOT, "params": {"selector": None}}),
    ]
    # No cloud browser, no browser minutes, and the session's cloud profile is not loaded.
    assert rig.kwargs["browser_pool"].asked == []


async def test_a_session_keeps_its_refs_between_calls_and_clicks_one_where_it_is(computer) -> None:
    rig = computer({"ok": FRAMES}, {"ok": {"x": 50, "y": 40}}, {"ok": {"notices": ["The page asked for a file to upload."]}})

    await browser._browser_get_state_handler({}, **rig.kwargs)
    body = json.loads(await browser._browser_click_handler({"ref": "@e1"}, **rig.kwargs))

    assert body == {"clicked": True, "notices": ["The page asked for a file to upload."]}
    assert rig.laptop.asked[1:] == [
        ("browser.observe", {"script": LOCATE, "params": {"backend_node_id": 12, "role": "button", "name": "Search", "nth": 0}}),
        ("browser.mouse", {"action": "click", "x": 50, "y": 40, "button": "left", "clicks": 1}),
    ]


async def test_a_screenshot_is_saved_in_the_folder_among_the_harness_files(computer) -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 32
    rig = computer({"ok": base64.b64encode(png).decode("ascii")})

    body = json.loads(await browser._browser_screenshot_handler({}, **rig.kwargs))

    assert body["saved"] is True
    assert body["relative_path"].startswith(".surogates-results/browser-screenshots/browser-screenshot-")
    assert body["path"] == body["relative_path"]
    assert (rig.folder / body["relative_path"]).read_bytes() == png
    # Not a read_file the agent cannot use on a computer's folder yet.
    assert "read_file" not in body["hint"]


async def test_a_screenshot_the_folder_cannot_take_is_said_so_as_the_cloud_says_it(computer) -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 32
    rig = computer({"ok": base64.b64encode(png).decode("ascii")})
    # A file where the harness's folder would be: the write fails on the computer.
    (rig.folder / ".surogates-results").write_text("not a folder")

    body = json.loads(await browser._browser_screenshot_handler({}, **rig.kwargs))

    assert body["error"] == "screenshot_save_failed"
    assert (body["bytes"], body["mime_type"]) == (len(png), "image/png")
    assert body["detail"]


async def test_closing_closes_only_the_sessions_tab_on_the_computer(computer) -> None:
    rig = computer({"ok": FRAMES}, {"ok": {"closed": True}}, {"ok": FRAMES})
    await browser._browser_get_state_handler({}, **rig.kwargs)

    assert json.loads(await browser._browser_close_handler({}, **rig.kwargs)) == {"closed": True}
    # Its refs went with the tab.
    assert json.loads(await browser._browser_click_handler({"ref": "@e1"}, **rig.kwargs))["error"] == "unknown_ref"
    assert rig.kwargs["browser_pool"].asked == []
    assert [kind for kind, _ in rig.laptop.asked] == ["browser.observe", "browser.close"]


@pytest.mark.parametrize(("error", "result"), [
    ({"type": "denied", "message": "The user did not let the agent use the browser on this computer"},
     {"error": "denied", "detail": "The user did not let the agent use the browser on this computer"}),
    ({"type": "no_browser", "message": "none"}, {"error": "no_browser", "detail": NO_BROWSER}),
    # Not the page failing: the computer's access ended, and no retry brings it back.
    ({"type": "revoked", "message": "Local access to this computer was revoked"},
     {"error": "revoked", "detail": "Local access to this computer was revoked"}),
    # Its user took the browser over: every browser tool answers as the cloud's do while a user holds its browser.
    ({"type": "paused_by_user", "message": "The user took over the agent's browser on this computer"},
     json.loads(paused_by_user_result())),
])
async def test_what_the_computer_refuses_is_the_tools_result(computer, error, result) -> None:
    for handler, args in [
        (browser._browser_navigate_handler, {"url": "https://example.com"}),
        (browser._browser_click_handler, {"x": 1, "y": 2}),
        (browser._browser_screenshot_handler, {}),
        (browser._browser_close_handler, {}),
        (browser._browser_upload_file_handler, {"paths": ["a.txt"]}),
    ]:
        rig = computer({"error": error})
        assert json.loads(await handler(args, **rig.kwargs)) == result


async def test_the_sessions_pane_hears_its_tab_on_the_computer_opened_and_closed(computer) -> None:
    rig = computer(
        {"ok": {"url": "https://example.com/", "title": "Example", "opened": True, "notices": []}},
        {"ok": {"url": "https://example.com/next", "title": "Next", "opened": False, "notices": []}},
        {"ok": {"closed": True}},
        {"ok": {"closed": False}},
    )
    session = rig.kwargs["session_id"]
    told = {"session_id": str(session), "computer": True}

    await browser._browser_navigate_handler({"url": "https://example.com", "snapshot": False}, **rig.kwargs)
    # In the tab it has: nothing new for the pane.
    await browser._browser_navigate_handler({"url": "https://example.com/next", "snapshot": False}, **rig.kwargs)
    await browser._browser_close_handler({}, **rig.kwargs)
    # No tab to close: nothing closed.
    await browser._browser_close_handler({}, **rig.kwargs)

    assert rig.kwargs["session_store"].emitted == [
        (session, EventType.BROWSER_PROVISIONED, told), (session, EventType.BROWSER_DESTROYED, told),
    ]


async def test_the_sessions_pane_hears_there_is_no_browser_on_the_computer(computer) -> None:
    rig = computer({"error": {"type": "no_browser", "message": "none"}}, {"error": {"type": "denied", "message": "no"}})
    session = rig.kwargs["session_id"]

    await browser._browser_navigate_handler({"url": "https://example.com"}, **rig.kwargs)
    # A denial is the agent's to hear, not the pane's.
    await browser._browser_get_state_handler({}, **rig.kwargs)

    assert rig.kwargs["session_store"].emitted == [
        (session, EventType.BROWSER_UNAVAILABLE, {"session_id": str(session), "computer": True}),
    ]


async def test_a_sub_agents_tab_on_the_computer_is_told_to_its_root_chats_pane_too(computer) -> None:
    rig = computer(
        {"ok": {"url": "https://example.com/", "title": "Example", "opened": True, "notices": []}},
        {"ok": {"closed": True}},
        {"error": {"type": "no_browser", "message": "none"}},
    )
    session, root = rig.kwargs["session_id"], uuid4()
    # As the server stamps a session made under a chat: the chat whose folder it works in.
    rig.kwargs["session_config"]["sandbox_root_session_id"] = str(root)
    # Each names the sub-agent: the chat's pane counts a tab for each session.
    told = {"session_id": str(session), "computer": True}

    await browser._browser_navigate_handler({"url": "https://example.com", "snapshot": False}, **rig.kwargs)
    await browser._browser_close_handler({}, **rig.kwargs)
    await browser._browser_navigate_handler({"url": "https://example.com"}, **rig.kwargs)

    assert rig.kwargs["session_store"].emitted == [
        (session, EventType.BROWSER_PROVISIONED, told), (root, EventType.BROWSER_PROVISIONED, told),
        (session, EventType.BROWSER_DESTROYED, told), (root, EventType.BROWSER_DESTROYED, told),
        (session, EventType.BROWSER_UNAVAILABLE, told), (root, EventType.BROWSER_UNAVAILABLE, told),
    ]


async def test_a_chat_that_is_its_own_root_is_told_of_its_tab_once(computer) -> None:
    rig = computer({"ok": {"url": "https://example.com/", "title": "Example", "opened": True, "notices": []}})
    session = rig.kwargs["session_id"]
    # A project's thread names itself: its own sandbox root.
    rig.kwargs["session_config"]["sandbox_root_session_id"] = str(session)

    await browser._browser_navigate_handler({"url": "https://example.com", "snapshot": False}, **rig.kwargs)

    assert rig.kwargs["session_store"].emitted == [
        (session, EventType.BROWSER_PROVISIONED, {"session_id": str(session), "computer": True}),
    ]


async def test_a_root_chats_pane_that_cannot_be_told_leaves_the_sub_agents_own_told_and_its_call_answered(computer) -> None:
    rig = computer({"ok": {"url": "https://example.com/", "title": "Example", "opened": True, "notices": []}})
    session, root = rig.kwargs["session_id"], uuid4()
    rig.kwargs["session_config"]["sandbox_root_session_id"] = str(root)
    events = rig.kwargs["session_store"]
    record = events.emit_event

    async def root_away(session_id: UUID, *args: Any) -> int:
        if session_id == root:
            raise RuntimeError("the root's event log is away")
        return await record(session_id, *args)

    events.emit_event = root_away
    body = json.loads(await browser._browser_navigate_handler({"url": "https://example.com", "snapshot": False}, **rig.kwargs))

    assert body["title"] == "Example"
    assert events.emitted == [(session, EventType.BROWSER_PROVISIONED, {"session_id": str(session), "computer": True})]


def test_a_sub_agents_tab_closed_on_the_computer_is_not_told_to_its_root_chats_agent() -> None:
    from surogates.harness.loop import AgentHarness

    root, child = uuid4(), uuid4()

    def closed(log: UUID, **data: Any) -> SimpleNamespace:
        return SimpleNamespace(id=1, session_id=log, type=EventType.BROWSER_DESTROYED.value, data=data)

    def told(event: SimpleNamespace) -> list[str]:
        return [message["content"] for message in AgentHarness._rebuild_messages(SimpleNamespace(), [event])]

    # In the root's log for its pane alone: the root's own tab is as it was, and its agent is told of no close.
    assert told(closed(root, session_id=str(child), computer=True)) == []
    # A session's own tab closed is told to its agent: the root's and the sub-agent's on the computer, the
    # cloud's, and one that names no session.
    for own in (
        closed(root, session_id=str(root), computer=True), closed(child, session_id=str(child), computer=True),
        closed(root, session_id=str(root), browser_id="b-1"), closed(root, browser_id="b-1"),
    ):
        [note] = told(own)
        assert "The browser was closed" in note


async def test_a_pane_that_cannot_be_told_leaves_the_browser_call_answered(computer) -> None:
    rig = computer({"ok": {"url": "https://example.com/", "title": "Example", "opened": True, "notices": []}})

    async def broken(*args: Any) -> int:
        raise RuntimeError("the event log is away")

    rig.kwargs["session_store"].emit_event = broken
    body = json.loads(await browser._browser_navigate_handler({"url": "https://example.com", "snapshot": False}, **rig.kwargs))

    assert body["title"] == "Example"


async def test_files_of_the_folder_go_to_the_page_once_it_asked_for_them(computer) -> None:
    rig = computer({"ok": {"files": 1, "notices": []}})
    (rig.folder / "report.pdf").write_bytes(b"%PDF")

    body = json.loads(await browser._browser_upload_file_handler({"paths": ["report.pdf"]}, **rig.kwargs))

    assert body == {"uploaded": 1}
    # Named as the folder's own operations name it: the computer reads each through its file host.
    assert rig.laptop.asked == [("browser.set_input_files", {"paths": [str(rig.folder / "report.pdf")]})]


async def test_a_path_out_of_the_folder_never_reaches_the_computers_browser(computer) -> None:
    rig = computer()

    body = json.loads(await browser._browser_upload_file_handler({"paths": ["../../etc/passwd"]}, **rig.kwargs))

    assert body["error"] == "upload_failed"
    assert rig.laptop.asked == []


async def test_a_path_the_computer_cannot_resolve_is_said_in_its_own_words_without_pythons_errno(computer) -> None:
    rig = computer()

    async def looping(kind: str, args: dict[str, Any], payload: bytes | None = None) -> dict[str, Any]:
        return {"error": {"type": "os", "code": "ELOOP", "message": "Too many levels of symbolic links: 'loop.txt'"}}

    rig.laptop.files = SimpleNamespace(run=looping)

    body = json.loads(await browser._browser_upload_file_handler({"paths": ["loop.txt"]}, **rig.kwargs))

    assert body == {"error": "upload_failed", "detail": "Too many levels of symbolic links: 'loop.txt'"}
    assert rig.laptop.asked == []


async def test_a_page_that_asked_for_no_file_is_said_so(computer) -> None:
    said = "The page has not asked for a file: click its upload button or its file input first"
    rig = computer({"error": {"type": "browser", "message": said}})

    body = json.loads(await browser._browser_upload_file_handler({"paths": ["a.txt"]}, **rig.kwargs))

    assert body == {"error": "upload_failed", "detail": said}


def test_the_upload_runs_in_the_harness_as_every_browser_tool_and_a_strict_coordinator_is_not_given_it() -> None:
    from surogates.tools.builtin.coordinator import COORDINATOR_IMPLEMENTATION_TOOLS
    from surogates.tools.router import TOOL_LOCATIONS, ToolLocation

    assert TOOL_LOCATIONS["browser_upload_file"] is ToolLocation.HARNESS
    assert "browser_upload_file" in COORDINATOR_IMPLEMENTATION_TOOLS


async def test_a_chat_in_the_cloud_has_no_upload() -> None:
    body = json.loads(await browser._browser_upload_file_handler({"paths": ["a.txt"]}, session_id=uuid4(), browser_pool=NoPool()))

    assert body["error"] == "unsupported"


# Every browser tool, with arguments it takes: none may reach the cloud's browser pool for a local-folder chat.
EVERY_TOOL = [
    (browser._browser_navigate_handler, {"url": "https://example.com"}),
    (browser._browser_get_state_handler, {}),
    (browser._browser_evaluate_handler, {"code": "return 1;"}),
    (browser._browser_close_handler, {}),
    (browser._browser_click_handler, {"x": 1, "y": 2}),
    (browser._browser_type_handler, {"text": "hello"}),
    (browser._browser_press_key_handler, {"keys": ["Enter"]}),
    (browser._browser_scroll_handler, {"x": 1, "y": 2, "delta_y": 100}),
    (browser._browser_drag_handler, {"path": [[1, 2], [3, 4]]}),
    (browser._browser_wait_handler, {"ms": 0}),
    (browser._browser_screenshot_handler, {}),
    (browser._browser_upload_file_handler, {"paths": ["a.txt"]}),
]


class AnyBrowser(Laptop):
    """A computer whose browser answers each kind as a page would."""

    async def run(self, kind: str, args: dict[str, Any], payload: bytes | None = None) -> dict[str, Any]:
        if not kind.startswith("browser."):
            return await self.files.run(kind, args, payload)
        self.asked.append((kind, args))
        if kind == "browser.mouse" and args.get("action") == "wheel":
            return {"ok": {"scroll_x": 0, "scroll_y": 100, "page_height": 2000, "viewport_height": 800, "notices": []}}
        return {"ok": {
            "browser.navigate": {"url": "https://example.com/", "title": "Example", "opened": True, "notices": []},
            "browser.observe": FRAMES,
            "browser.evaluate": {"value": 1},
            "browser.screenshot": base64.b64encode(b"\x89PNG\r\n\x1a\n").decode("ascii"),
            "browser.close": {"closed": True},
            "browser.set_input_files": {"files": 1, "notices": []},
        }.get(kind, {"notices": []})}


@pytest.mark.parametrize(("handler", "args"), EVERY_TOOL, ids=lambda value: getattr(value, "__name__", ""))
async def test_no_browser_tool_asks_the_cloud_pool_for_a_session_on_the_computer(computer, handler, args) -> None:
    rig = computer()
    laptop = AnyBrowser(str(rig.folder))
    kwargs = {**rig.kwargs, "workspace_io": DeviceWorkspaceIO(laptop, root=str(rig.folder))}

    result = await handler(args, **kwargs)

    # Answered by the computer's browser: an outline, a value, or a body without an error.
    assert '"error"' not in result, result
    assert kwargs["browser_pool"].asked == []
    assert all(kind.startswith("browser.") for kind, _ in laptop.asked)


@pytest.mark.parametrize(("handler", "args"), EVERY_TOOL, ids=lambda value: getattr(value, "__name__", ""))
async def test_a_local_folder_chats_call_without_its_computer_is_refused_and_never_reaches_the_cloud_pool(
    computer, handler, args,
) -> None:
    rig = computer()
    # Stamped a local-folder chat, but its call came without the computer's operations.
    kwargs = {**rig.kwargs, "workspace_io": None}

    body = json.loads(await handler(args, **kwargs))

    assert body["error"] == "browser_unavailable"
    assert "user's computer" in body["reason"]
    assert kwargs["browser_pool"].asked == []


async def test_a_session_in_the_cloud_still_uses_the_cloud_browser() -> None:
    # No workspace_io: the pool answers, as before.
    pool = NoPool()
    with pytest.raises(AssertionError, match="cloud's browser pool"):
        await browser._browser_navigate_handler(
            {"url": "https://example.com"}, tenant=SimpleNamespace(org_id=UUID(int=1), user_id=UUID(int=2)),
            session_id=uuid4(), browser_pool=pool,
        )
    assert pool.asked == ["ensure"]
