"""A folder's history and a thread's copy on the user's computer, as git in the desktop's VM runs them."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from surogates.sandbox import local_history
from surogates.sandbox.history import HISTORY_CAP, History, HistoryError
from surogates.sandbox.local_history import LocalHistory

A = {"name": "Draft A", "email": "thread:t1@surogate"}
B = {"name": "Draft B", "email": "thread:t2@surogate"}
YOURS = {"name": "u1", "email": "user:u1@surogate"}
#: For every git a test runs itself: none of the user's or the system's config.
HERMETIC = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


@pytest.fixture()
def folder(tmp_path: Path) -> Path:
    """A folder of the user's, as the guest sees it: its files saved a minute ago."""
    real = tmp_path / "Documents"
    real.mkdir()
    (real / "Report.docx").write_bytes(b"PK\x03\x04 report v1")
    (real / "notes.txt").write_text("v1 notes\n")
    for file in real.rglob("*"):
        os.utime(file, (time.time() - 60, time.time() - 60))
    return real


def a_copy(tmp_path: Path, folder: Path, thread: str = "t1") -> LocalHistory:
    """*thread*'s place in the folder's store, opened as its root's first operation opens it."""
    history = LocalHistory.at(tmp_path / "store", folder, thread=thread, user="u1")
    history.open()
    return history


def land(history: LocalHistory, saga: str, author: dict = A) -> dict:
    """*history*'s turn landed whole, as a landing saga runs it: the applies are the host's, a copy of each file."""
    picked = history.pickup(author=YOURS, trailers=[["Surogate-Saga", saga], ["Surogate-Kind", "pickup"]])
    turn = history.commit_turn(
        author=author, trailers=[["Surogate-Saga", saga], ["Surogate-Kind", "turn"]], pickup=picked["commit"],
    )
    if turn["commit"] is None:
        # The turn changed nothing since its base: nothing to apply, and nothing recorded.
        return {**turn, "landing": None}
    for change in turn["changes"]:
        target = history.project / change["path"]
        if change["after"] is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(history.copy / change["path"], target)
    recorded = history.record(
        turn=turn["commit"], applied=turn["changes"], author=author,
        trailers=[["Surogate-Saga", saga], ["Surogate-Kind", "landing"]], main=picked["main"], pickup=picked["commit"],
    )
    return {**turn, "landing": recorded["commit"]}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", f"--git-dir={repo}", *args], capture_output=True, text=True, check=True, env=HERMETIC).stdout.strip()


def a_repository_of_its_own(place: Path, like: Path, marker: Path) -> Path:
    """What an earlier guest's root can leave in a place it was given: a repository of its own making, a copy
    of the thread's whose config names a program for every file git reads or writes."""
    made = place / "spare"
    shutil.copytree(like, made, symlinks=True)
    with open(made / "config", "a") as config:
        config.write(f"[filter \"x\"]\n\tclean = \"echo ran >> {marker}; cat\"\n\tsmudge = \"echo ran >> {marker}; cat\"\n")
    (made / "info" / "attributes").write_text("* filter=x\n")
    (made / "worktrees" / "t1" / "commondir").write_text(f"{made}\n")
    return made


def files_of(folder: Path) -> list[tuple[str, bytes]]:
    return sorted((str(p.relative_to(folder)), p.read_bytes()) for p in folder.rglob("*") if p.is_file())


@contextlib.contextmanager
def refused(code: str, words: str | None = None):
    """A request the history does not answer.  Which way is its code, which whoever asked goes by; the
    *words* beside it are a person's."""
    with pytest.raises(HistoryError, match=words) as caught:
        yield
    assert caught.value.code == code, caught.value


def test_two_threads_on_one_folder_each_work_in_a_copy_and_the_folder_is_untouched(tmp_path, folder):
    one, two = a_copy(tmp_path, folder, "t1"), a_copy(tmp_path, folder, "t2")
    assert one.copy == tmp_path / "store" / "threads" / "t1"
    assert sorted(p.name for p in one.copy.iterdir()) == ["Report.docx", "notes.txt"]
    # No git state reaches a copy, and none the folder.
    assert not (one.copy / ".git").exists() and not (folder / ".git").exists()
    (one.copy / "Report.docx").write_bytes(b"PK\x03\x04 A's report")
    (two.copy / "Report.docx").write_bytes(b"PK\x03\x04 B's report")
    (two.copy / "B.md").write_text("B's own\n")
    one.snapshot("before a step")
    two.snapshot("before a step")
    # Each sees its own change alone, and the folder has neither.
    assert (one.copy / "Report.docx").read_bytes() == b"PK\x03\x04 A's report"
    assert not (one.copy / "B.md").exists()
    assert (folder / "Report.docx").read_bytes() == b"PK\x03\x04 report v1"
    assert sorted(p.name for p in folder.iterdir()) == ["Report.docx", "notes.txt"]
    # The folder's history is one, and each thread's repository its own.
    assert sorted(p.name for p in (tmp_path / "store" / "clones").iterdir()) == ["t1", "t2"]


