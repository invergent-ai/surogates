"""A context file that is a link out of the workspace never reaches the prompt.

``load_project_context`` (AGENTS.md / CLAUDE.md / ... in the session's workspace, shown in the system prompt), the
subdirectory hints (the same files in folders the agent touches, appended to tool results) and ``LocalBackend``
reads followed a link planted under the file's name: a command in the workspace could link ``AGENTS.md`` to a host
secret and the worker, which srt does not confine, put it in the prompt and sent it to the model provider.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from surogates.harness.context_files import load_project_context
from surogates.harness.prompt import PromptBuilder
from surogates.harness.subdirectory_hints import SubdirectoryHintTracker
from surogates.session.models import Session
from surogates.storage.backend import LocalBackend
from surogates.tenant.context import TenantContext

pytestmark = pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="POSIX only")


@pytest.fixture
def places(tmp_path):
    ws, out = tmp_path / "ws", tmp_path / "outside"
    ws.mkdir()
    out.mkdir()
    (out / "credentials").write_text("aws_secret_access_key = SECRET\n")
    return ws, out


def _system_prompt_context(workspace: Path) -> str:
    now = datetime.now(timezone.utc)
    session = Session(
        id=uuid4(), user_id=uuid4(), org_id=uuid4(), agent_id="agent-1", channel="web", status="active",
        config={"workspace_path": str(workspace)}, created_at=now, updated_at=now,
    )
    tenant = TenantContext(
        org_id=session.org_id, user_id=session.user_id, org_config={}, user_preferences={},
        permissions=frozenset(), asset_root="/tmp/test",
    )
    return PromptBuilder(tenant, session=session)._context_files_section()


def test_an_agents_md_linked_out_of_the_workspace_is_skipped_with_a_warning(places, caplog):
    ws, out = places
    (ws / "AGENTS.md").symlink_to(out / "credentials")
    caplog.set_level(logging.WARNING, logger="surogates.harness.context_files")
    assert "SECRET" not in _system_prompt_context(ws)
    assert load_project_context(str(ws)) is None
    assert "Context file AGENTS.md leads out of the workspace" in caplog.text
    assert str(out) not in caplog.text


def test_the_next_context_file_is_read_when_the_first_is_a_link_out(places):
    ws, out = places
    (ws / "AGENTS.md").symlink_to(out / "credentials")
    (ws / "CLAUDE.md").write_text("Use tabs.")
    context = _system_prompt_context(ws)
    assert "SECRET" not in context and "Use tabs." in context


def test_a_link_to_a_file_inside_the_workspace_is_read(places):
    ws, _ = places
    (ws / "docs").mkdir()
    (ws / "docs" / "rules.md").write_text("Use tabs.")
    (ws / "AGENTS.md").symlink_to(ws / "docs" / "rules.md")
    assert "Use tabs." in _system_prompt_context(ws)


def test_a_link_out_above_the_workspace_is_skipped_too(places):
    """The search walks up to the repository's root: a link there that leaves the workspace is skipped as well."""
    ws, out = places
    (ws.parent / ".git").mkdir()
    (ws.parent / "AGENTS.md").symlink_to(out / "credentials")
    sub = ws / "src"
    sub.mkdir()
    assert load_project_context(str(sub)) is None


def test_a_fifo_by_the_name_is_not_waited_on(places):
    from surogates.harness.context_files import read_context_file

    ws, _ = places
    os.mkfifo(ws / "AGENTS.md")
    result: dict[str, object] = {}
    reader = threading.Thread(target=lambda: result.setdefault("r", read_context_file(ws / "AGENTS.md", str(ws))),
                              daemon=True)
    reader.start()
    reader.join(5)
    assert not reader.is_alive() and result["r"] is None


def test_a_subdirectory_hint_linked_out_of_the_workspace_is_skipped(places):
    ws, out = places
    (ws / "sub").mkdir()
    (ws / "sub" / "AGENTS.md").symlink_to(out / "credentials")
    (ws / "sub" / "main.py").write_text("print(1)\n")
    tracker = SubdirectoryHintTracker(initial_cwd=str(ws))
    hint = tracker.check_tool_call("read_file", {"path": "sub/main.py"})
    assert hint is None or "SECRET" not in hint
    (ws / "other").mkdir()
    (ws / "other" / "AGENTS.md").write_text("Other rules.")
    assert "Other rules." in (tracker.check_tool_call("read_file", {"path": "other/x.py"}) or "")


