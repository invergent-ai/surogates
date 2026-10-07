"""A project's history and a thread's copy, in real git over temporary folders."""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from surogates.sandbox import history as history_module
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


def a_repository(folder: Path, *, committed: bool = True) -> None:
    """A git repository in *folder*, with a commit or, as a fresh ``git init`` leaves it, none."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "README.md").write_text("a repository")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    for args in (["init", "-q"], *([["add", "-A"], ["commit", "-qm", "first"]] if committed else [])):
        subprocess.run(
            ["git", "-C", str(folder), "-c", "user.name=u", "-c", "user.email=u@x", *args],
            env=env, capture_output=True, check=True,
        )


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
    assert b["overlapped"] == [{"path": "Report.docx", "reason": "changed"}, {"path": "uploads/brief.pdf", "reason": "with"}]
    assert [c["path"] for c in b["changes"]] == ["Budget.xlsx"]
    # The newer file stays, and B's other file lands.  B's deletion waits:
    # while a write of its turn is held, a deletion may be a move git could not see.
    assert (project / "Report.docx").read_bytes() == b"report by A"
    assert (project / "Budget.xlsx").read_bytes() == b"budget by B"
    assert (project / "uploads" / "brief.pdf").read_bytes() == b"%PDF brief"


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
    assert out["overlapped"] == [{"path": "Report.docx", "reason": "changed"}]
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
    assert out["overlapped"] == [{"path": "Report.docx", "reason": "changed"}]
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


def test_a_users_folder_holding_a_git_repository_is_left_out_of_the_copy_and_the_landing(tmp_path, project):
    a_repository(project / "app")
    a_repository(project / "scratch [v2]*", committed=False)  # git init ran, nothing committed
    history = opened(tmp_path, project)
    assert not (history.copy / "app").exists() and not (history.copy / "scratch [v2]*").exists()
    (history.copy / "app").mkdir()
    (history.copy / "app" / "notes.md").write_text("by the thread")
    (history.copy / "Report.docx").write_bytes(b"report v2")
    out = landed(history)
    assert [c["path"] for c in out["changes"]] == ["Report.docx"]
    # The thread's write there is named, apart from the excluded files.
    assert (out["repositories"], out["excluded"]) == (["app/"], [])
    assert not (project / "app" / "notes.md").exists()


def test_a_thread_that_clones_or_inits_a_repository_in_its_copy_still_lands_its_turn(tmp_path, project):
    history = opened(tmp_path, project)
    a_repository(history.copy / "clone")  # "clone X and write me a summary"
    history.snapshot("before terminal")
    a_repository(history.copy / "hello", committed=False)  # cargo new
    a_repository(history.copy / ".threads" / "pod-1")  # the coding tool's checkout
    history.snapshot("before write_file")
    (history.copy / "summary.md").write_text("a summary")
    out = landed(history)
    assert [c["path"] for c in out["changes"]] == ["summary.md"]
    assert (out["repositories"], out["excluded"]) == (["clone/", "hello/"], [])
    assert (project / "summary.md").read_text() == "a summary"
    assert not (project / "clone").exists() and not (project / "hello").exists()


def test_a_rename_onto_a_name_someone_took_since_lands_neither_side(tmp_path, project):
    (project / "Draft.docx").write_bytes(b"the draft")
    (project / "Old.docx").write_bytes(b"an old version")
    history = opened(tmp_path, project)
    (history.copy / "Draft.docx").rename(history.copy / "Final.docx")
    (history.copy / "Old.docx").rename(history.copy / "Archived.docx")
    (project / "Final.docx").write_bytes(b"your own final")
    out = landed(history)
    assert out["overlapped"] == [{"path": "Draft.docx", "reason": "with"}, {"path": "Final.docx", "reason": "changed"}]
    # The draft survives; a rename nobody crossed lands whole.
    assert (project / "Draft.docx").read_bytes() == b"the draft"
    assert (project / "Final.docx").read_bytes() == b"your own final"
    assert sorted(c["path"] for c in out["changes"]) == ["Archived.docx", "Old.docx"]
    assert (project / "Archived.docx").read_bytes() == b"an old version" and not (project / "Old.docx").exists()


def test_a_change_of_shape_is_left_out_and_never_fails_the_landing(tmp_path, project):
    (project / "notes").mkdir()
    (project / "notes" / "a.md").write_text("a")
    (project / "plan.md").write_text("plan")
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / "linked").symlink_to(outside)  # a real folder that is a link out of the project
    history = opened(tmp_path, project)
    shutil.rmtree(history.copy / "notes")
    (history.copy / "notes").write_text("notes, one file")  # a folder becomes a file
    (history.copy / "plan.md").unlink()
    (history.copy / "plan.md").mkdir()
    (history.copy / "plan.md" / "q1.md").write_text("q1")  # a file becomes a folder
    (history.copy / "linked").unlink()
    (history.copy / "linked").mkdir()
    (history.copy / "linked" / "x.md").write_text("x")
    (history.copy / "Report.docx").write_bytes(b"report v2")
    out = landed(history)
    assert [c["path"] for c in out["changes"]] == ["Report.docx"]
    assert [o["path"] for o in out["overlapped"]] == [
        "linked", "linked/x.md", "notes", "notes/a.md", "plan.md", "plan.md/q1.md",
    ]
    assert {o["reason"] for o in out["overlapped"]} == {"shape"}
    assert (project / "notes" / "a.md").read_text() == "a"
    assert (project / "plan.md").read_text() == "plan"
    assert (project / "Report.docx").read_bytes() == b"report v2"
    assert not list(outside.iterdir()) and not list(project.rglob("*.landing~"))


def test_a_write_cut_short_leaves_the_real_file_whole_and_nothing_beside_it(tmp_path, project, monkeypatch):
    history = opened(tmp_path, project)
    (history.copy / "Report.docx").write_bytes(b"report v2")
    [change] = history.commit_turn(author=THREAD_A, trailers=trailers("turn"))["changes"]
    run = subprocess.run

    def out_of_time(args, **kwargs):
        if args[:2] == ["git", "cat-file"]:
            kwargs["stdout"].write(b"half a rep")
            raise subprocess.TimeoutExpired(args, 120)
        return run(args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "run", out_of_time)
        with pytest.raises(HistoryError, match="timed out"):
            history.apply(change["path"], change["before"], change["after"])
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", lambda *_: (_ for _ in ()).throw(IsADirectoryError(21, "Is a directory")))
        with pytest.raises(OSError):
            history.apply(change["path"], change["before"], change["after"])
    assert (project / "Report.docx").read_bytes() == b"PK\x03\x04 report v1"
    assert not list(project.rglob("*.landing~"))


def test_a_git_killed_by_its_timeout_leaves_the_copy_usable(tmp_path, project, monkeypatch):
    history = opened(tmp_path, project)
    # A file-system monitor that git add runs while it holds the index's lock, and that outlasts it.
    slow = tmp_path / "slow-monitor"
    slow.write_text('#!/bin/sh\nexec >/dev/null 2>&1 </dev/null\n[ -e "$GIT_DIR/index.lock" ] && sleep 3\nexit 0\n')
    slow.chmod(0o755)
    git(history, "config", "core.fsmonitor", str(slow))
    (history.copy / "Report.docx").write_bytes(b"report v2")
    with monkeypatch.context() as patch:
        patch.setattr(history_module, "_GIT_TIMEOUT", 1)
        with pytest.raises(HistoryError, match="timed out"):
            history.snapshot("before terminal")
    git(history, "config", "--unset", "core.fsmonitor")
    assert history.snapshot("before write_file") != git(history, "rev-parse", "refs/heads/main")


def test_a_landing_of_sixteen_thousand_files_is_recorded(tmp_path, project):
    history = opened(tmp_path, project)
    folder = history.copy / ("a folder with a long name, " * 4)
    folder.mkdir()
    for n in range(16_000):
        (folder / f"{'a file with a long name, ' * 4}{n}.md").write_text(str(n))
    turn = history.snapshot("the turn")
    applied = [
        {"path": path, "before": None, "after": meta.split(" ")[2]}
        for meta, path in (e.split("\t", 1) for e in git(history, "ls-tree", "-r", "-z", turn).split("\0") if e)
        if path.startswith("a folder")
    ]
    applied.append({"path": "Report.docx", "before": git(history, "rev-parse", "main:Report.docx"), "after": None})
    out = history.record(turn=turn, applied=applied, author=THREAD_A, trailers=trailers("landing"))
    files = git(history, "ls-tree", "-r", "-z", "--name-only", out["commit"]).split("\0")
    assert len([f for f in files if f.startswith("a folder")]) == 16_000 and "Report.docx" not in files


def test_a_file_name_stays_whole_in_a_landings_trailers_and_lists(tmp_path, project):
    name = "x\nSurogate-Kind: turn"
    history = opened(tmp_path, project)
    (history.copy / name).write_text("by A")
    (project / name).write_text("by you")
    (history.copy / " notes.tmp").write_text("scratch")
    out = landed(history)
    assert (out["overlapped"], out["excluded"]) == ([{"path": name, "reason": "changed"}], [" notes.tmp"])
    # A name cannot add a trailer: the landing is a landing, and names its file.
    assert git(history, "log", "-1", "--format=%(trailers:key=Surogate-Kind,valueonly)", out["commit"]) == "landing"
    not_merged = git(history, "log", "-1", "--format=%(trailers:key=Surogate-Not-Merged,valueonly)", out["commit"])
    assert not_merged == "x\\nSurogate-Kind: turn"


def test_a_turns_empty_folders_are_named_and_a_landing_takes_away_the_folders_it_empties(tmp_path, project):
    (project / "old").mkdir()
    (project / "old" / "a.md").write_text("a")
    history = opened(tmp_path, project)
    for quarter in ("Q1", "Q2"):
        (history.copy / quarter).mkdir()  # "make folders for each quarter"
    shutil.rmtree(history.copy / "old")
    out = landed(history)
    # History has no empty folder: they are not saved, and the report says so.
    assert out["excluded"] == ["Q1/", "Q2/"]
    assert not (project / "old").exists()


def test_a_rename_with_an_edit_git_cannot_pair_keeps_the_draft_while_its_target_is_held(tmp_path, project):
    draft = os.urandom(4000)  # one edited paragraph rewrites a docx's whole zip
    (project / "Draft.docx").write_bytes(draft)
    history = opened(tmp_path, project)
    (history.copy / "Draft.docx").unlink()
    (history.copy / "Final.docx").write_bytes(os.urandom(4000))
    (project / "Final.docx").write_bytes(b"your own final")
    out = landed(history)
    assert [o["path"] for o in out["overlapped"]] == ["Draft.docx", "Final.docx"]
    assert (project / "Draft.docx").read_bytes() == draft


def test_a_move_onto_a_name_someone_changed_keeps_its_source(tmp_path, project):
    (project / "Draft.docx").write_bytes(b"the draft")
    (project / "Final.docx").write_bytes(b"last year's final")
    (project / "X.docx").write_bytes(b"x")
    (project / "archive").mkdir()
    (project / "archive" / "X.docx").write_bytes(b"an older x")
    history = opened(tmp_path, project)
    # mv -f Draft.docx Final.docx; and X moved into archive/ over its older self.
    for source, target in (("Draft.docx", "Final.docx"), ("X.docx", "archive/X.docx")):
        (history.copy / source).replace(history.copy / target)
        (project / target).write_bytes(b"changed by you")
    out = landed(history)
    assert [o["path"] for o in out["overlapped"]] == ["Draft.docx", "Final.docx", "X.docx", "archive/X.docx"]
    assert (project / "Draft.docx").read_bytes() == b"the draft" and (project / "X.docx").read_bytes() == b"x"


def test_two_identical_files_paired_wrong_lose_neither(tmp_path, project):
    for name in ("A.docx", "B.docx"):
        (project / name).write_bytes(b"one text, twice")
    history = opened(tmp_path, project)
    (history.copy / "A.docx").unlink()
    (history.copy / "B.docx").rename(history.copy / "C.docx")
    (project / "A.docx").write_bytes(b"A, edited by you")
    (project / "C.docx").write_bytes(b"your own C")
    out = landed(history)
    assert (out["changes"], [o["path"] for o in out["overlapped"]]) == ([], ["A.docx", "B.docx", "C.docx"])
    assert (project / "B.docx").read_bytes() == b"one text, twice"


def test_a_folder_deleted_and_cloned_into_keeps_its_files(tmp_path, project):
    (project / "docs").mkdir()
    for name in ("a.md", "b.md"):
        (project / "docs" / name).write_text(name)
    history = opened(tmp_path, project)
    shutil.rmtree(history.copy / "docs")
    history.snapshot("before terminal")
    a_repository(history.copy / "docs")  # git clone … docs
    out = landed(history)
    assert (out["repositories"], [o["path"] for o in out["overlapped"]]) == (["docs/"], ["docs/a.md", "docs/b.md"])
    assert sorted(p.name for p in (project / "docs").iterdir()) == ["a.md", "b.md"]


def test_a_put_back_takes_away_only_the_folders_its_apply_made(tmp_path, project):
    (project / "Reports").mkdir()  # the user's empty folder: history holds none
    history = opened(tmp_path, project)
    for name in ("Reports/q1.md", "Drafts/2026/a.md"):
        (history.copy / name).parent.mkdir(parents=True, exist_ok=True)
        (history.copy / name).write_text(name)
    turn = history.commit_turn(author=THREAD_A, trailers=trailers("turn"))
    for applied in [history.apply(c["path"], c["before"], c["after"]) for c in turn["changes"]]:
        history.unapply(**applied)
    assert sorted(p.name for p in project.iterdir() if p.is_dir()) == [".threads", "Reports", "node_modules", "uploads"]
    assert not any((project / "Reports").iterdir())


def test_a_turn_names_every_excluded_file_and_repository_it_made(tmp_path, project):
    history = opened(tmp_path, project)
    for n in range(12):
        (history.copy / f"notes {n:02}.tmp").write_text("scratch")
        a_repository(history.copy / f"clone {n:02}", committed=False)
    turn = history.commit_turn(author=THREAD_A, trailers=trailers("turn"))
    assert (len(turn["excluded"]), len(turn["repositories"])) == (12, 12)


def test_a_landings_check_reads_each_real_file_as_the_bucket_has_it(tmp_path, project, monkeypatch):
    history = opened(tmp_path, project)
    (history.copy / "Report.docx").write_bytes(b"report v2")
    landed(history)
    # A save between two landings of one pod is seen.
    (project / "Report.docx").write_bytes(b"saved by you")
    (history.copy / "Report.docx").write_bytes(b"report v3")
    asked, dropped = [], []
    monkeypatch.setattr(os, "setxattr", lambda path, name, value: asked.append((str(path), name)))
    monkeypatch.setattr(os, "posix_fadvise", lambda fd, offset, length, advice: dropped.append(advice))
    out = landed(history)
    assert out["overlapped"] == [{"path": "Report.docx", "reason": "changed"}]
    assert (project / "Report.docx").read_bytes() == b"saved by you"
    # geesefs checks the file with the bucket again, and the page cache does not answer for it.
    assert (str(project / "Report.docx"), ".invalidate") in asked
    assert dropped and set(dropped) == {os.POSIX_FADV_DONTNEED}


def fsynced(monkeypatch, fail: str | None = None) -> list[str]:
    """What each fsync made durable, by path; *fail* (``file`` or ``folder``) fails that kind."""
    synced, sync = [], os.fsync

    def fsync(fd):
        path = os.readlink(f"/proc/self/fd/{fd}")
        if fail == ("folder" if os.path.isdir(path) else "file"):
            raise OSError(errno.EIO, "Input/output error")
        synced.append(path)
        sync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    return synced


def test_a_landing_answers_only_once_its_writes_and_deletions_are_durable(tmp_path, project, monkeypatch):
    history = opened(tmp_path, project)
    (history.copy / "Report.docx").write_bytes(b"report v2")
    (history.copy / "uploads" / "brief.pdf").unlink()
    turn = history.commit_turn(author=THREAD_A, trailers=trailers("turn"))
    synced = fsynced(monkeypatch)
    for c in turn["changes"]:
        history.apply(c["path"], c["before"], c["after"])
    # The written file before it is renamed over the real one, then its
    # folder; the deletion's folder, which went with it, through its parent.
    assert [p.endswith(".landing~") for p in synced] == [True, False, False]
    assert synced[1:] == [str(project), str(project)]


@pytest.mark.parametrize("fail", ["file", "folder"])
def test_a_write_that_cannot_be_made_durable_fails_its_apply_and_is_put_back(tmp_path, project, monkeypatch, fail):
    history = opened(tmp_path, project)
    (history.copy / "Report.docx").write_bytes(b"report v2")
    [change] = history.commit_turn(author=THREAD_A, trailers=trailers("turn"))["changes"]
    with monkeypatch.context() as patch:
        fsynced(patch, fail=fail)
        with pytest.raises(OSError):
            history.apply(change["path"], change["before"], change["after"])
    # Like any failed apply's, its file is put back where it may have been written.
    history.unapply(change["path"], change["before"], change["after"], ran=False)
    assert (project / "Report.docx").read_bytes() == b"PK\x03\x04 report v1"
    assert not list(project.rglob("*.landing~"))


@pytest.mark.parametrize("target", [
    "coverage/Policy 17.docx", "Policies/Policy 17.docx.tmp", "build/Policy 17.docx", ".threads/Policy 17.docx",
])
def test_a_move_into_a_path_history_leaves_out_keeps_its_source(tmp_path, project, target):
    (project / "Policies").mkdir()
    (project / "Policies" / "Policy 17.docx").write_bytes(b"policy 17")
    history = opened(tmp_path, project)
    (history.copy / target).parent.mkdir(parents=True, exist_ok=True)
    (history.copy / "Policies" / "Policy 17.docx").rename(history.copy / target)
    out = landed(history)
    # The move's other half never lands, so its deletion waits too.
    assert out["overlapped"] == [{"path": "Policies/Policy 17.docx", "reason": "with"}]
    assert (project / "Policies" / "Policy 17.docx").read_bytes() == b"policy 17"


LONG_NAME = "Contrat " + "é" * 119 + ".docx"  # 251 bytes: no room left for a suffix


def test_a_file_whose_name_is_near_the_limit_lands(tmp_path, project):
    assert len(LONG_NAME.encode()) == 251
    history = opened(tmp_path, project)
    (history.copy / LONG_NAME).write_bytes(b"contrat")
    landed(history)
    assert (project / LONG_NAME).read_bytes() == b"contrat"


def test_a_landing_that_left_out_two_thousand_files_is_recorded(tmp_path, project):
    history = opened(tmp_path, project)
    (history.copy / "kept.md").write_text("kept")
    turn = history.commit_turn(author=THREAD_A, trailers=trailers("turn"))
    applied = [history.apply(c["path"], c["before"], c["after"]) for c in turn["changes"]]
    held = [["Surogate-Not-Merged", f"{'a folder with a long name, ' * 3}{n}.docx"] for n in range(2000)]
    out = history.record(turn=turn["commit"], applied=applied, author=THREAD_A, trailers=trailers("landing", *held))
    named = git(history, "log", "-1", "--format=%(trailers:key=Surogate-Not-Merged,valueonly)", out["commit"])
    assert len(named.splitlines()) == 2000


def test_a_rename_lands_its_new_name_before_it_removes_the_old(tmp_path, project):
    (project / "Draft.docx").write_bytes(b"the draft")
    history = opened(tmp_path, project)
    (history.copy / "Draft.docx").rename(history.copy / "Final.docx")
    turn = history.commit_turn(author=THREAD_A, trailers=trailers("turn"))
    # Cut off between the two, the document is under both names, never under neither.
    assert [(c["path"], c["after"] is None) for c in turn["changes"]] == [("Final.docx", False), ("Draft.docx", True)]
