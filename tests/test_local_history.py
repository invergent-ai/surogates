"""A folder's history and a thread's copy on the user's computer, as git in the desktop's VM runs them."""

from __future__ import annotations

import calendar
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


def as_it_is(top: Path) -> list[tuple[str, int, int, bytes]]:
    """Every name under *top* with its mode, its time and what it holds, no link followed: a request that
    leaves *top* alone leaves all of it the same, and a folder renamed whole takes all of it along."""
    seen = []
    for at, folders, files in os.walk(top):
        for name in (*folders, *files):
            path = Path(at, name)
            info = path.lstat()
            held = os.readlink(path).encode() if path.is_symlink() else path.read_bytes() if path.is_file() else b""
            seen.append((str(path.relative_to(top)), info.st_mode, info.st_mtime_ns, held))
    return sorted(seen)


def set_aside_whole(place: Path) -> list[str]:
    """Every name in the folder where *place* keeps what an open set aside whole."""
    return sorted(p.name for p in (place / "set-aside").iterdir()) if os.path.lexists(place / "set-aside") else []


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
    # The repository itself a link is no repository of the thread's: there is none to keep.
    held, kept = as_it_is(one.copy), None if linked == "." else as_it_is(one.repo)
    # Whatever request comes first runs no git in it: the repository is set aside as it is, and the copy is its open's.
    with refused("no_whole_copy"):
        LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").snapshot("before a step")
    assert [name.rpartition(".")[2] for name in set_aside_whole(tmp_path / "store")] == ([] if kept is None else ["repository"])
    assert as_it_is(one.copy) == held
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    # The repository is made again from the folder's history, and its copy with it.  What the thread had not
    # committed for a landing is set aside with the copy, whole, and the open names both.
    opened = again.open()
    assert set(opened) == {"copy", "set_aside_folders"} and opened["copy"] == "made", opened
    assert opened["set_aside_folders"] == set_aside_whole(tmp_path / "store")
    assert [name.rpartition(".")[2] for name in opened["set_aside_folders"]] == (["copy"] if kept is None else ["repository", "copy"])
    aside = tmp_path / "store" / "set-aside"
    assert as_it_is(aside / opened["set_aside_folders"][-1]) == held
    assert kept is None or as_it_is(aside / opened["set_aside_folders"][0]) == kept
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
    held, kept = as_it_is(one.copy), as_it_is(one.repo)
    # The next boot's guest: whatever request comes first runs no git in such a repository.
    with refused("no_whole_copy"):
        LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").changed()
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    # It is set aside as it was left, what was put among its objects with it, and the thread's copy beside it.
    opened = again.open()
    assert set(opened) == {"copy", "set_aside_folders"} and opened["copy"] == "made", opened
    repository, copy = (tmp_path / "store" / "set-aside" / name for name in opened["set_aside_folders"])
    assert (repository.suffix, copy.suffix) == (".repository", ".copy")
    assert (as_it_is(repository), as_it_is(copy)) == (kept, held)
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
    # What the cut left holds some of the folder's files and nothing of the thread's own: none of it is kept.
    assert again.open() == {"copy": "made"}
    assert set_aside_whole(tmp_path / "store") == []
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
    # The repository was whole: only the copy is made again, and the thread's snapshots are all there.  The
    # half that was there held one of its branch's files and no more, so none of it is kept.
    assert again.open() == {"copy": "kept"}
    assert set_aside_whole(tmp_path / "store") == []
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
    # And it is named by the git that failed, whatever options it was run with: a file it may not read.
    (one.copy / "locked.txt").write_text("not to be read\n")
    (one.copy / "locked.txt").chmod(0)
    with refused("failed", "^git add failed: "):
        one.snapshot("before a step")


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
    ({"action": "fetch", "args": {"saga": "s1", "since": "--not"}}, "its since holds what is not a commit id"),
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
    # The open finishes the record first: the copy is the landing's files, the newer report among them.
    assert again.open() == {"copy": "moved"}
    assert git(again.repo, "rev-parse", "refs/landed/t2") == cut["landing"]
    assert (again.copy / "Report.docx").read_bytes() == b"PK\x03\x04 A's report"
    assert (again.copy / "A-new.md").read_text() == "A's new file\n"
    assert again.changed() == {"paths": []}
    # Its next landing deletes no file another thread landed, and writes no old version over another's change.
    after = land(again, "saga:3", B)
    assert (after["commit"], after["changes"], after["overlapped"]) == (None, [], [])
    assert files_of(folder) == theirs
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
    # Nothing of the turn's was set aside for it, and the thread's next open has nothing left to finish.
    assert git(again.repo, "rev-parse", "refs/landed/t2") == cut["landing"]
    assert LocalHistory.at(tmp_path / "store", folder, thread="t2", user="u1").open() == {"copy": "moved"}


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