def test_a_hint_folder_that_is_a_link_out_gives_no_hint(places):
    ws, out = places
    (out / "AGENTS.md").write_text("SECRET rules")
    (out / "x.py").write_text("")
    (ws / "sub").symlink_to(out, target_is_directory=True)
    tracker = SubdirectoryHintTracker(initial_cwd=str(ws))
    assert "SECRET" not in (tracker.check_tool_call("read_file", {"path": "sub/x.py"}) or "")
    # Nor an absolute path out of the workspace.
    assert "SECRET" not in (tracker.check_tool_call("read_file", {"path": str(out / "x.py")}) or "")


def test_a_hint_folder_swapped_after_the_tracker_saw_it_is_skipped(places):
    from surogates.harness.context_files import read_context_file

    ws, out = places
    (out / "AGENTS.md").write_text("SECRET rules")
    (ws / "h").mkdir()
    seen = ws / "h" / "AGENTS.md"  # the path as the tracker resolved it, while h was a folder
    (ws / "h").rmdir()
    (ws / "h").symlink_to(out, target_is_directory=True)
    assert read_context_file(seen, str(ws)) is None


def test_without_a_workspace_a_linked_hint_file_is_skipped(places):
    ws, out = places
    (ws / "sub").mkdir()
    (ws / "sub" / "AGENTS.md").symlink_to(out / "credentials")
    tracker = SubdirectoryHintTracker()
    assert "SECRET" not in (tracker.check_tool_call("read_file", {"path": str(ws / "sub" / "main.py")}) or "")


def test_a_context_file_with_other_names_is_skipped(places):
    ws, out = places
    os.link(out / "credentials", ws / "AGENTS.md")  # a hard link: the same file, outside too
    assert load_project_context(str(ws)) is None


def test_a_context_file_that_is_not_utf8_gives_way_to_the_next(places):
    ws, _ = places
    (ws / "AGENTS.md").write_bytes(b"\xff\xfe not text")
    (ws / "CLAUDE.md").write_text("Use tabs.")
    assert "Use tabs." in load_project_context(str(ws))


def test_a_huge_context_file_is_read_only_up_to_the_cap(places):
    from surogates.harness.context_files import MAX_CONTEXT_FILE_BYTES, read_context_file

    ws, _ = places
    with open(ws / "AGENTS.md", "wb") as fh:
        fh.write(b"rules ")
        fh.truncate(50 * MAX_CONTEXT_FILE_BYTES)  # sparse: what a command can make in no time
    text = read_context_file(ws / "AGENTS.md", str(ws))
    assert text is not None and text.startswith("rules ") and len(text) <= MAX_CONTEXT_FILE_BYTES


async def test_a_storage_read_through_a_folder_swapped_after_its_check_is_refused(places, monkeypatch):
    ws, out = places
    base = ws.parent / "storage"
    (base / "b" / "ws" / "a").mkdir(parents=True)
    (base / "b" / "ws" / "a" / "credentials").write_text("mine\n")
    backend = LocalBackend(str(base))
    real_resolve = backend._resolve

    def resolve_then_swap(bucket, key):
        path = real_resolve(bucket, key)
        folder = base / "b" / "ws" / "a"
        folder.rename(folder.with_name("a.old"))
        folder.symlink_to(out, target_is_directory=True)  # a command swaps it after the check
        return path

    assert await backend.read("b", "ws/a/credentials") == b"mine\n"
    monkeypatch.setattr(backend, "_resolve", resolve_then_swap)
    # A bucket has no links: the key through one names no object.
    with pytest.raises(KeyError):
        await backend.read("b", "ws/a/credentials")


async def test_storage_reads_still_answer_as_before(tmp_path):
    backend = LocalBackend(str(tmp_path))
    await backend.write("b", "x/y.txt", b"hello")
    assert await backend.read("b", "x/y.txt") == b"hello"
    assert await backend.read_text("b", "x/y.txt") == "hello"
    await backend.write("b", "x/crlf.txt", b"a\r\nb\rc\n")
    assert await backend.read_text("b", "x/crlf.txt") == "a\nb\nc\n"  # universal newlines, as before
    for missing in ("x/none.txt", "x", "x/y.txt/z"):
        with pytest.raises(KeyError):
            await backend.read("b", missing)
    with pytest.raises(ValueError):
        await backend.read("b", "../escape")