def test_a_landing_moves_main_in_the_folders_history_and_the_next_copy_starts_from_it(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    (one.copy / "Report.docx").write_bytes(b"PK\x03\x04 report v2")
    (one.copy / "threads" / "Draft A").mkdir(parents=True)
    (one.copy / "threads" / "Draft A" / "outline.md").write_text("outline\n")
    landed = land(one, "saga:1")
    assert [c["path"] for c in landed["changes"]] == ["Report.docx", "threads/Draft A/outline.md"]
    store = tmp_path / "store" / "history.git"
    assert git(store, "rev-parse", "refs/heads/main") == landed["landing"]
    # Packs and packed-refs, each written whole: no loose object, no lock file.
    files = sorted(str(p.relative_to(store)) for p in store.rglob("*") if p.is_file())
    assert [f for f in files if not f.startswith("objects/pack/pack-")] == ["HEAD", "config", "index", "packed-refs"]
    assert git(store, "fsck", "--no-dangling") == ""
    # A landing is a merge: main's files with the thread's, the turn its second parent.
    assert git(store, "log", "-1", "--format=%an <%ae>", landed["landing"]) == "Draft A <thread:t1@surogate>"
    assert len(git(store, "log", "-1", "--format=%P", landed["landing"]).split()) == 2
    two = a_copy(tmp_path, folder, "t2")
    assert (two.copy / "threads" / "Draft A" / "outline.md").read_text() == "outline\n"


def test_the_second_thread_to_land_is_told_who_changed_its_file(tmp_path, folder):
    one, two = a_copy(tmp_path, folder, "t1"), a_copy(tmp_path, folder, "t2")
    (one.copy / "Report.docx").write_bytes(b"PK\x03\x04 A's report")
    (two.copy / "Report.docx").write_bytes(b"PK\x03\x04 B's report")
    (two.copy / "B.md").write_text("B's own\n")
    land(one, "saga:1")
    second = land(two, "saga:2", B)
    # The newer file stays; B's other file lands; B's version is kept in history, the landing's second parent.
    assert (folder / "Report.docx").read_bytes() == b"PK\x03\x04 A's report"
    assert (folder / "B.md").read_text() == "B's own\n"
    assert [c["path"] for c in second["changes"]] == ["B.md"]
    [held] = second["overlapped"]
    assert (held["path"], held["reason"], held["by"]) == ("Report.docx", "changed", {"kind": "thread", "id": "t1", "title": "Draft A"})
    store = tmp_path / "store" / "history.git"
    assert git(store, "cat-file", "-p", f"{second['landing']}^2:Report.docx") == "PK\x03\x04 B's report"


def test_after_a_landing_the_copy_holds_the_newer_file_it_left_out_and_a_redo_lands_on_it(tmp_path, folder):
    one, two = a_copy(tmp_path, folder, "t1"), a_copy(tmp_path, folder, "t2")
    (one.copy / "Report.docx").write_bytes(b"PK\x03\x04 A's report")
    (two.copy / "Report.docx").write_bytes(b"PK\x03\x04 B's report")
    (two.copy / "B.md").write_text("B's own\n")
    (two.copy / "scratch.tmp").write_text("left out of history\n")
    land(one, "saga:1")
    land(two, "saga:2", B)
    # The copy outlives the landing, as no pod does: it is the landing's files from now on, so the
    # file it left out is the newer one, and the thread's own version is history's alone.
    assert (two.copy / "Report.docx").read_bytes() == b"PK\x03\x04 A's report"
    assert (two.copy / "B.md").read_text() == "B's own\n"
    assert (two.copy / "scratch.tmp").read_text() == "left out of history\n"
    # A turn that leaves the file alone has decided not to change it: nothing lands, and nothing is held.
    assert two.changed() == {"paths": []}
    assert land(two, "saga:3", B)["commit"] is None
    # Its redo, made on the newer file, lands.
    (two.copy / "Report.docx").write_bytes(b"PK\x03\x04 A's report, with B's change")
    assert two.changed() == {"paths": ["Report.docx"]}
    redone = land(two, "saga:4", B)
    assert ([c["path"] for c in redone["changes"]], redone["overlapped"]) == (["Report.docx"], [])
    assert (folder / "Report.docx").read_bytes() == b"PK\x03\x04 A's report, with B's change"


def test_the_files_a_turn_changed_are_named_before_anything_is_committed(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    tip = git(one.repo, "rev-parse", "refs/heads/threads/t1")
    (one.copy / "notes.txt").unlink()
    (one.copy / "new folder").mkdir()
    (one.copy / "new folder" / "a b.md").write_text("new\n")
    (one.copy / "build").mkdir()
    (one.copy / "build" / "out.bin").write_bytes(b"\0")
    # What a landing looks at in the folder before its pickup: every file the copy changed since its base, and no other.
    assert one.changed() == {"paths": ["new folder/a b.md", "notes.txt"]}
    assert git(one.repo, "rev-parse", "refs/heads/threads/t1") == tip
    assert not (tmp_path / "store" / "history.git").exists()


def test_your_save_is_picked_up_as_yours_and_holds_the_threads_change(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    (one.copy / "notes.txt").write_text("the thread's notes\n")
    (one.copy / "Summary.md").write_text("summary\n")
    (folder / "notes.txt").write_text("your notes, saved while it worked\n")
    landed = land(one, "saga:1")
    assert (folder / "notes.txt").read_text() == "your notes, saved while it worked\n"
    [held] = landed["overlapped"]
    assert (held["path"], held["reason"], held["by"]) == ("notes.txt", "changed", {"kind": "you"})
    # Your save is a commit of main's, by you, under the landing.
    store = tmp_path / "store" / "history.git"
    assert git(store, "log", "--first-parent", "--format=%ae %s", "refs/heads/main").splitlines()[:2] == [
        "thread:t1@surogate Landing", "user:u1@surogate Your changes",
    ]


def test_a_clean_copy_moves_to_mains_tip_at_its_next_open_and_unlanded_work_stays(tmp_path, folder):
    one, two = a_copy(tmp_path, folder, "t1"), a_copy(tmp_path, folder, "t2")
    (one.copy / "A.md").write_text("A's, landed\n")
    land(one, "saga:1")
    (folder / "yours.txt").write_text("saved by you since\n")
    # Nothing unlanded in the other's copy: its next turn starts from the folder as it is now.
    assert two.open() == {"copy": "moved"}
    assert (two.copy / "A.md").read_text() == "A's, landed\n"
    assert (two.copy / "yours.txt").read_text() == "saved by you since\n"
    # Work it has not landed keeps it where it is, its base with it.
    (two.copy / "B.md").write_text("B's, not landed\n")
    (one.copy / "A2.md").write_text("more of A's\n")
    land(one, "saga:2")
    base = git(two.repo, "rev-parse", "refs/bases/t2")
    assert two.open() == {"copy": "kept"}
    assert not (two.copy / "A2.md").exists()
    assert (two.copy / "B.md").read_text() == "B's, not landed\n"
    assert git(two.repo, "rev-parse", "refs/bases/t2") == base


def test_a_copy_that_was_removed_is_made_again_from_its_branch(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    (one.copy / "Draft.md").write_text("kept on the branch\n")
    one.keep(author=A, trailers=[["Surogate-Kind", "turn"]], base=True)
    shutil.rmtree(one.copy)
    # Until its next open makes it again, a request that needs it says so.
    with refused("no_whole_copy", "refused the request: this thread has no whole copy, and its next open makes one"):
        one.snapshot("before a step")
    assert one.open() == {"copy": "kept"}
    assert (one.copy / "Draft.md").read_text() == "kept on the branch\n"
    assert not (one.copy / ".git").exists()
    # And lands with its next turn.
    assert [c["path"] for c in land(one, "saga:1")["changes"]] == ["Draft.md"]


def test_the_harness_folder_and_the_whiteboard_stay_out_and_a_users_own_artifacts_folder_is_tracked(tmp_path, folder):
    for name in (".surogates-results", "_whiteboard", "_artifacts", "_history", "node_modules"):
        (folder / name).mkdir()
    (folder / ".surogates-results" / ".turn").write_text("mark")
    (folder / "_whiteboard" / "canvas.json").write_text("{}")
    (folder / "_artifacts" / "mine.txt").write_text("the user's own folder of that name\n")
    (folder / "_history" / "family.txt").write_text("the user's own\n")
    (folder / "node_modules" / "left.js").write_text("// excluded")
    one = a_copy(tmp_path, folder)
    assert sorted(p.name for p in one.copy.iterdir()) == ["Report.docx", "_artifacts", "_history", "notes.txt"]
    # What the harness keeps in the copy while the thread works never lands, and is not said to be unsaved.
    (one.copy / ".surogates-results" / "artifacts").mkdir(parents=True)
    (one.copy / ".surogates-results" / "artifacts" / "chart.json").write_text("{}")
    (one.copy / "build").mkdir()
    (one.copy / "build" / "out.bin").write_bytes(b"\0")
    (one.copy / "_artifacts" / "more.txt").write_text("the thread's\n")
    landed = land(one, "saga:1")
    assert [c["path"] for c in landed["changes"]] == ["_artifacts/more.txt"]
    assert landed["excluded"] == ["build/"]
    assert not (folder / ".surogates-results" / "artifacts").exists()


def test_a_file_under_the_coding_tools_folder_never_lands(tmp_path, folder):
    (folder / ".threads").mkdir()
    (folder / ".threads" / "old.txt").write_text("left by an earlier checkout\n")
    one = a_copy(tmp_path, folder)
    assert not (one.copy / ".threads").exists()
    # A coding tool's checkout in the copy, and a loose file beside it.
    checkout = one.copy / ".threads" / "t1" / "repo"
    checkout.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(checkout)], check=True, env=HERMETIC)
    (checkout / "main.py").write_text("print('x')\n")
    (one.copy / ".threads" / "scratch.txt").write_text("loose, beside the checkout\n")
    (one.copy / "notes.txt").write_text("the thread's notes\n")
    assert one.changed() == {"paths": ["notes.txt"]}
    landed = land(one, "saga:1")
    assert [c["path"] for c in landed["changes"]] == ["notes.txt"]
    # The platform's own: never landed, and not said to be unsaved.
    assert (landed["excluded"], landed["repositories"]) == ([], [])
    assert sorted(p.name for p in (folder / ".threads").iterdir()) == ["old.txt"]


def test_a_folder_over_the_cap_has_no_history(tmp_path, folder, monkeypatch):
    monkeypatch.setattr("surogates.sandbox.local_history.HISTORY_CAP", 3)
    (folder / "node_modules").mkdir()
    for n in range(5):
        (folder / "node_modules" / f"{n}.js").write_text("// never counted")
    (folder / "third.txt").write_text("3")
    history = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    assert history.open() == {"copy": "made"}
    assert HISTORY_CAP == 50_000
    (folder / "fourth.txt").write_text("4")
    other = LocalHistory.at(tmp_path / "store", folder, thread="t2", user="u1")
    assert other.open() == {"history": "off", "reason": "cap"}
    # Nothing is made for it: its thread works in the folder itself.
    assert not other.repo.exists() and not other.copy.exists()


def test_a_folder_holding_a_name_that_is_not_utf8_has_no_history(tmp_path, folder):
    (folder / "Résumé.docx").write_bytes(b"PK\x03\x04 a name that is")
    # Left out of history, such a name is none it would have to carry.
    (folder / "node_modules").mkdir()
    open(os.fsencode(folder / "node_modules") + b"/caf\xe9.js", "wb").close()
    one = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    assert one.open() == {"copy": "made"}
    assert sorted(p.name for p in one.copy.iterdir()) == ["Report.docx", "Résumé.docx", "notes.txt"]
    # Made in a copy afterwards, it is refused by name at each request, and the copy works on once it is gone.
    latin1 = os.fsencode(one.copy) + b"/caf\xe9.txt"
    open(latin1, "wb").close()
    with refused("name_not_utf8", "refused the request: a file's name is not UTF-8, which history cannot record"):
        one.snapshot("before a step")
    os.unlink(latin1)
    one.snapshot("before a step")
    # In the folder at a copy's first open, history is off for it, and nothing is made.
    open(os.fsencode(folder) + b"/caf\xe9.txt", "wb").close()
    other = LocalHistory.at(tmp_path / "store", folder, thread="t2", user="u1")
    assert other.open() == {"history": "off", "reason": "names"}
    assert not other.repo.exists() and not other.copy.exists()


def test_what_an_earlier_guest_planted_in_the_history_runs_nothing(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    (one.copy / "A.md").write_text("A\n")
    land(one, "saga:1")
    marker = tmp_path / "ran"
    planted = tmp_path / "planted.sh"
    planted.write_text(f"#!/bin/sh\necho ran >> {marker}\nexit 0\n")
    planted.chmod(0o755)
    store = tmp_path / "store"
    # Everything git would run a program from: hooks, the monitor, the signing program, a filter, a pack hook.
    for repo in (one.repo, store / "history.git"):
        (repo / "hooks").mkdir(exist_ok=True)
        for hook in ("pre-commit", "post-commit", "reference-transaction", "post-checkout", "pre-auto-gc"):
            shutil.copy(planted, repo / "hooks" / hook)
        with open(repo / "config", "a") as config:
            config.write(
                f"[core]\n\tfsmonitor = {planted}\n\thooksPath = {repo / 'hooks'}\n"
                f"[commit]\n\tgpgSign = true\n[gpg]\n\tprogram = {planted}\n"
                f"[filter \"x\"]\n\tclean = {planted}\n\tsmudge = {planted}\n"
                f"[uploadpack]\n\tpackObjectsHook = {planted}\n"
            )
    (one.repo / "info" / "attributes").write_text("* filter=x\n")
    # A copy's own repository named elsewhere: git would read that one's config.
    elsewhere = tmp_path / "elsewhere"
    shutil.copytree(one.repo, elsewhere, ignore=shutil.ignore_patterns("worktrees"))
    (one.repo / "worktrees" / "t1" / "commondir").write_text(f"{elsewhere}\n")
    (one.repo / "worktrees" / "t1" / "config.worktree").write_text(f"[core]\n\tfsmonitor = {planted}\n")
    # The same one level up: the thread's repository and the folder's history each naming that one as its
    # own, with a submodule's repository and objects borrowed from it; a second worktree; and the copy's git
    # folder naming another copy and another branch.
    for repo in (one.repo, store / "history.git"):
        (repo / "commondir").write_text(f"{elsewhere}\n")
        (repo / "objects" / "info").mkdir(exist_ok=True)
        (repo / "objects" / "info" / "alternates").write_text(f"{elsewhere / 'objects'}\n")
        shutil.copytree(elsewhere, repo / "modules" / "s")
    shutil.copytree(one.repo / "worktrees" / "t1", one.repo / "worktrees" / "t9")
    (one.repo / "worktrees" / "t1" / "gitdir").write_text(f"{elsewhere / '.git'}\n")
    (one.repo / "worktrees" / "t1" / "HEAD").write_text("ref: refs/heads/main\n")
    # You save a file meanwhile: the next open reads it into main, through a filter were one left.
    (folder / "notes.txt").write_text("saved by you meanwhile\n")

    again = LocalHistory.at(store, folder, thread="t1", user="u1")
    assert again.open() == {"copy": "moved"}
    for repo in (again.repo, store / "history.git"):
        assert not any(os.path.lexists(repo / name) for name in ("commondir", "hooks", "modules", "objects/info/alternates"))
    assert sorted(p.name for p in (again.repo / "worktrees").iterdir()) == ["t1"]
    assert (again.repo / "worktrees" / "t1" / "gitdir").read_text() == f"{again.copy / '.git'}\n"
    assert (again.repo / "worktrees" / "t1" / "HEAD").read_text() == "ref: refs/heads/threads/t1\n"
    (again.copy / "A.md").write_text("A, again\n")
    again.snapshot("before a step")
    assert [c["path"] for c in land(again, "saga:2")["changes"]] == ["A.md"]
    assert LocalHistory.at(store, folder, thread="t3", user="u1").open() == {"copy": "made"}
    assert not marker.exists()
    assert (folder / "A.md").read_text() == "A, again\n"


@pytest.mark.parametrize("linked", [
    # A folder git takes for its own: the copy's git folder, the folder that holds it, the repository itself.
    "worktrees", "worktrees/t1", ".", "refs", "info",
    # A file git writes where it lies: a commit's message, a reflog, the excludes.
    "worktrees/t1/COMMIT_EDITMSG", "worktrees/t1/logs/HEAD", "info/exclude",
])
def test_a_link_an_earlier_guest_left_in_a_threads_repository_is_never_followed(tmp_path, folder, linked):
    one = a_copy(tmp_path, folder)
    (one.copy / "A.md").write_text("A\n")
    land(one, "saga:1")
    marker = tmp_path / "ran"
    spare = a_repository_of_its_own(tmp_path / "store", one.repo, marker)
    theirs = files_of(spare)
    # The earlier guest's root, which writes the place: part of the thread's repository is now a link into its own.
    at = one.repo / linked
    shutil.rmtree(at) if at.is_dir() else at.unlink(missing_ok=True)
    os.symlink(spare / linked, at)
    # The next boot's guest. A command of the thread's has changed a file, and you have saved one.
    (one.copy / "notes.txt").write_text("the thread's, not yet committed\n")
    (folder / "Report.docx").write_bytes(b"PK\x03\x04 saved by you meanwhile")
    # Whatever request comes first runs no git in it.
    with refused("no_whole_copy"):
        LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").snapshot("before a step")
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    # The repository is made again from the folder's history, and its copy with it: what the thread had not
    # committed for a landing is gone, as a pod's is with its pod.
    assert again.open() == {"copy": "made"}
    assert not one.repo.is_symlink() and not any(p.is_symlink() for p in one.repo.rglob("*"))
    assert (again.copy / "notes.txt").read_text() == "v1 notes\n"
    assert (again.copy / "Report.docx").read_bytes() == b"PK\x03\x04 saved by you meanwhile"
    (again.copy / "A.md").write_text("A, again\n")
    assert [c["path"] for c in land(again, "saga:2")["changes"]] == ["A.md"]
    # Nothing of the earlier guest's ran, and nothing was written where its link led.
    assert not marker.exists()
    assert files_of(spare) == theirs


@pytest.mark.parametrize("left", ["a link to another thread's object", "a pipe"])
def test_nothing_an_earlier_guest_left_among_a_threads_objects_is_read_as_one(tmp_path, folder, left):
    one, two = a_copy(tmp_path, folder), a_copy(tmp_path, folder, "t2")
    (two.copy / "Secret.md").write_text("the second thread's, not landed\n")
    unlanded = two.snapshot("before a step")
    tree = git(two.repo, "rev-parse", f"{unlanded}^{{tree}}")
    # A snapshot's commit and its tree are loose objects of the thread's own repository, and of no other.
    theirs = {oid: two.repo / "objects" / oid[:2] / oid[2:] for oid in (unlanded, tree)}
    assert all(loose.is_file() for loose in theirs.values())
    with pytest.raises(subprocess.CalledProcessError):
        git(one.repo, "cat-file", "-e", unlanded)
    # The earlier guest's root, which writes the place: among the first thread's loose objects, each of
    # those by a link, which git reads as the object it names; or a pipe, which a git that read it would wait on.
    for oid, loose in theirs.items():
        at = one.repo / "objects" / oid[:2] / oid[2:]
        at.parent.mkdir(exist_ok=True)
        at.symlink_to(loose) if left.startswith("a link") else os.mkfifo(at)
    (one.copy / "notes.txt").write_text("the thread's, not yet committed\n")
    # The next boot's guest: whatever request comes first runs no git in such a repository.
    with refused("no_whole_copy"):
        LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").changed()
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    assert again.open() == {"copy": "made"}
    assert not any(p.is_symlink() or p.is_fifo() for p in one.repo.rglob("*"))
    # Neither the second thread's commit nor the names of its files is anything the first one's git reads.
    for oid in theirs:
        with pytest.raises(subprocess.CalledProcessError):
            git(one.repo, "cat-file", "-e", oid)
    # The second thread's own repository is as it was.
    assert all(loose.is_file() and not loose.is_symlink() for loose in theirs.values())
    assert LocalHistory.at(tmp_path / "store", folder, thread="t2", user="u1").changed() == {"paths": ["Secret.md"]}


@pytest.mark.parametrize("first", ["open", "changed", "snapshot", "restore", "fetch", "pickup", "commit", "record", "keep"])
def test_whichever_request_comes_first_the_place_is_put_right_before_git_runs(tmp_path, folder, first):
    one = a_copy(tmp_path, folder)
    (one.copy / "notes.txt").write_text("the thread's notes\n")
    tip = one.snapshot("before a step")
    saga = [["Surogate-Saga", "saga:1"]]
    picked = one.pickup(author=YOURS, trailers=saga)
    turn = one.commit_turn(author=A, trailers=saga, pickup=picked["commit"])
    marker = tmp_path / "ran"
    with open(one.repo / "config", "a") as config:
        config.write(f"[filter \"x\"]\n\tclean = \"echo ran >> {marker}; cat\"\n\tsmudge = \"echo ran >> {marker}; cat\"\n")
    (one.repo / "info" / "attributes").write_text("* filter=x\n")
    # A file for git to read in the copy, and one in the folder.
    (one.copy / "notes.txt").write_text("the thread's notes, changed again\n")
    (folder / "Report.docx").write_bytes(b"PK\x03\x04 saved by you meanwhile")
    # The next boot's guest: its first request may be any the history takes.
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    {
        "open": again.open,
        "changed": again.changed,
        "snapshot": lambda: again.snapshot("before a step"),
        "restore": lambda: again.restore(tip),
        "fetch": lambda: again.fetch(saga="saga:1"),
        "pickup": lambda: again.pickup(author=YOURS, trailers=saga),
        "commit": lambda: again.commit_turn(author=A, trailers=saga, pickup=picked["commit"]),
        "record": lambda: again.record(turn=turn["commit"], applied=[], author=A, trailers=saga, main=picked["main"], pickup=picked["commit"]),
        "keep": lambda: again.keep(author=A, trailers=[["Surogate-Kind", "turn"]], base=True),
    }[first]()
    assert not marker.exists()
    assert "filter" not in (one.repo / "config").read_text()


@pytest.mark.parametrize("linked", [
    ".", "objects", "objects/pack", "refs/kept",
    # Among its loose objects, of which a history has none: git would read the link as an object.
    "objects/0a/" + "b" * 38,
])
def test_a_folders_history_holding_a_link_is_refused_before_anything_is_written_through_it(tmp_path, folder, linked):
    one = a_copy(tmp_path, folder)
    (one.copy / "A.md").write_text("A\n")
    land(one, "saga:1")
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "hooks").mkdir(parents=True)
    (elsewhere / "hooks" / "kept").write_text("a file of another's\n")
    (elsewhere / "config").write_text("a file of another's\n")
    theirs = files_of(elsewhere)
    at = tmp_path / "store" / "history.git" / linked
    if at.is_dir():
        at.rename(tmp_path / "set-aside")
    at.parent.mkdir(exist_ok=True)
    os.symlink(elsewhere, at)
    # Every request of every thread on the folder, before it writes a byte.
    for thread, ask in (("t1", lambda h: h.snapshot("before a step")), ("t1", lambda h: h.open()), ("t2", lambda h: h.open())):
        with refused("history_refused", "refused the project's history: something in it is neither a file nor a folder"):
            ask(LocalHistory.at(tmp_path / "store", folder, thread=thread, user="u1"))
    assert files_of(elsewhere) == theirs
    assert not (tmp_path / "store" / "clones" / "t2").exists()


def test_a_copy_left_as_a_link_is_made_again_from_its_branch(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    (one.copy / "Draft.md").write_text("on the branch\n")
    one.snapshot("before a step")
    # Another thread's copy, or any folder the next guest can write: the earlier one names it as this thread's.
    other = tmp_path / "another-copy"
    other.mkdir()
    (other / "theirs.docx").write_text("another thread's file\n")
    shutil.rmtree(one.copy)
    os.symlink(other, one.copy)
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    assert again.open() == {"copy": "kept"}
    assert not again.copy.is_symlink()
    assert again.changed() == {"paths": ["Draft.md"]}
    assert (again.copy / "Draft.md").read_text() == "on the branch\n"
    assert files_of(other) == [("theirs.docx", b"another thread's file\n")]


#: Where an open is killed with its request, and what its git has done by then.
CUTS = {
    # Reading the folder's files into main: nothing of the thread's is made yet.
    "reading the folder": ("update-index --add", ""),
    # Writing the copy's files: its folder is there, and its git folder, with no index yet.
    "writing the copy": ("worktree add", '"$GIT" "$@" --no-checkout; '),
}


def an_open_killed(tmp_path: Path, folder: Path, cut: str) -> None:
    """``t1``'s open, every process of it killed at once at the step *cut* names: its request's bound, a stop, a lost guest."""
    step, before = CUTS[cut]
    stopped = tmp_path / "stopped"
    (tmp_path / "bin").mkdir()
    wrapper = tmp_path / "bin" / "git"
    # git, up to the step the request is killed at: it says it got there, and waits.
    wrapper.write_text(
        f"#!/bin/sh\nGIT={shutil.which('git')}\n"
        f'case " $* " in *" {step} "*) {before}: > {stopped}; exec sleep 600;; esac\nexec "$GIT" "$@"\n'
    )
    wrapper.chmod(0o755)
    opening = subprocess.Popen(
        [sys.executable, "-c", (
            "import sys; from pathlib import Path; from surogates.sandbox.local_history import LocalHistory; "
            "LocalHistory.at(Path(sys.argv[1]), Path(sys.argv[2]), thread='t1', user='u1').open()"
        ), str(tmp_path / "store"), str(folder)],
        cwd=Path(__file__).parents[1], env={**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"}, start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 60
        while not stopped.exists():
            assert opening.poll() is None and time.monotonic() < deadline, "the open never reached the step"
            time.sleep(0.01)
    finally:
        if opening.poll() is None:
            os.killpg(opening.pid, signal.SIGKILL)
        opening.wait()


@pytest.mark.parametrize("cut", list(CUTS))
def test_a_first_open_killed_part_way_is_made_again_whole_and_lands_no_deletion(tmp_path, folder, cut):
    an_open_killed(tmp_path, folder, cut)
    one = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    assert (one.repo / "HEAD").exists()
    if cut == "writing the copy":
        # As a checkout killed after its first file leaves it: one of the folder's two files.
        (one.copy / "notes.txt").write_text("v1 notes\n")
        # No request commits such a copy: the file it lacks would land as a deletion.
        with refused("no_whole_copy"):
            one.snapshot("before a step")
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    assert again.open() == {"copy": "made"}
    assert sorted(p.name for p in again.copy.iterdir()) == ["Report.docx", "notes.txt"]
    assert again.changed() == {"paths": []}
    assert land(again, "saga:1")["commit"] is None
    assert sorted(p.name for p in folder.iterdir()) == ["Report.docx", "notes.txt"]


def test_a_copy_whose_making_again_was_killed_is_made_again_from_its_branch_with_what_the_thread_committed(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    (one.copy / "Draft.md").write_text("on the branch, in no landing yet\n")
    one.snapshot("before a step")
    # Its copy was let go, and the open that makes it again is killed as it writes the copy's files.
    shutil.rmtree(one.copy)
    an_open_killed(tmp_path, folder, "writing the copy")
    (one.copy / "notes.txt").write_text("v1 notes\n")
    with refused("no_whole_copy"):
        LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").changed()
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    # The repository was whole: only the copy is made again, and the thread's snapshots are all there.
    assert again.open() == {"copy": "kept"}
    assert sorted(p.name for p in again.copy.iterdir()) == ["Draft.md", "Report.docx", "notes.txt"]
    assert again.changed() == {"paths": ["Draft.md"]}
    assert [c["path"] for c in land(again, "saga:1")["changes"]] == ["Draft.md"]


def test_git_takes_no_hook_monitor_or_signing_program_from_any_config(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    with open(one.repo / "config", "a") as config:
        config.write(
            "[core]\n\tfsmonitor = /planted\n\thooksPath = /planted\n[commit]\n\tgpgSign = true\n"
            "[fetch]\n\trecurseSubmodules = true\n[submodule]\n\trecurse = true\n"
        )

    def read(key: str) -> str:
        return subprocess.run(
            ["git", "config", "--get", key], capture_output=True, text=True, check=True,
            env={**os.environ, "GIT_DIR": str(one.repo), **local_history._PINNED},
        ).stdout.strip()

    # Whatever a config file says, as git itself resolves it under the engine's environment.
    assert [read(key) for key in ("core.hooksPath", "core.fsmonitor", "commit.gpgSign")] == ["/dev/null", "false", "false"]
    # Nor does a commit start any maintenance that would outlast its request, or a fetch or a checkout go
    # into a submodule's repository, whose config is its own.
    assert [read(key) for key in ("gc.auto", "maintenance.auto")] == ["0", "false"]
    assert [read(key) for key in ("fetch.recurseSubmodules", "submodule.recurse")] == ["false", "false"]
    assert local_history._PINNED["GIT_ALLOW_PROTOCOL"] == "file"


def test_a_copys_files_are_kept_in_packs_and_packed_again_at_a_turns_start(tmp_path, folder, monkeypatch):
    monkeypatch.setattr(local_history, "_PACKS", 3)
    one = a_copy(tmp_path, folder)
    packs = one.repo / "objects" / "pack"
    # No file of the folder is a loose object: each would be a file made through the share.
    blobs = {git(one.repo, "rev-parse", f"refs/heads/main:{name}") for name in ("Report.docx", "notes.txt")}
    assert not any((one.repo / "objects" / blob[:2] / blob[2:]).exists() for blob in blobs)
    for n in range(5):
        (one.copy / "notes.txt").write_text(f"edit {n}\n")
        one.snapshot("before a step")
    assert len(list(packs.glob("*.pack"))) > 3
    tip = git(one.repo, "rev-parse", "refs/heads/threads/t1")
    assert one.open() == {"copy": "kept"}
    assert len(list(packs.glob("*.pack"))) == 1
    assert git(one.repo, "rev-parse", "refs/heads/threads/t1") == tip
    assert git(one.repo, "fsck", "--no-dangling") == ""
    assert (one.copy / "notes.txt").read_text() == "edit 4\n"


def test_a_request_cut_short_leaves_no_lock_in_the_next_ones_way(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    # What a git killed with its request leaves: git never removes another's lock.
    for lock in (one.repo / "index.lock", one.repo / "worktrees" / "t1" / "index.lock", one.repo / "refs" / "heads" / "main.lock"):
        lock.write_text("")
    (one.copy / "notes.txt").write_text("after the cut\n")
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    assert again.open() == {"copy": "kept"}
    assert [c["path"] for c in land(again, "saga:1")["changes"]] == ["notes.txt"]


def test_a_history_whose_refs_are_not_its_own_is_refused(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    (one.copy / "A.md").write_text("A\n")
    land(one, "saga:1")
    packed = tmp_path / "store" / "history.git" / "packed-refs"
    packed.write_text(packed.read_text() + "--upload-pack=touch${IFS}/tmp/ran refs/heads/x\n")
    with refused("history_refused", "refused the project's history: its packed-refs holds what is not a commit id"):
        LocalHistory.at(tmp_path / "store", folder, thread="t2", user="u1").open()


def test_a_stop_puts_the_copy_back_and_takes_away_what_the_turn_made(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    before = one.snapshot("before a step")
    (one.copy / "notes.txt").write_text("changed by the turn\n")
    (one.copy / "made.txt").write_text("made by the turn\n")
    one.restore(before)
    assert (one.copy / "notes.txt").read_text() == "v1 notes\n"
    assert not (one.copy / "made.txt").exists()
    assert (folder / "notes.txt").read_text() == "v1 notes\n"
    # A snapshot the repository does not hold: what git could not do is a failure, and no refusal.
    with refused("failed", "git read-tree failed"):
        one.restore("f" * 40)
    assert (one.copy / "notes.txt").read_text() == "v1 notes\n"


THREAD = "0b6c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f"


def ask(tree: Path, request: dict) -> dict:
    """One request to the history as the guest's agent runs it: the tree its disk carries, on a python that
    reads nothing of its user's, with no environment but a PATH."""
    ran = subprocess.run(
        [sys.executable, "-I", str(tree / "main.py")], input=json.dumps(request), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin"}, cwd="/", timeout=120,
    )
    assert ran.returncode == 0, ran.stderr
    return json.loads(ran.stdout)


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    made = tmp_path / "agent-disk" / "history"
    subprocess.run([str(Path(__file__).parents[1] / "desktop" / "vm" / "history-tree.sh"), str(made)], check=True)
    return made


def test_the_agent_disk_carries_the_history_and_one_request_runs_it(tmp_path, folder, tree):
    # The cloud's two modules and the one they import, and nothing else of the platform.
    assert sorted(str(p.relative_to(tree)) for p in tree.rglob("*.py") if p.stat().st_size) == [
        "main.py", "surogates/sandbox/history.py", "surogates/sandbox/local_history.py",
        "surogates/tools/utils/checkpoint_manager.py",
    ]
    place = {"store": str(tmp_path / "store"), "folder": str(folder), "thread": THREAD, "user": "u1"}
    assert ask(tree, {**place, "action": "open", "args": {}}) == {"copy": "made"}
    copy = tmp_path / "store" / "threads" / THREAD
    (copy / "notes.txt").write_text("the thread's\n")
    taken = ask(tree, {**place, "action": "snapshot", "args": {"reason": "before a step"}})
    assert re.fullmatch(r"[0-9a-f]{40}", taken["hash"])
    picked = ask(tree, {**place, "action": "pickup", "args": {"author": YOURS, "trailers": [["Surogate-Saga", "s1"]]}})
    assert picked == {"main": None, "commit": None, "picked_up": [], "packs": 0}
    turn = ask(tree, {**place, "action": "commit", "args": {"author": A, "trailers": [["Surogate-Saga", "s1"]], "pickup": None}})
    assert [(c["path"], c["after"] is not None) for c in turn["changes"]] == [("notes.txt", True)]


@pytest.mark.parametrize("change, said", [
    ({"thread": "t1"}, "it names no thread"),
    ({"thread": "../../etc"}, "it names no thread"),
    ({"user": "u1; rm -rf /"}, "it names no user"),
    ({"store": "relative/place"}, "it names no place"),
    ({"action": "apply"}, "it names no action this computer's history takes"),
    ({"action": "prune"}, "it names no action this computer's history takes"),
    ({"action": "pickup", "args": {"author": YOURS, "trailers": [], "push": True}}, "it names no action this computer's history takes"),
    ({"action": "restore", "args": {"commit": "--upload-pack=/planted"}}, "its commit holds what is not a commit id"),
    ({"action": "fetch", "args": {"commits": ["-o", "x"]}}, "its commits holds what is not a commit id"),
    ({"action": "record", "args": {
        "turn": "0" * 40, "applied": [{"path": "a\0b", "before": None, "after": None}], "author": A, "trailers": [], "main": None,
    }}, "a file it applied has no path"),
])
def test_a_request_that_names_no_thread_action_or_id_of_the_historys_is_refused(tmp_path, folder, tree, change, said):
    place = {"store": str(tmp_path / "store"), "folder": str(folder), "thread": THREAD, "user": "u1", "action": "open", "args": {}}
    answer = ask(tree, {**place, **change})
    assert answer["error"]["code"] == "not_a_request" and said in answer["error"]["message"], answer
    assert not (tmp_path / "store").exists()


def test_a_name_that_is_not_utf8_is_answered_as_the_agent_runs_the_history(tmp_path, folder, tree):
    # With no environment but a PATH, a name that is UTF-8 is one history carries.
    (folder / "Résumé.docx").write_bytes(b"PK\x03\x04 a name that is")
    place = {"store": str(tmp_path / "store"), "folder": str(folder), "thread": THREAD, "user": "u1"}
    assert ask(tree, {**place, "action": "open", "args": {}}) == {"copy": "made"}
    copy = tmp_path / "store" / "threads" / THREAD
    assert (copy / "Résumé.docx").exists()
    open(os.fsencode(copy) + b"/caf\xe9.txt", "wb").close()
    refused = ask(tree, {**place, "action": "snapshot", "args": {"reason": "before a step"}})
    assert refused == {"error": {
        "code": "name_not_utf8", "message": "refused the request: a file's name is not UTF-8, which history cannot record",
    }}
    open(os.fsencode(folder) + b"/caf\xe9.txt", "wb").close()
    other = {**place, "thread": THREAD.replace("0b6c", "1b6c")}
    assert ask(tree, {**other, "action": "open", "args": {}}) == {"history": "off", "reason": "names"}


def test_a_request_not_answered_says_which_way_by_its_code_and_to_a_person_by_its_words(tmp_path, folder, tree):
    place = {"store": str(tmp_path / "store"), "folder": str(folder), "thread": THREAD, "user": "u1"}
    snapshot = {**place, "action": "snapshot", "args": {"reason": "before a step"}}
    assert ask(tree, {**place, "action": "open", "args": {}}) == {"copy": "made"}
    # The request is none this history takes.
    assert ask(tree, {**place, "action": "prune", "args": {}}) == {"error": {
        "code": "not_a_request", "message": "refused the request: it names no action this computer's history takes",
    }}
    # Git could not do what was asked: a snapshot the repository does not hold.
    failed = ask(tree, {**place, "action": "restore", "args": {"commit": "f" * 40}})["error"]
    assert failed["code"] == "failed" and failed["message"].startswith("git read-tree failed: "), failed
    # The thread's copy is gone: its next open makes it.
    shutil.rmtree(tmp_path / "store" / "threads" / THREAD)
    assert ask(tree, snapshot) == {"error": {
        "code": "no_whole_copy", "message": "refused the request: this thread has no whole copy, and its next open makes one",
    }}
    # The folder's history is not one the platform wrote: here, it holds a link.
    (tmp_path / "store" / "history.git").mkdir(exist_ok=True)
    (tmp_path / "store" / "history.git" / "kept").symlink_to(tmp_path)
    assert ask(tree, snapshot) == {"error": {
        "code": "history_refused", "message": "refused the project's history: something in it is neither a file nor a folder",
    }}


def test_a_request_that_is_not_one_is_answered_never_raised(tree):
    for raw, code in (("", "failed"), ("[]", "not_a_request"), ('"open"', "not_a_request"), ("{", "failed")):
        ran = subprocess.run(
            [sys.executable, "-I", str(tree / "main.py")], input=raw, capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}, cwd="/",
        )
        assert ran.returncode == 0, (raw, ran.stderr)
        answer = json.loads(ran.stdout)["error"]
        assert answer["code"] == code and isinstance(answer["message"], str) and set(answer) == {"code", "message"}, (raw, ran.stdout)


def test_a_history_that_moved_under_a_landing_is_told_from_a_git_that_failed(tmp_path, folder, tree):
    one, two = a_copy(tmp_path, folder, THREAD), a_copy(tmp_path, folder, "t2")
    (one.copy / "notes.txt").write_text("the thread's notes\n")
    (two.copy / "B.md").write_text("B's own\n")
    saga = [["Surogate-Saga", "saga:1"]]
    picked = one.pickup(author=YOURS, trailers=saga)
    turn = one.commit_turn(author=A, trailers=saga, pickup=picked["commit"])
    # Another thread lands between this landing's first look and its record.
    land(two, "saga:2", B)
    step = {"turn": turn["commit"], "applied": [], "author": A, "trailers": saga, "main": picked["main"], "pickup": picked["commit"]}
    with refused("conflict", "main moved in the project's history since the landing began"):
        one.record(**step)
    # And so it reaches whoever asked, by its code.
    place = {"store": str(tmp_path / "store"), "folder": str(folder), "thread": THREAD, "user": "u1"}
    assert ask(tree, {**place, "action": "record", "args": step}) == {"error": {
        "code": "conflict", "message": "main moved in the project's history since the landing began",
    }}


def a_record_cut_after_its_push(tmp_path: Path, folder: Path) -> tuple[LocalHistory, dict]:
    """Thread ``t2``'s landing, its record cut right after its push: the history holds the landing, and the
    copy still holds the turn's files.  Thread ``t1`` landed first: a change to the report, and a new file."""
    one, two = a_copy(tmp_path, folder, "t1"), a_copy(tmp_path, folder, "t2")
    (one.copy / "Report.docx").write_bytes(b"PK\x03\x04 A's report")
    (one.copy / "A-new.md").write_text("A's new file\n")
    (two.copy / "Report.docx").write_bytes(b"PK\x03\x04 B's report")
    (two.copy / "B.md").write_text("B's own\n")
    land(one, "saga:1")
    saga = [["Surogate-Saga", "saga:2"]]
    picked = two.pickup(author=YOURS, trailers=saga)
    turn = two.commit_turn(author=B, trailers=saga, pickup=picked["commit"])
    assert [c["path"] for c in turn["changes"]] == ["B.md"] and [o["path"] for o in turn["overlapped"]] == ["Report.docx"]
    shutil.copyfile(two.copy / "B.md", folder / "B.md")
    step = {"turn": turn["commit"], "applied": turn["changes"], "author": B, "trailers": saga, "main": picked["main"], "pickup": picked["commit"]}
    # The cloud's half of the record alone: the push, and the refs moved; the copy is never made the landing's.
    step["landing"] = History.record(two, **step)["commit"]
    return LocalHistory.at(tmp_path / "store", folder, thread="t2", user="u1"), step


def test_a_record_cut_after_its_push_and_never_asked_again_is_finished_at_the_threads_next_open(tmp_path, folder):
    again, cut = a_record_cut_after_its_push(tmp_path, folder)
    theirs = files_of(folder)
    # The open finishes the record first, and says so: the copy is the landing's files, the newer report among them.
    assert again.open() == {"copy": "moved", "finished": {"landing": cut["landing"], "set_aside": None}}
    assert (again.copy / "Report.docx").read_bytes() == b"PK\x03\x04 A's report"
    assert (again.copy / "A-new.md").read_text() == "A's new file\n"
    assert again.changed() == {"paths": []}
    # Its next landing deletes no file another thread landed, and writes no old version over another's change.
    after = land(again, "saga:3", B)
    assert (after["commit"], after["changes"], after["overlapped"]) == (None, [], [])
    assert files_of(folder) == theirs
    # Said once: the next open has nothing to finish.
    assert again.open() == {"copy": "moved"}
    # A redo of the report, made on the newer one, lands.
    (again.copy / "Report.docx").write_bytes(b"PK\x03\x04 A's report, with B's change")
    assert [c["path"] for c in land(again, "saga:4", B)["changes"]] == ["Report.docx"]
    assert sorted(p.name for p in folder.iterdir()) == ["A-new.md", "B.md", "Report.docx", "notes.txt"]


@pytest.mark.parametrize("first", ["changed", "snapshot", "restore", "commit", "keep"])
def test_whichever_act_reads_the_copy_first_after_a_record_cut_after_its_push_finishes_it(tmp_path, folder, first):
    again, cut = a_record_cut_after_its_push(tmp_path, folder)
    theirs = files_of(folder)
    tip = git(again.repo, "rev-parse", "refs/heads/threads/t2")
    saga = [["Surogate-Saga", "saga:3"]]
    answer = {
        "changed": again.changed,
        "snapshot": lambda: again.snapshot("before a step"),
        "restore": lambda: again.restore(tip),
        "commit": lambda: again.commit_turn(author=B, trailers=saga, pickup=None),
        "keep": lambda: again.keep(author=B, trailers=[["Surogate-Kind", "turn"]], base=True),
    }[first]()
    # None takes the turn's files, which landed or were left out, for work the thread has not landed.
    assert (again.copy / "Report.docx").read_bytes() == b"PK\x03\x04 A's report"
    assert (again.copy / "A-new.md").exists()
    if first == "changed":
        assert answer == {"paths": []}
    if first == "commit":
        assert (answer["commit"], answer["changes"]) == (None, [])
    assert files_of(folder) == theirs
    # The thread's next open says what was finished.
    assert LocalHistory.at(tmp_path / "store", folder, thread="t2", user="u1").open() == {
        "copy": "moved", "finished": {"landing": cut["landing"], "set_aside": None},
    }


def test_a_record_asked_again_after_other_threads_landed_answers_its_landing(tmp_path, folder):
    again, cut = a_record_cut_after_its_push(tmp_path, folder)
    one = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    assert one.open() == {"copy": "moved"}
    (one.copy / "A2.md").write_text("more of A's\n")
    land(one, "saga:9")
    # main is another thread's landing now: the record is still this saga's, found where its thread's branch has it.
    step = {key: value for key, value in cut.items() if key != "landing"}
    assert again.record(**step) == {"commit": cut["landing"], "set_aside": None}
    assert (again.copy / "Report.docx").read_bytes() == b"PK\x03\x04 A's report"
    assert again.open() == {"copy": "moved"}
    assert (again.copy / "A2.md").exists()
    # Asked once more after the thread has worked on, it answers the same and leaves the copy alone.
    (again.copy / "notes.txt").write_text("B's next turn, not landed\n")
    assert again.record(**step) == {"commit": cut["landing"], "set_aside": None}
    assert (again.copy / "notes.txt").read_text() == "B's next turn, not landed\n"


def test_a_record_cut_part_way_through_making_the_copy_the_landings_is_finished_whole(tmp_path, folder):
    again, cut = a_record_cut_after_its_push(tmp_path, folder)
    # As a reset killed after its first file leaves the copy: one file the landing's, the rest and the index the turn's.
    (again.copy / "Report.docx").write_bytes(b"PK\x03\x04 A's report")
    finished = again.open()["finished"]
    assert finished["landing"] == cut["landing"]
    assert files_of(again.copy) == files_of(folder)
    assert land(again, "saga:3", B)["commit"] is None


def test_what_a_copy_holds_beyond_its_turn_is_set_aside_before_a_record_makes_it_the_landings(tmp_path, folder):
    one, two = a_copy(tmp_path, folder, "t1"), a_copy(tmp_path, folder, "t2")
    (one.copy / "Report.docx").write_bytes(b"PK\x03\x04 A's report")
    (two.copy / "Report.docx").write_bytes(b"PK\x03\x04 B's report")
    land(one, "saga:1")
    saga = [["Surogate-Saga", "saga:2"]]
    picked = two.pickup(author=YOURS, trailers=saga)
    turn = two.commit_turn(author=B, trailers=saga, pickup=picked["commit"])
    # A command still running writes the copy after the turn was committed: a file of its own, and one of the turn's.
    (two.copy / "late.md").write_text("written after the turn was committed\n")
    (two.copy / "notes.txt").write_text("changed after the turn was committed\n")
    recorded = two.record(turn=turn["commit"], applied=turn["changes"], author=B, trailers=saga, main=picked["main"], pickup=picked["commit"])
    # The copy is the landing's files, and what it held is a snapshot the thread can be put back to.
    aside = recorded["set_aside"]
    assert re.fullmatch(r"[0-9a-f]{40}", aside)
    assert sorted(p.name for p in two.copy.iterdir()) == ["Report.docx", "notes.txt"]
    assert (two.copy / "notes.txt").read_text() == "v1 notes\n"
    assert git(two.repo, "for-each-ref", "--format=%(objectname) %(refname)", "refs/set-aside/") == (
        f"{aside} refs/set-aside/t2/00000001-{turn['commit']}"
    )
    assert git(two.repo, "cat-file", "-p", f"{aside}:late.md") == "written after the turn was committed"
    assert git(two.repo, "cat-file", "-p", f"{aside}:notes.txt") == "changed after the turn was committed"
    # Packed again, the repository still holds it.
    git(two.repo, "repack", "-a", "-d", "-q")
    git(two.repo, "prune", "--expire=now")
    two.restore(aside)
    assert (two.copy / "late.md").read_text() == "written after the turn was committed\n"
    # Asked again, the record says the same.
    again = LocalHistory.at(tmp_path / "store", folder, thread="t2", user="u1")
    assert again.record(turn=turn["commit"], applied=turn["changes"], author=B, trailers=saga, main=picked["main"], pickup=picked["commit"]) == recorded


def test_a_landing_that_left_nothing_out_leaves_the_copy_and_what_was_written_since_as_they_are(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    (one.copy / "notes.txt").write_text("the thread's notes\n")
    saga = [["Surogate-Saga", "saga:1"]]
    picked = one.pickup(author=YOURS, trailers=saga)
    turn = one.commit_turn(author=A, trailers=saga, pickup=picked["commit"])
    shutil.copyfile(one.copy / "notes.txt", folder / "notes.txt")
    (one.copy / "late.md").write_text("written after the turn was committed\n")
    recorded = one.record(turn=turn["commit"], applied=turn["changes"], author=A, trailers=saga, main=picked["main"], pickup=picked["commit"])
    # The landing's files are the turn's: nothing is put back, so nothing is set aside, and the late file is the next turn's work.
    assert recorded["set_aside"] is None
    assert one.changed() == {"paths": ["late.md"]}
    assert [c["path"] for c in land(one, "saga:2")["changes"]] == ["late.md"]


def test_a_thread_keeps_what_its_last_sixteen_records_set_aside(tmp_path, folder, monkeypatch):
    monkeypatch.setattr(local_history, "_ASIDE", 2)
    one, two = a_copy(tmp_path, folder, "t1"), a_copy(tmp_path, folder, "t2")
    assert local_history.LocalHistory is LocalHistory
    kept = []
    for n in range(3):
        (one.copy / "Report.docx").write_bytes(b"PK\x03\x04 A's report %d" % n)
        (two.copy / "Report.docx").write_bytes(b"PK\x03\x04 B's report %d" % n)
        land(one, f"saga:a{n}")
        saga = [["Surogate-Saga", f"saga:b{n}"]]
        picked = two.pickup(author=YOURS, trailers=saga)
        turn = two.commit_turn(author=B, trailers=saga, pickup=picked["commit"])
        (two.copy / "late.md").write_text(f"late {n}\n")
        kept.append(two.record(
            turn=turn["commit"], applied=turn["changes"], author=B, trailers=saga, main=picked["main"], pickup=picked["commit"],
        )["set_aside"])
        assert one.open() == {"copy": "moved"}
    # The oldest goes for one more.
    assert sorted(git(two.repo, "for-each-ref", "--format=%(objectname)", "refs/set-aside/t2/").split()) == sorted(kept[1:])


def test_a_landing_the_history_holds_whose_copy_cannot_be_made_its_files_refuses_every_act_and_writes_nothing(tmp_path, folder):
    again, cut = a_record_cut_after_its_push(tmp_path, folder)
    theirs, held = files_of(folder), files_of(again.copy)
    # What no request can put right: the copy's index is no index.
    (again.repo / "worktrees" / "t2" / "index").write_bytes(b"not an index\n")
    refs = git(again.repo, "for-each-ref")
    saga = [["Surogate-Saga", "saga:3"]]
    for ask in (
        lambda h: h.open(), lambda h: h.changed(), lambda h: h.snapshot("before a step"), lambda h: h.restore(cut["landing"]),
        lambda h: h.commit_turn(author=B, trailers=saga, pickup=None), lambda h: h.keep(author=B, trailers=saga, base=True),
    ):
        with refused("record_unfinished", "refused the request: a landing of this thread's is in the history, and its copy could not be made the landing's files: git write-tree failed"):
            ask(LocalHistory.at(tmp_path / "store", folder, thread="t2", user="u1"))
    # Nothing was read from the copy as the thread's work, and nothing moved.
    assert git(again.repo, "for-each-ref") == refs
    assert (files_of(folder), files_of(again.copy)) == (theirs, held)
    assert git(tmp_path / "store" / "history.git", "rev-parse", "refs/heads/threads/t2") == cut["landing"]


def test_a_copy_that_is_gone_after_a_record_cut_after_its_push_is_made_again_as_the_landings_files(tmp_path, folder):
    again, cut = a_record_cut_after_its_push(tmp_path, folder)
    shutil.rmtree(again.copy)
    with refused("no_whole_copy"):
        again.changed()
    opened = LocalHistory.at(tmp_path / "store", folder, thread="t2", user="u1").open()
    assert opened == {"copy": "moved", "finished": {"landing": cut["landing"], "set_aside": None}}
    assert files_of(again.copy) == files_of(folder)


def a_landing_applied_and_not_recorded(tmp_path: Path, folder: Path) -> tuple[LocalHistory, dict, dict]:
    """A thread's landing with its files applied and no record yet; the turn, and the folder's files it replaced."""
    one = a_copy(tmp_path, folder)
    (one.copy / "notes.txt").write_text("the thread's notes\n")
    (one.copy / "Summary.md").write_text("summary\n")
    (one.copy / "Report.docx").unlink()
    saga = [["Surogate-Saga", "saga:1"]]
    picked = one.pickup(author=YOURS, trailers=saga)
    turn = one.commit_turn(author=A, trailers=[*saga, ["Surogate-Kind", "turn"]], pickup=picked["commit"])
    replaced = {name: (folder / name).read_bytes() for name in ("notes.txt", "Report.docx")}
    shutil.copyfile(one.copy / "notes.txt", folder / "notes.txt")
    shutil.copyfile(one.copy / "Summary.md", folder / "Summary.md")
    (folder / "Report.docx").unlink()
    turn["step"] = {"turn": turn["commit"], "applied": turn["changes"], "author": A, "trailers": saga, "main": picked["main"], "pickup": picked["commit"]}
    return one, turn, replaced


def test_what_a_landing_kept_is_forgotten_only_once_it_was_recorded(tmp_path, folder):
    one, turn, _ = a_landing_applied_and_not_recorded(tmp_path, folder)
    # Not recorded, and its files are in the folder: what it replaced is all its put-back has.
    with refused("landing_unsettled", "refused the request: this landing was neither recorded nor put back"):
        one.forget(saga="saga:1")
    recorded = one.record(**turn["step"])
    assert one.forget(saga="saga:1") == {"landing": recorded["commit"]}
    # And for good: wherever main is by then, and whatever the thread has pushed since.
    two = a_copy(tmp_path, folder, "t2")
    (two.copy / "B.md").write_text("B's own\n")
    land(two, "saga:2", B)
    (one.copy / "Draft.md").write_text("a failed turn's, kept\n")
    one.keep(author=A, trailers=[["Surogate-Kind", "turn"]], base=True)
    assert LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").forget(saga="saga:1") == {"landing": recorded["commit"]}
    # A saga the history holds neither a landing nor a turn of: it cannot tell what that one wrote.
    with refused("landing_unsettled", "refused the request: the history holds neither this landing nor its turn"):
        one.forget(saga="saga:none")
    # The landing's own pickup, which carries its saga on main too, is not its record.
    assert one.forget(saga="saga:2") == {"landing": git(tmp_path / "store" / "history.git", "rev-parse", "refs/heads/threads/t2")}


@pytest.mark.parametrize("kept_since", [False, True])
def test_what_a_landing_kept_is_forgotten_once_it_was_put_back_whole(tmp_path, folder, kept_since):
    one, turn, replaced = a_landing_applied_and_not_recorded(tmp_path, folder)
    # Put back in part: a file the landing wrote, and the one it deleted, are as it left them.
    (folder / "Summary.md").unlink()
    with refused("landing_unsettled"):
        one.forget(saga="saga:1")
    (folder / "notes.txt").write_bytes(replaced["notes.txt"])
    with refused("landing_unsettled"):
        one.forget(saga="saga:1")
    (folder / "Report.docx").write_bytes(replaced["Report.docx"])
    if kept_since:
        # The failed turn kept on its branch, which is no longer the landing's turn in the history.
        one.keep(author=A, trailers=[["Surogate-Kind", "turn"]], base=True)
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    assert again.forget(saga="saga:1") == {"landing": None}
    assert sorted(p.name for p in folder.iterdir()) == ["Report.docx", "notes.txt"]


def test_a_landing_is_not_taken_for_recorded_by_what_another_landings_trailer_holds(tmp_path, folder):
    one, _, _ = a_landing_applied_and_not_recorded(tmp_path, folder)
    two = a_copy(tmp_path, folder, "t2")
    (two.copy / "B.md").write_text("B's own\n")
    # Another thread's landing, a value of its trailers holding the first one's saga after a character some read as a line's end.
    said = [["Surogate-Saga", "saga:2"], ["Surogate-Title", "Draft B\u2028Surogate-Saga: saga:1\x0cSurogate-Saga: saga:1"]]
    picked = two.pickup(author=YOURS, trailers=said)
    turn = two.commit_turn(author=B, trailers=said, pickup=picked["commit"])
    shutil.copyfile(two.copy / "B.md", folder / "B.md")
    two.record(turn=turn["commit"], applied=turn["changes"], author=B, trailers=said, main=picked["main"], pickup=picked["commit"])
    with refused("landing_unsettled", "refused the request: this landing was neither recorded nor put back"):
        LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").forget(saga="saga:1")


def test_a_request_to_forget_is_one_the_agent_runs_and_names_a_saga(tmp_path, folder, tree):
    place = {"store": str(tmp_path / "store"), "folder": str(folder), "thread": THREAD, "user": "u1"}
    assert ask(tree, {**place, "action": "open", "args": {}}) == {"copy": "made"}
    for saga in (None, 7, "", ["saga:1"]):
        answer = ask(tree, {**place, "action": "forget", "args": {"saga": saga}})
        assert answer == {"error": {"code": "not_a_request", "message": "refused the request: it names no saga"}}, saga
    assert ask(tree, {**place, "action": "forget", "args": {"saga": "saga:1"}})["error"]["code"] == "landing_unsettled"


@pytest.mark.parametrize("act, why", [
    (lambda h: h.apply("notes.txt", None, None), "a folder's history writes no file of the folder"),
    (lambda h: h.unapply("notes.txt", None, None), "a folder's history writes no file of the folder"),
    (lambda h: h._put("notes.txt", "0" * 40), "a folder's history writes no file of the folder"),
    (lambda h: h._remove("notes.txt", []), "a folder's history writes no file of the folder"),
    (lambda h: h.pickup(author=YOURS, trailers=[], push=True), "a folder's history records no routine's run"),
    (lambda h: h.prune(keep=[], now=time.time()), "a folder's history is not pruned"),
    (lambda h: h.hand_off(author=A, trailers=[]), "a thread on a computer has no helper with a copy of its own"),
    (lambda h: h.hand_back(author=A, trailers=[]), "a thread on a computer has no helper with a copy of its own"),
    (lambda h: h.keep_apart(author=A, trailers=[]), "a thread on a computer has no helper with a copy of its own"),
    (lambda h: h.drop_hand_off(), "a thread on a computer has no helper with a copy of its own"),
    (lambda h: h.opened(), "a thread on a computer has no helper with a copy of its own"),
])
def test_what_a_folders_history_takes_no_part_in_is_refused_in_words_and_changes_nothing(tmp_path, folder, act, why):
    one = a_copy(tmp_path, folder)
    (one.copy / "notes.txt").write_text("the thread's notes\n")
    land(one, "saga:1")
    theirs, refs = files_of(folder), (tmp_path / "store" / "history.git" / "packed-refs").read_bytes()
    with refused("not_a_request", f"refused the request: {why}"):
        act(LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1"))
    assert (files_of(folder), (tmp_path / "store" / "history.git" / "packed-refs").read_bytes()) == (theirs, refs)


def test_a_folders_history_is_no_helpers_and_no_turns_and_holds_no_hand_off(tmp_path, folder):
    place = {"repo": tmp_path / "r", "project": folder, "copy": tmp_path / "c", "thread": "t1", "user": "u1", "store": tmp_path / "s"}
    for more in ({"helper": "h1"}, {"turn": "turn-1"}):
        with refused("not_a_request", "refused the request: a thread on a computer has no helper with a copy of its own"):
            LocalHistory(**place, **more)
    one = a_copy(tmp_path, folder)
    (one.copy / "notes.txt").write_text("the thread's notes\n")
    landed = land(one, "saga:1")
    # A hand-off in the folder's history is no ref a folder's history writes: nothing of it is taken up into a copy.
    packed = tmp_path / "store" / "history.git" / "packed-refs"
    packed.write_text(packed.read_text() + f"{landed['landing']} refs/handoff/t1\n")
    for ask in (lambda h: h.open(), lambda h: h.commit_turn(author=A, trailers=[["Surogate-Saga", "saga:2"]]), lambda h: h.take_up()):
        with refused("history_refused", "refused the project's history: it holds a hand-off, which a folder's history never does"):
            ask(LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1"))


@pytest.mark.parametrize("pushed", ["a turn", "a kept turn"])
def test_a_push_cut_before_the_repository_noted_it_does_not_keep_the_threads_next_turn_from_landing(tmp_path, folder, pushed):
    one = a_copy(tmp_path, folder)
    (one.copy / "notes.txt").write_text("the thread's notes\n")
    noted = git(one.repo, "for-each-ref", "refs/synced/")
    if pushed == "a turn":
        one.commit_turn(author=A, trailers=[["Surogate-Saga", "saga:1"]], pickup=None)
    else:
        one.keep(author=A, trailers=[["Surogate-Kind", "turn"]], base=True)
    # As the request cut right after its push leaves the repository: the branch is in the history, and not noted here.
    assert git(one.repo, "for-each-ref", "refs/synced/") != noted == ""
    git(one.repo, "update-ref", "-d", "refs/synced/t1")
    # Never asked again: the thread's next turn is another landing, and it lands.
    (one.copy / "more.md").write_text("the next turn's\n")
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    assert again.open() == {"copy": "kept"}
    assert [c["path"] for c in land(again, "saga:2")["changes"]] == ["more.md", "notes.txt"]


def test_a_branch_the_threads_repository_did_not_push_is_still_one_that_moved(tmp_path, folder):
    one, two = a_copy(tmp_path, folder, "t1"), a_copy(tmp_path, folder, "t2")
    (one.copy / "notes.txt").write_text("the thread's notes\n")
    (two.copy / "B.md").write_text("B's own\n")
    theirs = two.commit_turn(author=B, trailers=[["Surogate-Saga", "saga:2"]], pickup=None)["commit"]
    # The history names another's commit as this thread's branch: nothing this repository made.
    packed = tmp_path / "store" / "history.git" / "packed-refs"
    packed.write_text(packed.read_text() + f"{theirs} refs/heads/threads/t1\n")
    with refused("conflict", "refs/heads/threads/t1 moved in the project's history"):
        one.commit_turn(author=A, trailers=[["Surogate-Saga", "saga:1"]], pickup=None)


def test_a_repository_made_again_owes_its_copy_no_record_of_a_landing_made_before_it(tmp_path, folder):
    one, two = a_copy(tmp_path, folder, "t1"), a_copy(tmp_path, folder, "t2")
    (one.copy / "A.md").write_text("A's, landed\n")
    land(one, "saga:1")
    (two.copy / "B.md").write_text("B's, landed since\n")
    land(two, "saga:2", B)
    # The thread's repository and copy are gone, and made again from the history: at main, which has moved on.
    shutil.rmtree(one.repo)
    shutil.rmtree(one.copy)
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    assert again.open() == {"copy": "made"}
    # Its own landing is still where the history has its branch: the copy is not put back to it.
    assert again.changed() == {"paths": []}
    assert (again.copy / "B.md").read_text() == "B's, landed since\n"
    assert again.open() == {"copy": "moved"}
    assert not list((again.repo / "refs").glob("set-aside/*/*"))


#: One request, as its runner takes it, run to its end or killed at one of its steps: each git it
#: runs, and each file it writes or puts in its place.  Killed, every process of it goes at once.
STEPPED = """
import json, os, pathlib, signal, subprocess, sys
at, count = int(sys.argv[2]), [0]
def stepped(real):
    def step(*args, **kwargs):
        count[0] += 1
        if count[0] == at:
            os.killpg(0, signal.SIGKILL)
        return real(*args, **kwargs)
    return step
subprocess.run, os.replace = stepped(subprocess.run), stepped(os.replace)
pathlib.Path.write_bytes = stepped(pathlib.Path.write_bytes)
from surogates.sandbox import local_history
try:
    answer = local_history.run(json.loads(sys.argv[1]))
except local_history.HistoryError as refusal:
    answer = {"error": refusal.code}
print(json.dumps({"steps": count[0], "answer": answer}))
"""
OURS, THEIRS, NEW = THREAD, THREAD.replace("0b6c", "1b6c"), THREAD.replace("0b6c", "2b6c")
SAGA = [["Surogate-Saga", "saga:ours"]]


def stepped(root: Path, thread: str, action: str, args: dict, at: int = 0) -> dict | None:
    """*thread*'s request on the folder under *root*, killed at its step *at*; its steps and answer when it ran to its end."""
    request = {"store": str(root / "store"), "folder": str(root / "Documents"), "thread": thread, "user": "u1", "action": action, "args": args}
    ran = subprocess.run(
        [sys.executable, "-c", STEPPED, json.dumps(request), str(at)], capture_output=True, text=True,
        cwd=Path(__file__).parents[1], start_new_session=True, timeout=300,
    )
    if at:
        assert ran.returncode == -signal.SIGKILL, (at, ran.returncode, ran.stdout, ran.stderr)
        return None
    assert ran.returncode == 0, ran.stderr
    return json.loads(ran.stdout)


def a_folder_two_threads_work_on(root: Path) -> tuple[LocalHistory, dict]:
    """A folder under *root* with our thread's turn about to land, and what the folder must hold whatever is cut.

    Another thread has landed a change to the report and a new file, and you have saved a file since.
    Our thread changed the report too, on the older one; changed the notes; made a file; and deleted one,
    which waits with the report: a deletion beside a write that is left out may be a move.
    """
    folder = root / "Documents"
    (folder / "sub").mkdir(parents=True)
    for name, text in (("Report.docx", "report v1"), ("notes.txt", "v1 notes"), ("old.txt", "to be deleted"), ("sub/deep.txt", "deep")):
        (folder / name).write_text(f"{text}\n")
        os.utime(folder / name, (time.time() - 60, time.time() - 60))
    ours, theirs = a_copy(root, folder, OURS), a_copy(root, folder, THEIRS)
    (theirs.copy / "Report.docx").write_text("their report\n")
    (theirs.copy / "A-new.md").write_text("their new file\n")
    land(theirs, "saga:theirs")
    (folder / "yours.txt").write_text("saved by you since\n")
    (ours.copy / "Report.docx").write_text("our report, made on the old one\n")
    (ours.copy / "notes.txt").write_text("our notes\n")
    (ours.copy / "B.md").write_text("our own\n")
    (ours.copy / "old.txt").unlink()
    # Whatever is cut: no file of yours or of the other thread's is gone or written over.
    return ours, {
        "Report.docx": b"their report\n", "A-new.md": b"their new file\n", "yours.txt": b"saved by you since\n",
        "sub/deep.txt": b"deep\n", "old.txt": b"to be deleted\n",
    }


def applied(folder: Path, copy: Path, changes: list[dict]) -> None:
    """The host's applies, a copy of each file."""
    for change in changes:
        if change["after"] is None:
            (folder / change["path"]).unlink(missing_ok=True)
        else:
            shutil.copyfile(copy / change["path"], folder / change["path"])


def the_next_turn_lands_whole(root: Path, kept: dict, *, landed: bool, new: str = NEW) -> None:
    """After a cut: the thread's next turn opens and lands, and the folder holds every file it had, each with its bytes.

    *kept* are your files and the other thread's.  The history is whole: its objects are all there, a
    new thread's first copy is the folder's files, and no landing after the cut deletes or writes
    anything but our thread's own work.  With *landed*, our work reached the folder before the cut.
    """
    folder, store = root / "Documents", root / "store" / "history.git"
    ours = LocalHistory.at(root / "store", folder, thread=OURS, user="u1")
    opened = ours.open()
    assert opened["copy"] in ("made", "moved", "kept"), opened
    after = land(ours, "saga:next", B)
    assert {(c["path"], c["after"] is None) for c in after["changes"]} <= {("notes.txt", False), ("B.md", False)}, after
    now = dict(files_of(folder))
    assert {name: now.get(name) for name in kept} == kept
    if landed or opened["copy"] != "made":
        # The thread's own work is in the folder: landed before the cut, or by this turn.
        assert (now["notes.txt"], now["B.md"]) == (b"our notes\n", b"our own\n"), (opened, now)
    if store.exists():
        assert git(store, "fsck", "--no-dangling") == ""
    fresh = LocalHistory.at(root / "store", folder, thread=new, user="u1")
    assert fresh.open() == {"copy": "made"}
    assert files_of(fresh.copy) == files_of(folder)


def each_cut(tmp_path: Path, thread: str, action: str, args: dict):
    """The folder under ``tmp_path/whole`` copied for each step of *thread*'s request, and the request killed at that step."""
    whole = tmp_path / "whole"
    shutil.copytree(whole, tmp_path / "counted", symlinks=True)
    steps = stepped(tmp_path / "counted", thread, action, args)["steps"]
    assert steps > 1
    for at in range(1, steps + 1):
        root = tmp_path / f"cut-{at}"
        shutil.copytree(whole, root, symlinks=True)
        before = files_of(root / "Documents")
        stepped(root, thread, action, args, at)
        # The history writes no file of the folder, at any step.
        assert files_of(root / "Documents") == before, at
        yield at, root


def test_a_first_open_killed_at_any_step_leaves_the_folder_as_it_was_and_the_history_whole(tmp_path):
    _, kept = a_folder_two_threads_work_on(tmp_path / "whole")
    for _at, root in each_cut(tmp_path, NEW, "open", {}):
        again = LocalHistory.at(root / "store", root / "Documents", thread=NEW, user="u1")
        assert again.open() == {"copy": "made"}
        assert files_of(again.copy) == files_of(root / "Documents")
        assert land(again, "saga:new")["commit"] is None
        the_next_turn_lands_whole(root, kept, landed=False, new=THREAD.replace("0b6c", "3b6c"))


@pytest.mark.parametrize("action", ["open", "changed", "pickup", "commit", "keep"])
def test_a_request_killed_at_any_step_before_a_landing_applies_leaves_the_folder_as_it_was_and_the_history_whole(tmp_path, action):
    ours, kept = a_folder_two_threads_work_on(tmp_path / "whole")
    args = {
        "open": {}, "changed": {}, "pickup": {"author": YOURS, "trailers": SAGA},
        "commit": {"author": B, "trailers": SAGA, "pickup": None}, "keep": {"author": B, "trailers": SAGA, "base": True},
    }[action]
    if action == "commit":
        args["pickup"] = ours.pickup(author=YOURS, trailers=SAGA)["commit"]
    for _at, root in each_cut(tmp_path, OURS, action, args):
        the_next_turn_lands_whole(root, kept, landed=False)


def test_a_landing_stopped_after_any_of_its_applies_deletes_and_overwrites_nothing_of_anyone_elses(tmp_path):
    ours, kept = a_folder_two_threads_work_on(tmp_path / "whole")
    picked = ours.pickup(author=YOURS, trailers=SAGA)
    turn = ours.commit_turn(author=B, trailers=SAGA, pickup=picked["commit"])
    assert [c["path"] for c in turn["changes"]] == ["B.md", "notes.txt"]
    assert [(o["path"], o["reason"]) for o in turn["overlapped"]] == [("Report.docx", "changed"), ("old.txt", "with")]
    store = tmp_path / "whole" / "store" / "history.git"
    for done in range(len(turn["changes"]) + 1):
        root = tmp_path / f"applied-{done}"
        shutil.copytree(tmp_path / "whole", root, symlinks=True)
        # What each apply replaces or removes is a version the history holds already: the turn's base was pushed first.
        for change in turn["changes"][:done]:
            if change["before"] is not None:
                assert git(store, "cat-file", "-t", change["before"]) == "blob"
        applied(root / "Documents", root / "store" / "threads" / OURS, turn["changes"][:done])
        # Never recorded and never asked again: what it kept is not to be forgotten, unless it wrote nothing.
        again = LocalHistory.at(root / "store", root / "Documents", thread=OURS, user="u1")
        if done:
            with refused("landing_unsettled"):
                again.forget(saga="saga:ours")
        else:
            assert again.forget(saga="saga:ours") == {"landing": None}
        the_next_turn_lands_whole(root, kept, landed=False)


@pytest.mark.parametrize("asked_again", [False, True])
def test_a_record_killed_at_any_step_is_finished_or_never_was_and_the_next_landing_takes_nothing_of_anyone_elses(tmp_path, asked_again):
    ours, kept = a_folder_two_threads_work_on(tmp_path / "whole")
    picked = ours.pickup(author=YOURS, trailers=SAGA)
    turn = ours.commit_turn(author=B, trailers=SAGA, pickup=picked["commit"])
    applied(tmp_path / "whole" / "Documents", ours.copy, turn["changes"])
    # A command still running writes the copy after the turn was committed.
    (ours.copy / "late.md").write_text("written after the turn was committed\n")
    step = {"turn": turn["commit"], "applied": turn["changes"], "author": B, "trailers": SAGA, "main": picked["main"], "pickup": picked["commit"]}
    pushed = 0
    for at, root in each_cut(tmp_path, OURS, "record", step):
        store, folder = root / "store" / "history.git", root / "Documents"
        again = LocalHistory.at(root / "store", folder, thread=OURS, user="u1")
        landing = git(store, "rev-parse", "refs/heads/main")
        recorded = "Surogate-Saga: saga:ours" in git(store, "log", "-1", "--format=%B", landing) and git(store, "log", "-1", "--format=%s", landing) == "Landing"
        pushed += recorded
        if asked_again:
            answer = again.record(**step)
            assert re.fullmatch(r"[0-9a-f]{40}", answer["set_aside"]), at
            assert git(again.repo, "cat-file", "-p", f"{answer['set_aside']}:late.md") == "written after the turn was committed"
            assert again.forget(saga="saga:ours") == {"landing": answer["commit"]}
        elif recorded:
            # The push counted: the thread's next open finishes it, and says what it set aside.
            finished = again.open()["finished"]
            assert finished["landing"] == landing, at
            assert git(again.repo, "cat-file", "-p", f"{finished['set_aside']}:late.md") == "written after the turn was committed"
        if asked_again or recorded:
            # The copy is the landing's files: the newer report, and no file the thread wrote since.
            assert dict(files_of(again.copy)) == {**dict(files_of(folder))}, at
            (again.copy / "late.md").unlink(missing_ok=True)
        else:
            # It never counted: the landing is as it was before its record, and its kept files are not forgotten.
            with refused("landing_unsettled"):
                again.forget(saga="saga:ours")
            (root / "store" / "threads" / OURS / "late.md").unlink()
        the_next_turn_lands_whole(root, kept, landed=True)
    # The cuts fall on both sides of the push.
    assert 0 < pushed < at


def test_a_forget_killed_at_any_step_changes_nothing_and_answers_the_same_when_asked_again(tmp_path):
    ours, kept = a_folder_two_threads_work_on(tmp_path / "whole")
    landed = land(ours, "saga:ours", B)
    for at, root in each_cut(tmp_path, OURS, "forget", {"saga": "saga:ours"}):
        again = LocalHistory.at(root / "store", root / "Documents", thread=OURS, user="u1")
        assert again.forget(saga="saga:ours") == {"landing": landed["landing"]}, at
    the_next_turn_lands_whole(root, kept, landed=True)
