"""MCP OAuth token storage hardening tests."""

from __future__ import annotations

import json
import os
import time

import pytest

from surogates.tools.mcp import oauth


class FakeOAuthToken:
    def __init__(self, data: dict) -> None:
        self.data = data

    @classmethod
    def model_validate(cls, data: dict):
        return cls(dict(data))

    def model_dump(self, exclude_none: bool = True) -> dict:
        return dict(self.data)


@pytest.fixture(autouse=True)
def fake_oauth_token(monkeypatch) -> None:
    monkeypatch.setattr(oauth, "OAuthToken", FakeOAuthToken, raising=False)


@pytest.mark.asyncio
async def test_token_storage_reloads_when_file_mtime_changes(tmp_path) -> None:
    storage = oauth.TokenStorage("server", token_dir=str(tmp_path))

    await storage.set_tokens(FakeOAuthToken({"access_token": "old"}))
    assert (await storage.get_tokens()).data["access_token"] == "old"

    path = tmp_path / "server.json"
    path.write_text(json.dumps({"access_token": "new"}), encoding="utf-8")
    future = time.time() + 1
    os.utime(path, (future, future))

    assert (await storage.get_tokens()).data["access_token"] == "new"


@pytest.mark.asyncio
async def test_token_storage_persists_absolute_expiry(tmp_path) -> None:
    storage = oauth.TokenStorage("server", token_dir=str(tmp_path))

    await storage.set_tokens(
        FakeOAuthToken({"access_token": "tok", "expires_in": 3600})
    )

    raw = json.loads((tmp_path / "server.json").read_text(encoding="utf-8"))
    assert raw["expires_at"] > 0
    assert raw["expires_in"] == 3600


@pytest.mark.asyncio
async def test_token_storage_remove_clears_memory_cache(tmp_path) -> None:
    storage = oauth.TokenStorage("server", token_dir=str(tmp_path))
    await storage.set_tokens(FakeOAuthToken({"access_token": "tok"}))

    storage.remove()

    assert await storage.get_tokens() is None
