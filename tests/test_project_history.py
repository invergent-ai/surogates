"""A project's history and a thread's copy, in real git over temporary folders."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from surogates.sandbox.history import History, HistoryError
from surogates.tools.utils.checkpoint_manager import _shadow_repo_path


def opened(tmp_path: Path, project: Path, thread: str = "t1") -> History:
    """*thread*'s copy of *project*, opened as a thread's pod opens it."""
    copy = tmp_path / f"copy-{thread}"
    copy.mkdir()
    history = History(
        repo=_shadow_repo_path(str(project), base=tmp_path / "home" / ".surogates" / "history"),
        project=project, copy=copy, thread=thread, user="u1",
    )
    history.open()
    return history


def git(history: History, *args: str) -> str:
    return subprocess.run(
        ["git", f"--git-dir={history.repo}", *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    real = tmp_path / "project"
    (real / "uploads").mkdir(parents=True)
    (real / "Report.docx").write_bytes(b"PK\x03\x04 report v1")
    (real / "uploads" / "brief.pdf").write_bytes(b"%PDF brief")
    (real / "server.log").write_text("a log the user dropped in\n")
    (real / "~$Report.docx").write_bytes(b"owner file")
    (real / "node_modules").mkdir()
    (real / "node_modules" / "x.js").write_text("x")
    (real / ".threads").mkdir()
    (real / ".threads" / "clone.txt").write_text("a coding checkout")
    return real


def test_a_copy_is_the_real_files_less_the_excludes(tmp_path, project):
    history = opened(tmp_path, project)
    copied = sorted(str(p.relative_to(history.copy)) for p in history.copy.rglob("*") if p.is_file())
    assert copied == ["Report.docx", "server.log", "uploads/brief.pdf"]
    # No .git in the copy: a thread never sees one.
    assert not (history.copy / ".git").exists()
    # main is the real files, by the user; the branch and its base start there.
    main = git(history, "rev-parse", "refs/heads/main")
    assert git(history, "log", "-1", "--format=%an <%ae>", main) == "u1 <user:u1@surogate>"
    assert git(history, "rev-parse", "refs/heads/threads/t1") == main
    assert git(history, "rev-parse", "refs/bases/t1") == main


def test_a_snapshot_is_the_branchs_tip_and_a_restore_brings_it_back_whole(tmp_path, project):
    history = opened(tmp_path, project)
    first = history.snapshot("before write_file")
    assert first == git(history, "rev-parse", "refs/heads/main")  # nothing changed yet
    (history.copy / "Report.docx").write_bytes(b"PK\x03\x04 report v2")
    (history.copy / "threads" / "Draft").mkdir(parents=True)
    (history.copy / "threads" / "Draft" / "new.md").write_text("made in the turn")
    second = history.snapshot("before terminal")
    assert second != first
    assert git(history, "rev-parse", "refs/heads/threads/t1") == second

    history.restore(first)
    assert (history.copy / "Report.docx").read_bytes() == b"PK\x03\x04 report v1"
    # A file the snapshot did not have is removed, not left behind.
    assert not (history.copy / "threads" / "Draft" / "new.md").exists()
    # The real files never moved.
    assert (project / "Report.docx").read_bytes() == b"PK\x03\x04 report v1"
    # The branch keeps the snapshots whose changes were undone.
    assert git(history, "merge-base", "--is-ancestor", second, "refs/heads/threads/t1") == ""


def test_a_copy_that_could_not_be_made_is_made_on_the_next_try(tmp_path, project, monkeypatch):
    main, failed = History._main, []

    def a_read_fails_once(self, *args):
        if args[:1] == ("add",) and not failed:
            failed.append(args)
            raise HistoryError("git add failed: a read error")
        return main(self, *args)

    monkeypatch.setattr(History, "_main", a_read_fails_once)
    with pytest.raises(HistoryError):
        opened(tmp_path, project, "t1")
    history = opened(tmp_path, project, "t2")
    assert "Report.docx" in git(history, "ls-tree", "--name-only", "refs/heads/main")
    # A git that runs out of time is a history error, which a readiness check answers.
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("git", 1)))
    with pytest.raises(HistoryError):
        history.snapshot("before terminal")


def test_an_excluded_file_a_thread_makes_stays_out_of_its_snapshots(tmp_path, project):
    history = opened(tmp_path, project)
    (history.copy / "notes.tmp").write_text("scratch")
    snap = history.snapshot("before terminal")
    assert "notes.tmp" not in git(history, "ls-tree", "-r", "--name-only", snap)
