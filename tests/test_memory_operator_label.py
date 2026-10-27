"""Every Studio operator of an agent shares its memory, so an entry names who it came from."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

import surogates.tools.builtin.memory as memory_tool

OPERATOR = {"name": "Pieter Bakker", "email": "pieter@example.com"}


class _Manager:
    def __init__(self):
        self.calls = []

    def handle_tool_call(self, name, arguments):
        self.calls.append(arguments)
        return json.dumps({"success": True})


async def _write(monkeypatch, *, trusted=True, config=None, action="add", content="Prefers short replies"):
    async def proof(session_store, sa_id):
        return trusted

    monkeypatch.setattr(memory_tool, "is_ops_chat_service_account", proof)
    manager = _Manager()
    await memory_tool._memory_handler(
        {"action": action, "target": "memory", "content": content, "old_text": "x"},
        memory_manager=manager,
        session_store=object(),
        tenant=SimpleNamespace(service_account_id=uuid4()),
        session_config={"operator": OPERATOR} if config is None else config,
    )
    return manager.calls[0]["content"]


@pytest.mark.parametrize("action", ["add", "replace"])
async def test_an_entry_from_a_studio_chat_names_its_operator(monkeypatch, action):
    assert await _write(monkeypatch, action=action) == "Prefers short replies\n(noted by Pieter Bakker)"


async def test_the_first_line_stays_the_entrys_title(monkeypatch):
    written = await _write(monkeypatch, content="Acme Corp\nPays on the 15th")
    assert written.splitlines()[0] == "Acme Corp"


async def test_an_entry_is_named_once(monkeypatch):
    labelled = "Prefers short replies\n(noted by Pieter Bakker)"
    assert await _write(monkeypatch, content=labelled) == labelled


async def test_an_untrusted_stamp_names_nobody(monkeypatch):
    assert await _write(monkeypatch, trusted=False) == "Prefers short replies"


async def test_a_session_without_an_operator_names_nobody(monkeypatch):
    assert await _write(monkeypatch, config={}) == "Prefers short replies"
