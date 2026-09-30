"""Workspace tools through their handlers, on each kind of workspace access.

Each class runs against a plain workspace path ("local").  Once a class's
handlers take a WorkspaceIO it also runs "remapped": the same directory
served under a root that does not exist on this host, so a handler that
touches the filesystem without its WorkspaceIO finds nothing and fails.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from surogates.tools.builtin import file_ops, research, terminal
from surogates.tools.utils import document_cache
from surogates.tools.utils import process_registry as registry_module
from surogates.tools.workspace_io import LocalWorkspaceIO, RunResult
from tests.tools.fixtures.build_documents import build_minimal_docx

LOCAL = ["local"]
BOTH = ["local", "remapped"]
VIRTUAL_ROOT = "/nonexistent-virtual-workspace"


class RemappingWorkspaceIO:
    """A real directory served under VIRTUAL_ROOT, which does not exist here.

    Keys are virtual.  Each call translates them to the real directory and
    delegates to LocalWorkspaceIO, so a handler that opens, stats or lists a
    key itself finds nothing and its remapped test fails.
    """

    def __init__(self, real_root: str) -> None:
        self._inner = LocalWorkspaceIO(workspace_path=real_root)
        self._real = real_root
        self.root = VIRTUAL_ROOT

    def _to_real(self, text: str) -> str:
        return text.replace(VIRTUAL_ROOT, self._real)

    def _to_virtual(self, text: str) -> str:
        return text.replace(self._real, VIRTUAL_ROOT)

    async def resolve(self, path):
        return self._to_virtual(await self._inner.resolve(self._to_real(path)))

    async def check_write(self, path):
        refusal = await self._inner.check_write(self._to_real(path))
        return refusal and self._to_virtual(refusal)

    async def stat(self, key):
        return await self._inner.stat(self._to_real(key))

    async def read(self, key, max_bytes=None):
        return await self._inner.read(self._to_real(key), max_bytes)

    async def write(self, key, data):
        await self._inner.write(self._to_real(key), data)

    async def delete(self, key):
        await self._inner.delete(self._to_real(key))

    async def list_dir(self, key):
        return await self._inner.list_dir(self._to_real(key))

    def local_file(self, key):
        return self._inner.local_file(self._to_real(key))

    async def ripgrep(self, key, **options):
        return self._to_virtual(await self._inner.ripgrep(self._to_real(key), **options))

    async def which(self, name):
        return await self._inner.which(name)

    async def run(self, command, *, workdir, timeout):
        result = await self._inner.run(
            self._to_real(command), workdir=workdir and self._to_real(workdir), timeout=timeout,
        )
        return RunResult(self._to_virtual(result.output), result.returncode, result.timed_out)

    async def start(self, command, *, workdir, **options):
        return await self._inner.start(
            self._to_real(command), workdir=workdir and self._to_real(workdir), **options,
        )

    def __getattr__(self, name):
        # poll, read_output, wait, kill, write_stdin, list_processes: no keys.
        return getattr(self._inner, name)

needs_rg = pytest.mark.skipif(shutil.which("rg") is None, reason="needs ripgrep")
needs_python = pytest.mark.skipif(
    shutil.which("python") is None, reason="lint needs python on PATH",
)


@dataclass
class Ws:
    root: str
    real: Path
    kwargs: dict[str, Any] = field(default_factory=dict)

    def path(self, rel: str) -> str:
        return f"{self.root}/{rel}"


@pytest.fixture
def ws(request, tmp_path) -> Ws:
    real = (tmp_path / "ws").resolve()
    real.mkdir()
    if request.param == "local":
        return Ws(str(real), real, {"workspace_path": str(real)})
    if request.param == "remapped":
        return Ws(VIRTUAL_ROOT, real, {"workspace_io": RemappingWorkspaceIO(str(real))})
    raise ValueError(f"unknown workspace kind {request.param}")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    file_ops._read_tracker.clear()
    monkeypatch.setattr(registry_module, "CHECKPOINT_PATH", tmp_path / "processes.json")
    monkeypatch.setattr(
        document_cache, "_DEFAULT", document_cache.DocumentCache(root=tmp_path / "doc-cache"),
    )
    yield
    registry_module.process_registry.kill_all()
    file_ops._read_tracker.clear()


async def raw_call(handler, ws: Ws, **arguments: Any) -> str:
    result = handler(arguments, **ws.kwargs)
    if asyncio.iscoroutine(result):
        result = await result
    return result


async def call(handler, ws: Ws, **arguments: Any) -> dict[str, Any]:
    raw = await raw_call(handler, ws, **arguments)
    return json.loads(raw.split("\n\n[Hint:")[0])


@pytest.mark.parametrize("ws", LOCAL, indirect=True)
class TestReadFile:
    async def test_reads_lines(self, ws):
        (ws.real / "a.txt").write_text("one\ntwo\n")
        out = await call(file_ops._read_file_handler, ws, path=ws.path("a.txt"))
        assert out["content"] == "one\ntwo\n"
        assert out["total_lines"] == 2
        assert out["file_size"] == 8

    async def test_crlf_is_read_as_newlines(self, ws):
        (ws.real / "a.txt").write_bytes(b"a\r\nb\r\n")
        out = await call(file_ops._read_file_handler, ws, path=ws.path("a.txt"))
        assert out["content"] == "a\nb\n"

    async def test_utf16_bom_is_decoded(self, ws):
        (ws.real / "u.txt").write_bytes("héllo\n".encode("utf-16"))
        out = await call(file_ops._read_file_handler, ws, path=ws.path("u.txt"))
        assert out["content"].lstrip("\ufeff") == "héllo\n"

    async def test_second_identical_read_is_a_dedup_stub(self, ws):
        (ws.real / "a.txt").write_text("one\n")
        await call(file_ops._read_file_handler, ws, path=ws.path("a.txt"))
        out = await call(file_ops._read_file_handler, ws, path=ws.path("a.txt"))
        assert out["dedup"] is True

    async def test_missing_file_suggests_neighbours(self, ws):
        (ws.real / "notes.txt").write_text("x")
        out = await call(file_ops._read_file_handler, ws, path=ws.path("notes.md"))
        assert out["error"] == f"File not found: {ws.path('notes.md')}"
        assert out["similar_files"] == [ws.path("notes.txt")]

    async def test_nul_bytes_mean_binary(self, ws):
        (ws.real / "blob.log").write_bytes(b"\x00\x01rest")
        out = await call(file_ops._read_file_handler, ws, path=ws.path("blob.log"))
        assert out["error"].startswith("Cannot read binary file")

    async def test_reads_a_docx(self, ws):
        build_minimal_docx(ws.real / "d.docx")
        out = await call(file_ops._read_file_handler, ws, path=ws.path("d.docx"))
        assert "Hello DOCX" in out["content"]

    async def test_path_outside_workspace_is_refused(self, ws):
        out = await call(file_ops._read_file_handler, ws, path="/etc/hostname")
        assert "Path traversal blocked" in out["error"]


@pytest.mark.parametrize("ws", LOCAL, indirect=True)
class TestWriteFile:
    async def test_creates_file_and_parents(self, ws):
        out = await call(
            file_ops._write_file_handler, ws, path=ws.path("sub/new.txt"), content="hi\n",
        )
        assert (out["status"], out["bytes_written"], out["lines_written"]) == ("ok", 3, 1)
        assert (ws.real / "sub" / "new.txt").read_text() == "hi\n"

    async def test_refuses_unread_overwrite(self, ws):
        (ws.real / "a.txt").write_text("orig")
        out = await call(file_ops._write_file_handler, ws, path=ws.path("a.txt"), content="new")
        assert out["error"].startswith("Refusing to overwrite")
        assert (ws.real / "a.txt").read_text() == "orig"

    async def test_overwrites_after_read(self, ws):
        (ws.real / "a.txt").write_text("orig\n")
        await call(file_ops._read_file_handler, ws, path=ws.path("a.txt"))
        out = await call(file_ops._write_file_handler, ws, path=ws.path("a.txt"), content="new\n")
        assert out["status"] == "ok"
        assert "_warning" not in out
        assert (ws.real / "a.txt").read_text() == "new\n"

    async def test_warns_when_file_changed_since_read(self, ws):
        target = ws.real / "a.txt"
        target.write_text("orig\n")
        await call(file_ops._read_file_handler, ws, path=ws.path("a.txt"))
        target.write_text("external\n")
        os.utime(target, (1, 1))
        out = await call(file_ops._write_file_handler, ws, path=ws.path("a.txt"), content="mine\n")
        assert "was modified since you last read it" in out["_warning"]

    async def test_protected_path_is_denied(self, ws):
        out = await call(file_ops._write_file_handler, ws, path="/etc/passwd", content="x")
        assert out["error"] == "Write denied: '/etc/passwd' is a protected system/credential file."

    @needs_python
    async def test_lints_python(self, ws):
        good = await call(file_ops._write_file_handler, ws, path=ws.path("ok.py"), content="x = 1\n")
        assert good["lint"] == {"status": "ok"}
        bad = await call(file_ops._write_file_handler, ws, path=ws.path("bad.py"), content="def (:\n")
        assert bad["lint"]["status"] == "error"
        assert "SyntaxError" in bad["lint"]["output"]


@pytest.mark.parametrize("ws", LOCAL, indirect=True)
class TestPatch:
    async def test_replace_returns_diff(self, ws):
        (ws.real / "a.txt").write_text("x = 1\ny = 2\n")
        out = await call(
            file_ops._patch_handler, ws,
            mode="replace", path=ws.path("a.txt"), old_string="y = 2", new_string="y = 3",
        )
        assert out["status"] == "ok"
        assert "-y = 2\n+y = 3\n" in out["diff"]
        assert (ws.real / "a.txt").read_text() == "x = 1\ny = 3\n"

    async def test_replace_keeps_crlf(self, ws):
        (ws.real / "a.txt").write_bytes(b"one\r\ntwo\r\n")
        await call(
            file_ops._patch_handler, ws,
            mode="replace", path=ws.path("a.txt"), old_string="two", new_string="TWO",
        )
        assert (ws.real / "a.txt").read_bytes() == b"one\r\nTWO\r\n"

    async def test_missing_text_hints_to_reread(self, ws):
        (ws.real / "a.txt").write_text("one\n")
        raw = await raw_call(
            file_ops._patch_handler, ws,
            mode="replace", path=ws.path("a.txt"), old_string="zzz", new_string="y",
        )
        assert "Could not find" in raw
        assert "[Hint: old_string not found" in raw

    async def test_v4a_updates_adds_and_deletes(self, ws):
        (ws.real / "u.txt").write_text("alpha\nbeta\n")
        (ws.real / "gone.txt").write_text("bye\n")
        patch = (
            "*** Begin Patch\n"
            f"*** Update File: {ws.path('u.txt')}\n"
            "@@\n"
            "-beta\n"
            "+BETA\n"
            f"*** Add File: {ws.path('new/n.txt')}\n"
            "+fresh\n"
            f"*** Delete File: {ws.path('gone.txt')}\n"
            # A Delete section with no line after it is dropped (a known bug,
            # out of scope here); the blank line keeps this one.
            "\n"
            "*** End Patch"
        )
        out = await call(file_ops._patch_handler, ws, mode="patch", patch=patch)
        assert out["status"] == "ok"
        assert [f["operation"] for f in out["files"]] == ["updated", "created", "deleted"]
        assert (ws.real / "u.txt").read_text() == "alpha\nBETA\n"
        assert (ws.real / "new" / "n.txt").read_text() == "fresh"
        assert not (ws.real / "gone.txt").exists()


@needs_rg
@pytest.mark.parametrize("ws", LOCAL, indirect=True)
class TestSearch:
    async def test_content_matches_with_line_numbers(self, ws):
        (ws.real / "a.py").write_text("x = 1\nneedle = 2\n")
        out = await call(file_ops._search_files_handler, ws, pattern="needle")
        assert out["matches"] == [{"file": ws.path("a.py"), "line": 2, "content": "needle = 2"}]

    async def test_count_and_files_only(self, ws):
        (ws.real / "a.txt").write_text("n\nn\n")
        (ws.real / "b.txt").write_text("n\n")
        counted = await call(
            file_ops._search_files_handler, ws, pattern="n", path=ws.root, output_mode="count",
        )
        assert counted["matches"] == [
            {"file": ws.path("a.txt"), "count": 2},
            {"file": ws.path("b.txt"), "count": 1},
        ]
        assert counted["total_matches"] == 3
        listed = await call(
            file_ops._search_files_handler, ws, pattern="n", path=ws.root, output_mode="files_only",
        )
        assert listed["matches"] == [{"file": ws.path("a.txt")}, {"file": ws.path("b.txt")}]

    async def test_list_files_sorts_newest_first(self, ws):
        old = ws.real / "old.py"
        new = ws.real / "new.py"
        old.write_text("")
        new.write_text("")
        os.utime(old, (1, 1))
        out = await call(file_ops._list_files_handler, ws, path=ws.root, pattern="*.py")
        assert out["matches"] == [ws.path("new.py"), ws.path("old.py")]

    async def test_truncation_hint(self, ws):
        for name in ("a", "b", "c"):
            (ws.real / f"{name}.txt").write_text("hit\n")
        raw = await raw_call(file_ops._search_files_handler, ws, pattern="hit", limit=2)
        assert "[Hint: Results truncated. Use offset=2" in raw


@pytest.mark.parametrize("ws", LOCAL, indirect=True)
class TestTerminal:
    async def test_runs_in_workspace_with_home_there(self, ws):
        out = await call(terminal._terminal_handler, ws, command="pwd; echo $HOME")
        assert out["output"].splitlines() == [ws.root, ws.root]
        assert out["exit_code"] == 0

    async def test_relative_workdir(self, ws):
        (ws.real / "sub").mkdir()
        out = await call(terminal._terminal_handler, ws, command="pwd", workdir="sub")
        assert out["output"] == ws.path("sub")

    async def test_workdir_outside_is_blocked_at_once(self, ws):
        started = time.monotonic()
        out = await call(terminal._terminal_handler, ws, command="pwd", workdir="/etc")
        assert out["status"] == "blocked"
        assert "All commands must run within the workspace directory" in out["error"]
        assert time.monotonic() - started < 1.0

    async def test_exit_code_meaning_for_grep(self, ws):
        (ws.real / "a.txt").write_text("x\n")
        out = await call(terminal._terminal_handler, ws, command="grep nothere a.txt")
        assert out["exit_code"] == 1
        assert out["exit_code_meaning"] == "No matches found (not an error)"

    async def test_secrets_are_not_inherited(self, ws, monkeypatch):
        monkeypatch.setenv("SUROGATES_TEST_SECRET", "s3cret")
        out = await call(terminal._terminal_handler, ws, command='echo "[$SUROGATES_TEST_SECRET]"')
        assert out["output"] == "[]"


@pytest.mark.parametrize("ws", LOCAL, indirect=True)
class TestProcess:
    async def test_background_command_runs_and_reports(self, ws):
        started = await call(
            terminal._terminal_handler, ws,
            command="echo started; sleep 0.2; echo done", background=True,
        )
        assert started["output"] == "Background process started"
        sid = started["session_id"]
        waited = await call(
            registry_module._handle_process, ws, action="wait", session_id=sid, timeout=10,
        )
        assert (waited["status"], waited["exit_code"]) == ("exited", 0)
        assert "done" in waited["output"]
        log = await call(registry_module._handle_process, ws, action="log", session_id=sid)
        assert log["output"].splitlines()[-1] == "done"
        listed = await call(registry_module._handle_process, ws, action="list")
        assert sid in [p["session_id"] for p in listed["processes"]]

    async def test_submit_reaches_stdin(self, ws):
        # `head -n 1` echoes one line and exits.  A pipe-mode process only
        # hands its output over at end of stream (the reader blocks in
        # read(4096)), so a process that keeps running would show nothing.
        started = await call(terminal._terminal_handler, ws, command="head -n 1", background=True)
        sid = started["session_id"]
        sent = await call(
            registry_module._handle_process, ws, action="submit", session_id=sid, data="ping",
        )
        assert sent["status"] == "ok"
        waited = await call(
            registry_module._handle_process, ws, action="wait", session_id=sid, timeout=10,
        )
        assert (waited["status"], waited["exit_code"]) == ("exited", 0)
        assert "ping" in waited["output"]

    async def test_kill_ends_the_process(self, ws):
        # The shell writes its own PID, so the test can tell that the process
        # is gone and not just that the tool says "killed".  The PID file is
        # written only once the shell is up, which is also when it starts
        # honouring SIGTERM.
        started = await call(
            terminal._terminal_handler, ws, command="echo $$ > pid; sleep 30", background=True,
        )
        sid = started["session_id"]
        pid = 0
        for _ in range(50):
            text = (ws.real / "pid").read_text() if (ws.real / "pid").exists() else ""
            if text.endswith("\n"):
                pid = int(text)
                break
            await asyncio.sleep(0.1)
        assert pid, "the background command never wrote its PID"
        killed = await call(registry_module._handle_process, ws, action="kill", session_id=sid)
        assert killed["status"] == "killed"
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            await asyncio.sleep(0.1)
        os.killpg(pid, signal.SIGKILL)
        pytest.fail(f"process {pid} was still running 5 s after kill")


@pytest.mark.parametrize("ws", LOCAL, indirect=True)
class TestResearch:
    async def test_memory_and_outline_round_trip(self, ws):
        added = await call(
            research._research_memory_handler, ws,
            action="add", url="https://example.org/a", title="A", summary="s", evidence=["q"],
        )
        assert added["success"] is True
        listed = await call(research._research_memory_handler, ws, action="list")
        assert [s["url"] for s in listed["sources"]] == ["https://example.org/a"]
        await call(research._research_outline_handler, ws, action="set", outline="## One\n")
        got = await call(research._research_outline_handler, ws, action="get")
        assert got["outline"].startswith("## One")
        assert (ws.real / ".research" / "memory.jsonl").is_file()


async def test_remapped_workspace_serves_the_real_directory(tmp_path):
    real = tmp_path.resolve()
    (real / "a.txt").write_text("x")
    wio = RemappingWorkspaceIO(str(real))
    key = await wio.resolve("a.txt")
    assert key == f"{VIRTUAL_ROOT}/a.txt"
    assert await wio.read(key) == b"x"
    assert not os.path.exists(key)
