"""Channel-deliverable code-run progress text and summary-model activity line."""

from types import SimpleNamespace

import pytest

from surogates.coding_agents.run_progress import (
    render_code_run_ack,
    render_code_run_done,
    render_code_run_update,
    summarize_progress_activity,
)

REPO = {"url": "https://github.com/acme/api", "default_branch": "main"}


def test_ack_names_agent_and_repo():
    s = render_code_run_ack("claude", REPO)
    assert "claude" in s
    assert "acme/api" in s


def test_ack_without_repo_is_still_valid():
    s = render_code_run_ack("codex", None)
    assert "codex" in s
    assert "on `" not in s  # no dangling repo mention


def test_update_includes_repo_and_activity_hint():
    s = render_code_run_update(REPO, "Editing search.py to add fuzzy match\n› Bash")
    assert "acme/api" in s
    assert "Editing search.py" in s  # the prose line, not the › tool marker


def test_update_uses_most_recent_prose_line():
    # Later prose supersedes earlier prose — the hint tracks current activity.
    s = render_code_run_update(REPO, "Reading the module\n› Edit\nNow wiring the CLI")
    assert "Now wiring the CLI" in s
    assert "Reading the module" not in s


def test_update_falls_back_to_last_tool_name():
    # No prose yet → show the last tool (marker stripped) rather than nothing.
    s = render_code_run_update(REPO, "› Bash\n› Edit")
    assert "acme/api" in s
    assert "Edit" in s
    assert "›" not in s


def test_update_truncates_long_activity():
    s = render_code_run_update(REPO, "x" * 500)
    assert len(s) < 400


def test_update_empty_activity_ok():
    s = render_code_run_update(REPO, "")
    assert "acme/api" in s


def test_done_ok_and_error_states():
    ok = render_code_run_done("claude", REPO, ok=True)
    assert "claude" in ok and "acme/api" in ok
    assert "working" not in ok  # no longer "still working"
    err = render_code_run_done("claude", REPO, ok=False)
    assert "acme/api" in err
    assert ok != err


# --- summary-model activity line -------------------------------------------


class _FakeSummaryClient:
    """Minimal ``client.chat.completions.create`` shim capturing its kwargs."""

    def __init__(self, content, *, raises=None):
        self._content = content
        self._raises = raises
        self.calls = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create),
        )

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self._content))],
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_summarize_returns_model_line_and_sends_transcript():
    client = _FakeSummaryClient("Adding fuzzy matching to the search tool")
    out = await summarize_progress_activity(
        client, "gpt-x", "Editing search.py\n› Edit\nWiring the CLI",
    )
    assert out == "Adding fuzzy matching to the search tool"
    # The transcript reaches the model as the user message.
    user = client.calls[0]["messages"][-1]
    assert user["role"] == "user"
    assert "Editing search.py" in user["content"]
    assert client.calls[0]["model"] == "gpt-x"


@pytest.mark.asyncio(loop_scope="session")
async def test_summarize_no_client_returns_empty():
    assert await summarize_progress_activity(None, "gpt-x", "did stuff") == ""


@pytest.mark.asyncio(loop_scope="session")
async def test_summarize_no_model_returns_empty():
    client = _FakeSummaryClient("x")
    assert await summarize_progress_activity(client, "", "did stuff") == ""
    assert client.calls == []  # never called without a model


@pytest.mark.asyncio(loop_scope="session")
async def test_summarize_empty_transcript_returns_empty():
    client = _FakeSummaryClient("x")
    assert await summarize_progress_activity(client, "gpt-x", "   ") == ""
    assert client.calls == []


@pytest.mark.asyncio(loop_scope="session")
async def test_summarize_swallows_errors():
    client = _FakeSummaryClient(None, raises=RuntimeError("boom"))
    assert await summarize_progress_activity(client, "gpt-x", "did stuff") == ""
