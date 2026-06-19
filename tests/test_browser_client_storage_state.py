import json

from surogates.browser.client import KernelBrowserClient


async def test_storage_state_returns_execute_result(monkeypatch):
    client = KernelBrowserClient("http://browser:10001")
    captured = {}

    async def fake_exec(code, *, timeout_sec=60):
        captured["code"] = code
        return {"cookies": [{"name": "SID"}], "origins": []}

    monkeypatch.setattr(client, "_playwright_execute", fake_exec)
    state = await client.storage_state()
    assert state["cookies"][0]["name"] == "SID"
    assert "storageState()" in captured["code"]
    await client.close()


async def test_apply_storage_state_adds_cookies(monkeypatch):
    client = KernelBrowserClient("http://browser:10001")
    captured = {}

    async def fake_exec(code, *, timeout_sec=60):
        captured["code"] = code
        return None

    monkeypatch.setattr(client, "_playwright_execute", fake_exec)
    state = {
        "cookies": [{"name": "SID", "domain": ".google.com", "value": "x"}],
        "origins": [
            {
                "origin": "https://google.com",
                "localStorage": [{"name": "k", "value": "v"}],
            }
        ],
    }
    await client.apply_storage_state(state)
    assert "addCookies" in captured["code"]
    assert json.dumps(state["cookies"]) in captured["code"]
    # The kernel-images execute wrapper already binds ``context``; redeclaring
    # it is a SyntaxError that aborts the injection (profile cookies never land
    # and browser provisioning fails). Guard against reintroducing the shadow.
    assert "const context" not in captured["code"]
    await client.close()
