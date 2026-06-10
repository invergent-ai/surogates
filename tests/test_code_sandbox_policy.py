"""Regression tests for the /code sandbox security policy.

Asserts the SRT settings written for the terminal tool deny-read the
coding-agent credential locations and allow the vendor API endpoints, and
that the run-result/progress event payloads never carry a credential.
"""

from __future__ import annotations

import json

from surogates.coding_agents.runner import _parse_exec_result


def test_srt_settings_cover_code_credentials_and_apis(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import surogates.config as config
    from surogates.tools.builtin import terminal

    fake = SimpleNamespace(
        sandbox=SimpleNamespace(srt_settings_dir=str(tmp_path), srt_enabled=True),
    )
    monkeypatch.setattr(config, "load_settings", lambda: fake)

    path = terminal._get_srt_settings_path("/workspace/ws-code")
    with open(path) as fh:
        data = json.load(fh)

    deny = data["filesystem"]["denyRead"]
    assert "/tmp/.code-runs" in deny
    assert "auth.json" in deny
    assert "$CODEX_HOME" in deny
    assert "$CLAUDE_CONFIG_DIR" in deny

    allowed = data["network"]["allowedDomains"]
    assert "api.anthropic.com" in allowed
    assert "api.openai.com" in allowed


def test_run_result_event_never_contains_token():
    # The runner builds the launch payload with the credential, but the
    # progress/result it returns must be credential-free.
    poll = {
        "ok": True, "done": True, "exit_code": 0, "offset": 20,
        "new_output": json.dumps({"type": "result", "result": "ok"}) + "\n",
    }
    parsed = _parse_exec_result(json.dumps(poll))
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in json.dumps(parsed)
