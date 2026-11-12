"""A browser that is not on the user's computer is given files as web links it downloads itself."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from surogates.browser.client import KernelBrowserClient, _upload_name
from surogates.tools.builtin.browser import _browser_upload_file_handler
from tests.test_browser_tools import FakeControlStore, FakePool

TENANT = SimpleNamespace(org_id=UUID(int=1), user_id=UUID(int=2))


class UploadClient:
    def __init__(self, fail: Exception | None = None) -> None:
        self.given: list[list[str]] = []
        self.fail = fail

    async def __aenter__(self) -> "UploadClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def set_input_files_from_urls(self, urls: list[str]) -> dict[str, Any]:
        if self.fail:
            raise self.fail
        self.given.append(urls)
        return {"files": len(urls), "to": "the page's file input"}


async def upload(arguments: dict[str, Any], client: UploadClient) -> dict[str, Any]:
    return json.loads(await _browser_upload_file_handler(
        arguments, tenant=TENANT, session_id=uuid4(), browser_pool=FakePool(),
        browser_control=FakeControlStore(), _client_factory=lambda endpoint, cache=None: client,
    ))


async def test_links_go_to_the_cloud_browser() -> None:
    client = UploadClient()
    body = await upload({"urls": ["https://superstar.example/api/files/abc?exp=1&sig=x&name=launch.mp4"]}, client)
    assert body == {"uploaded": 1, "to": "the page's file input"}
    assert client.given == [["https://superstar.example/api/files/abc?exp=1&sig=x&name=launch.mp4"]]


async def test_paths_in_the_cloud_are_answered_with_how_to_give_files() -> None:
    client = UploadClient()
    body = await upload({"paths": ["out/card.png"]}, client)
    assert body["error"] == "unsupported" and "'urls'" in body["detail"]
    assert client.given == []


@pytest.mark.parametrize("urls", [["file:///etc/passwd"], ["ftp://x/y"], ["not a link"], [], [1], "https://x/y"])
async def test_only_web_links_are_downloaded(urls: Any) -> None:
    client = UploadClient()
    body = await upload({"urls": urls}, client)
    assert body["error"] == "upload_failed"
    assert client.given == []


async def test_a_download_that_fails_says_why() -> None:
    body = await upload({"urls": ["https://x/a.png"]}, UploadClient(RuntimeError("Could not download https://x/a.png: 404")))
    assert body == {"error": "upload_failed", "detail": "Could not download https://x/a.png: 404"}


@pytest.mark.parametrize(("url", "name"), [
    ("https://h/api/files/0b1c?exp=1&sig=s&name=launch%20film.mp4", "launch_film.mp4"),
    ("https://h/img/card.png", "card.png"),
    ("https://h/", "file-1"),
    ("https://h/.hidden", "hidden"),
])
def test_a_downloaded_file_keeps_a_safe_name_with_its_extension(url: str, name: str) -> None:
    assert _upload_name(url, 0) == name


def test_a_name_never_leaves_the_uploads_folder() -> None:
    name = _upload_name("https://h/x?name=../../etc/passwd", 0)
    assert "/" not in name and not name.startswith(".")


def rig(exit_code: int = 0, result: Any = None) -> tuple[KernelBrowserClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/process/exec":
            return httpx.Response(200, json={"exit_code": exit_code, "stderr_b64": "Y3VybDogKDIyKSA0MDQ="})
        return httpx.Response(200, json={"success": True, "result": result})

    client = KernelBrowserClient("http://browser")
    client._http = httpx.AsyncClient(base_url="http://browser", transport=httpx.MockTransport(answer))
    return client, seen


async def test_the_browser_downloads_each_link_then_the_page_gets_them() -> None:
    client, seen = rig(result={"files": 2, "to": "the file picker the page opened"})
    async with client:
        got = await client.set_input_files_from_urls(["https://h/a.png?name=a.png", "https://h/b.mp4"])
    assert got == {"files": 2, "to": "the file picker the page opened"}
    downloads = [json.loads(r.content) for r in seen if r.url.path == "/process/exec"]
    assert [d["command"] for d in downloads] == ["sh", "sh"]
    for d, url, name in zip(downloads, ["https://h/a.png?name=a.png", "https://h/b.mp4"], ["a.png", "b.mp4"]):
        _c, script, _argv0, folder, dest, link = d["args"][:6]
        # The link is an argument, never part of the shell text.
        assert url not in script and link == url and "--proto =http,https" in script and "chmod 0644" in script
        assert folder.startswith("/tmp/surogates-uploads/") and dest == f"{folder}/{name}"
    [script] = [json.loads(r.content)["code"] for r in seen if r.url.path == "/playwright/execute"]
    assert "window.__sgFileInput" in script and "/tmp/surogates-uploads/" in script and "DOM.setFileInputFiles" in script
    # Old uploads are cleared from the browser's disk on the way in.
    assert all(d["args"][-1] == "/tmp/surogates-uploads" and "-mmin +1440" in d["args"][1] for d in downloads)


async def test_a_link_that_cannot_be_downloaded_stops_before_the_page() -> None:
    client, seen = rig(exit_code=22)
    async with client:
        with pytest.raises(RuntimeError, match="curl: \\(22\\) 404"):
            await client.set_input_files_from_urls(["https://h/gone.png"])
    assert all(r.url.path == "/process/exec" for r in seen)


async def test_a_page_with_no_file_input_says_to_open_its_upload_control() -> None:
    client, _seen = rig(result={"error": "no_file_input"})
    async with client:
        with pytest.raises(RuntimeError, match="upload control"):
            await client.set_input_files_from_urls(["https://h/a.png"])


async def test_every_navigation_holds_the_pages_file_picker_in_the_page_itself() -> None:
    client, seen = rig(result={"url": "https://x.com/compose/post", "title": "X"})
    async with client:
        await client.navigate("https://x.com/compose/post")
    code = json.loads(seen[0].content)["code"]
    # Kept by the page's JavaScript, since Playwright is attached for one call only; armed after the load.
    assert "page.on('filechooser'" not in code and code.index("page.goto") < code.index("__sgPickArmed")


def test_every_click_arms_the_file_picker_first() -> None:
    from surogates.browser.client import _bounded_click

    code = _bounded_click("await page.mouse.click(1, 2);")
    assert code.index("__sgPickArmed") < code.index("page.mouse.click")
