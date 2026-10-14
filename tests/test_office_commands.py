"""Commands to the document server: the Command Service's answer is checked, not only its HTTP status."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from jose import jwt

from surogates.config import OfficeSettings
from surogates.office import commands

OFFICE = OfficeSettings(
    public_url="https://office.example", internal_url="http://docs.internal",
    callback_base_url="https://api.example", jwt_secret="j" * 32, token_secret="t" * 32,
)


@pytest.fixture
def answer(monkeypatch):
    """Make the Command Service answer with the given JSON body."""
    real_client = httpx.AsyncClient

    def install(body: dict) -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
        monkeypatch.setattr(commands.httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))

    return install


async def test_a_refused_drop_is_logged(answer, caplog):
    answer({"error": 1, "key": "k"})
    with caplog.at_level(logging.WARNING, logger=commands.__name__):
        await commands.drop_editors(OFFICE, "k", ["user:1"])
    assert "error 1" in caplog.text


async def test_an_accepted_drop_logs_nothing(answer, caplog):
    answer({"error": 0, "key": "k"})
    with caplog.at_level(logging.WARNING, logger=commands.__name__):
        await commands.drop_editors(OFFICE, "k", ["user:1"])
    assert caplog.text == ""


async def test_force_save_returns_the_command_services_code(answer):
    answer({"error": 0, "key": "k"})
    assert await commands.force_save(OFFICE, "k", '{"turn": "m"}') == 0
    answer({"error": 4, "key": "k"})
    assert await commands.force_save(OFFICE, "k", '{"turn": "m"}') == 4


async def test_force_save_sends_the_userdata_signed(monkeypatch):
    seen: list[dict] = []
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"error": 0})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(commands.httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))
    await commands.force_save(OFFICE, "k", '{"turn": "m"}')
    assert {key: seen[0][key] for key in ("c", "key", "userdata")} == {"c": "forcesave", "key": "k", "userdata": '{"turn": "m"}'}
    assert jwt.decode(seen[0]["token"], OFFICE.jwt_secret, algorithms=["HS256"])["userdata"] == '{"turn": "m"}'


async def test_an_unreachable_or_odd_command_service_is_none(monkeypatch, answer):
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(500))
    monkeypatch.setattr(commands.httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))
    assert await commands.force_save(OFFICE, "k", "u") is None
    answer([])  # not an object
    assert await commands.force_save(OFFICE, "k", "u") is None
    await commands.drop_editors(OFFICE, "k", ["user:1"])  # logs, never raises
