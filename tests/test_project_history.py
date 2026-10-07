"""A project's history and a thread's copy, in real git over temporary folders."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from surogates.sandbox.history import History, HistoryConflict, HistoryError
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


THREAD_A = {"name": "Draft A", "email": "thread:t1@surogate"}


def trailers(kind: str, *more: list[str]) -> list[list[str]]:
    return [["Surogate-Thread", "t1"], ["Surogate-Kind", kind], *more]


def landed(history: History, author=THREAD_A) -> dict:
    """*history*'s turn committed, applied whole and recorded, as a landing saga runs it."""
    turn = history.commit_turn(author=author, trailers=trailers("turn"))
    applied = [history.apply(c["path"], c["before"], c["after"]) for c in turn["changes"]]
    not_merged = [["Surogate-Not-Merged", o["path"]] for o in turn["overlapped"]]
    record = history.record(
        turn=turn["commit"], applied=applied, author=author,
        trailers=trailers("landing", *not_merged),
    )
    return {**turn, "turn": turn["commit"], **record}


def test_a_landing_leaves_out_a_file_the_real_files_changed_since_its_branch_point(tmp_path, project):
    (project / "Budget.xlsx").write_bytes(b"budget v1")
    first = opened(tmp_path, project, "t1")
    second = opened(tmp_path, project, "t2")
    (first.copy / "Report.docx").write_bytes(b"report by A")
    (first.copy / "threads" / "A").mkdir(parents=True)
    (first.copy / "threads" / "A" / "a.md").write_text("A's notes")
    (second.copy / "Report.docx").write_bytes(b"report by B")
    (second.copy / "Budget.xlsx").write_bytes(b"budget by B")
    (second.copy / "uploads" / "brief.pdf").unlink()

    a = landed(first)
    assert a["overlapped"] == []
    assert (project / "Report.docx").read_bytes() == b"report by A"
    assert (project / "threads" / "A" / "a.md").read_text() == "A's notes"

    b = landed(second, author={"name": "Draft B", "email": "thread:t2@surogate"})
    assert b["overlapped"] == [{"path": "Report.docx"}]
    assert sorted(c["path"] for c in b["changes"]) == ["Budget.xlsx", "uploads/brief.pdf"]
    # The newer file stays; B's other files land, a deletion included.
    assert (project / "Report.docx").read_bytes() == b"report by A"
    assert (project / "Budget.xlsx").read_bytes() == b"budget by B"
    assert not (project / "uploads" / "brief.pdf").exists()


def test_a_landing_is_a_merge_with_the_turn_as_its_second_parent(tmp_path, project):
    history = opened(tmp_path, project)
    main = git(history, "rev-parse", "refs/heads/main")
    (history.copy / "Report.docx").write_bytes(b"report v2")
    out = landed(history)
    assert git(history, "rev-parse", f"{out['commit']}^1", f"{out['commit']}^2").split() == [main, out["turn"]]
    # The turn, and the landing, by the thread with their trailers.
    for commit, kind in ((out["turn"], "turn"), (out["commit"], "landing")):
        assert git(history, "log", "-1", "--format=%an <%ae>", commit) == "Draft A <thread:t1@surogate>"
        assert git(history, "log", "-1", "--format=%(trailers:key=Surogate-Kind,valueonly)", commit) == kind
    # main is the landing; the branch and its base move to it.
    for ref in ("refs/heads/main", "refs/heads/threads/t1", "refs/bases/t1"):
        assert git(history, "rev-parse", ref) == out["commit"]


def test_a_thread_version_that_did_not_land_stays_reachable_from_main(tmp_path, project):
    history = opened(tmp_path, project)
    (history.copy / "Report.docx").write_bytes(b"report by A")
    (project / "Report.docx").write_bytes(b"report by you")
    out = landed(history)
    assert out["overlapped"] == [{"path": "Report.docx"}]
    assert git(history, "log", "-1", "--format=%(trailers:key=Surogate-Not-Merged,valueonly)", out["commit"]) == "Report.docx"
    assert git(history, "show", f"{out['commit']}^2:Report.docx") == "report by A"
    assert (project / "Report.docx").read_bytes() == b"report by you"


