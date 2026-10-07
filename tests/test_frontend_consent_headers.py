"""The SPA's Surogate Desktop consent page cannot be framed: a page framing it could hide its warning."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from surogates.api.frontend import setup_frontend


def client(tmp_path) -> TestClient:
    (tmp_path / "index.html").write_text("<!doctype html><title>Surogate</title>")
    app = FastAPI()
    assert setup_frontend(app, tmp_path)
    return TestClient(app)


# The web client's router matches paths whatever their case, so the headers must too.
@pytest.mark.parametrize("path", ["/oauth/authorize", "/OAuth/Authorize", "/oauth/authorize/"])
def test_the_consent_page_refuses_to_be_framed(tmp_path, path):
    response = client(tmp_path).get(f"{path}?client_id=surogate-desktop")
    assert response.status_code == 200
    assert response.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert response.headers["x-frame-options"] == "DENY"


def test_the_web_clients_other_pages_are_left_as_they_were(tmp_path):
    response = client(tmp_path).get("/chat")
    assert response.status_code == 200
    assert "content-security-policy" not in response.headers
    assert "x-frame-options" not in response.headers
