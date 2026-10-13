"""drop_editors: the Command Service's answer is checked, not only its HTTP status."""

from __future__ import annotations

import logging

import httpx
import pytest

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