def test_an_apply_checks_the_real_file_and_its_undo_checks_it_again(tmp_path, project):
    history = opened(tmp_path, project)
    (history.copy / "Report.docx").write_bytes(b"report v2")
    (history.copy / "new.md").write_text("new")
    turn = history.commit_turn(author=THREAD_A, trailers=trailers("turn"))
    report, new = sorted(turn["changes"], key=lambda c: c["path"])

    (project / "Report.docx").write_bytes(b"saved by you just now")
    with pytest.raises(HistoryConflict):
        history.apply(report["path"], report["before"], report["after"])
    assert (project / "Report.docx").read_bytes() == b"saved by you just now"

    history.apply(new["path"], new["before"], new["after"])
    assert (project / "new.md").read_text() == "new"
    history.unapply(new["path"], new["before"], new["after"])
    assert not (project / "new.md").exists()
    # Safe to repeat: a file already back is left as it is.
    history.unapply(new["path"], new["before"], new["after"])
    assert not (project / "new.md").exists()

    history.apply(new["path"], new["before"], new["after"])
    (project / "new.md").write_text("changed after the landing wrote it")
    with pytest.raises(HistoryConflict):
        history.unapply(new["path"], new["before"], new["after"])
    assert (project / "new.md").read_text() == "changed after the landing wrote it"


def test_an_apply_whose_reply_was_lost_runs_again_as_a_no_op(tmp_path, project):
    history = opened(tmp_path, project)
    (history.copy / "Report.docx").write_bytes(b"report v2")
    [change] = history.commit_turn(author=THREAD_A, trailers=trailers("turn"))["changes"]
    history.apply(change["path"], change["before"], change["after"])
    # Its retry finds the real file already the turn's, and leaves it.
    history.apply(change["path"], change["before"], change["after"])
    assert (project / "Report.docx").read_bytes() == b"report v2"
    # The file is written beside the real one and renamed over it: nothing is left behind.
    assert not list(project.rglob("*.landing~"))
    # Put back after a failed apply, a real file that is neither version is
    # someone else's: that apply found it changed and wrote nothing.
    (project / "Report.docx").write_bytes(b"saved by you")
    history.unapply(change["path"], change["before"], change["after"], ran=False)
    assert (project / "Report.docx").read_bytes() == b"saved by you"


def test_a_turn_names_the_excluded_files_it_made(tmp_path, project):
    history = opened(tmp_path, project)
    (history.copy / "notes.tmp").write_text("scratch")
    (history.copy / "node_modules").mkdir()
    (history.copy / "node_modules" / "y.js").write_text("y")
    (history.copy / ".threads" / "t1").mkdir(parents=True)
    (history.copy / ".threads" / "t1" / "clone.txt").write_text("a coding checkout")
    (history.copy / "kept.md").write_text("kept")
    turn = history.commit_turn(author=THREAD_A, trailers=trailers("turn"))
    assert turn["excluded"] == ["node_modules/", "notes.tmp"]
    assert [c["path"] for c in turn["changes"]] == ["kept.md"]


def test_a_turn_that_changed_nothing_has_nothing_to_land(tmp_path, project):
    history = opened(tmp_path, project)
    history.snapshot("before terminal")
    assert history.commit_turn(author=THREAD_A, trailers=trailers("turn"))["commit"] is None


def test_an_apply_cannot_write_outside_the_project(tmp_path, project):
    history = opened(tmp_path, project)
    with pytest.raises(HistoryError):
        history.apply("../outside.txt", None, "0" * 40)


