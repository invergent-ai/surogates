"""browser_type types only into a text field: keys anywhere else are the site's shortcuts."""

import json
from types import SimpleNamespace
from typing import Any, Self
from uuid import UUID, uuid4

import httpx
import pytest

from surogates.browser.client import KernelBrowserClient, NotATextField
from tests.test_browser_tools import FakeControlStore, FakePool


def _client(answer: dict[str, Any], sent: list[str]) -> KernelBrowserClient:
    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["code"])
        return httpx.Response(200, json=answer)

    client = KernelBrowserClient("http://browser")
    client._http = httpx.AsyncClient(
        base_url="http://browser", transport=httpx.MockTransport(respond)
    )
    return client


async def test_the_focus_is_checked_before_the_first_key_in_the_same_call() -> None:
    sent: list[str] = []
    await _client({"success": True, "result": True}, sent).type_text("hello?")
    assert len(sent) == 1
    assert sent[0].index("not_a_text_field") < sent[0].index("page.keyboard.type")


async def test_focus_outside_a_text_field_types_nothing_and_says_where_it_is() -> None:
    sent: list[str] = []
    client = _client(
        {"success": False, "error": 'Error: not_a_text_field: button "Reply"'}, sent
    )
    with pytest.raises(NotATextField, match='button "Reply"'):
        await client.type_text("If quota runs out?")


async def test_any_other_failure_is_not_taken_for_a_focus_miss() -> None:
    client = _client({"success": False, "error": "Target closed"}, [])
    with pytest.raises(RuntimeError) as caught:
        await client.type_text("x")
    assert not isinstance(caught.value, NotATextField)


class _MissingFocus:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def type_into_ref(self, ref: str, text: str, **kwargs: Any) -> None:
        raise NotATextField("the page itself")

    async def type_text(self, text: str, **kwargs: Any) -> None:
        raise NotATextField("the page itself")


@pytest.fixture()
def tenant() -> SimpleNamespace:
    return SimpleNamespace(org_id=UUID(int=1), user_id=UUID(int=2))


@pytest.mark.parametrize("args", [{"ref": "@e209", "text": "why?"}, {"text": "why?"}])
async def test_the_tool_refuses_instead_of_saying_typed(
    args: dict[str, Any], tenant
) -> None:
    import surogates.tools.builtin.browser as browser_tools

    result = json.loads(
        await browser_tools._browser_type_handler(
            args,
            tenant=tenant,
            session_id=uuid4(),
            browser_pool=FakePool(),
            browser_control=FakeControlStore(),
            _client_factory=lambda endpoint: _MissingFocus(),
        )
    )
    assert result["error"] == "not_a_text_field"
    assert "nothing was typed" in result["detail"]
    assert "typed" not in result