def test_a_record_of_another_saga_is_not_answered_with_the_threads_last_landing(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    (one.copy / "notes.txt").write_text("the thread's notes\n")
    landed = land(one, "saga:1")
    other = [["Surogate-Saga", "saga:9"]]
    with refused("conflict", "main moved in the project's history since the landing began"):
        one.record(turn=landed["commit"], applied=[], author=A, trailers=other, main=None, pickup=None)


def test_a_branch_on_its_base_that_is_no_landing_is_none_to_finish(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    (one.copy / "notes.txt").write_text("the thread's notes\n")
    turn = one.commit_turn(author=A, trailers=[["Surogate-Saga", "saga:1"]], pickup=None)["commit"]
    # A history no request wrote: the thread's base is its turn, which has one parent and is no landing.
    packed = tmp_path / "store" / "history.git" / "packed-refs"
    packed.write_text("".join(
        f"{turn} refs/bases/t1\n" if line.endswith(" refs/bases/t1\n") else line for line in packed.read_text().splitlines(keepends=True)
    ))
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    assert again.changed() == {"paths": ["notes.txt"]}
    assert (again.copy / "notes.txt").read_text() == "the thread's notes\n"
    assert again.open() == {"copy": "kept"}


def test_a_record_cut_part_way_through_making_the_copy_the_landings_is_finished_whole(tmp_path, folder):
    again, cut = a_record_cut_after_its_push(tmp_path, folder)
    # As a reset killed after its first file leaves the copy: one file the landing's, the rest and the index the turn's.
    (again.copy / "Report.docx").write_bytes(b"PK\x03\x04 A's report")
    # Each file it holds is the turn's or the landing's: nothing of the thread's is set aside.
    assert again.open() == {"copy": "moved"}
    assert git(again.repo, "rev-parse", "refs/landed/t2") == cut["landing"]
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
    # The copy is the landing's files, and what it held is a snapshot the thread's repository keeps.
    aside = recorded["set_aside"]
    assert re.fullmatch(r"[0-9a-f]{40}", aside)
    assert sorted(p.name for p in two.copy.iterdir()) == ["Report.docx", "notes.txt"]
    assert (two.copy / "notes.txt").read_text() == "v1 notes\n"
    assert git(two.repo, "for-each-ref", "--format=%(objectname) %(refname)", "refs/set-aside/") == (
        f"{aside} refs/set-aside/t2/00000001-{turn['commit']}"
    )
    assert git(two.repo, "cat-file", "-p", f"{aside}:late.md") == "written after the turn was committed"
    assert git(two.repo, "cat-file", "-p", f"{aside}:notes.txt") == "changed after the turn was committed"
    # It is the copy as it was on its turn: what differs from its first parent is what was written since.
    assert git(two.repo, "rev-parse", f"{aside}^1") == turn["commit"]
    assert git(two.repo, "diff", "--name-only", f"{aside}^1", aside).split() == ["late.md", "notes.txt"]
    # Packed again, the repository still holds it.
    git(two.repo, "repack", "-a", "-d", "-q")
    git(two.repo, "prune", "--expire=now")
    assert git(two.repo, "cat-file", "-t", aside) == "commit"
    # The copy is not put back to it: its base has moved since, and the other thread's report would go with it.
    held = files_of(two.copy)
    with refused("not_on_base", "refused the request: this snapshot is not built on the copy's base as it stands"):
        two.restore(aside)
    assert files_of(two.copy) == held
    # Asked again the record says the same, and each open of the thread's names what its repository holds set aside.
    again = LocalHistory.at(tmp_path / "store", folder, thread="t2", user="u1")
    assert again.record(turn=turn["commit"], applied=turn["changes"], author=B, trailers=saga, main=picked["main"], pickup=picked["commit"]) == recorded
    assert again.open() == {"copy": "moved", "set_asides": [aside]}
    assert LocalHistory.at(tmp_path / "store", folder, thread="t2", user="u1").open() == {"copy": "moved", "set_asides": [aside]}


def test_what_was_set_aside_for_a_turn_stays_under_what_a_later_try_sets_aside_for_it(tmp_path, folder):
    one, two = a_copy(tmp_path, folder, "t1"), a_copy(tmp_path, folder, "t2")
    (one.copy / "Report.docx").write_bytes(b"PK\x03\x04 A's report")
    (two.copy / "Report.docx").write_bytes(b"PK\x03\x04 B's report")
    land(one, "saga:1")
    saga = [["Surogate-Saga", "saga:2"]]
    picked = two.pickup(author=YOURS, trailers=saga)
    turn = two.commit_turn(author=B, trailers=saga, pickup=picked["commit"])
    (two.copy / "late.md").write_text("written after the turn was committed\n")
    step = {"turn": turn["commit"], "applied": turn["changes"], "author": B, "trailers": saga, "main": picked["main"], "pickup": picked["commit"]}
    first = two.record(**step)["set_aside"]
    # As a try cut part way through the copy's files leaves it, the copy written again since: not noted as
    # done, its index still the turn's.  The next act finishes it once more.
    git(two.repo, "update-ref", "-d", "refs/landed/t2")
    git(two.repo / "worktrees" / "t2", "read-tree", turn["commit"])
    (two.copy / "later.md").write_text("written since\n")
    assert two.changed() == {"paths": []}
    [again] = two.open()["set_asides"]
    # One ref for the turn, and the first snapshot under the second: neither file is lost.
    assert git(two.repo, "for-each-ref", "--format=%(objectname)", "refs/set-aside/") == again != first
    assert git(two.repo, "rev-parse", f"{again}^2") == first
    assert git(two.repo, "cat-file", "-p", f"{again}:later.md") == "written since"
    assert git(two.repo, "cat-file", "-p", f"{again}^2:late.md") == "written after the turn was committed"


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
    monkeypatch.setattr(local_history, "_ASIDE", 4)
    one, two = a_copy(tmp_path, folder, "t1"), a_copy(tmp_path, folder, "t2")
    assert local_history.LocalHistory is LocalHistory
    kept = []
    for n in range(5):
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
    # The oldest goes for one more, by the count in each one's name.
    left = dict(line.split(" refs/set-aside/t2/") for line in git(two.repo, "for-each-ref", "--format=%(objectname) %(refname)", "refs/set-aside/").splitlines())
    assert (list(left), [name[:9] for name in left.values()]) == (kept[1:], ["00000002-", "00000003-", "00000004-", "00000005-"])
    # And an open names them as they were set aside, the oldest first.
    assert two.open() == {"copy": "moved", "set_asides": kept[1:]}


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
    assert opened == {"copy": "moved"}
    assert git(again.repo, "rev-parse", "refs/landed/t2") == cut["landing"]
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


def test_what_a_recorded_landing_kept_is_forgotten_wherever_main_is_by_then(tmp_path, folder):
    one, turn, _ = a_landing_applied_and_not_recorded(tmp_path, folder)
    wrote = turn["step"]["applied"]
    # Not recorded, and its files are in the folder: what it replaced is all its put-back has.
    with refused("landing_unsettled", "refused the request: this landing was neither recorded nor put back whole"):
        one.forget(saga="saga:1", applied=wrote)
    recorded = one.record(**turn["step"])
    assert one.forget(saga="saga:1", applied=wrote) == {"landing": recorded["commit"]}
    # And for good: wherever main is by then, and whatever the thread has pushed since.
    two = a_copy(tmp_path, folder, "t2")
    (two.copy / "B.md").write_text("B's own\n")
    land(two, "saga:2", B)
    (one.copy / "Draft.md").write_text("a failed turn's, kept\n")
    one.keep(author=A, trailers=[["Surogate-Kind", "turn"]], base=True)
    assert LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").forget(saga="saga:1", applied=wrote) == {"landing": recorded["commit"]}
    # Any thread's landing, by its saga.
    assert one.forget(saga="saga:2", applied=[]) == {"landing": git(tmp_path / "store" / "history.git", "rev-parse", "refs/heads/threads/t2")}
    # A landing that applied nothing kept nothing.
    assert one.forget(saga="saga:none", applied=[]) == {"landing": None}


def test_what_a_landing_kept_is_forgotten_once_each_file_it_applied_is_as_it_was_before(tmp_path, folder):
    one, turn, replaced = a_landing_applied_and_not_recorded(tmp_path, folder)
    wrote = turn["step"]["applied"]
    # Put back in part: a file the landing wrote, and the one it deleted, are as it left them.
    (folder / "Summary.md").unlink()
    with refused("landing_unsettled"):
        one.forget(saga="saga:1", applied=wrote)
    (folder / "notes.txt").write_bytes(replaced["notes.txt"])
    with refused("landing_unsettled"):
        one.forget(saga="saga:1", applied=wrote)
    (folder / "Report.docx").write_bytes(replaced["Report.docx"])
    # Whole.  It goes by the folder and by what it is told was applied: the thread's own repository is not read, nor made.
    shutil.rmtree(one.repo)
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    assert again.forget(saga="saga:1", applied=wrote) == {"landing": None}
    assert not one.repo.exists()
    assert sorted(p.name for p in folder.iterdir()) == ["Report.docx", "notes.txt"]


@pytest.mark.parametrize("left", [
    "a file you changed after the landing wrote it", "a link where the landing's file was",
    "the first try's file, its commit step tried again after a late write",
    "a link where the file's folder was", "a folder where the file the landing made was",
    "a link where the file the landing made was",
])
def test_a_put_back_that_is_not_whole_is_not_taken_for_one(tmp_path, folder, left):
    one, turn, replaced = a_landing_applied_and_not_recorded(tmp_path, folder)
    wrote = turn["step"]["applied"]
    # The put-back takes away the file the landing made and gives back the one it deleted; the third it could not.
    (folder / "Summary.md").unlink()
    (folder / "Report.docx").write_bytes(replaced["Report.docx"])
    if left.startswith("a link where the file's folder"):
        # Every file is as it was, read through a link to a folder that holds them: the folder's own are not.
        (folder / "notes.txt").write_bytes(replaced["notes.txt"])
        wrote = [{**change, "path": f"sub/{change['path']}"} for change in wrote]
        (folder / "sub").symlink_to(folder)
    elif left.startswith("a folder"):
        (folder / "notes.txt").write_bytes(replaced["notes.txt"])
        (folder / "Summary.md").mkdir()
    elif left.startswith("a link where the file the landing made"):
        # To nothing: no file is there, as before the landing, and a name is.
        (folder / "notes.txt").write_bytes(replaced["notes.txt"])
        (folder / "Summary.md").symlink_to(folder / "none")
    elif left.startswith("a file you changed"):
        # Neither the landing's nor what was there before it: the put-back leaves it, and keeps what the landing replaced.
        (folder / "notes.txt").write_text("the thread's notes\nand a line of yours, after the landing\n")
    elif left.startswith("a link"):
        # To a file that holds what was there before: no file of the folder's is.
        (tmp_path / "elsewhere.txt").write_bytes(replaced["notes.txt"])
        (folder / "notes.txt").unlink()
        (folder / "notes.txt").symlink_to(tmp_path / "elsewhere.txt")
    else:
        (one.copy / "notes.txt").write_text("the thread's notes, written late\n")
        again = one.commit_turn(author=A, trailers=[["Surogate-Saga", "saga:1"], ["Surogate-Kind", "turn"]], pickup=turn["step"]["pickup"])
        assert again["commit"] != turn["commit"] and (folder / "notes.txt").read_text() == "the thread's notes\n"
    with refused("landing_unsettled", "refused the request: this landing was neither recorded nor put back whole"):
        LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").forget(saga="saga:1", applied=wrote)


def test_a_landing_is_not_taken_for_recorded_by_what_another_landings_trailer_holds(tmp_path, folder):
    _, mine, _ = a_landing_applied_and_not_recorded(tmp_path, folder)
    two = a_copy(tmp_path, folder, "t2")
    (two.copy / "B.md").write_text("B's own\n")
    # Another thread's landing, a value of its trailers holding the first one's saga after a character some read as a line's end.
    said = [["Surogate-Saga", "saga:2"], ["Surogate-Title", "Draft B\u2028Surogate-Saga: saga:1\x0cSurogate-Saga: saga:1"]]
    picked = two.pickup(author=YOURS, trailers=said)
    turn = two.commit_turn(author=B, trailers=said, pickup=picked["commit"])
    shutil.copyfile(two.copy / "B.md", folder / "B.md")
    two.record(turn=turn["commit"], applied=turn["changes"], author=B, trailers=said, main=picked["main"], pickup=picked["commit"])
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    with refused("landing_unsettled", "refused the request: this landing was neither recorded nor put back whole"):
        again.forget(saga="saga:1", applied=mine["step"]["applied"])
    assert again.forget(saga="saga:1", applied=[]) == {"landing": None}
    # Nor is that landing, where its own thread's branch has it, the record of the first one's saga.
    with refused("conflict", "main moved in the project's history since the landing began"):
        two.record(turn=turn["commit"], applied=[], author=B, trailers=[["Surogate-Saga", "saga:1"]], main=None, pickup=None)
    # Nor is main's tip the first one's landing, to its record.
    with refused("conflict", "main moved in the project's history since the landing began"):
        again.record(**mine["step"])


def test_a_request_to_forget_is_one_the_agent_runs_and_names_a_saga(tmp_path, folder, tree):
    place = {"store": str(tmp_path / "store"), "folder": str(folder), "thread": THREAD, "user": "u1"}
    assert ask(tree, {**place, "action": "open", "args": {}}) == {"copy": "made"}
    for saga in (None, 7, "", ["saga:1"]):
        answer = ask(tree, {**place, "action": "forget", "args": {"saga": saga, "applied": []}})
        assert answer == {"error": {"code": "not_a_request", "message": "refused the request: it names no saga"}}, saga
    for args in ({}, {"applied": None}, {"applied": "notes.txt"}, {"applied": {"path": "notes.txt"}}):
        answer = ask(tree, {**place, "action": "forget", "args": {"saga": "saga:1", **args}})
        assert answer == {"error": {"code": "not_a_request", "message": "refused the request: it names no files a landing applied"}}, args
    for path in ("/etc/passwd", "../notes.txt", "sub//notes.txt", "sub/./notes.txt", "", "."):
        answer = ask(tree, {**place, "action": "forget", "args": {"saga": "saga:1", "applied": [{"path": path, "before": None, "after": None}]}})
        assert answer == {"error": {"code": "not_a_request", "message": "refused the request: a file it applied has no path in the folder"}}, path
    # What was there before each file is said, None for nothing: left unsaid it is not taken for nothing.
    answer = ask(tree, {**place, "action": "forget", "args": {"saga": "saga:1", "applied": [{"path": "none.txt", "after": None}]}})
    assert answer == {"error": {"code": "not_a_request", "message": "refused the request: a file it applied has no version from before it"}}
    wrote = [{"path": "notes.txt", "before": "0" * 40, "after": None}]
    assert ask(tree, {**place, "action": "forget", "args": {"saga": "saga:1", "applied": wrote}})["error"]["code"] == "landing_unsettled"
    assert ask(tree, {**place, "action": "forget", "args": {"saga": "saga:1", "applied": []}}) == {"landing": None}


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


@pytest.mark.parametrize("moved_by", ["a landing recorded whole", "a landing whose record was cut after its push", "an open's move to main"])
def test_a_copy_is_not_put_back_to_a_snapshot_from_before_its_base_moved(tmp_path, folder, moved_by):
    one, two = a_copy(tmp_path, folder, "t1"), a_copy(tmp_path, folder, "t2")
    # What a Stop puts the copy back to: the snapshot before the turn's first step.
    before_the_turn = one.snapshot("before a step")
    (two.copy / "Report.docx").write_bytes(b"PK\x03\x04 B's report")
    (two.copy / "B.md").write_text("B's own\n")
    land(two, "saga:2", B)
    (folder / "yours.txt").write_text("saved by you since\n")
    if moved_by == "an open's move to main":
        assert one.open() == {"copy": "moved"}
    else:
        (one.copy / "notes.txt").write_text("the thread's notes\n")
        saga = [["Surogate-Saga", "saga:1"]]
        picked = one.pickup(author=YOURS, trailers=saga)
        turn = one.commit_turn(author=A, trailers=saga, pickup=picked["commit"])
        shutil.copyfile(one.copy / "notes.txt", folder / "notes.txt")
        step = {"turn": turn["commit"], "applied": turn["changes"], "author": A, "trailers": saga, "main": picked["main"], "pickup": picked["commit"]}
        (one.record if moved_by == "a landing recorded whole" else lambda **cut: History.record(one, **cut))(**step)
    theirs = files_of(folder)
    # The put-back arrives after all that.  The copy as it was then, on the base as it is now, would be the
    # thread deleting the other's file and writing its own old versions over the other's and yours.
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    with refused("not_on_base", "refused the request: this snapshot is not built on the copy's base as it stands"):
        again.restore(before_the_turn)
    assert files_of(again.copy) == theirs
    assert again.changed() == {"paths": []}
    after = land(again, "saga:3")
    assert (after["commit"], after["changes"], after["overlapped"]) == (None, [], [])
    assert files_of(folder) == theirs
    # A snapshot of the stretch the copy is on now is one it is put back to.
    here = again.snapshot("before a step")
    (again.copy / "made.txt").write_text("made by the turn\n")
    again.restore(here)
    assert files_of(again.copy) == theirs


def test_what_a_cut_record_set_aside_is_named_by_every_open_whatever_the_thread_did_between(tmp_path, folder):
    again, _ = a_record_cut_after_its_push(tmp_path, folder)
    # A command still running wrote the copy after the turn was committed.
    (again.copy / "late.md").write_text("written after the turn was committed\n")
    # No open comes first: the snapshot before the next turn's first step finishes the record, and the turn lands.
    again.snapshot("before a step")
    assert not (again.copy / "late.md").exists()
    (again.copy / "next.md").write_text("the next turn's\n")
    assert [c["path"] for c in land(again, "saga:3", B)["changes"]] == ["next.md"]
    # The file is out of the copy and on a ref, and no answer has named it yet: each open does, asked again or not.
    opened = LocalHistory.at(tmp_path / "store", folder, thread="t2", user="u1").open()
    [aside] = opened["set_asides"]
    assert opened == {"copy": "moved", "set_asides": [aside]}
    assert git(again.repo, "cat-file", "-p", f"{aside}:late.md") == "written after the turn was committed"
    assert LocalHistory.at(tmp_path / "store", folder, thread="t2", user="u1").open() == opened


def a_fetch_cut_before_its_cut_was_recorded(tmp_path: Path, folder: Path) -> tuple[LocalHistory, str]:
    """A thread's repository as a fetch of ``main`` killed part way leaves it, and that ``main``: the commit is
    there, its parents are not, and ``shallow`` does not say its history is cut there."""
    one, two = a_copy(tmp_path, folder, "t1"), a_copy(tmp_path, folder, "t2")
    (two.copy / "B.md").write_text("B's own\n")
    main = land(two, "saga:2", B)["landing"]
    subprocess.run(
        ["git", f"--git-dir={one.repo}", "fetch", "-q", "--depth", "1", "--no-tags", "--no-write-fetch-head", "--", str(tmp_path / "store" / "history.git"), main],
        check=True, env=HERMETIC,
    )
    shallow = one.repo / "shallow"
    shallow.write_text("".join(line for line in shallow.read_text().splitlines(keepends=True) if line.strip() != main))
    with pytest.raises(subprocess.CalledProcessError):
        git(one.repo, "rev-list", "-n", "1", main)
    return one, main


def test_a_commit_a_cut_fetch_left_with_no_parents_and_no_cut_is_fetched_again(tmp_path, folder):
    one, main = a_fetch_cut_before_its_cut_was_recorded(tmp_path, folder)
    # The next open takes main: the commit is in the repository, and is not taken for fetched.
    assert LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").open() == {"copy": "moved"}
    assert main in (one.repo / "shallow").read_text().split()
    assert git(one.repo, "fsck", "--no-dangling") == ""
    assert (one.copy / "B.md").read_text() == "B's own\n"


def test_a_thread_whose_repository_cannot_be_packed_again_still_opens(tmp_path, folder, monkeypatch, caplog):
    monkeypatch.setattr(local_history, "_PACKS", 1)
    one, broken = a_fetch_cut_before_its_cut_was_recorded(tmp_path, folder)
    # A ref of the repository's names the commit git cannot walk from, and no fetch of this open's brings it whole:
    # main has moved on from it.
    git(one.repo, "update-ref", "refs/heads/kept", broken)
    two = LocalHistory.at(tmp_path / "store", folder, thread="t2", user="u1")
    (two.copy / "B2.md").write_text("more of B's\n")
    land(two, "saga:3", B)
    for n in range(3):
        (one.copy / "notes.txt").write_text(f"edit {n}\n")
        one.snapshot("before a step")
    assert len(list((one.repo / "objects" / "pack").glob("*.pack"))) > 1
    # Packing is upkeep: the thread's turn starts all the same, on its unlanded work, and lands.
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    assert again.open() == {"copy": "kept"}
    assert "could not be packed again" in caplog.text
    assert [c["path"] for c in land(again, "saga:4")["changes"]] == ["notes.txt"]


def test_a_repository_whose_first_open_did_not_end_is_set_aside_whole_with_its_copy_and_named_by_every_open(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    # The thread's work, none of it in the folder's history: a snapshot, and since it an edit, a new file
    # in a new folder, and a file history leaves out.
    (one.copy / "Draft.md").write_text("in a snapshot\n")
    LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").snapshot("before a step")
    (one.copy / "notes.txt").write_text("the thread's notes, in no snapshot\n")
    (one.copy / "sub").mkdir()
    (one.copy / "sub" / "new.md").write_text("new, in no snapshot\n")
    (one.copy / "scratch.tmp").write_text("left out of history\n")
    # As an earlier guest can leave it: the mark of its first open's end is gone.
    (one.repo / "made").unlink()
    held, kept, theirs = as_it_is(one.copy), as_it_is(one.repo), as_it_is(folder)
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    opened = again.open()
    assert set(opened) == {"copy", "set_aside_folders"} and opened["copy"] == "made", opened
    # Each is named for the order it was set aside in, when, whose it was and which of the two it is.
    copy, repository = opened["set_aside_folders"]
    assert re.fullmatch(r"00000001-[0-9]{8}T[0-9]{6}Z-t1\.copy", copy) and repository == copy.replace(".copy", ".repository")
    assert abs(calendar.timegm(time.strptime(copy.split("-")[1], "%Y%m%dT%H%M%SZ")) - time.time()) < 120
    aside = tmp_path / "store" / "set-aside"
    assert set_aside_whole(tmp_path / "store") == [copy, repository]
    # And is what it was, by every name, mode, time and byte: renamed, with nothing in it read or written.
    assert (as_it_is(aside / copy), as_it_is(aside / repository), as_it_is(folder)) == (held, kept, theirs)
    assert git(aside / repository, "cat-file", "-p", "refs/heads/threads/t1:Draft.md") == "in a snapshot"
    # The copy made again is whole, the folder's files, and the thread's next turn lands from it.
    assert files_of(again.copy) == files_of(folder) and again.changed() == {"paths": []}
    (again.copy / "A.md").write_text("made after\n")
    assert [c["path"] for c in land(again, "saga:1")["changes"]] == ["A.md"]
    # Every open says so while they are kept, whoever heard the first.
    for _ in range(2):
        assert LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").open() == {
            "copy": "moved", "set_aside_folders": [copy, repository],
        }
    assert (as_it_is(aside / copy), as_it_is(aside / repository)) == (held, kept)


def test_a_copy_whose_index_is_gone_is_set_aside_whole_and_made_again_from_its_branch(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    (one.copy / "Draft.md").write_text("in a snapshot\n")
    tip = one.snapshot("before a step")
    (one.copy / "notes.txt").write_text("the thread's notes, in no snapshot\n")
    (one.copy / "scratch.tmp").write_text("left out of history\n")
    (one.repo / "worktrees" / "t1" / "index").unlink()
    held, theirs = as_it_is(one.copy), as_it_is(folder)
    # No request reads such a copy, and none but its open touches it.
    with refused("no_whole_copy"):
        one.snapshot("before a step")
    assert as_it_is(one.copy) == held
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    opened = again.open()
    assert set(opened) == {"copy", "set_aside_folders"} and opened["copy"] == "kept", opened
    [copy] = opened["set_aside_folders"]
    assert re.fullmatch(r"00000001-[0-9]{8}T[0-9]{6}Z-t1\.copy", copy) and set_aside_whole(tmp_path / "store") == [copy]
    assert (as_it_is(tmp_path / "store" / "set-aside" / copy), as_it_is(folder)) == (held, theirs)
    # Its repository was whole and is the thread's still: the copy made again is its branch's files, the
    # snapshot among them, and what no snapshot took is in the copy set aside alone.
    assert git(again.repo, "rev-parse", "refs/heads/threads/t1") == tip
    assert files_of(again.copy) == [
        ("Draft.md", b"in a snapshot\n"), ("Report.docx", b"PK\x03\x04 report v1"), ("notes.txt", b"v1 notes\n"),
    ]
    assert again.changed() == {"paths": ["Draft.md"]}
    assert [c["path"] for c in land(again, "saga:1")["changes"]] == ["Draft.md"]
    assert LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").open() == {"copy": "moved", "set_aside_folders": [copy]}


def a_copy_with_nothing_of_its_own(tmp_path: Path, folder: Path) -> LocalHistory:
    """A thread's copy that holds nothing the folder or its history does not.  Its turn landed, and its next
    open moved it to the folder as you have changed it since: a file, one in a folder, a program and a
    link, which no history holds yet."""
    one = a_copy(tmp_path, folder)
    (one.copy / "A.md").write_text("A's, landed\n")
    land(one, "saga:1")
    (folder / "sub").mkdir()
    (folder / "sub" / "yours.txt").write_text("saved by you since\n")
    (folder / "run.sh").write_text("#!/bin/sh\n")
    (folder / "run.sh").chmod(0o755)
    (folder / "latest").symlink_to("Report.docx")
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    assert again.open() == {"copy": "moved"}
    return again


#: What a thread's place can lose that has its next open make its copy again: with the first its repository too.
LOST = {
    "the mark of its first open's end": lambda one: (one.repo / "made").unlink(),
    "its index": lambda one: (one.repo / "worktrees" / one.thread / "index").unlink(),
}


@pytest.mark.parametrize("lost", list(LOST))
def test_a_copy_made_again_that_holds_some_of_its_branchs_files_and_no_more_is_not_kept(tmp_path, folder, lost):
    one = a_copy_with_nothing_of_its_own(tmp_path, folder)
    # As a copy whose making was cut holds them: a file is not there, and a folder holds nothing.
    (one.copy / "Report.docx").unlink()
    (one.copy / "empty").mkdir()
    LOST[lost](one)
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    assert again.open() == {"copy": "made" if lost.startswith("the mark") else "moved"}
    assert set_aside_whole(tmp_path / "store") == []
    assert files_of(again.copy) == files_of(folder)


#: What a thread can leave in its copy that neither the folder nor its history holds there.
HELD = {
    "a file it changed": lambda copy: (copy / "notes.txt").write_text("the thread's notes\n"),
    "a file it made": lambda copy: (copy / "sub" / "new.md").write_text("new\n"),
    "a file history leaves out": lambda copy: (copy / "scratch.tmp").write_text("left out of history\n"),
    "a file it made a program": lambda copy: (copy / "notes.txt").chmod(0o755),
    "a program it made a file": lambda copy: (copy / "run.sh").chmod(0o644),
    "a link it put where a file was": lambda copy: ((copy / "notes.txt").unlink(), (copy / "notes.txt").symlink_to("A.md")),
    "a link it pointed elsewhere": lambda copy: ((copy / "latest").unlink(), (copy / "latest").symlink_to("A.md")),
    "a pipe": lambda copy: os.mkfifo(copy / "pipe"),
}


@pytest.mark.parametrize("lost", list(LOST))
@pytest.mark.parametrize("held", list(HELD))
def test_a_copy_made_again_that_holds_anything_of_the_threads_own_is_set_aside_whole(tmp_path, folder, lost, held):
    one = a_copy_with_nothing_of_its_own(tmp_path, folder)
    HELD[held](one.copy)
    LOST[lost](one)
    was, theirs = as_it_is(one.copy), as_it_is(folder)
    opened = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").open()
    assert set(opened) == {"copy", "set_aside_folders"}, opened
    # The copy alone: its repository, where that is made again too, holds no commit the folder's history lacks.
    [copy] = opened["set_aside_folders"]
    assert copy.endswith("-t1.copy") and set_aside_whole(tmp_path / "store") == [copy]
    assert (as_it_is(tmp_path / "store" / "set-aside" / copy), as_it_is(folder)) == (was, theirs)


@pytest.mark.parametrize("holds", ["a snapshot", "what a record set aside", "a ref that names no commit"])
def test_a_repository_made_again_that_holds_a_commit_of_the_threads_own_is_set_aside_though_its_copy_is_not(tmp_path, folder, holds):
    one = a_copy_with_nothing_of_its_own(tmp_path, folder)
    if holds == "a ref that names no commit":
        (one.repo / "refs" / "heads" / "odd").write_text("what is no commit's id\n")
    else:
        # The thread wrote a file, a snapshot took it, and the file is gone from the copy again.
        (one.copy / "Draft.md").write_text("in a snapshot\n")
        tip = one.snapshot("before a step")
        (one.copy / "Draft.md").unlink()
    if holds == "what a record set aside":
        # As a record leaves it: the branch on the folder's files again, and the snapshot on a ref of its own.
        git(one.repo, "update-ref", f"refs/set-aside/t1/00000001-{tip}", tip)
        git(one.repo, "update-ref", "refs/heads/threads/t1", git(one.repo, "rev-parse", "refs/heads/main"))
    (one.repo / "made").unlink()
    kept = as_it_is(one.repo)
    opened = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").open()
    assert set(opened) == {"copy", "set_aside_folders"} and opened["copy"] == "made", opened
    [repository] = opened["set_aside_folders"]
    assert repository.endswith("-t1.repository") and set_aside_whole(tmp_path / "store") == [repository]
    assert as_it_is(tmp_path / "store" / "set-aside" / repository) == kept
    if holds != "a ref that names no commit":
        assert git(tmp_path / "store" / "set-aside" / repository, "cat-file", "-p", f"{tip}:Draft.md") == "in a snapshot"


def test_a_folder_that_gets_no_history_has_what_was_set_aside_named_all_the_same(tmp_path, folder, monkeypatch):
    one = a_copy(tmp_path, folder)
    (one.copy / "notes.txt").write_text("the thread's notes, in no snapshot\n")
    (one.repo / "made").unlink()
    was = as_it_is(one.copy)
    # The folder holds more files now than a history tracks: no copy is made again for it.
    monkeypatch.setattr("surogates.sandbox.local_history.HISTORY_CAP", 1)
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    opened = again.open()
    assert set(opened) == {"history", "reason", "set_aside_folders"}, opened
    [copy] = opened["set_aside_folders"]
    assert opened == {"history": "off", "reason": "cap", "set_aside_folders": [copy]}
    assert not again.repo.exists() and not again.copy.exists()
    assert as_it_is(tmp_path / "store" / "set-aside" / copy) == was
    assert LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").open() == opened


@pytest.mark.parametrize("left", ["a link out of the place", "a file"])
def test_what_is_set_aside_goes_nowhere_an_earlier_guest_led_the_places_folder_for_it(tmp_path, folder, left):
    one = a_copy(tmp_path, folder)
    (one.copy / "notes.txt").write_text("the thread's notes, in no snapshot\n")
    (one.repo / "worktrees" / "t1" / "index").unlink()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "theirs.txt").write_text("another's\n")
    aside = tmp_path / "store" / "set-aside"
    aside.symlink_to(elsewhere) if left.startswith("a link") else aside.write_text("where a folder would be\n")
    theirs, held = as_it_is(elsewhere), as_it_is(one.copy)
    opened = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").open()
    # The place's own folder, made for it: nothing went where the link led.
    assert aside.is_dir() and not aside.is_symlink()
    [copy] = opened["set_aside_folders"]
    assert set_aside_whole(tmp_path / "store") == [copy] and as_it_is(aside / copy) == held
    assert as_it_is(elsewhere) == theirs


def test_no_name_an_earlier_guest_left_among_what_is_set_aside_is_gone_through_written_over_or_told(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    (one.copy / "notes.txt").write_text("the thread's notes, in no snapshot\n")
    (one.repo / "worktrees" / "t1" / "index").unlink()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "theirs.txt").write_text("another's\n")
    aside = tmp_path / "store" / "set-aside"
    aside.mkdir()
    # Another thread's, a folder.  And named as this thread's own would be: a file, and a link out of the
    # place, the last of them all by its count.
    (aside / "00000007-20260101T000000Z-t2.copy").mkdir()
    (aside / "00000007-20260101T000000Z-t2.copy" / "theirs.md").write_text("the other thread's\n")
    (aside / "00000008-20260101T000000Z-t1.repository").write_text("where a folder would be\n")
    (aside / "00000009-20260101T000000Z-t1.copy").symlink_to(elsewhere)
    # And names that are none this history gives.
    (aside / "t1.copy").mkdir()
    (aside / "0000010-20260101T000000Z-t1.copy").mkdir()
    (aside / "00000011-20260101T000000Z-t1.copy.txt").write_text("no folder of its own\n")
    left, theirs, held = as_it_is(aside), as_it_is(elsewhere), as_it_is(one.copy)
    opened = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").open()
    # Its own is one more than any name there has, whatever that name is of: nothing was at its name before.
    [copy] = opened["set_aside_folders"]
    assert re.fullmatch(r"00000010-[0-9]{8}T[0-9]{6}Z-t1\.copy", copy) and as_it_is(aside / copy) == held
    # And nothing left there was gone through, written over or taken away.
    assert [entry for entry in as_it_is(aside) if entry[0].split("/")[0] != copy] == left
    assert as_it_is(elsewhere) == theirs
    assert LocalHistory.at(tmp_path / "store", folder, thread="t2", user="u1").open() == {
        "copy": "made", "set_aside_folders": ["00000007-20260101T000000Z-t2.copy"],
    }


def test_a_place_with_no_name_left_for_what_is_set_aside_refuses_the_open_and_moves_nothing(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    (one.copy / "notes.txt").write_text("the thread's notes, in no snapshot\n")
    (one.repo / "worktrees" / "t1" / "index").unlink()
    (tmp_path / "store" / "set-aside").mkdir()
    (tmp_path / "store" / "set-aside" / "99999999-20260101T000000Z-t2.copy").mkdir()
    held = as_it_is(one.copy)
    with refused("failed", "^the place has no name left for what is set aside$"):
        LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").open()
    assert as_it_is(one.copy) == held and set_aside_whole(tmp_path / "store") == ["99999999-20260101T000000Z-t2.copy"]


@pytest.mark.parametrize("thread", [
    "../set-aside/00000001-20260101T000000Z-t1.copy", "..", ".", "t1/../t2", "t1.copy", "t 1", "", "t" * 65,
])
def test_a_thread_is_named_by_one_name_that_leads_nowhere_else_in_the_place(tmp_path, folder, thread):
    with refused("not_a_request", "^refused the request: it names no thread$"):
        LocalHistory.at(tmp_path / "store", folder, thread=thread, user="u1")


def test_no_request_of_any_thread_reads_or_writes_what_is_set_aside(tmp_path, folder):
    place = tmp_path / "store"
    one, _ = a_copy(tmp_path, folder, "t1"), a_copy(tmp_path, folder, "t2")
    (one.copy / "Draft.md").write_text("in a snapshot\n")
    LocalHistory.at(place, folder, thread="t1", user="u1").snapshot("before a step")
    (one.copy / "notes.txt").write_text("the thread's notes, in no snapshot\n")
    (one.repo / "made").unlink()
    names = LocalHistory.at(place, folder, thread="t1", user="u1").open()["set_aside_folders"]
    assert [name.rpartition(".")[2] for name in names] == ["copy", "repository"]
    held = as_it_is(place / "set-aside")
    # Every request the history takes, of the thread whose they were and of another.
    for thread, author in (("t1", A), ("t2", B)):
        history = LocalHistory.at(place, folder, thread=thread, user="u1")
        assert history.open()["copy"] in ("moved", "kept")
        tip = history.snapshot("before a step")
        (history.copy / f"{thread}.md").write_text("a turn's\n")
        assert history.changed() == {"paths": [f"{thread}.md"]}
        history.restore(tip)
        (history.copy / f"{thread}.md").write_text("a turn's\n")
        history.fetch(saga=f"saga:{thread}")
        landed = land(history, f"saga:{thread}", author)
        assert history.forget(saga=f"saga:{thread}", applied=landed["changes"]) == {"landing": landed["landing"]}
        (history.copy / f"{thread}-kept.md").write_text("a failed turn's\n")
        history.keep(author=author, trailers=[["Surogate-Kind", "turn"]], base=True)
        assert as_it_is(place / "set-aside") == held, thread
    # They are no thread's.  A place's repositories and copies are its threads' alone, and no request, and no
    # history made for one, names what is set aside as its thread.
    assert sorted(p.name for p in (place / "clones").iterdir()) == sorted(p.name for p in (place / "threads").iterdir()) == ["t1", "t2"]
    for named in (names[0], names[1], f"../set-aside/{names[0]}", f"../set-aside/{names[1]}"):
        request = {"store": str(place), "folder": str(folder), "thread": named, "user": "u1", "action": "open", "args": {}}
        with refused("not_a_request", "^refused the request: it names no thread$"):
            local_history.run(request)
        with refused("not_a_request", "^refused the request: it names no thread$"):
            LocalHistory.at(place, folder, thread=named, user="u1")
    assert as_it_is(place / "set-aside") == held


def set_aside_once_more(history: LocalHistory, text: str) -> dict:
    """One more copy of the thread's set aside whole: a file in no snapshot, the copy's index gone, and what
    the thread's next open answers."""
    (history.copy / "unlanded.md").write_text(text)
    (history.repo / "worktrees" / history.thread / "index").unlink()
    return LocalHistory.at(history.store.parent, history.project, thread=history.thread, user="u1").open()


def test_a_thread_keeps_the_last_four_times_it_was_set_aside_whole_and_a_place_sixteen_and_every_open_says_what_went(tmp_path, folder, monkeypatch):
    assert (local_history._ASIDE_WHOLE, local_history._ASIDE_WHOLE_IN_ALL, local_history._ASIDE_GONE) == (4, 16, 16)
    monkeypatch.setattr(local_history, "_ASIDE_WHOLE", 2)
    monkeypatch.setattr(local_history, "_ASIDE_WHOLE_IN_ALL", 3)
    monkeypatch.setattr(local_history, "_ASIDE_GONE", 2)
    place, aside = tmp_path / "store", tmp_path / "store" / "set-aside"
    one, two, three = (a_copy(tmp_path, folder, thread) for thread in ("t1", "t2", "t3"))

    def again(thread: str) -> dict:
        return LocalHistory.at(place, folder, thread=thread, user="u1").open()

    # The first thread's first: its repository with its copy, one time set aside under one count.
    (one.copy / "Draft.md").write_text("in a snapshot\n")
    LocalHistory.at(place, folder, thread="t1", user="u1").snapshot("before a step")
    (one.repo / "made").unlink()
    first = again("t1")["set_aside_folders"]
    assert [name[:9] + name.rpartition(".")[2] for name in first] == ["00000001-copy", "00000001-repository"]
    kept = [None, first[0]]
    for count in (2, 3):
        opened = set_aside_once_more(one, f"one's, {count}\n")
        kept.append(opened["set_aside_folders"][-1])
        if count == 2:
            assert opened == {"copy": "moved", "set_aside_folders": [*first, kept[2]]}
    # The third time, the oldest goes for it, both its folders: never the one just set aside.  The open says which.
    assert opened == {"copy": "moved", "set_aside_folders": kept[2:4], "set_aside_gone": first}
    assert [(aside / name / "unlanded.md").read_text() for name in kept[2:4]] == ["one's, 2\n", "one's, 3\n"]
    # What went left its name and nothing under it, and every open of the thread's says so again.
    assert set_aside_whole(place) == sorted([*(f"{name}.gone" for name in first), *kept[2:4]])
    assert not any(entry for name in first for entry in (aside / f"{name}.gone").iterdir())
    assert again("t1") == opened
    # A place keeps three times in all.  Another thread's second takes the oldest there is, the first thread's,
    # and that thread's own open says so.
    for count in (4, 5):
        opened = set_aside_once_more(two, f"two's, {count}\n")
        kept.append(opened["set_aside_folders"][-1])
    assert opened == {"copy": "moved", "set_aside_folders": kept[4:6]}
    assert again("t1") == {"copy": "moved", "set_aside_folders": [kept[3]], "set_aside_gone": [*first, kept[2]]}
    for count in (6, 7):
        opened = set_aside_once_more(two, f"two's, {count}\n")
        kept.append(opened["set_aside_folders"][-1])
    assert opened == {"copy": "moved", "set_aside_folders": kept[6:8], "set_aside_gone": kept[4:6]}
    # A thread's newest never goes, whoever's open it is and however many the place holds: the first thread's
    # stays past the place's three, and the next oldest goes instead.
    kept.append(set_aside_once_more(three, "three's, 8\n")["set_aside_folders"][-1])
    assert [name for name in set_aside_whole(place) if not name.endswith(".gone")] == [kept[3], *kept[6:9]]
    kept.append(set_aside_once_more(three, "three's, 9\n")["set_aside_folders"][-1])
    assert [name for name in set_aside_whole(place) if not name.endswith(".gone")] == [kept[3], *kept[7:10]]
    assert (aside / kept[3] / "unlanded.md").read_text() == "one's, 3\n"
    # A thread is told of the last two that went: the name of one before them is let go.
    assert again("t2") == {"copy": "moved", "set_aside_folders": [kept[7]], "set_aside_gone": kept[5:7]}
    assert not os.path.lexists(aside / f"{kept[4]}.gone")
    assert again("t1") == {"copy": "moved", "set_aside_folders": [kept[3]], "set_aside_gone": [*first, kept[2]]}


@pytest.mark.skipif(os.geteuid() == 0, reason="root removes from a folder nothing may be removed from")
def test_what_cannot_be_let_go_at_the_bound_is_tried_at_the_next_open_and_this_one_goes_on(tmp_path, folder, monkeypatch, caplog):
    monkeypatch.setattr(local_history, "_ASIDE_WHOLE", 1)
    one = a_copy(tmp_path, folder)
    # In the copy, a folder of the thread's that nothing may be removed from.
    (one.copy / "locked").mkdir()
    (one.copy / "locked" / "kept.md").write_text("the thread's\n")
    (one.copy / "locked").chmod(0o555)
    [first] = set_aside_once_more(one, "one's, 1\n")["set_aside_folders"]
    opened = set_aside_once_more(one, "one's, 2\n")
    # Letting go is upkeep: the turn starts all the same, and the one that is going is no longer said to be kept.
    [second] = opened["set_aside_folders"]
    assert opened == {"copy": "moved", "set_aside_folders": [second], "set_aside_gone": [first]}
    assert "could not be let go" in caplog.text
    going = tmp_path / "store" / "set-aside" / f"{first}.gone"
    assert (going / "locked" / "kept.md").exists()
    (going / "locked").chmod(0o755)
    assert LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").open() == opened
    assert not any(going.iterdir())


def test_what_was_set_aside_whole_is_named_in_the_answer_the_agent_gives(tmp_path, folder, tree):
    place = {"store": str(tmp_path / "store"), "folder": str(folder), "thread": THREAD, "user": "u1"}
    assert ask(tree, {**place, "action": "open", "args": {}}) == {"copy": "made"}
    (tmp_path / "store" / "threads" / THREAD / "notes.txt").write_text("the thread's, in no snapshot\n")
    (tmp_path / "store" / "clones" / THREAD / "worktrees" / THREAD / "index").unlink()
    answer = ask(tree, {**place, "action": "open", "args": {}})
    assert set(answer) == {"copy", "set_aside_folders"} and answer["copy"] == "moved", answer
    [copy] = answer["set_aside_folders"]
    assert re.fullmatch(rf"00000001-[0-9]{{8}}T[0-9]{{6}}Z-{THREAD}\.copy", copy)
    assert (tmp_path / "store" / "set-aside" / copy / "notes.txt").read_text() == "the thread's, in no snapshot\n"
    assert ask(tree, {**place, "action": "open", "args": {}}) == answer


#: One request, as its runner takes it, run to its end or killed at one of its steps: each git it
#: runs, and each file it writes or puts in its place.  Killed, every process of it goes at once.
STEPPED = """
import json, os, pathlib, signal, subprocess, sys
at, before, count = int(sys.argv[2]), sys.argv[3], [0]
def stepped(real):
    def step(*args, **kwargs):
        count[0] += 1
        if count[0] == at or before and args and isinstance(args[0], list) and before in " ".join(args[0]):
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


def stepped(root: Path, thread: str, action: str, args: dict, at: int = 0, before: str = "") -> dict | None:
    """*thread*'s request on the folder under *root*, killed at its step *at*, or right before the git that is
    *before*; its steps and answer when it ran to its end."""
    request = {"store": str(root / "store"), "folder": str(root / "Documents"), "thread": thread, "user": "u1", "action": action, "args": args}
    ran = subprocess.run(
        [sys.executable, "-c", STEPPED, json.dumps(request), str(at), before], capture_output=True, text=True,
        cwd=Path(__file__).parents[1], start_new_session=True, timeout=300,
    )
    if at or before:
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
    # And the turn after it lands too: nothing the cut left keeps the thread's branch from being pushed.
    (ours.copy / "after.md").write_text("the turn after\n")
    assert [c["path"] for c in land(ours, "saga:after", B)["changes"]] == ["after.md"]
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
                again.forget(saga="saga:ours", applied=turn["changes"][:done])
        else:
            assert again.forget(saga="saga:ours", applied=[]) == {"landing": None}
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
            assert again.forget(saga="saga:ours", applied=turn["changes"]) == {"landing": answer["commit"]}
        elif recorded:
            # The push counted: the thread's next open finishes it, and says what was set aside.
            [aside] = again.open()["set_asides"]
            assert git(again.repo, "rev-parse", f"refs/landed/{OURS}") == landing, at
            assert git(again.repo, "cat-file", "-p", f"{aside}:late.md") == "written after the turn was committed"
        if asked_again or recorded:
            # The copy is the landing's files: the newer report, and no file the thread wrote since.
            assert dict(files_of(again.copy)) == {**dict(files_of(folder))}, at
            (again.copy / "late.md").unlink(missing_ok=True)
        else:
            # It never counted: the landing is as it was before its record, and its kept files are not forgotten.
            with refused("landing_unsettled"):
                again.forget(saga="saga:ours", applied=turn["changes"])
            (root / "store" / "threads" / OURS / "late.md").unlink()
        the_next_turn_lands_whole(root, kept, landed=True)
    # The cuts fall on both sides of the push.
    assert 0 < pushed < at


def test_a_forget_killed_at_any_step_changes_nothing_and_answers_the_same_when_asked_again(tmp_path):
    ours, kept = a_folder_two_threads_work_on(tmp_path / "whole")
    landed = land(ours, "saga:ours", B)
    for at, root in each_cut(tmp_path, OURS, "forget", {"saga": "saga:ours", "applied": landed["changes"]}):
        again = LocalHistory.at(root / "store", root / "Documents", thread=OURS, user="u1")
        assert again.forget(saga="saga:ours", applied=landed["changes"]) == {"landing": landed["landing"]}, at
    the_next_turn_lands_whole(root, kept, landed=True)


def a_clean_copy_whose_main_moved(root: Path, after_a_cut_record: bool) -> None:
    """A folder under *root* whose history moved on while our thread's copy held nothing unlanded.

    The other thread has landed a change and a new file, and you have changed a file, deleted one and made
    one since: our thread's next open moves its copy to all of that.  With *after_a_cut_record*, our thread
    landed a file first, and that landing's record was cut right after its push: the open finishes it first.
    """
    folder = root / "Documents"
    folder.mkdir(parents=True)
    for name in "abeg":
        (folder / f"{name}.txt").write_text(f"{name} v1\n")
        os.utime(folder / f"{name}.txt", (time.time() - 60, time.time() - 60))
    ours, theirs = a_copy(root, folder, OURS), a_copy(root, folder, THEIRS)
    if after_a_cut_record:
        (ours.copy / "a.txt").write_text("a by us\n")
        picked = ours.pickup(author=YOURS, trailers=SAGA)
        turn = ours.commit_turn(author=B, trailers=SAGA, pickup=picked["commit"])
        applied(folder, ours.copy, turn["changes"])
        History.record(ours, turn=turn["commit"], applied=turn["changes"], author=B, trailers=SAGA, main=picked["main"], pickup=picked["commit"])
    (theirs.copy / "g.txt").write_text("g by them\n")
    (theirs.copy / "B.md").write_text("new by them\n")
    land(theirs, "saga:theirs")
    (folder / "b.txt").write_text("b v2 by you\n")
    (folder / "e.txt").unlink()
    (folder / "yours.txt").write_text("a new file of yours\n")


def the_folder_goes_back_and_the_thread_lands_its_own_file_alone(root: Path, at: object) -> None:
    """After our thread's copy moved to ``main``: you undo your edit, bring your deleted file back, and remove
    your new file and the other thread's, whose change you undo too.  Our thread's turn writes one file, and its
    landing names that file and no other: nothing of yours or the other's is taken for its work."""
    folder = root / "Documents"
    ours = LocalHistory.at(root / "store", folder, thread=OURS, user="u1")
    assert ours.changed() == {"paths": []}, at
    assert files_of(ours.copy) == files_of(folder), at
    (folder / "b.txt").write_text("b v1\n")
    (folder / "e.txt").write_text("e v1\n")
    (folder / "yours.txt").unlink()
    (folder / "B.md").unlink()
    (folder / "g.txt").write_text("g v1\n")
    yours = files_of(folder)
    (ours.copy / "A2.md").write_text("our own work this turn\n")
    landed = land(ours, "saga:next", B)
    assert ([c["path"] for c in landed["changes"]], landed["overlapped"]) == (["A2.md"], []), at
    assert files_of(folder) == sorted([*yours, ("A2.md", b"our own work this turn\n")]), at


@pytest.mark.parametrize("after", ["a landing", "a record cut after its push"])
def test_an_open_that_moves_a_clean_copy_killed_at_any_step_leaves_the_copy_and_its_base_agreeing(tmp_path, after):
    a_clean_copy_whose_main_moved(tmp_path / "whole", after == "a record cut after its push")
    for at, root in each_cut(tmp_path, OURS, "open", {}):
        ours = LocalHistory.at(root / "store", root / "Documents", thread=OURS, user="u1")
        # Whatever the cut left of the move, the next open finishes it: the copy is the folder, on a base that is.
        assert ours.open() == {"copy": "moved"}, at
        assert not git(ours.repo, "for-each-ref", "refs/moving/", "refs/set-aside/"), at
        the_folder_goes_back_and_the_thread_lands_its_own_file_alone(root, at)


@pytest.mark.parametrize("first", ["open", "changed", "snapshot", "commit", "keep"])
@pytest.mark.parametrize("written_since", [False, True])
def test_a_move_of_a_clean_copy_cut_part_way_through_its_files_is_finished_by_whichever_act_comes_first(tmp_path, first, written_since):
    root = tmp_path / "whole"
    a_clean_copy_whose_main_moved(root, False)
    stepped(root, OURS, "open", {}, before="read-tree -u -m")
    ours = LocalHistory.at(root / "store", root / "Documents", thread=OURS, user="u1")
    # As a checkout killed after some of its files leaves the copy: one of main's files written and one of its
    # deletions made, the rest and the index as they were.
    (ours.copy / "g.txt").write_text("g by them\n")
    (ours.copy / "e.txt").unlink()
    if written_since:
        # A command of the thread's still running wrote a file of its own, and one that main changed.
        (ours.copy / "late.md").write_text("written by a command still running\n")
        (ours.copy / "b.txt").write_text("b, written by a command still running\n")
    answer = {
        "open": ours.open, "changed": ours.changed, "snapshot": lambda: ours.snapshot("before a step"),
        "commit": lambda: ours.commit_turn(author=B, trailers=SAGA, pickup=None),
        "keep": lambda: ours.keep(author=B, trailers=SAGA, base=True),
    }[first]()
    if first == "changed":
        assert answer == {"paths": []}
    if first == "commit":
        assert (answer["commit"], answer["changes"]) == (None, [])
    # Finished, and no longer said to be under way: a later act would put the copy back to where it was going.
    assert not git(ours.repo, "for-each-ref", "refs/moving/")
    # The branch moved with the base: a snapshot of the turn that goes on is one the copy is put back to.
    here = ours.snapshot("before a step")
    (ours.copy / "junk.md").write_text("made by a step that is stopped\n")
    ours.restore(here)
    assert not (ours.copy / "junk.md").exists()
    asides = git(ours.repo, "for-each-ref", "--format=%(objectname)", "refs/set-aside/").split()
    if written_since:
        # Neither what the copy held nor what main holds: set aside before the copy is made main's files, and named.
        [aside] = asides
        assert git(ours.repo, "cat-file", "-p", f"{aside}:late.md") == "written by a command still running"
        assert git(ours.repo, "cat-file", "-p", f"{aside}:b.txt") == "b, written by a command still running"
        assert LocalHistory.at(root / "store", root / "Documents", thread=OURS, user="u1").open() == {"copy": "moved", "set_asides": [aside]}
    else:
        assert asides == []
        assert LocalHistory.at(root / "store", root / "Documents", thread=OURS, user="u1").open() == {"copy": "moved"}
    the_folder_goes_back_and_the_thread_lands_its_own_file_alone(root, first)


def test_a_move_cut_after_the_copys_files_were_moved_leaves_what_the_thread_wrote_since_as_its_work(tmp_path):
    root = tmp_path / "whole"
    a_clean_copy_whose_main_moved(root, False)
    # Every file of the copy is main's, and its index: the branch and the base are where they were.
    stepped(root, OURS, "open", {}, before="update-ref refs/heads/threads/")
    ours = LocalHistory.at(root / "store", root / "Documents", thread=OURS, user="u1")
    assert (ours.copy / "B.md").exists() and git(ours.repo, "rev-parse", f"refs/moving/{OURS}") != git(ours.repo, "rev-parse", f"refs/bases/{OURS}")
    (ours.copy / "late.md").write_text("written by a command still running\n")
    # Nothing is put back, so nothing is set aside: the file is the turn's own, on a base that is main.
    assert ours.changed() == {"paths": ["late.md"]}
    assert not git(ours.repo, "for-each-ref", "refs/moving/", "refs/set-aside/")
    assert ours.open() == {"copy": "kept"}
    assert [c["path"] for c in land(ours, "saga:next", B)["changes"]] == ["late.md"]


def test_a_move_that_cannot_be_finished_refuses_every_act_and_moves_nothing(tmp_path):
    root = tmp_path / "whole"
    a_clean_copy_whose_main_moved(root, False)
    stepped(root, OURS, "open", {}, before="read-tree -u -m")
    ours = LocalHistory.at(root / "store", root / "Documents", thread=OURS, user="u1")
    # What no request can put right: the copy's index is no index.
    (ours.repo / "worktrees" / OURS / "index").write_bytes(b"not an index\n")
    refs, held = git(ours.repo, "for-each-ref"), files_of(ours.copy)
    for ask in (
        lambda h: h.open(), lambda h: h.changed(), lambda h: h.snapshot("before a step"),
        lambda h: h.commit_turn(author=B, trailers=SAGA, pickup=None), lambda h: h.keep(author=B, trailers=SAGA, base=True),
    ):
        with refused("move_unfinished", "refused the request: this thread's copy was being moved to main, and the move could not be finished: git write-tree failed"):
            ask(LocalHistory.at(root / "store", root / "Documents", thread=OURS, user="u1"))
    assert (git(ours.repo, "for-each-ref"), files_of(ours.copy)) == (refs, held)