def test_a_file_both_threads_changed_the_same_way_counts_as_landed(tmp_path, project):
    first, second = opened(tmp_path, project, "t1"), opened(tmp_path, project, "t2")
    for history in (first, second):
        (history.copy / "Report.docx").write_bytes(b"report, typo fixed")
    landed(first)
    out = landed(second, author={"name": "Draft B", "email": "thread:t2@surogate"})
    assert (out["overlapped"], [c["path"] for c in out["changes"]]) == ([], ["Report.docx"])
    assert (project / "Report.docx").read_bytes() == b"report, typo fixed"


def test_a_deletion_leaves_a_file_someone_changed_since(tmp_path, project):
    history = opened(tmp_path, project)
    (history.copy / "Report.docx").unlink()
    (project / "Report.docx").write_bytes(b"saved by you")
    out = landed(history)
    assert out["overlapped"] == [{"path": "Report.docx"}]
    assert (project / "Report.docx").read_bytes() == b"saved by you"


def test_any_file_name_lands_as_it_is_spelt(tmp_path, project):
    history = opened(tmp_path, project)
    names = ["Résumé, final 2026.docx", "-notes.md", "dir with spaces/a;b.txt", ":notes.md", "Q1 [draft].docx"]
    for name in names:
        (history.copy / name).parent.mkdir(parents=True, exist_ok=True)
        (history.copy / name).write_text(name)
    landed(history)
    assert all((project / name).read_text() == name for name in names)


def test_the_first_thread_of_an_empty_project_lands(tmp_path):
    empty = tmp_path / "project"
    empty.mkdir()
    history = opened(tmp_path, empty)
    (history.copy / "Plan.docx").write_bytes(b"plan")
    out = landed(history)
    assert [c["path"] for c in out["changes"]] == ["Plan.docx"]
    assert (empty / "Plan.docx").read_bytes() == b"plan"


def test_a_projects_gitattributes_never_change_a_files_bytes(tmp_path, project):
    docx = b"PK\x03\x04\r\n\x00 report v1\r\n"
    (project / ".gitattributes").write_text("* eol=lf\n")
    (project / "Report.docx").write_bytes(docx)
    history = opened(tmp_path, project)
    assert (history.copy / "Report.docx").read_bytes() == docx
    # A thread that changes the rules lands them too, with the files after them.
    (history.copy / ".gitattributes").write_text("* text=auto eol=crlf\n")
    (history.copy / "Report.docx").write_bytes(docx + b"edited\r\n")
    out = landed(history)
    assert out["overlapped"] == []
    assert (project / "Report.docx").read_bytes() == docx + b"edited\r\n"
    assert (project / ".gitattributes").read_text() == "* text=auto eol=crlf\n"


def test_the_pods_own_git_settings_never_reach_the_history(tmp_path, project, monkeypatch):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "post-commit").write_text(f"#!/bin/sh\ntouch '{tmp_path / 'hook ran'}'\n")
    (hooks / "post-commit").chmod(0o755)
    (tmp_path / "xdg" / "git").mkdir(parents=True)
    (tmp_path / "xdg" / "git" / "ignore").write_text("*.xlsx\n")
    (tmp_path / "xdg" / "git" / "attributes").write_text("* eol=lf\n")
    (project / "Budget.xlsx").write_bytes(b"budget\r\n")
    for name, value in {
        "GIT_AUTHOR_NAME": "Someone Else", "GIT_AUTHOR_EMAIL": "else@example.com",
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": str(hooks),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
    }.items():
        monkeypatch.setenv(name, value)
    history = opened(tmp_path, project)
    assert (history.copy / "Budget.xlsx").read_bytes() == b"budget\r\n"
    (history.copy / "Report.docx").write_bytes(b"report v2")
    out = landed(history)
    assert git(history, "log", "-1", "--format=%an <%ae>", f"{out['commit']}^1") == "u1 <user:u1@surogate>"
    for commit in (out["turn"], out["commit"]):
        assert git(history, "log", "-1", "--format=%an <%ae>", commit) == "Draft A <thread:t1@surogate>"
    assert not (tmp_path / "hook ran").exists()
