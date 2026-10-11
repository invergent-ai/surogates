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


def held_open(path: Path) -> list[str]:
    """This process's file descriptors open on *path*, or on what was there and is gone."""
    held = []
    for fd in os.listdir("/proc/self/fd"):
        with contextlib.suppress(OSError):
            if os.readlink(f"/proc/self/fd/{fd}") in (str(path), f"{path} (deleted)"):
                held.append(fd)
    return held


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
    """*top* and every name under it with its mode, its time and what it holds, no link followed: a request that
    leaves *top* alone leaves all of it the same, and a folder renamed whole takes all of it along."""
    if not os.path.lexists(top):
        return []
    itself = top.lstat()
    seen = [(".", itself.st_mode, itself.st_mtime_ns, b"")]
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


def size(path: Path) -> int:
    """The bytes of the files under *path*, no link followed."""
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink())


def connected(place: Path, kept: bool = True) -> None:
    """Each thread's repository in *place* names nothing that neither it nor the folder's history holds: every
    object its refs, its reflogs and its copies' indexes lead to is its own or the history's, by git's own look.
    And, but where *kept* is false, each repository the place keeps set aside is so as it is kept, by its own
    alternates alone."""
    live = sorted((place / "clones").iterdir()) if (place / "clones").is_dir() else []
    aside = sorted((place / "set-aside").glob("*.repository")) if kept and (place / "set-aside").is_dir() else []
    for repo in (*live, *aside):
        if not (repo / "HEAD").is_file() or local_history._linked(repo):
            # No repository yet, which its open makes; or one with a link an earlier guest left in it, which
            # no git reads and the next request sets aside.
            continue
        # A live one with the history's objects whatever its alternates say: one astray is read by no git.
        borrowed = {"GIT_ALTERNATE_OBJECT_DIRECTORIES": str(place / "history.git" / "objects")} if repo in live else {}
        # Its refs as they lead to objects, not its refs' own files: a lock a killed git left among them is the next request's to clear.
        ran = subprocess.run(
            ["git", f"--git-dir={repo}", "fsck", "--connectivity-only", "--no-dangling", "--cache", "--no-references"],
            capture_output=True, text=True, env={**HERMETIC, **borrowed},
        )
        assert ran.returncode == 0 and ran.stdout == "" and not re.search(r"(?m)^(error|missing|broken|fatal)", ran.stderr), (repo, ran)


def deltas(repo: Path) -> dict[str, str]:
    """Each object *repo*'s own packs hold as a delta, and the object it is a delta of."""
    found = {}
    for index in (repo / "objects" / "pack").glob("*.idx"):
        listed = subprocess.run(["git", f"--git-dir={repo}", "verify-pack", "-v", str(index)], capture_output=True, text=True, check=True, env=HERMETIC)
        found |= {fields[0]: fields[6] for fields in map(str.split, listed.stdout.splitlines()) if len(fields) == 7}
    return found


def held_here(repo: Path) -> set[str]:
    """The objects *repo* holds itself, loose and packed: none it reads where another store holds them."""
    loose = {f"{at.name}{name.name}" for at in (repo / "objects").glob("[0-9a-f][0-9a-f]") for name in at.iterdir()}
    packed = set()
    for index in (repo / "objects" / "pack").glob("*.idx"):
        with open(index, "rb") as listed:
            shown = subprocess.run(["git", "show-index"], stdin=listed, capture_output=True, text=True, check=True, env=HERMETIC).stdout
        packed |= {line.split()[1] for line in shown.splitlines()}
    return loose | packed


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


def test_a_file_no_landing_writes_stays_in_the_threads_copy_as_the_thread_left_it(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    (one.copy / ".vscode").mkdir()
    (one.copy / ".vscode" / "tasks.json").write_text("{}\n")
    (one.copy / "Report.docx").write_bytes(b"PK\x03\x04 A's report")
    (one.copy / "notes.txt").unlink()
    # As the app and the worker leave them out: a name that runs code, and a file the folder has under a second name.
    step = up_to_its_record(one, "saga:1", A, (".vscode/tasks.json", "notes.txt"))
    assert [change["path"] for change in step["applied"]] == ["Report.docx"]
    landed = one.record(**step)
    assert landed["set_aside"] is None
    # Nobody else changed them, so the copy keeps each as its thread left it: the one it made, and the one it deleted.
    assert (one.copy / ".vscode" / "tasks.json").read_text() == "{}\n" and not (one.copy / "notes.txt").exists()
    assert sorted(p.name for p in folder.iterdir()) == ["Report.docx", "notes.txt"]
    # The folder's history records neither as landed, and the landing's own commit names both as left out.
    store = tmp_path / "store" / "history.git"
    assert git(store, "ls-tree", "-r", "--name-only", landed["commit"]).split() == ["Report.docx", "notes.txt"]
    assert git(store, "show", "-s", "--format=%(trailers:key=Surogate-Left,valueonly)", landed["commit"]).split() == [
        '".vscode/tasks.json"', '"notes.txt"',
    ]
    # They are the thread's changes still, and nothing else is: its next turn lands them, where a landing then writes them.
    assert one.changed() == {"paths": [".vscode/tasks.json", "notes.txt"]}
    assert one.open() == {"copy": "kept"}
    after = land(one, "saga:2")
    assert [(c["path"], c["after"] is None) for c in after["changes"]] == [(".vscode/tasks.json", False), ("notes.txt", True)]
    assert (folder / ".vscode" / "tasks.json").read_text() == "{}\n" and not (folder / "notes.txt").exists()
    # A name is a path, never a pattern: one that names no file keeps none as the thread has it.
    star = a_copy(tmp_path, folder, "t2")
    (star.copy / "Report.docx").write_bytes(b"PK\x03\x04 B's report")
    step = up_to_its_record(star, "saga:3", B, ("Report.docx",))
    star.record(**{**step, "left": ["*.docx"]})
    assert (star.copy / "Report.docx").read_bytes() == (folder / "Report.docx").read_bytes() == b"PK\x03\x04 A's report"


def test_a_file_left_out_where_the_landing_holds_a_file_at_its_folders_name_stays_as_the_thread_left_it(tmp_path, folder):
    (folder / ".vscode").write_text("a file of yours\n")
    one = a_copy(tmp_path, folder)
    # The thread put a folder where the file was, and a name that runs code in it: the two go together, and neither lands.
    (one.copy / ".vscode").unlink()
    (one.copy / ".vscode").mkdir()
    (one.copy / ".vscode" / "tasks.json").write_text("{}\n")
    (one.copy / "Report.docx").write_bytes(b"PK\x03\x04 A's report")
    step = up_to_its_record(one, "saga:1", A, (".vscode/tasks.json",))
    assert [change["path"] for change in step["applied"]] == ["Report.docx"]
    one.record(**step)
    # The copy is made the landing's files but for it, and what it no longer has room for: the record is not stuck on it.
    assert (one.copy / ".vscode" / "tasks.json").read_text() == "{}\n"
    assert (folder / ".vscode").read_text() == "a file of yours\n"
    assert one.changed() == {"paths": [".vscode", ".vscode/tasks.json"]}
    assert git(one.repo, "rev-parse", "refs/landed/t1") == git(tmp_path / "store" / "history.git", "rev-parse", "refs/heads/main")


@pytest.mark.parametrize("landed_since", [False, True])
def test_a_file_left_out_that_was_written_again_after_its_turn_is_set_aside_only_where_the_copy_is_made_other_files(tmp_path, folder, landed_since):
    one, two = a_copy(tmp_path, folder, "t1"), a_copy(tmp_path, folder, "t2")
    if landed_since:
        (two.copy / "B.md").write_text("B's own\n")
        land(two, "saga:2", B)
    (one.copy / ".vscode").mkdir()
    (one.copy / ".vscode" / "tasks.json").write_text("{}\n")
    step = up_to_its_record(one, "saga:1", A, (".vscode/tasks.json",))
    # A command still running writes it again after the turn was committed.
    (one.copy / ".vscode" / "tasks.json").write_text('{"late": true}\n')
    aside = one.record(**step)["set_aside"]
    if landed_since:
        # The copy is made the landing's files, another thread's among them: the late write is neither the turn's
        # nor the landing's, and is set aside; the file is as the turn left it.
        assert (one.copy / ".vscode" / "tasks.json").read_text() == "{}\n" and (one.copy / "B.md").is_file()
        assert git(one.repo, "cat-file", "-p", f"{aside}:.vscode/tasks.json") == '{"late": true}'
    else:
        # The copy's files were the landing's but for it already: nothing is put back, and the late write is the thread's work.
        assert aside is None and (one.copy / ".vscode" / "tasks.json").read_text() == '{"late": true}\n'
    assert one.changed() == {"paths": [".vscode/tasks.json"]}


@pytest.mark.parametrize("named", ['"../outside"', "7", "notes.txt", "[" * 100_000 + "]" * 100_000], ids=[
    "a path out of the folder", "no path", "no json", "json nested past any bound",
])
def test_a_landing_whose_commit_names_as_left_out_what_is_no_path_of_the_folder_is_refused_and_nothing_is_read(tmp_path, folder, named):
    one = a_copy(tmp_path, folder)
    (one.copy / "notes.txt").write_text("the thread's notes\n")
    step = up_to_its_record(one, "saga:1", A)
    # What no request writes: the cloud's half of a record, its trailers naming what is no file of the folder.
    History.record(one, **{**step, "trailers": [*step["trailers"], ["Surogate-Left", named]]})
    refs, held = git(one.repo, "for-each-ref"), files_of(one.copy)
    for ask in (lambda h: h.open(), lambda h: h.changed(), lambda h: h.snapshot("before a step")):
        with refused("history_refused", "refused the project's history: a landing names as left out what is no file of the folder"):
            ask(LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1"))
    assert (git(one.repo, "for-each-ref"), files_of(one.copy)) == (refs, held)


def somebody_elses(tmp_path: Path, folder: Path, case: str) -> tuple[LocalHistory, dict, str]:
    """Thread ``t1``'s landing up to its record, with a *left* that names a file somebody else changed since its turn
    began, or one in the way of such a file; the record's step, and the file whose version is the other's."""
    (folder / "X.md").write_text("X v1\n")
    one, two = a_copy(tmp_path, folder, "t1"), a_copy(tmp_path, folder, "t2")
    (one.copy / "Report.docx").write_bytes(b"PK\x03\x04 report by one")
    if case == "a file the thread never touched, which another thread landed":
        (two.copy / "X.md").write_text("X by thread two\n")
        land(two, "saga:2", B)
        return one, up_to_its_record(one, "saga:1", A, ("X.md",)), "X.md"
    if case == "a file the commit step held, which you saved since":
        (one.copy / "notes.txt").write_text("notes by one\n")
        (folder / "notes.txt").write_text("notes saved by you\n")
        step = up_to_its_record(one, "saga:1", A, ("notes.txt",))
        return one, step, "notes.txt"
    # The thread's own file, where another thread landed a folder of that name.
    (one.copy / "a").write_text("a file of one's\n")
    (two.copy / "a").mkdir()
    (two.copy / "a" / "b.md").write_text("b by thread two\n")
    land(two, "saga:2", B)
    return one, up_to_its_record(one, "saga:1", A, ("a",)), "a/b.md"


SOMEBODY_ELSES = [
    "a file the thread never touched, which another thread landed",
    "a file the commit step held, which you saved since",
    "a file in the way of one another thread landed",
]


@pytest.mark.parametrize("case", SOMEBODY_ELSES)
def test_a_record_refuses_to_leave_out_a_file_somebody_else_changed_since_its_turn_began_and_pushes_nothing(tmp_path, folder, case):
    one, step, theirs = somebody_elses(tmp_path, folder, case)
    store = tmp_path / "store" / "history.git"
    before, pushed, held, refs = as_it_is(folder), git(store, "for-each-ref"), files_of(one.copy), git(one.repo, "for-each-ref")
    with refused("not_a_request", "refused the request: a file it left out, or one in its way, was changed by somebody else since the thread's turn began"):
        one.record(**step)
    # Before its push: the folder, its history, the copy and the thread's refs are as they were.
    assert (as_it_is(folder), git(store, "for-each-ref")) == (before, pushed)
    assert (files_of(one.copy), git(one.repo, "for-each-ref")) == (held, refs)
    # The same landing with nothing left out records, and the copy takes the other's file, as for any file it leaves out.
    one.record(**{**step, "left": []})
    assert (one.copy / theirs).read_bytes() == (folder / theirs).read_bytes()
    assert theirs not in one.changed()["paths"]


@pytest.mark.parametrize("case", SOMEBODY_ELSES)
def test_a_landing_whose_commit_names_as_left_out_a_file_somebody_else_changed_since_its_turn_began_is_refused(tmp_path, folder, case):
    one, step, _ = somebody_elses(tmp_path, folder, case)
    # What no request writes: the cloud's half of a record, its trailers naming such a file as left out.
    named = [*step["trailers"], *(["Surogate-Left", json.dumps(path)] for path in step.pop("left"))]
    History.record(one, **{**step, "trailers": named})
    refs, held = git(one.repo, "for-each-ref"), files_of(one.copy)
    for ask in (lambda h: h.open(), lambda h: h.changed(), lambda h: h.snapshot("before a step")):
        with refused("history_refused", "refused the project's history: a landing names as left out a file somebody else changed since its turn began"):
            ask(LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1"))
    assert (git(one.repo, "for-each-ref"), files_of(one.copy)) == (refs, held)


@pytest.mark.parametrize("first", ["open", "changed", "snapshot", "commit", "keep", "record"])
def test_a_record_that_left_files_out_cut_after_its_push_leaves_them_as_the_thread_has_them_whichever_act_comes_next(tmp_path, first):
    root = tmp_path / "whole"
    ours, step, kept = a_landing_that_leaves_files_out(root)
    stepped(root, OURS, "record", step, before="update-ref refs/heads/main")
    again = LocalHistory.at(root / "store", root / "Documents", thread=OURS, user="u1")
    {
        "open": again.open, "changed": again.changed, "snapshot": lambda: again.snapshot("before a step"),
        "commit": lambda: again.commit_turn(author=B, trailers=[["Surogate-Saga", "saga:next"]], pickup=None),
        "keep": lambda: again.keep(author=B, trailers=[["Surogate-Kind", "turn"]], base=True),
        "record": lambda: again.record(**step),
    }[first]()
    left_as_the_thread_has_them(root, kept, first)


def test_the_files_a_turn_changed_are_named_before_anything_is_committed(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    tip = git(one.repo, "rev-parse", "refs/heads/threads/t1")
    refs = (tmp_path / "store" / "history.git" / "packed-refs").read_bytes()
    (one.copy / "notes.txt").unlink()
    (one.copy / "new folder").mkdir()
    (one.copy / "new folder" / "a b.md").write_text("new\n")
    (one.copy / "build").mkdir()
    (one.copy / "build" / "out.bin").write_bytes(b"\0")
    # What a landing looks at in the folder before its pickup: every file the copy changed since its base, and no other.
    assert one.changed() == {"paths": ["new folder/a b.md", "notes.txt"]}
    assert git(one.repo, "rev-parse", "refs/heads/threads/t1") == tip
    assert (tmp_path / "store" / "history.git" / "packed-refs").read_bytes() == refs


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


def you_add_photos(folder: Path, many: int) -> list[str]:
    """*many* photos you add to *folder*, named as a camera names them: their paths, in the order a pickup names files."""
    photos = folder / "Photos" / "2026"
    photos.mkdir(parents=True, exist_ok=True)
    for n in range(many):
        (photos / f"IMG_{n:06d}.jpg").write_bytes(b"\xff\xd8 photo %d" % n)
    return [f"Photos/2026/IMG_{n:06d}.jpg" for n in range(many)]


def parts_of(history: LocalHistory, picked: dict) -> list[list[dict]]:
    """Each part a pickup's files are named in, its own answer's first: every part after it asked from where the last one ended."""
    parts, left = [picked["picked_up"]], picked["more"]
    while left:
        part = history.picked(pickup=picked["commit"], after=parts[-1][-1]["path"])
        # Each goes on from where the last ended, and says how many are left after it.
        assert part["picked_up"] and len(part["picked_up"]) + part["more"] == left, part
        parts.append(part["picked_up"])
        left = part["more"]
    return parts


def test_a_pickup_of_more_files_than_one_answer_names_names_them_in_parts_each_file_once_in_order(tmp_path, folder, monkeypatch):
    one = a_copy(tmp_path, folder)
    photos = you_add_photos(folder, 40)
    (folder / "notes.txt").write_text("your notes\n")
    (folder / "Report.docx").unlink()
    store, refs = tmp_path / "store", git(one.repo, "for-each-ref")
    monkeypatch.setattr(local_history, "_PICKED_CHARS", 1_000)
    picked = one.pickup(author=YOURS, trailers=[["Surogate-Saga", "saga:1"], ["Surogate-Kind", "pickup"]])
    place = as_it_is(store)
    parts = parts_of(one, picked)
    assert len(parts) > 3
    # Each part is at most what one answer names, in characters of its files as JSON spells them.
    assert all(sum(len(json.dumps(entry)) + 2 for entry in part) <= 1_000 for part in parts)
    # Together they are the pickup's files, each once and in order: your photos, the report you deleted, your notes.
    named = [entry for part in parts for entry in part]
    expected = []
    for path in [*photos, "Report.docx", "notes.txt"]:
        before = None if path in photos else git(one.repo, "rev-parse", f"refs/heads/main:{path}")
        after = None if path == "Report.docx" else git(one.repo, "rev-parse", f"{picked['commit']}:{path}")
        expected.append({"path": path, "before": before, "after": after})
    assert named == expected
    # A part asked from a file that is not one of them goes on from where that file would be.
    assert one.picked(pickup=picked["commit"], after="Photos/2026/IMG_000038.jpz") == {"picked_up": named[39:], "more": 0}
    # Its parts only read: nothing of the place changed since the pickup, and no ref moved.
    assert as_it_is(store) == place and git(one.repo, "for-each-ref") == refs
    # Where they fit one answer, the pickup's own names them all.
    monkeypatch.setattr(local_history, "_PICKED_CHARS", 1 << 20)
    again = one.pickup(author=YOURS, trailers=[["Surogate-Saga", "saga:2"], ["Surogate-Kind", "pickup"]])
    assert (again["picked_up"], again["more"]) == (named, 0)


def test_a_part_of_a_pickup_names_one_file_at_least_and_never_more_than_an_answer_lists(tmp_path, folder, monkeypatch):
    one = a_copy(tmp_path, folder)
    photos = you_add_photos(folder, 7)
    trailers = [["Surogate-Saga", "saga:1"], ["Surogate-Kind", "pickup"]]
    # Fewer characters than any file's name takes: one file a part, so every part goes on from the last.
    monkeypatch.setattr(local_history, "_PICKED_CHARS", 10)
    assert [[entry["path"] for entry in part] for part in parts_of(one, one.pickup(author=YOURS, trailers=trailers))] == [[p] for p in photos]
    # However short their names, no more files than a list of an answer holds.
    monkeypatch.setattr(local_history, "_PICKED_CHARS", 1 << 20)
    monkeypatch.setattr(local_history, "HISTORY_CAP", 3)
    assert [len(part) for part in parts_of(one, one.pickup(author=YOURS, trailers=trailers))] == [3, 3, 1]


def test_a_part_of_a_pickup_is_asked_only_of_a_pickup_this_thread_made_and_from_a_file_of_the_folder(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    (one.copy / "Summary.md").write_text("summary\n")
    landing = land(one, "saga:0")["landing"]
    you_add_photos(folder, 3)
    picked = one.pickup(author=YOURS, trailers=[["Surogate-Saga", "saga:1"], ["Surogate-Kind", "pickup"]])
    first = git(one.repo, "rev-list", "--max-parents=0", "refs/heads/main")
    for pickup in ("f" * 40, first, landing):
        # A commit this thread's repository does not hold, the folder's first, or a landing: none picked up your edits.
        with refused("not_a_request", "it names no commit of this thread's with one parent"):
            one.picked(pickup=pickup, after="Photos")
    for after in ("../outside", "/etc/passwd", "", "a//b", "x/", None, 7):
        with refused("not_a_request", "it names no file to go on from"):
            one.picked(pickup=picked["commit"], after=after)
    assert one.picked(pickup=picked["commit"], after="Photos/2026/IMG_000000.jpg") == {"picked_up": picked["picked_up"][1:], "more": 0}


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


def test_an_open_that_does_not_move_leaves_a_clean_copy_where_its_branch_is(tmp_path, folder):
    assert LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").open(moves=False) == {"copy": "made"}
    one, two = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1"), a_copy(tmp_path, folder, "t2")
    (two.copy / "notes.txt").write_text("v2 notes, by B\n")
    land(two, "saga:1", B)
    (folder / "yours.txt").write_text("saved by you since\n")
    refs = [git(one.repo, "rev-parse", ref) for ref in ("refs/heads/threads/t1", "refs/bases/t1")]
    # Asked inside a turn, as the app asks before a step: the copy stays on the base its turn began on, though
    # what landed meanwhile and your save are the folder's now.
    assert one.open(moves=False) == {"copy": "kept"}
    assert (one.copy / "notes.txt").read_text() == "v1 notes\n"
    assert not (one.copy / "yours.txt").exists()
    assert [git(one.repo, "rev-parse", ref) for ref in ("refs/heads/threads/t1", "refs/bases/t1")] == refs
    # Made again from its branch where it was removed, and not moved either.
    shutil.rmtree(one.copy)
    assert one.open(moves=False) == {"copy": "kept"}
    assert (one.copy / "notes.txt").read_text() == "v1 notes\n"
    assert not (one.copy / ".git").exists()
    assert [git(one.repo, "rev-parse", ref) for ref in ("refs/heads/threads/t1", "refs/bases/t1")] == refs
    # Its next turn starts on the folder as it is now.
    assert one.open() == {"copy": "moved"}
    assert (one.copy / "notes.txt").read_text() == "v2 notes, by B\n"
    assert (one.copy / "yours.txt").read_text() == "saved by you since\n"


def test_an_open_that_does_not_move_still_finishes_a_record_cut_after_its_push(tmp_path, folder):
    again, cut = a_record_cut_after_its_push(tmp_path, folder)
    theirs = files_of(folder)
    # A host works in the copy right after this open: it is the landing's files first, the newer report among them.
    assert again.open(moves=False) == {"copy": "kept"}
    assert git(again.repo, "rev-parse", "refs/landed/t2") == cut["landing"]
    assert (again.copy / "Report.docx").read_bytes() == b"PK\x03\x04 A's report"
    assert (again.copy / "A-new.md").read_text() == "A's new file\n"
    assert again.changed() == {"paths": []}
    # Its next landing deletes no file another thread landed, and writes no old version over another's change.
    after = land(again, "saga:3", B)
    assert (after["commit"], after["changes"], after["overlapped"]) == (None, [], [])
    assert files_of(folder) == theirs


def test_an_open_that_does_not_move_is_one_the_agent_runs(tmp_path, folder, tree):
    place = {"store": str(tmp_path / "store"), "folder": str(folder), "thread": THREAD, "user": "u1"}
    assert ask(tree, {**place, "action": "open", "args": {"moves": False}}) == {"copy": "made"}
    (folder / "yours.txt").write_text("saved by you since\n")
    assert ask(tree, {**place, "action": "open", "args": {"moves": False}}) == {"copy": "kept"}
    assert not (tmp_path / "store" / "threads" / THREAD / "yours.txt").exists()
    assert ask(tree, {**place, "action": "open", "args": {"moves": True}}) == {"copy": "moved"}
    assert (tmp_path / "store" / "threads" / THREAD / "yours.txt").read_text() == "saved by you since\n"


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


def test_a_gitignore_of_the_folders_that_takes_back_what_history_leaves_out_brings_none_of_it_in(tmp_path, folder):
    # The folder's own ignores, as a repository of yours may have them: each line takes back what history leaves out.
    (folder / ".gitignore").write_text("!.env\n!*.tmp\n!_whiteboard/\n!.threads/\n!node_modules/\n")
    (folder / ".env").write_text("SECRET=1\n")
    (folder / ".surogate-0123abcd-0000-4000-8000-000000000000.tmp").write_text("the file helper's own, half written\n")
    for name, file in (("_whiteboard", "canvas.json"), (".threads", "old.txt"), ("node_modules", "left.js")):
        (folder / name).mkdir()
        (folder / name / file).write_text("none of the folder's files\n")
    one = a_copy(tmp_path, folder)
    store = tmp_path / "store" / "history.git"
    tracked = [".gitignore", "Report.docx", "notes.txt"]
    assert git(store, "ls-tree", "-r", "--name-only", "refs/heads/main").splitlines() == tracked
    assert sorted(p.name for p in one.copy.iterdir()) == tracked
    # A turn's write there, and your save there, are none of history's either.
    (one.copy / ".env").write_text("SECRET=2\n")
    (one.copy / "notes.txt").write_text("the thread's notes\n")
    (folder / "node_modules" / "left.js").write_text("saved by you\n")
    landed = land(one, "saga:1")
    assert [c["path"] for c in landed["changes"]] == ["notes.txt"] and landed["excluded"] == [".env"]
    assert git(store, "ls-tree", "-r", "--name-only", "refs/heads/main").splitlines() == tracked
    assert (folder / ".env").read_text() == "SECRET=1\n"


@pytest.mark.parametrize("read", ["several at once", "by one git"])
def test_a_request_cut_as_it_first_made_a_threads_repository_leaves_out_what_history_leaves_out_all_the_same(tmp_path, folder, monkeypatch, read):
    (folder / ".env").write_text("SECRET=1\n")
    (folder / "build").mkdir()
    (folder / "build" / "out.bin").write_bytes(b"\0")
    # And a package's own repository, with nothing committed in it, where history leaves out.
    subprocess.run(["git", "init", "-q", str(folder / "node_modules" / "pkg")], check=True, env=HERMETIC)
    (folder / "node_modules" / "pkg" / "index.js").write_text("// a package\n")
    one = a_copy(tmp_path, folder)
    if read == "by one git":
        # As where a reader fails twice: one git reads the folder, as it finds it.
        monkeypatch.setattr(History, "_read_at_once", lambda self, new: False)
    (one.copy / "A.md").write_text("A\n")
    land(one, "saga:1")
    main = History._main

    def cut(self, *args, **kwargs):
        # Killed right after git made the repository, before its first config was set.
        if args[0] == "config":
            raise KeyboardInterrupt
        return main(self, *args, **kwargs)

    yours = [["Surogate-Saga", "saga:2"], ["Surogate-Kind", "pickup"]]
    with monkeypatch.context() as patch:
        patch.setattr(History, "_main", cut)
        with pytest.raises(KeyboardInterrupt):
            LocalHistory.at(tmp_path / "store", folder, thread="t2", user="u1").pickup(author=YOURS, trailers=yours)
    assert (tmp_path / "store" / "clones" / "t2" / "HEAD").is_file()
    # The next request finds the repository there, and names nothing history leaves out as yours.
    picked = LocalHistory.at(tmp_path / "store", folder, thread="t2", user="u1").pickup(author=YOURS, trailers=yours)
    assert (picked["commit"], picked["picked_up"]) == (None, [])
    (folder / "notes.txt").write_text("v2 notes, saved by you\n")
    picked = LocalHistory.at(tmp_path / "store", folder, thread="t2", user="u1").pickup(author=YOURS, trailers=yours)
    assert [p["path"] for p in picked["picked_up"]] == ["notes.txt"]


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


def test_a_deletion_lands_from_a_copy_that_holds_the_harnesss_own_folders(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    # As every turn of a thread on a computer leaves its copy: the harness's mark and what its tools kept, and the whiteboard.
    (one.copy / ".surogates-results").mkdir()
    (one.copy / ".surogates-results" / ".turn").write_text("mark")
    (one.copy / "_whiteboard").mkdir()
    (one.copy / "_whiteboard" / "canvas.json").write_text("{}")
    (one.copy / "notes.txt").unlink()
    landed = land(one, "saga:1")
    assert [(c["path"], c["after"]) for c in landed["changes"]] == [("notes.txt", None)] and landed["overlapped"] == []
    assert not (folder / "notes.txt").exists()
    # A file the turn itself made where history leaves out still holds a deletion git cannot pair: it may be a move there.
    (one.copy / "build").mkdir()
    os.replace(one.copy / "Report.docx", one.copy / "build" / "Report.docx")
    held = land(one, "saga:2")
    assert (held["changes"], [(o["path"], o["reason"]) for o in held["overlapped"]]) == ([], [("Report.docx", "with")])
    assert (folder / "Report.docx").is_file()


@pytest.mark.parametrize(("left_out", "said"), [
    ("mkdir __pycache__ && echo x > __pycache__/tool.cpython-312.pyc", (["__pycache__/"], [])),
    ("echo SECRET=1 > .env", ([".env"], [])),
    ("mkdir -p node_modules/pkg && echo x > node_modules/pkg/index.js", (["node_modules/"], [])),
    ("git init -q vendor && echo x > vendor/a.txt", ([], ["vendor/"])),
    ("mkdir -p .threads/checkout && echo x > .threads/checkout/a.txt", ([], [])),
    ("mkdir empty", (["empty/"], [])),
])
def test_a_later_turns_deletion_lands_though_the_copy_holds_what_an_earlier_turn_left_out(tmp_path, folder, left_out, said):
    one = a_copy(tmp_path, folder)
    subprocess.run(left_out, shell=True, cwd=one.copy, check=True, env=HERMETIC, capture_output=True)
    (one.copy / "Report.docx").write_bytes(b"PK\x03\x04 report v2")
    first = land(one, "saga:1")
    assert (first["excluded"], first["repositories"]) == said and [c["path"] for c in first["changes"]] == ["Report.docx"]
    # A copy here outlives its turn, with what the turn left out in it.  The next turn made none of that:
    # its deletion is no move git could not see, and lands.
    (one.copy / "notes.txt").unlink()
    second = land(one, "saga:2")
    assert [(c["path"], c["after"]) for c in second["changes"]] == [("notes.txt", None)] and second["overlapped"] == []
    assert not (folder / "notes.txt").exists()
    # Nor is it said again to be unsaved: it was the first turn's, and was said then.
    assert (second["excluded"], second["repositories"]) == ([], [])


@pytest.mark.parametrize(("left_out", "again", "said"), [
    ("echo SECRET=1 > .env", "echo SECRET=2 > .env", ([".env"], [])),
    ("mkdir -p node_modules/pkg && echo x > node_modules/pkg/index.js", "echo y > node_modules/pkg/other.js", (["node_modules/"], [])),
    ("git init -q vendor && mkdir vendor/lib && echo x > vendor/lib/a.txt", "echo xx > vendor/lib/a.txt", ([], ["vendor/"])),
])
def test_what_an_earlier_turn_left_out_and_a_later_one_writes_again_is_the_later_ones_write(tmp_path, folder, left_out, again, said):
    one = a_copy(tmp_path, folder)
    subprocess.run(left_out, shell=True, cwd=one.copy, check=True, env=HERMETIC, capture_output=True)
    (one.copy / "Report.docx").write_bytes(b"PK\x03\x04 report v2")
    land(one, "saga:1")
    # The next turn writes there again, and deletes a file: that may be a move, and the deletion waits.
    subprocess.run(again, shell=True, cwd=one.copy, check=True, env=HERMETIC, capture_output=True)
    (one.copy / "notes.txt").unlink()
    second = land(one, "saga:2")
    assert (second["changes"], [(o["path"], o["reason"]) for o in second["overlapped"]]) == ([], [("notes.txt", "with")])
    assert (second["excluded"], second["repositories"]) == said and (folder / "notes.txt").is_file()


def test_a_name_that_is_not_utf8_inside_a_folder_history_leaves_out_is_left_out_with_it_and_counted_by_it(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    (one.copy / "node_modules" / "pkg").mkdir(parents=True)
    # A package's test data, named in latin-1: history never records it, and never names it.
    latin = os.fsencode(one.copy / "node_modules" / "pkg") + b"/caf\xe9.txt"
    with open(latin, "wb") as file:
        file.write(b"test data\n")
    (one.copy / "Report.docx").write_bytes(b"PK\x03\x04 report by one")
    first = land(one, "saga:1")
    assert ([c["path"] for c in first["changes"]], first["excluded"]) == (["Report.docx"], ["node_modules/"])
    assert (folder / "Report.docx").read_bytes() == b"PK\x03\x04 report by one"
    # Left as it is, it is no later turn's write: the next turn's deletion lands.
    (one.copy / "notes.txt").unlink()
    second = land(one, "saga:2")
    assert ([(c["path"], c["after"]) for c in second["changes"]], second["excluded"]) == ([("notes.txt", None)], [])
    # Written again, it is: the folder it is in is new since the copy's base, and the turn's deletion waits.
    with open(latin, "wb") as file:
        file.write(b"test data, written again\n")
    (one.copy / "Report.docx").unlink()
    third = land(one, "saga:3")
    assert ([(o["path"], o["reason"]) for o in third["overlapped"]], third["excluded"]) == ([("Report.docx", "with")], ["node_modules/"])


def test_what_history_leaves_out_is_told_from_what_its_copy_held_at_its_base_in_the_time_it_takes_to_look_at_it(tmp_path):
    folder = tmp_path / "Documents"
    for n in range(1000):
        (folder / f"p-{n:04d}").mkdir(parents=True)
        (folder / f"p-{n:04d}" / "mod.py").write_text(f"N = {n}\n")
    (folder / "notes.txt").write_text("v1 notes\n")
    one = a_copy(tmp_path, folder)
    # A __pycache__/ in every package, noted at the landing: a thousand folders history leaves out.
    for n in range(1000):
        (one.copy / f"p-{n:04d}" / "__pycache__").mkdir()
        for k in range(5):
            (one.copy / f"p-{n:04d}" / "__pycache__" / f"m{k}.pyc").write_bytes(b"\0")
    (one.copy / "notes.txt").write_text("v2 notes\n")
    assert len(land(one, "saga:1")["excluded"]) == 1000
    # Then one of them gets fifty thousand files written again, as a reinstall would.
    for k in range(50_000):
        (one.copy / "p-0000" / "__pycache__" / f"w{k}.pyc").write_bytes(b"\1")
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    looks, tells = [], []
    for _ in range(2):
        begun = time.monotonic()
        again._left_out()
        looks.append(time.monotonic() - begun)
        begun = time.monotonic()
        excluded, repositories, wrote = again._excluded()
        tells.append(time.monotonic() - begun)
    assert (excluded, repositories, wrote) == (["p-0000/__pycache__/"], [], True)
    # Telling them apart costs about what looking at them does: it grows with them, not with folders times files.
    assert min(tells) < 3 * min(looks) + 1.0, (looks, tells)


#: What an earlier guest can leave where a thread's repository notes what its copy held at its base.
NOTES = {
    "a link to a note": lambda note, real: note.symlink_to(real),
    "a pipe": lambda note, real: os.mkfifo(note),
    "a folder": lambda note, real: note.mkdir(),
    "no json": lambda note, real: note.write_text("{"),
    "json nested past any bound": lambda note, real: note.write_text("[" * 100_000 + "]" * 100_000),
    "a list": lambda note, real: note.write_text("[]"),
    "names that are no list": lambda note, real: note.write_text(json.dumps({"names": "__pycache__/", "files": {}})),
    "tokens that are no words": lambda note, real: note.write_text(json.dumps({"names": [], "files": {"__pycache__/tool.cpython-312.pyc": 1}})),
    "more than a request reads": lambda note, real: note.write_bytes(b" " * ((64 << 20) + 1)),
}


@pytest.mark.parametrize("planted", list(NOTES))
def test_what_an_earlier_guest_left_for_a_copys_note_is_no_note_and_counts_all_it_holds_as_written(tmp_path, folder, planted):
    one = a_copy(tmp_path, folder)
    (one.copy / "__pycache__").mkdir()
    (one.copy / "__pycache__" / "tool.cpython-312.pyc").write_bytes(b"\0tool")
    (one.copy / "Report.docx").write_bytes(b"PK\x03\x04 report v2")
    land(one, "saga:1")
    note = one.repo / "left-out"
    real = tmp_path / "elsewhere"
    shutil.move(note, real)
    NOTES[planted](note, real)
    # Read as data, it is none: everything history leaves out in the copy is taken as written since, and the deletion waits.
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    assert again._seen() == {"names": [], "files": {}}
    # And nothing of it is left open: the guest's agent runs request after request, and a handle a read kept is never let go.
    assert held_open(note) == []
    if planted in ("a link to a note", "a pipe"):
        # Anything but files and folders in the repository has it set aside whole at the next request, the note with it.
        assert again.open() == {"copy": "made", "set_aside_folders": set_aside_whole(tmp_path / "store")}
        return
    (again.copy / "notes.txt").unlink()
    held = land(again, "saga:2")
    assert [(o["path"], o["reason"]) for o in held["overlapped"]] == [("notes.txt", "with")] and held["excluded"] == ["__pycache__/"]


def test_a_folder_an_earlier_guest_left_where_the_repository_keeps_a_file_of_its_own_is_made_that_file_and_left_open_nowhere(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    attributes = one.repo / "info" / "attributes"
    attributes.unlink()
    attributes.mkdir()
    # The next request pins the file in the folder's place before any git reads it.
    LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").open()
    assert attributes.read_text() == local_history._ATTRIBUTES
    assert held_open(attributes) == []


def test_a_file_that_a_folder_takes_the_place_of_as_it_is_read_is_left_open_nowhere(tmp_path, monkeypatch):
    file = tmp_path / "notes.txt"
    file.write_text("v1 notes\n")
    real = os.open

    def swapped(path, *args, **kwargs):
        # Between the look at it and the open, a folder takes the file's place.
        if path == str(file):
            file.unlink()
            file.mkdir()
        return real(path, *args, **kwargs)

    monkeypatch.setattr(local_history.os, "open", swapped)
    with pytest.raises(IsADirectoryError):
        local_history._recorded(str(file))
    monkeypatch.undo()
    assert held_open(file) == []


def test_a_deletion_waits_while_the_turn_itself_writes_where_history_leaves_out_however_old_that_place_is(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    (one.copy / "build").mkdir()
    (one.copy / "build" / "old.bin").write_text("built\n")
    (one.copy / "Report.docx").write_bytes(b"PK\x03\x04 report v2")
    land(one, "saga:1")
    # A move into a folder history leaves out, which an earlier turn made: git sees a deletion, and no landing has the file.
    os.replace(one.copy / "notes.txt", one.copy / "build" / "notes.txt")
    moved = land(one, "saga:2")
    assert (moved["changes"], [(o["path"], o["reason"]) for o in moved["overlapped"]]) == ([], [("notes.txt", "with")])
    assert (folder / "notes.txt").is_file() and moved["excluded"] == ["build/"]
    # A file there written again in place holds a deletion the same way: its name is not new, and it is newer.
    (one.copy / "build" / "old.bin").write_text("built again, and longer\n")
    (one.copy / "notes.txt").unlink()
    again = land(one, "saga:3")
    assert [(o["path"], o["reason"]) for o in again["overlapped"]] == [("notes.txt", "with")] and (folder / "notes.txt").is_file()
    # A turn that writes nothing there deletes the file, and it lands.
    (one.copy / "notes.txt").unlink()
    assert [(c["path"], c["after"]) for c in land(one, "saga:4")["changes"]] == [("notes.txt", None)]
    assert not (folder / "notes.txt").exists()


def test_what_a_kept_turn_left_out_is_its_threads_to_land_still_so_its_deletion_waits(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    (one.copy / "build").mkdir()
    os.replace(one.copy / "notes.txt", one.copy / "build" / "notes.txt")
    # The turn fails, and is kept on its branch.  Nothing of it has landed: what it wrote is still its thread's to land.
    one.keep(author=A, trailers=[["Surogate-Saga", "saga:kept"], ["Surogate-Kind", "turn"]], base=True)
    held = land(one, "saga:2")
    assert [(o["path"], o["reason"]) for o in held["overlapped"]] == [("notes.txt", "with")]
    assert (folder / "notes.txt").read_text() == "v1 notes\n"


def up_to_its_record(history: LocalHistory, saga: str, author: dict = B, left: tuple[str, ...] = ()) -> dict:
    """*history*'s turn landed up to its record, as a landing saga runs it, and the record's step: each change
    applied by the host, a copy of each file, but those of *left*, which no landing writes."""
    picked = history.pickup(author=YOURS, trailers=[["Surogate-Saga", saga], ["Surogate-Kind", "pickup"]])
    turn = history.commit_turn(author=author, trailers=[["Surogate-Saga", saga], ["Surogate-Kind", "turn"]], pickup=picked["commit"])
    changes = [change for change in turn["changes"] if change["path"] not in left]
    applied(history.project, history.copy, changes)
    step = {
        "turn": turn["commit"], "applied": changes, "author": author, "trailers": [["Surogate-Saga", saga], ["Surogate-Kind", "landing"]],
        "main": picked["main"], "pickup": picked["commit"],
    }
    return {**step, "left": list(left)} if left else step


def late(ours: LocalHistory) -> None:
    """A command of the landed turn's, still running after its record, writes where history leaves out."""
    (ours.copy / "__pycache__" / "late.cpython-312.pyc").write_bytes(b"\0late")


def a_landing_of_theirs(root: Path) -> None:
    """Another thread lands a file: ``main`` moves, and our thread's clean copy moves to it at its next open."""
    theirs = LocalHistory.at(root / "store", root / "Documents", thread=THEIRS, user="u1")
    theirs.open()
    (theirs.copy / "theirs.md").write_text("theirs\n")
    land(theirs, "saga:theirs")


#: Each way a thread's copy starts again from its base, holding what history leaves out; then the next turn's
#: first act.  An open that moves the copy is that way itself; a turn whose open was not answered starts with
#: the snapshot before its first step.
STARTS = {
    "a landing recorded whole": lambda root, ours: (land(ours, "saga:ours", B), "snapshot"),
    "a landing whose record was cut after its push": lambda root, ours: (
        stepped(root, OURS, "record", up_to_its_record(ours, "saga:ours"), before="update-ref refs/heads/main"), "snapshot",
    ),
    "an open's move to main": lambda root, ours: (land(ours, "saga:ours", B), late(ours), a_landing_of_theirs(root), "open"),
    "an open's move to main cut before its base": lambda root, ours: (
        land(ours, "saga:ours", B), late(ours), a_landing_of_theirs(root),
        stepped(root, OURS, "open", {}, before="update-ref refs/bases/"), "snapshot",
    ),
}


@pytest.mark.parametrize("start", list(STARTS))
def test_what_the_copy_holds_where_history_leaves_out_when_it_starts_again_from_its_base_is_no_later_turns_write(tmp_path, start):
    root = tmp_path / "whole"
    folder = root / "Documents"
    folder.mkdir(parents=True)
    for name in ("Report.docx", "notes.txt"):
        (folder / name).write_text(f"{name} v1\n")
    ours = a_copy(root, folder, OURS)
    # The turn runs a script, as any coding thread does: python leaves __pycache__ in the copy, which outlives the turn.
    (ours.copy / "__pycache__").mkdir()
    (ours.copy / "__pycache__" / "tool.cpython-312.pyc").write_bytes(b"\0tool")
    (ours.copy / "Report.docx").write_text("our report\n")
    *_, first = STARTS[start](root, ours)
    # The next turn deletes a file and writes nothing where history leaves out: the deletion lands.
    again = LocalHistory.at(root / "store", folder, thread=OURS, user="u1")
    if first == "open":
        assert again.open() == {"copy": "moved"}
    else:
        again.snapshot("before a step")
    (again.copy / "notes.txt").unlink()
    after = land(again, "saga:next", B)
    assert [(c["path"], c["after"]) for c in after["changes"]] == [("notes.txt", None)] and after["overlapped"] == [], start
    assert (after["excluded"], after["repositories"]) == ([], []) and not (folder / "notes.txt").exists()


def test_a_copy_made_again_starts_with_nothing_history_leaves_out_seen_in_it(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    (one.copy / "__pycache__").mkdir()
    (one.copy / "__pycache__" / "tool.cpython-312.pyc").write_bytes(b"\0tool")
    (one.copy / "notes.txt").write_text("the thread's notes\n")
    land(one, "saga:1")
    assert one._seen()["names"] == ["__pycache__/"]
    # A failed turn is kept on the branch; then the copy is let go, and made again from its branch, which it does
    # not move from: none of what the old one held is in the new one, and nothing of it is taken as seen there.
    (one.copy / "Draft.md").write_text("a failed turn's, kept\n")
    one.keep(author=A, trailers=[["Surogate-Kind", "turn"]], base=True)
    shutil.rmtree(one.copy)
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    assert again.open() == {"copy": "kept"} and not (again.copy / "__pycache__").exists()
    assert again._seen() == {"names": [], "files": {}}


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


def test_a_new_file_this_computer_cannot_read_is_refused_by_its_name_until_it_is_left_out(tmp_path, folder):
    (folder / "build out").mkdir()
    secret = folder / "build out" / "secret.key"
    # As a docker build leaves one of yours: another user's, which git cannot read.
    secret.write_text("a key\n")
    secret.chmod(0)
    try:
        for _ in range(2):
            with refused("file_unreadable", (
                '^refused the request: the file "build out/secret.key" cannot be read on this computer, so history cannot '
                "record it: let it be read, as by chmod a\\+r, or add it to the folder's .gitignore$"
            )):
                a_copy(tmp_path, folder)
        # Left out by the folder's own ignores, it is none of history's to read.
        (folder / ".gitignore").write_text("secret.key\n")
        one = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
        assert one.open() == {"copy": "made"} and not (one.copy / "build out").exists()
    finally:
        secret.chmod(0o644)


def test_a_file_of_historys_this_computer_cannot_read_since_is_refused_by_its_name_until_it_is_read(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    (one.copy / "notes.txt").write_text("the thread's notes\n")
    (folder / "Report.docx").write_bytes(b"PK\x03\x04 report v2, saved by you")
    (folder / "Report.docx").chmod(0)
    try:
        for _ in range(2):
            with refused("file_unreadable", (
                '^refused the request: the file "Report.docx" cannot be read on this computer, so history cannot record '
                "its change: let it be read, as by chmod a\\+r, or move it out of the folder$"
            )):
                land(LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1"), "saga:1")
    finally:
        (folder / "Report.docx").chmod(0o644)
    landed = land(LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1"), "saga:1")
    assert [c["path"] for c in landed["changes"]] == ["notes.txt"]
    assert git(tmp_path / "store" / "history.git", "show", "refs/heads/main:Report.docx") == "PK\x03\x04 report v2, saved by you"


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
    # The thread's repository read another store's objects beside the history's: it may name what the history
    # does not hold, so it is made again, and its copy, which you have saved a file past, is set aside whole.
    opened = again.open()
    assert opened["copy"] == "made" and [name.rpartition(".")[2] for name in opened.get("set_aside_folders", [])] == ["copy"], opened
    assert not any(os.path.lexists(store / "history.git" / name) for name in ("commondir", "hooks", "modules"))
    # Made in this request, the thread's repository has only git's own sample hooks, which nothing runs.
    assert not any(os.path.lexists(again.repo / name) for name in ("commondir", "modules"))
    assert all(hook.name.endswith(".sample") for hook in (again.repo / "hooks").iterdir())
    # The repository's alternates are the one line that borrows the history's objects; the history's objects are its packs alone.
    assert (again.repo / "objects" / "info" / "alternates").read_text() == "../../../history.git/objects\n"
    assert sorted(p.name for p in (store / "history.git" / "objects").iterdir()) == ["pack"]
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


@pytest.mark.parametrize("first", ["open", "changed", "snapshot", "restore", "fetch", "pickup", "picked", "commit", "record", "keep", "close"])
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
        "picked": lambda: again.picked(pickup=tip, after="notes"),
        "commit": lambda: again.commit_turn(author=A, trailers=saga, pickup=picked["commit"]),
        "record": lambda: again.record(turn=turn["commit"], applied=[], author=A, trailers=saga, main=picked["main"], pickup=picked["commit"]),
        "keep": lambda: again.keep(author=A, trailers=[["Surogate-Kind", "turn"]], base=True),
        "close": again.close,
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


@pytest.fixture()
def film(folder: Path) -> Path:
    """The folder with a file that does not compress: what a thread's repository would hold a second copy of."""
    (folder / "Film.bin").write_bytes(os.urandom(2 << 20))
    os.utime(folder / "Film.bin", (time.time() - 60, time.time() - 60))
    return folder


def test_a_threads_repository_borrows_the_historys_objects_and_holds_only_what_it_made(tmp_path, film):
    one = a_copy(tmp_path, film)
    store = tmp_path / "store" / "history.git"
    # The folder's first commit is its history's first, pushed with the first copy, and its index is kept with it.
    assert git(store, "rev-parse", "refs/heads/main") == git(one.repo, "rev-parse", "refs/heads/main")
    assert (store / "index").is_file()
    assert (one.repo / "objects" / "info" / "alternates").read_text() == "../../../history.git/objects\n"
    assert size(store / "objects") > 2 << 20 and size(one.repo / "objects") < 64 << 10
    two = a_copy(tmp_path, film, "t2")
    assert (two.copy / "Film.bin").read_bytes() == (film / "Film.bin").read_bytes()
    assert size(two.repo / "objects") < 64 << 10
    (two.copy / "notes.txt").write_text("v2 notes, by B\n")
    two.snapshot("before a step")
    assert [c["path"] for c in land(two, "saga:1", B)["changes"]] == ["notes.txt"]
    # What it made and the history now holds is the history's alone: its snapshot is its own.
    assert size(two.repo / "objects") < 64 << 10
    assert git(store, "rev-parse", "refs/heads/main") not in held_here(two.repo)
    connected(tmp_path / "store")
    # The first thread's next turn reads the landing where the history has it, and copies none of it.
    assert one.open() == {"copy": "moved"}
    assert (one.copy / "notes.txt").read_text() == "v2 notes, by B\n"
    assert size(one.repo / "objects") < 64 << 10 and size(store / "objects") < 3 << 20
    # A request that finds the repository borrowing as it should, and noting the history's packs as they are, writes nothing for it.
    assert LocalHistory.at(tmp_path / "store", film, thread="t1", user="u1").changed() == {"paths": []}
    written = [(one.repo / name).stat().st_ctime_ns for name in ("objects/info/alternates", "borrowed")]
    assert LocalHistory.at(tmp_path / "store", film, thread="t1", user="u1").changed() == {"paths": []}
    assert [(one.repo / name).stat().st_ctime_ns for name in ("objects/info/alternates", "borrowed")] == written
    connected(tmp_path / "store")


def test_a_push_notes_its_pack_before_the_repack_lets_go_of_what_the_pack_holds(tmp_path, folder, monkeypatch):
    one = a_copy(tmp_path, folder)
    (one.copy / "notes.txt").write_text("the thread's notes\n")
    one.snapshot("before a step")
    pack = LocalHistory._pack

    def killed_after_it(self, **kwargs):
        pack(self, **kwargs)
        raise KeyboardInterrupt

    # Killed right after the repack that follows the turn's push: the turn's objects are the history's alone now.
    with monkeypatch.context() as cut:
        cut.setattr(LocalHistory, "_pack", killed_after_it)
        with pytest.raises(KeyboardInterrupt):
            one.commit_turn(author=A, trailers=[["Surogate-Saga", "saga:1"], ["Surogate-Kind", "turn"]])
    turn = git(tmp_path / "store" / "history.git", "rev-parse", "refs/heads/threads/t1")
    assert turn not in held_here(one.repo)
    # Each object the repository reaches it holds, or reads from a pack its note names: a history that no longer
    # holds that pack is known by the note.
    lent = set()
    for pack_id in noted(one.repo):
        with open(tmp_path / "store" / "history.git" / "objects" / "pack" / f"pack-{pack_id}.idx", "rb") as listed:
            shown = subprocess.run(["git", "show-index"], stdin=listed, capture_output=True, text=True, check=True, env=HERMETIC).stdout
        lent |= {line.split()[1] for line in shown.splitlines()}
    reached = {line[:40] for line in git(one.repo, "rev-list", "--objects", "--all").splitlines()}
    assert turn in reached and reached <= held_here(one.repo) | lent
    connected(tmp_path / "store")


def test_a_copys_repository_packed_again_takes_none_of_the_historys_objects(tmp_path, film, monkeypatch):
    monkeypatch.setattr(local_history, "_PACKS", 2)
    one = a_copy(tmp_path, film)
    for n in range(4):
        (one.copy / "notes.txt").write_text(f"edit {n}\n")
        one.snapshot("before a step")
    assert len(list((one.repo / "objects" / "pack").glob("*.pack"))) > 2
    assert one.open() == {"copy": "kept"}
    assert len(list((one.repo / "objects" / "pack").glob("*.pack"))) == 1
    assert size(one.repo / "objects") < 64 << 10
    connected(tmp_path / "store")


def test_a_push_searches_no_delta_between_large_files_and_a_large_files_later_version_is_whole_as_ever(tmp_path):
    folder = tmp_path / "Documents"
    (folder / "many").mkdir(parents=True)
    film = os.urandom(2 << 20)
    # Two large files alike but for a byte, as a film and its copy; two small ones alike but for a line.
    (folder / "film.bin").write_bytes(film)
    (folder / "film copy.bin").write_bytes(film[:-1] + b"x")
    rows = "".join(f"row {n}, the same in both\n" for n in range(2000))
    (folder / "notes.txt").write_text(f"{rows}one\n")
    (folder / "notes copy.txt").write_text(f"{rows}two\n")
    # Enough files besides that the folder is read as a real one is, by several readers into packs of their own.
    for n in range(100):
        (folder / "many" / f"{n}.txt").write_text(f"file {n}\n")
    for file in folder.rglob("*"):
        os.utime(file, (time.time() - 60, time.time() - 60))
    one = a_copy(tmp_path, folder)
    store = tmp_path / "store" / "history.git"

    def blob(name: str) -> str:
        return git(store, "rev-parse", f"refs/heads/main:{name}")

    # The first copy's push looks for no delta between large files, which in media it finds none of at great
    # cost; between small files it still does.
    held = deltas(store)
    assert blob("film.bin") not in held and blob("film copy.bin") not in held
    assert {blob("notes.txt"), blob("notes copy.txt")} & set(held)
    # A later push of a small change to a large file holds the new version whole, as it always did: a push's pack
    # is no thin one, so no version the history holds is a delta's base.
    (one.copy / "film.bin").write_bytes(film[:1000] + b"y" + film[1001:])
    assert [c["path"] for c in land(one, "saga:1")["changes"]] == ["film.bin"]
    assert blob("film.bin") not in deltas(store)
    connected(tmp_path / "store")


def test_a_threads_versions_of_a_large_file_are_packed_against_each_other_at_a_turns_start_and_not_after_each_push(tmp_path, folder, monkeypatch):
    monkeypatch.setattr(local_history, "_PACKS", 3)
    one = a_copy(tmp_path, folder)
    table = "".join(f"row {n}, value {n * 7}\n" for n in range(60_000))
    assert len(table) > 1 << 20
    versions = []
    for n in range(4):
        (one.copy / "table.csv").write_text(f"{table}edit {n}\n")
        one.snapshot("before a step")
        versions.append(git(one.repo, "rev-parse", "refs/heads/threads/t1:table.csv"))
    # The repack after a push lets go of what the history holds, its last version among it, and keeps the rest:
    # it runs at every landing, and looks for no delta between large files.
    one.keep(author=A, trailers=[["Surogate-Kind", "turn"]], base=True)
    assert versions[3] not in held_here(one.repo) and set(versions[:3]) <= held_here(one.repo)
    assert not set(versions[:3]) & set(deltas(one.repo))
    # Once the snapshots have left enough packs, a turn's start packs them again against each other: one version
    # of a file and the small changes to it.
    for n in range(4, 8):
        (one.copy / "table.csv").write_text(f"{table}edit {n}\n")
        one.snapshot("before a step")
    assert LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").open() == {"copy": "kept"}
    assert len(set(versions[:3]) & set(deltas(one.repo))) >= 2
    connected(tmp_path / "store")


def test_a_threads_repository_asks_of_no_commit_whether_it_was_fetched(tmp_path, folder, monkeypatch):
    commands = []
    run = subprocess.run

    def watched(args, *rest, **kwargs):
        if args and args[0] == "git":
            command = list(args[1:])
            while command[:1] == ["-c"]:
                command = command[2:]
            commands.append(command)
        return run(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "run", watched)
    # A thread's life: its first copy, another's, landings whole, one leaving a file out, one cut after its push and
    # finished by the next open, and an open that moves the copy.
    one, two = a_copy(tmp_path, folder, "t1"), a_copy(tmp_path, folder, "t2")
    (two.copy / "B.md").write_text("B's own\n")
    land(two, "saga:1", B)
    assert one.open() == {"copy": "moved"}
    (one.copy / ".vscode").mkdir()
    (one.copy / ".vscode" / "tasks.json").write_text("{}\n")
    (one.copy / "A.md").write_text("A's own\n")
    one.record(**up_to_its_record(one, "saga:2", A, (".vscode/tasks.json",)))
    (two.copy / "B2.md").write_text("more of B's\n")
    History.record(two, **up_to_its_record(two, "saga:3", B))
    assert LocalHistory.at(tmp_path / "store", folder, thread="t2", user="u1").open() == {"copy": "moved"}
    assert LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").fetch(saga="saga:3")["landing"] is not None
    # Every commit the history holds is read where it lies: none is fetched, and of none is it asked whether it was.
    assert commands and not [command for command in commands if command[:1] == ["fetch"] or command[:3] == ["rev-list", "-n", "1"]]
    connected(tmp_path / "store")


def test_a_threads_repository_fetches_nothing_and_reads_the_historys_commits_with_their_parents(tmp_path, folder):
    one, two = a_copy(tmp_path, folder, "t1"), a_copy(tmp_path, folder, "t2")
    (two.copy / "B.md").write_text("B's own\n")
    main = land(two, "saga:2", B)["landing"]
    # What an earlier guest can leave: the thread's repository told the history is cut where main is, which the history's own says nowhere.
    (one.repo / "shallow").write_text(f"{main}\n")
    assert LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").open() == {"copy": "moved"}
    assert (one.copy / "B.md").read_text() == "B's own\n"
    # The repository reads the history as the history does, whole, and took nothing of it by a fetch.
    assert not os.path.lexists(one.repo / "shallow")
    assert git(one.repo, "rev-list", "--count", main) == git(tmp_path / "store" / "history.git", "rev-list", "--count", main)
    assert main not in held_here(one.repo)
    connected(tmp_path / "store")


OTHER = "7d8e9f00-1a2b-4c3d-8e4f-5a6b7c8d9e0f"


def request(tmp_path: Path, folder: Path, thread: str, action: str, **args) -> dict:
    """One request, as the agent's runner takes it."""
    return local_history.run({
        "store": str(tmp_path / "store"), "folder": str(folder), "thread": thread, "user": "u1", "action": action, "args": args,
    })


def test_what_a_thread_holds_reaches_another_only_as_objects_of_the_history(tmp_path, folder):
    assert request(tmp_path, folder, THREAD, "open") == {"copy": "made"}
    assert request(tmp_path, folder, OTHER, "open") == {"copy": "made"}
    place = tmp_path / "store"
    store = place / "history.git"
    one = LocalHistory.at(place, folder, thread=THREAD, user="u1")
    two = LocalHistory.at(place, folder, thread=OTHER, user="u1")
    (one.copy / "Report.docx").write_bytes(b"PK\x03\x04 A's unlanded report")
    unlanded = request(tmp_path, folder, THREAD, "snapshot", reason="before a step")["hash"]
    # Two of another folder's files, in an object store the guest's root can read.
    outside = tmp_path / "outside.git"
    subprocess.run(["git", "init", "-q", "--bare", str(outside)], check=True, env=HERMETIC)
    secret, loose = (subprocess.run(
        ["git", f"--git-dir={outside}", "hash-object", "-w", "--stdin"], input=text,
        capture_output=True, text=True, check=True, env=HERMETIC,
    ).stdout.strip() for text in ("another folder's file\n", "and another\n"))
    # What an earlier guest could plant: the history's objects leading on to another store, files that steer
    # git's walk of them, and a loose object that is a file of that store's.
    (store / "objects" / "info").mkdir(exist_ok=True)
    (store / "objects" / "info" / "alternates").write_text(f"{outside / 'objects'}\n")
    (store / "objects" / "info" / "commit-graph").write_bytes(b"CGPH not a graph")
    (store / "objects" / loose[:2]).mkdir()
    shutil.copyfile(outside / "objects" / loose[:2] / loose[2:], store / "objects" / loose[:2] / loose[2:])
    pack = next((store / "objects" / "pack").glob("pack-*.pack"))
    for planted in ("multi-pack-index", *(pack.with_suffix(kind).name for kind in (".promisor", ".bitmap", ".rev", ".keep")), "pack-planted.idx"):
        (store / "objects" / "pack" / planted).write_bytes(b"planted")
    (two.copy / "notes.txt").write_text("B's notes\n")
    request(tmp_path, folder, OTHER, "snapshot", reason="before a step")
    # The history's objects are its packs alone, each with its index.
    assert [p.name for p in (store / "objects").iterdir()] == ["pack"]
    assert sorted(p.suffix for p in (store / "objects" / "pack").iterdir()) == [".idx", ".pack"] * len(list((store / "objects" / "pack").glob("*.pack")))
    # Neither of the other folder's files, nor the first thread's unlanded work, is an object B's repository reads.
    for hidden in (secret, loose, unlanded):
        with pytest.raises(subprocess.CalledProcessError):
            git(two.repo, "cat-file", "-e", hidden)
    assert git(two.repo, "for-each-ref", "--format=%(refname)").splitlines() == [f"refs/bases/{OTHER}", "refs/heads/main", f"refs/heads/threads/{OTHER}"]
    # A's work reaches B once a landing has put it in the history, as the folder's own file.
    assert [c["path"] for c in land(one, "saga:1")["changes"]] == ["Report.docx"]
    assert [c["path"] for c in land(two, "saga:2", B)["changes"]] == ["notes.txt"]
    assert (two.copy / "Report.docx").read_bytes() == b"PK\x03\x04 A's unlanded report"
    assert (folder / "notes.txt").read_text() == "B's notes\n"
    connected(place)
    # The same loose object as a link into that store is no file of the history's: it refuses the history whole,
    # as a link anywhere in it does, before any thread's git reads it as an object.
    (store / "objects" / loose[:2]).mkdir()
    (store / "objects" / loose[:2] / loose[2:]).symlink_to(outside / "objects" / loose[:2] / loose[2:])
    for thread in (THREAD, OTHER):
        with refused("history_refused"):
            request(tmp_path, folder, thread, "changed")
    assert (store / "objects" / loose[:2] / loose[2:]).is_symlink()


def test_a_repository_or_a_copy_that_is_a_link_to_another_threads_is_no_threads(tmp_path, folder):
    assert request(tmp_path, folder, THREAD, "open") == {"copy": "made"}
    assert request(tmp_path, folder, OTHER, "open") == {"copy": "made"}
    place = tmp_path / "store"
    (place / "threads" / THREAD / "Report.docx").write_bytes(b"PK\x03\x04 A's report")
    # An earlier guest made the second thread's repository and copy the first one's.
    for part in ("clones", "threads"):
        shutil.rmtree(place / part / OTHER)
        (place / part / OTHER).symlink_to(place / part / THREAD)
    assert request(tmp_path, folder, OTHER, "open", moves=False) == {"copy": "made"}
    assert not (place / "clones" / OTHER).is_symlink() and not (place / "threads" / OTHER).is_symlink()
    assert (place / "threads" / OTHER / "Report.docx").read_bytes() == b"PK\x03\x04 report v1"
    assert (place / "threads" / THREAD / "Report.docx").read_bytes() == b"PK\x03\x04 A's report"
    # The second thread's repository reads the history's objects, and none of the first one's.
    assert (place / "clones" / OTHER / "objects" / "info" / "alternates").read_text() == "../../../history.git/objects\n"
    connected(place)


#: What can leave a thread's repository reading objects that are not its folder's history's, or not reading that history
#: as it did: whatever it names may then be held by neither.
ASTRAY = {
    "its alternates gone": lambda one, other: (one.repo / "objects" / "info" / "alternates").unlink(),
    "another store beside the history": lambda one, other: (one.repo / "objects" / "info" / "alternates").write_text(
        f"../../../history.git/objects\n{other / 'objects'}\n",
    ),
    "another store in the history's stead": lambda one, other: (one.repo / "objects" / "info" / "alternates").write_text(f"{other / 'objects'}\n"),
    "the history by another path": lambda one, other: (one.repo / "objects" / "info" / "alternates").write_text(f"{one.store / 'objects'}\n"),
    "a pack it read from gone from the history": lambda one, other: (one.repo / "borrowed").write_text(f"{(one.repo / 'borrowed').read_text()}{'0' * 40}\n"),
    "a pack it read from left there without its index": lambda one, other: (
        shutil.copyfile(sorted((one.store / "objects" / "pack").glob("pack-*.pack"))[0], one.store / "objects" / "pack" / f"pack-{'e' * 40}.pack"),
        (one.repo / "borrowed").write_text(f"{(one.repo / 'borrowed').read_text()}{'e' * 40}\n"),
    ),
    "its note of the packs it reads gone": lambda one, other: (one.repo / "borrowed").unlink(),
    "its note of the packs it reads no note": lambda one, other: (one.repo / "borrowed").write_text("not a pack\n"),
}


@pytest.mark.parametrize("astray", list(ASTRAY))
def test_a_threads_repository_that_does_not_read_its_history_as_it_borrowed_it_is_used_no_more_and_made_again(tmp_path, folder, astray):
    one = a_copy(tmp_path, folder)
    (one.copy / "Draft.md").write_text("in a snapshot\n")
    tip = one.snapshot("before a step")
    (one.copy / "notes.txt").write_text("the thread's notes, in no snapshot\n")
    other = tmp_path / "other.git"
    subprocess.run(["git", "init", "-q", "--bare", str(other)], check=True, env=HERMETIC)
    ASTRAY[astray](one, other)
    held = as_it_is(one.copy)
    # No git reads it, whatever request that reads it comes first: each is refused for want of a whole copy, and the
    # copy is as it was.  A landing's first look reads the history alone, and is answered from it.
    for ask in (lambda h: h.changed(), lambda h: h.snapshot("before a step"), lambda h: h.restore(tip)):
        with refused("no_whole_copy", "^refused the request: this thread has no whole copy, and its next open makes one$"):
            ask(LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1"))
    looked = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").fetch(saga="saga:1")
    assert looked["main"] == git(tmp_path / "store" / "history.git", "rev-parse", "refs/heads/main") and looked["landing"] is None
    assert as_it_is(one.copy) == held
    # Its next open makes it again from the folder's history; the repository and the copy, which hold the thread's
    # work, are set aside whole.
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    opened = again.open()
    assert set(opened) == {"copy", "set_aside_folders"} and opened["copy"] == "made", opened
    copy, repository = (tmp_path / "store" / "set-aside" / name for name in opened["set_aside_folders"])
    assert (copy.suffix, repository.suffix) == (".copy", ".repository") and as_it_is(copy) == held
    assert git(repository, "cat-file", "-p", f"{tip}:Draft.md") == "in a snapshot"
    # Set aside, it is read whole as it is kept: through the one line, which this computer writes, to the history
    # it borrowed, and to no other store an earlier guest gave it.
    assert (repository / "objects" / "info" / "alternates").read_text() == "../../../history.git/objects\n"
    assert (again.repo / "objects" / "info" / "alternates").read_text() == "../../../history.git/objects\n"
    connected(tmp_path / "store")
    assert files_of(again.copy) == files_of(folder) and again.changed() == {"paths": []}
    (again.copy / "A.md").write_text("made after\n")
    assert [c["path"] for c in land(again, "saga:1")["changes"]] == ["A.md"]


def test_a_threads_repository_whose_history_is_not_the_one_it_borrowed_is_used_no_more_and_made_again(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    (one.copy / "Draft.md").write_text("in a snapshot\n")
    one.snapshot("before a step")
    held = as_it_is(one.copy)
    # The folder's history is taken away by hand, you save a file, and the next thread's first copy makes the folder another.
    # (Made again from the same files in the same second, it would be the same history, pack for pack.)
    (tmp_path / "store" / "history.git").rename(tmp_path / "taken away")
    (folder / "yours.txt").write_text("saved by you since\n")
    assert a_copy(tmp_path, folder, "t2").changed() == {"paths": []}
    assert git(tmp_path / "store" / "history.git", "rev-parse", "refs/heads/main") != git(one.repo, "rev-parse", "refs/heads/main")
    # The first thread's repository names what that history never held: no git reads it, and its open makes it again.
    with refused("no_whole_copy"):
        LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").changed()
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    opened = again.open()
    assert opened["copy"] == "made" and [name.rpartition(".")[2] for name in opened["set_aside_folders"]] == ["copy", "repository"], opened
    assert as_it_is(tmp_path / "store" / "set-aside" / opened["set_aside_folders"][0]) == held
    # The repository set aside borrows the history there is, by the one line; what it read from went with the
    # history taken away, and only the copy beside it holds the thread's files whole.
    repository = tmp_path / "store" / "set-aside" / opened["set_aside_folders"][1]
    assert (repository / "objects" / "info" / "alternates").read_text() == "../../../history.git/objects\n"
    connected(tmp_path / "store", kept=False)
    assert files_of(again.copy) == files_of(folder)
    (again.copy / "A.md").write_text("made after\n")
    assert [c["path"] for c in land(again, "saga:1")["changes"]] == ["A.md"]


def test_a_folders_first_open_ends_only_once_its_first_commit_is_its_historys(tmp_path, folder, monkeypatch):
    one = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")

    def killed(*args, **kwargs):
        raise KeyboardInterrupt

    # Stopped as the folder's first commit is pushed: the first open has not ended, and the next makes all of it again.
    with monkeypatch.context() as cut:
        cut.setattr(History, "_push", killed)
        with pytest.raises(KeyboardInterrupt):
            one.open()
    assert (one.repo / "HEAD").is_file() and not (one.repo / "made").exists()
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    assert again.open() == {"copy": "made"} and set_aside_whole(tmp_path / "store") == []
    assert git(tmp_path / "store" / "history.git", "rev-parse", "refs/heads/main") == git(again.repo, "rev-parse", "refs/heads/main")
    assert files_of(again.copy) == files_of(folder)
    connected(tmp_path / "store")


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


def a_folder_of_thousands(tmp_path: Path) -> Path:
    """A folder whose copy takes git long enough to write that an open can be killed while it does: six
    thousand files, which git writes several at once."""
    real = tmp_path / "Documents"
    real.mkdir()
    for n in range(6000):
        (real / f"file-{n:04d}.txt").write_text(f"{n}\n" * 50)
        os.utime(real / f"file-{n:04d}.txt", (time.time() - 60, time.time() - 60))
    return real


def an_open_killed_in_its_checkout(place: Path, folder: Path, thread: str = "t1") -> None:
    """*thread*'s open, it and every process it started killed at once while git writes the files of the copy
    it makes: as a request's bound, a quit or a lost guest ends it.  What was at the copy's path before is
    out of the way by then, and the copy holds some of its files, git's own among them, and has no index."""
    copy, index = place / "threads" / thread, place / "clones" / thread / "worktrees" / thread / "index"
    # What is at the copy's path first is taken out of the way: the copy that is being made is another folder.
    first = os.stat(copy).st_ino if copy.exists() else None
    opening = subprocess.Popen(
        [sys.executable, "-c", (
            "import sys; from pathlib import Path; from surogates.sandbox.local_history import LocalHistory; "
            "LocalHistory.at(Path(sys.argv[1]), Path(sys.argv[2]), thread=sys.argv[3], user='u1').open()"
        ), str(place), str(folder), thread],
        cwd=Path(__file__).parents[1], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 120
        while opening.poll() is None and time.monotonic() < deadline:
            try:
                names, at = os.listdir(copy), os.stat(copy).st_ino
            except FileNotFoundError:
                first = None
                continue
            if at != first and len(names) >= 300 and ".git" in names:
                break
    finally:
        if opening.poll() is None:
            os.killpg(opening.pid, signal.SIGKILL)
        opening.wait()
    assert opening.returncode == -signal.SIGKILL, "the open ended before it was killed"
    assert ".git" in os.listdir(copy) and not index.is_file(), "the open was not killed inside its checkout"


@pytest.mark.parametrize("which", ["a first open", "a making again"])
def test_an_open_killed_inside_its_checkout_however_often_leaves_nothing_set_aside_and_nothing_told(tmp_path, which):
    folder, place = a_folder_of_thousands(tmp_path), tmp_path / "store"
    if which == "a making again":
        # The thread's repository is whole, a snapshot of its work in it, and its copy was let go.
        one = a_copy(tmp_path, folder)
        (one.copy / "work.md").write_text("in a snapshot\n")
        LocalHistory.at(place, folder, thread="t1", user="u1").snapshot("before a step")
        shutil.rmtree(one.copy)
    theirs = as_it_is(folder)
    for _ in range(5):
        an_open_killed_in_its_checkout(place, folder)
        connected(place)
        # What the cut left is no copy of the thread's: no request reads it, and nothing is set aside for it,
        # by the open that made it or by the one that finds it.
        with refused("no_whole_copy"):
            LocalHistory.at(place, folder, thread="t1", user="u1").changed()
        assert set_aside_whole(place) == []
    again = LocalHistory.at(place, folder, thread="t1", user="u1")
    assert again.open() == {"copy": "made" if which == "a first open" else "kept"}
    assert set_aside_whole(place) == [] and as_it_is(folder) == theirs
    # The copy made in the end is whole, with nothing of git's own in it, and the thread lands from it.
    assert files_of(again.copy) == sorted([*files_of(folder), *([("work.md", b"in a snapshot\n")] if which == "a making again" else [])])
    assert sorted(p.name for p in (place / "threads").iterdir()) == ["t1"] == sorted(p.name for p in (place / "clones").iterdir())
    (again.copy / "after.md").write_text("the turn after\n")
    assert [c["path"] for c in land(again, "saga:1")["changes"]] == ["after.md", "work.md"][:1 + (which == "a making again")]


def test_opens_killed_inside_their_checkout_never_let_a_threads_set_aside_work_go_nor_pass_what_it_keeps(tmp_path):
    folder, place = a_folder_of_thousands(tmp_path), tmp_path / "store"
    one = a_copy(tmp_path, folder)
    # The thread's copy was set aside whole four times, as many as a thread keeps, each with work of its own.
    for count in (1, 2, 3, 4):
        names = set_aside_once_more(one, f"the thread's work, {count}\n")["set_aside_folders"]
    assert len(names) == 4
    # It is once more, by an open that is killed while it writes the copy made again; and so are four opens after it.
    (one.copy / "unlanded.md").write_text("the thread's work, 5\n")
    (one.repo / "worktrees" / "t1" / "index").unlink()
    for _ in range(5):
        an_open_killed_in_its_checkout(place, folder)
        connected(place)
        kept = [name for name in set_aside_whole(place) if not name.endswith(".gone")]
        # Never more than a thread keeps, whatever was cut: the oldest went for the fifth, and nothing for a
        # copy whose making was cut.  Each is the thread's work, whole.
        assert len(kept) == 4 and kept[:3] == names[1:], kept
        assert [(place / "set-aside" / name / "unlanded.md").read_text() for name in kept] == [f"the thread's work, {count}\n" for count in (2, 3, 4, 5)]
    assert LocalHistory.at(place, folder, thread="t1", user="u1").open() == {"copy": "moved", "set_aside_folders": kept, "set_aside_gone": names[:1]}
    assert [(place / "set-aside" / name / "unlanded.md").read_text() for name in kept] == [f"the thread's work, {count}\n" for count in (2, 3, 4, 5)]
    assert set_aside_whole(place) == sorted([f"{names[0]}.gone", *kept])


@pytest.mark.parametrize("cut", list(CUTS))
def test_a_first_open_killed_part_way_is_made_again_whole_and_lands_no_deletion(tmp_path, folder, cut):
    an_open_killed(tmp_path, folder, cut)
    connected(tmp_path / "store")
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
    connected(tmp_path / "store")
    (one.copy / "notes.txt").write_text("v1 notes\n")
    (one.copy / "Draft.md").write_text("on the branch, in no landing yet\n")
    with refused("no_whole_copy"):
        LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").changed()
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    # The repository was whole: only the copy is made again, and the thread's snapshots are all there.  The
    # half that was there held two of its branch's files, one the thread's own, and no more: none of it is kept.
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
    # Nor does any file beside the history's packs steer git's walk of what a thread's repository borrows.
    assert [read(key) for key in ("core.commitGraph", "core.multiPackIndex", "pack.useBitmaps")] == ["false", "false", "false"]
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
    # And a file git may not read is refused by its name.
    (one.copy / "locked.txt").write_text("not to be read\n")
    (one.copy / "locked.txt").chmod(0)
    with refused("file_unreadable", '^refused the request: the file "locked.txt" cannot be read on this computer'):
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
    # The folder's first commit is in its history from the copy's making on.
    main = git(tmp_path / "store" / "history.git", "rev-parse", "refs/heads/main")
    assert picked == {"main": main, "commit": None, "picked_up": [], "packs": picked["packs"], "more": 0} and picked["packs"] > 0
    # A part of a pickup's files, asked from where the last part ended.
    (folder / "yours.md").write_text("yours\n")
    (folder / "zeta.md").write_text("zeta\n")
    picked = ask(tree, {**place, "action": "pickup", "args": {"author": YOURS, "trailers": [["Surogate-Saga", "s1"]]}})
    assert ([entry["path"] for entry in picked["picked_up"]], picked["more"]) == (["yours.md", "zeta.md"], 0)
    part = ask(tree, {**place, "action": "picked", "args": {"pickup": picked["commit"], "after": "yours.md"}})
    assert part == {"picked_up": picked["picked_up"][1:], "more": 0}
    turn = ask(tree, {**place, "action": "commit", "args": {"author": A, "trailers": [["Surogate-Saga", "s1"]], "pickup": None}})
    assert [(c["path"], c["after"] is not None) for c in turn["changes"]] == [("notes.txt", True)]


@pytest.mark.parametrize("change, said", [
    ({"thread": "t1"}, "it names no thread"),
    ({"thread": "../../etc"}, "it names no thread"),
    ({"user": "u1; rm -rf /"}, "it names no user"),
    ({"store": "relative/place"}, "it names no place"),
    ({"action": "apply"}, "it names no action this computer's history takes"),
    ({"action": "hand_off"}, "it names no action this computer's history takes"),
    ({"action": "prune"}, "a pruning takes the refs it keeps, and the time"),
    ({"action": "pickup", "args": {"author": YOURS, "trailers": [], "push": True}}, "it names no action this computer's history takes"),
    ({"action": "open", "args": {"moves": "no"}}, "whether it moves the copy is neither true nor false"),
    ({"action": "open", "args": {"moves": 0}}, "whether it moves the copy is neither true nor false"),
    ({"action": "open", "args": {"moves": None}}, "whether it moves the copy is neither true nor false"),
    ({"action": "restore", "args": {"commit": "--upload-pack=/planted"}}, "its commit holds what is not a commit id"),
    ({"action": "fetch", "args": {"commits": ["-o", "x"]}}, "its commits holds what is not a commit id"),
    ({"action": "fetch", "args": {"saga": "s1", "since": "--not"}}, "its since holds what is not a commit id"),
    ({"action": "picked", "args": {"pickup": "--upload-pack=/planted", "after": "a.txt"}}, "its pickup holds what is not a commit id"),
    ({"action": "picked", "args": {"pickup": "0" * 40, "after": "../../etc/passwd"}}, "it names no file to go on from"),
    ({"action": "picked", "args": {"pickup": "0" * 40, "after": "a.txt", "most": 10}}, "it names no action this computer's history takes"),
    ({"action": "record", "args": {
        "turn": "0" * 40, "applied": [{"path": "a\0b", "before": None, "after": None}], "author": A, "trailers": [], "main": None,
    }}, "a file it applied has no path"),
    # What a landing left out is named by its path in the folder, spelt as it is walked, and never a pattern's.
    *(({"action": "record", "args": {
        "turn": "0" * 40, "applied": [], "author": A, "trailers": [], "main": None, "left": left,
    }}, "a file it left out has no path in the folder") for left in (
        "notes.txt", None, [None], [7], ["../outside"], ["/etc/passwd"], [""], ["."], ["sub//x"], ["sub/./x"], ["x/"], ["a\0b"], ["\udc80"],
    )),
    # And by the history alone, in the landing's own commit.
    ({"action": "record", "args": {
        "turn": "0" * 40, "applied": [], "author": A, "trailers": [["Surogate-Left", '"notes.txt"']], "main": None,
    }}, "a trailer it names is the history's own"),
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
    assert ask(tree, {**place, "action": "hand_off", "args": {}}) == {"error": {
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


def recorded_aside(one: LocalHistory, two: LocalHistory, n: int) -> str:
    """A record of *two*'s, after a landing of *one*'s, that sets aside what *two*'s copy held beyond its turn: what it set aside."""
    (one.copy / "Report.docx").write_bytes(b"PK\x03\x04 A's report %d" % n)
    (two.copy / "Report.docx").write_bytes(b"PK\x03\x04 B's report %d" % n)
    land(one, f"saga:a{n}")
    saga = [["Surogate-Saga", f"saga:b{n}"]]
    picked = two.pickup(author=YOURS, trailers=saga)
    turn = two.commit_turn(author=B, trailers=saga, pickup=picked["commit"])
    (two.copy / "late.md").write_text(f"late {n}\n")
    aside = two.record(
        turn=turn["commit"], applied=turn["changes"], author=B, trailers=saga, main=picked["main"], pickup=picked["commit"],
    )["set_aside"]
    assert aside is not None and one.open()["copy"] == "moved"
    return aside


def test_a_thread_keeps_what_its_last_sixteen_records_set_aside(tmp_path, folder, monkeypatch):
    monkeypatch.setattr(local_history, "_ASIDE", 4)
    one, two = a_copy(tmp_path, folder, "t1"), a_copy(tmp_path, folder, "t2")
    assert local_history.LocalHistory is LocalHistory
    kept = [recorded_aside(one, two, n) for n in range(5)]
    # The oldest goes for one more, by the count in each one's name.
    left = dict(line.split(" refs/set-aside/t2/") for line in git(two.repo, "for-each-ref", "--format=%(objectname) %(refname)", "refs/set-aside/").splitlines())
    assert (list(left), [name[:9] for name in left.values()]) == (kept[1:], ["00000002-", "00000003-", "00000004-", "00000005-"])
    # And an open names them as they were set aside, the oldest first, and the one that went no more: nor once the
    # repository that held them is set aside whole.
    assert two.open() == {"copy": "moved", "set_asides": kept[1:]}
    (two.repo / "made").unlink()
    opened = LocalHistory.at(tmp_path / "store", folder, thread="t2", user="u1").open()
    assert (opened["copy"], opened["set_asides"], opened["set_aside_folders"][-1][-11:]) == ("made", kept[1:], ".repository")
    # Of a repository set aside, as many as the thread keeps, the latest.
    monkeypatch.setattr(local_history, "_ASIDE", 2)
    assert LocalHistory.at(tmp_path / "store", folder, thread="t2", user="u1").open()["set_asides"] == kept[-2:]


def test_the_snapshots_a_repository_set_aside_whole_holds_are_named_by_every_open_while_it_is_kept(tmp_path, folder, monkeypatch):
    monkeypatch.setattr(local_history, "_ASIDE_WHOLE", 1)
    one, two = a_copy(tmp_path, folder, "t1"), a_copy(tmp_path, folder, "t2")
    first = recorded_aside(one, two, 0)
    # Its refs packed, as git may keep them, and in its copy what a command wrote to look like them.
    git(two.repo, "pack-refs", "--all")
    (two.copy / "packed-refs").write_text(f"{'0' * 40} refs/set-aside/t2/00000009-{'0' * 40}\n")
    # As an earlier guest can leave it: the mark of its first open's end is gone, and it is set aside whole.
    (two.repo / "made").unlink()
    opened = LocalHistory.at(tmp_path / "store", folder, thread="t2", user="u1").open()
    held = opened["set_aside_folders"]
    assert (opened["copy"], opened["set_asides"], [name.rpartition(".")[2] for name in held]) == ("made", [first], ["copy", "repository"])
    # Read as data: a ref there that names no commit is named not, nor one under a name no snapshot has.
    refs = tmp_path / "store" / "set-aside" / held[1] / "refs" / "set-aside" / "t2"
    refs.mkdir(parents=True, exist_ok=True)
    (refs / f"00000008-{'1' * 40}").write_text("no commit\n")
    (refs / "stray").write_text(f"{'2' * 40}\n")
    # The repository made again sets aside its own: an open names both, the oldest first, and again at every open.
    second = recorded_aside(one, two, 1)
    for _ in range(2):
        assert LocalHistory.at(tmp_path / "store", folder, thread="t2", user="u1").open() == {
            "copy": "moved", "set_asides": [first, second], "set_aside_folders": held,
        }
    # Set aside whole once more, the thread keeps that repository alone: what the one that went held is named no more.
    (two.repo / "made").unlink()
    opened = LocalHistory.at(tmp_path / "store", folder, thread="t2", user="u1").open()
    assert (opened["set_asides"], opened["set_aside_gone"]) == ([second], held)


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


def test_a_thread_whose_repository_cannot_be_packed_again_still_opens(tmp_path, folder, monkeypatch, caplog):
    monkeypatch.setattr(local_history, "_PACKS", 1)
    one, two = a_copy(tmp_path, folder, "t1"), a_copy(tmp_path, folder, "t2")
    # A ref an earlier guest left in the thread's repository, naming a commit nobody holds: git packs nothing past it.
    (one.repo / "refs" / "heads" / "kept").write_text(f"{'f' * 40}\n")
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


# -- a pruning on the computer -----------------------------------------------------------------------------------------


#: A hundred days on: the window is main's last twenty commits.
LATER = 100 * 86_400


def lands_often(history: LocalHistory, path: str, times: int, author: dict = B) -> None:
    """*history*'s thread lands a new version of *path* *times* times, as a busy thread does: a commit of main's each."""
    for _ in range(times):
        stamp = time.monotonic_ns()
        (history.copy / path).write_bytes(b"PK version %d of %s" % (stamp, path.encode()))
        assert land(history, f"saga:{history.thread}-{stamp}", author)["landing"] is not None


def packs_of(place: Path) -> set[str]:
    """The packs of the folder's history in *place*, each by its id: a pack whose index is there too."""
    found = {p.name for p in (place / "history.git" / "objects" / "pack").iterdir()}
    return {name[5:-5] for name in found if name.endswith(".pack") and f"{name[:-5]}.idx" in found}


def noted(repo: Path) -> set[str]:
    """The history's packs a thread's repository notes it may read objects from."""
    return set((repo / "borrowed").read_text().split())


def refs_of(store: Path) -> list[str]:
    return sorted(git(store, "for-each-ref", "--format=%(refname)").split())


def test_a_pruning_keeps_what_every_threads_repository_on_the_folder_names(tmp_path, folder):
    a_copy(tmp_path, folder, "t1")  # the idle thread's: opened on the folder's first commit, it lands nothing for a long while
    busy = a_copy(tmp_path, folder, "t2")
    lands_often(busy, "notes.txt", 25)
    place, store = tmp_path / "store", tmp_path / "store" / "history.git"
    assert int(git(store, "rev-list", "--count", "--first-parent", "refs/heads/main")) == 26
    before = packs_of(place)
    pruned = busy.prune(keep=["refs/heads/threads/t2", "refs/bases/t2"], now=time.time() + LATER)
    assert (pruned["pruned"], pruned["commits"]) == (True, 20)
    assert int(git(store, "rev-list", "--count", "--first-parent", "refs/heads/main")) == 20
    # One pack holds the history now, and each repository that borrows it notes that pack alone.
    [one] = packs_of(place)
    assert one not in before and noted(place / "clones" / "t1") == noted(place / "clones" / "t2") == {one}
    # The history's refs are its own still: what was kept for a thread's repository was a ref only while it was cut.
    # The idle thread has pushed none, and the history has none of its.
    assert refs_of(store) == ["refs/bases/t2", "refs/heads/main", "refs/heads/threads/t2"]
    assert not os.path.lexists(place / "pruning.git")
    connected(place)
    # The idle thread's repository borrows the history's objects: its base is still there, and its next turn lands.
    again = LocalHistory.at(place, folder, thread="t1", user="u1")
    (again.copy / "Report.docx").write_bytes(b"PK\x03\x04 report by the idle thread")
    assert again.changed() == {"paths": ["Report.docx"]}
    landed = land(again, "saga:idle")
    assert landed["landing"] is not None and [c["path"] for c in landed["changes"]] == ["Report.docx"]
    assert (folder / "Report.docx").read_bytes() == b"PK\x03\x04 report by the idle thread"
    # A second pruning within a day of that one does nothing.
    assert busy.prune(keep=[], now=time.time() + LATER + 3_600) == {"pruned": False}


def logged(repo: Path) -> set[str]:
    """Every commit a reflog of *repo* names, read as git writes them: the repository's own, and its copy's."""
    named = set()
    for log in [*(repo / "logs").rglob("*"), *(repo / "worktrees").glob("*/logs/**/*")]:
        if log.is_file():
            for line in log.read_text().splitlines():
                named.update(line.split()[:2])
    return named - {"0" * 40}


def put_back(history: LocalHistory, base: str) -> str:
    """A step's snapshot of *history*'s copy, its copy then put back to *base* as a Stop puts it back; the snapshot."""
    taken = history.snapshot("before a step")
    history.restore(base)
    return taken


def test_a_pruning_leaves_each_repository_in_the_place_whole_by_what_it_names_and_not_by_the_historys_refs(tmp_path, folder):
    place, store = tmp_path / "store", tmp_path / "store" / "history.git"
    main = lambda: git(store, "rev-parse", "refs/heads/main")  # noqa: E731
    # t3's work, a snapshot on the folder's first commit; then its repository is set aside whole and made again.
    t3 = a_copy(tmp_path, folder, "t3")
    first = main()
    (t3.copy / "Plan.md").write_text("t3's plan\n")
    planned = t3.snapshot("before a step")
    busy = a_copy(tmp_path, folder, "t2")
    lands_often(busy, "notes.txt", 1)
    (t3.repo / "made").unlink()
    [repository] = [name for name in LocalHistory.at(place, folder, thread="t3", user="u1").open()["set_aside_folders"] if name.endswith(".repository")]
    aside = place / "set-aside" / repository
    lands_often(busy, "notes.txt", 1)
    # t1 drafts on main as it was then, and is put back; again within the window; each time its copy moves to main after.
    t1 = a_copy(tmp_path, folder, "t1")
    (t1.copy / "Draft.md").write_text("drafted then\n")
    drafted = put_back(t1, main())
    # t4 lands after another landing, and what a command of its turn wrote after its commit is set aside: on the turn,
    # which only its landing leads to.
    t4 = a_copy(tmp_path, folder, "t4")
    lands_often(busy, "notes.txt", 1)
    (t4.copy / "Plan4.md").write_text("t4's plan\n")
    step = up_to_its_record(t4, "saga:t4")
    (t4.copy / "late.md").write_text("written after its turn was committed\n")
    aside_of_t4 = t4.record(**step)["set_aside"]
    assert aside_of_t4 is not None
    lands_often(busy, "notes.txt", 1)
    untold = git(store, "rev-parse", "refs/heads/main:notes.txt")
    lands_often(busy, "notes.txt", 15)
    assert LocalHistory.at(place, folder, thread="t1", user="u1").open() == {"copy": "moved"}
    (t1.copy / "Later.md").write_text("drafted within the window\n")
    later = put_back(LocalHistory.at(place, folder, thread="t1", user="u1"), main())
    lands_often(busy, "notes.txt", 6)
    assert LocalHistory.at(place, folder, thread="t1", user="u1").open() == {"copy": "moved"}
    # Its index, read before main moved on, names a version of the notes no commit of the window holds.
    subprocess.run(
        ["git", f"--git-dir={t1.repo}", f"--work-tree={folder}", "update-index", "--add", "--cacheinfo", f"100644,{untold},then.txt"],
        check=True, env=HERMETIC,
    )
    assert {drafted, later} <= logged(t1.repo) and int(git(store, "rev-list", "--count", "--first-parent", "refs/heads/main")) == 27
    before = packs_of(place)

    pruned = LocalHistory.at(place, folder, thread="t2", user="u1").prune(keep=["refs/heads/threads/t2", "refs/bases/t2"], now=time.time() + LATER)
    assert (pruned["pruned"], pruned["commits"]) == (True, 20)
    # The history is one pack, its old ones gone, and every repository that borrows it, live or set aside, notes that one.
    [one] = packs_of(place)
    assert one not in before and {name for name in os.listdir(store / "objects" / "pack")} == {f"pack-{one}.pack", f"pack-{one}.idx"}
    assert all(noted(repo) == {one} for repo in [*(place / "clones").iterdir(), aside])
    connected(place)
    # What the repository set aside names is whole: its snapshot, on a commit no live thread names.
    assert git(aside, "show", f"{planned}:Plan.md") == "t3's plan" and git(store, "cat-file", "-t", first) == "commit"
    # So is what t4 set aside, on its turn, which only a landing the window let go leads to.  The history keeps that turn:
    # t4's repository holds none of it itself.
    assert git(t4.repo, "show", f"{aside_of_t4}:late.md") == "written after its turn was committed"
    assert git(store, "cat-file", "-t", step["turn"]) == "commit" and step["turn"] not in held_here(t4.repo)
    # The version only its index named, t1 holds itself now.
    assert untold in held_here(t1.repo)
    assert subprocess.run(["git", f"--git-dir={store}", "cat-file", "-e", untold], env=HERMETIC).returncode != 0
    # Its reflog keeps what the window keeps, and lets go of what it does not: git packs the repository again.
    assert later in logged(t1.repo) and drafted not in logged(t1.repo)
    assert git(t1.repo, "show", f"{later}:Later.md") == "drafted within the window"
    subprocess.run(["git", f"--git-dir={t1.repo}", "repack", "-a", "-d", "-l", "-q"], check=True, env=HERMETIC)
    # Each live thread lands again.
    for name, path in (("t1", "Later.md"), ("t2", "B.md"), ("t3", "Plan.md"), ("t4", "Plan5.md")):
        again = LocalHistory.at(place, folder, thread=name, user="u1")
        again.open()
        (again.copy / path).write_text(f"{name}'s, after the pruning\n")
        assert [c["path"] for c in land(again, f"saga:{name}-after")["changes"]] == [path], name
    connected(place)


def test_a_pruning_leaves_a_threads_copy_whole_by_what_its_index_names_where_no_kept_commit_holds_it(tmp_path, folder, monkeypatch):
    place, store = tmp_path / "store", tmp_path / "store" / "history.git"
    one, busy = a_copy(tmp_path, folder, "t1"), a_copy(tmp_path, folder, "t2")
    # t1's turn writes the notes, and a landing's first look adds them to its copy's index: in no commit yet.
    (one.copy / "notes.txt").write_text("the notes, as both threads wrote them\n")
    assert one.changed() == {"paths": ["notes.txt"]}
    staged = subprocess.run(
        ["git", f"--git-dir={one.repo / 'worktrees' / 't1'}", "rev-parse", ":notes.txt"], capture_output=True, text=True, check=True, env=HERMETIC,
    ).stdout.strip()
    # t2 lands the same notes, and t1's repository, packed again at its next turn's start, lets go of them: the history holds them.
    (busy.copy / "notes.txt").write_text("the notes, as both threads wrote them\n")
    land(busy, "saga:same", B)
    monkeypatch.setattr(local_history, "_PACKS", 0)
    assert LocalHistory.at(place, folder, thread="t1", user="u1").open(moves=False)["copy"] == "kept"
    monkeypatch.undo()
    assert staged not in held_here(one.repo)
    # Main moves on past the window, and only the landing the pruning lets go of held those notes.
    lands_often(busy, "notes.txt", 21)
    assert LocalHistory.at(place, folder, thread="t2", user="u1").prune(keep=[], now=time.time() + LATER)["pruned"] is True
    assert subprocess.run(["git", f"--git-dir={store}", "cat-file", "-e", staged], env=HERMETIC).returncode != 0
    # The version only t1's copy's index named, t1 holds itself now: its next snapshot commits it.
    assert staged in held_here(one.repo)
    again = LocalHistory.at(place, folder, thread="t1", user="u1")
    tip = again.snapshot("before a step")
    assert git(one.repo, "rev-parse", f"{tip}:notes.txt") == staged
    connected(place)


def test_a_pruning_writes_nothing_through_a_link_an_earlier_guest_left_in_a_repository_and_keeps_what_its_refs_name(tmp_path, folder):
    one, busy = a_copy(tmp_path, folder, "t1"), a_copy(tmp_path, folder, "t2")
    lands_often(busy, "notes.txt", 22)
    outside = tmp_path / "outside.txt"
    outside.write_text("no file of the place's\n")
    for staged in (".~borrowed", ".~shallow"):
        (one.repo / staged).symlink_to(outside)
    assert busy.prune(keep=["refs/heads/threads/t2", "refs/bases/t2"], now=time.time() + LATER)["pruned"] is True
    assert outside.read_text() == "no file of the place's\n"
    # Read as data alone: its base, the folder's first commit, is kept.  Its next request sets it aside whole, as before.
    assert git(tmp_path / "store" / "history.git", "cat-file", "-t", git(one.repo, "rev-parse", "refs/bases/t1")) == "commit"
    opened = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").open()
    assert opened["copy"] == "made" and any(name.endswith("-t1.repository") for name in opened["set_aside_folders"]), opened


def test_a_repository_astray_already_is_read_as_data_alone_and_the_pruning_goes_on(tmp_path, folder):
    one = a_copy(tmp_path, folder, "t1")
    (one.copy / "Draft.md").write_text("on the history that was\n")
    one.snapshot("before a step")
    # The folder's history was taken away by hand, and another thread's first copy made a new one: t1's repository
    # notes packs the history no longer holds, and its snapshot stands on a commit nobody holds now.
    shutil.rmtree(tmp_path / "store" / "history.git")
    # Your save since: the new history's first commit is not the old one's, made again in the same second from the same files.
    (folder / "notes.txt").write_text("saved by you before the history was made again\n")
    busy = a_copy(tmp_path, folder, "t2")
    lands_often(busy, "notes.txt", 3)
    held = as_it_is(one.repo)
    assert busy.prune(keep=["refs/heads/threads/t2", "refs/bases/t2"], now=time.time())["pruned"] is True
    # Nothing of it is touched, and its next request finds it astray as before: made again, its work set aside.
    assert as_it_is(one.repo) == held
    opened = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").open()
    assert opened["copy"] == "made" and any(name.endswith("-t1.repository") for name in opened["set_aside_folders"]), opened


@pytest.mark.parametrize("left", [".pack", ".idx"])
def test_a_pruning_finishes_where_a_write_cut_between_a_pack_and_its_index_left_one_alone_and_takes_it_away(tmp_path, folder, left):
    one = a_copy(tmp_path, folder)
    lands_often(one, "notes.txt", 2)
    pack = tmp_path / "store" / "history.git" / "objects" / "pack"
    # As a push, or a pruning, killed between a pack and its index leaves it; or a pruning killed between the two it let go.
    source = sorted(pack.glob(f"pack-*{left}"))[0]
    shutil.copyfile(source, pack / f"pack-{'f' * 40}{left}")
    assert one.prune(keep=[], now=time.time())["pruned"] is True
    [kept] = packs_of(tmp_path / "store")
    assert sorted(p.name for p in pack.iterdir()) == [f"pack-{kept}.idx", f"pack-{kept}.pack"]
    assert noted(one.repo) == {kept}
    connected(tmp_path / "store")


def test_what_a_pruning_cut_part_way_left_beside_the_history_is_gone_with_the_next_request(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    work = tmp_path / "store" / "pruning.git"
    shutil.copytree(tmp_path / "store" / "history.git", work, copy_function=os.link)
    assert LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").changed() == {"paths": []}
    assert not os.path.lexists(work)
    # A link an earlier guest left at its name goes too, and what it led to stays.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "theirs.txt").write_text("another folder's file\n")
    work.symlink_to(elsewhere)
    assert one.copy.exists() and LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").changed() == {"paths": []}
    assert not os.path.lexists(work) and (elsewhere / "theirs.txt").read_text() == "another folder's file\n"


def test_a_pruning_keeps_the_refs_of_every_thread_with_a_repository_on_the_folder_whatever_it_is_told(tmp_path, folder):
    one = a_copy(tmp_path, folder, "t1")
    (one.copy / "notes.txt").write_text("t1's notes\n")
    assert land(one, "saga:t1-first")["landing"] is not None
    busy = a_copy(tmp_path, folder, "t2")
    lands_often(busy, "Report.docx", 25)
    place, store = tmp_path / "store", tmp_path / "store" / "history.git"
    # t1 was resolved longer ago than the window, so the server names t2's refs alone.  Its repository is still in the place.
    assert busy.prune(keep=["refs/heads/threads/t2", "refs/bases/t2"], now=time.time() + LATER)["pruned"] is True
    assert refs_of(store) == ["refs/bases/t1", "refs/bases/t2", "refs/heads/main", "refs/heads/threads/t1", "refs/heads/threads/t2"]
    # Reopened, its next turn lands: its push finds its branch in the history where its repository left it.
    again = LocalHistory.at(place, folder, thread="t1", user="u1")
    assert again.open() == {"copy": "moved"}
    (again.copy / "notes.txt").write_text("t1's notes, again\n")
    landed = land(again, "saga:t1-second")
    assert landed["landing"] is not None and [c["path"] for c in landed["changes"]] == ["notes.txt"]
    # A thread with no repository in the place any more has its refs go with the next pruning, as the server names it.
    shutil.rmtree(place / "clones" / "t1")
    shutil.rmtree(place / "threads" / "t1")
    assert busy.prune(keep=["refs/heads/threads/t2", "refs/bases/t2"], now=time.time() + 3 * LATER)["pruned"] is True
    assert refs_of(store) == ["refs/bases/t2", "refs/heads/main", "refs/heads/threads/t2"]
    connected(place)


def test_a_pruning_is_once_a_day_by_the_clock_that_asks_for_it_whatever_this_computers_says(tmp_path, folder):
    busy = a_copy(tmp_path, folder, "t2")
    (busy.copy / "notes.txt").write_text("v2 notes\n")
    assert land(busy, "saga:1", B)["landing"] is not None
    # The server's clock is five days ahead of this computer's.  Its own two askings, an hour apart, are one day's.
    ahead = time.time() + 5 * 86_400
    assert busy.prune(keep=[], now=ahead)["pruned"] is True
    assert busy.prune(keep=[], now=ahead + 3_600) == {"pruned": False}
    # A day on by that clock, it prunes again.
    assert busy.prune(keep=[], now=ahead + 86_400 + 1)["pruned"] is True
    # And a server whose clock is behind this computer's is not refused for a day it has not had.
    behind = time.time() - 5 * 86_400
    assert busy.prune(keep=[], now=behind) == {"pruned": False}
    assert busy.prune(keep=[], now=ahead + 2 * 86_400 + 2)["pruned"] is True


def pruned_size(place: Path) -> int:
    return sum(p.stat().st_size for p in (place / "history.git" / "objects" / "pack").glob("pack-*.pack"))


def test_a_pruning_is_asked_with_the_refs_of_threads_and_no_other(tmp_path, folder):
    place, thread = tmp_path / "store", "0b6c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f"
    request = {"store": str(place), "folder": str(folder), "thread": thread, "user": "u1"}
    assert local_history.run({**request, "action": "open", "args": {}}) == {"copy": "made"}
    asked = {**request, "action": "prune"}
    kept = [f"refs/heads/threads/{thread}", f"refs/bases/{thread}"]
    answer = local_history.run({**asked, "args": {"keep": kept, "now": time.time()}})
    assert (answer["pruned"], answer["commits"], answer["size"]) == (True, 1, pruned_size(place))
    for args, words in (
        ({"keep": ["refs/heads/main"], "now": 1.0}, "a pruning keeps a thread's branch and its base, and no other ref"),
        ({"keep": ["--upload-pack=/x"], "now": 1.0}, "a pruning keeps a thread's branch and its base, and no other ref"),
        ({"keep": [f"refs/bases/{thread}/../../heads/main"], "now": 1.0}, "a pruning keeps a thread's branch and its base, and no other ref"),
        ({"keep": [f"refs/bases/{thread}", 7], "now": 1.0}, "a pruning keeps a thread's branch and its base, and no other ref"),
        ({"keep": "refs/bases/x", "now": 1.0}, "a pruning takes the refs it keeps, and the time"),
        ({"keep": [], "now": "today"}, "a pruning takes the refs it keeps, and the time"),
        ({"keep": [], "now": True}, "a pruning takes the refs it keeps, and the time"),
        ({"keep": [], "now": float("nan")}, "a pruning takes the refs it keeps, and the time"),
        ({"keep": [], "now": 1e300}, "a pruning takes the refs it keeps, and the time"),
        ({"keep": [], "now": -1}, "a pruning takes the refs it keeps, and the time"),
        ({"keep": []}, "a pruning takes the refs it keeps, and the time"),
    ):
        with refused("not_a_request", f"refused the request: {words}"):
            local_history.run({**asked, "args": args})


def test_a_pruning_stores_a_large_file_whole_and_a_small_ones_versions_as_changes(tmp_path, folder):
    rows = [f"{n},{n * 7919 % 10_007},{n * 104_729 % 1_000_003}\n" for n in range(120_000)]
    notes = [f"line {n} of the notes\n" for n in range(200)]
    (folder / "ledger.csv").write_text("".join(rows))
    (folder / "notes.txt").write_text("".join(notes))
    assert os.path.getsize(folder / "ledger.csv") > 2 * 2**20
    one = a_copy(tmp_path, folder)
    (one.copy / "ledger.csv").write_text("".join(rows[:500] + ["500,changed,once\n"] + rows[501:]))
    (one.copy / "notes.txt").write_text("".join(notes[:7] + ["line seven, changed\n"] + notes[8:]))
    land(one, "saga:1")
    store = tmp_path / "store" / "history.git"
    assert one.prune(keep=[], now=time.time())["pruned"] is True
    [index] = (store / "objects" / "pack").glob("pack-*.idx")
    # Each blob of the one pack: one kept as a change of another names that one last.
    packed = {line.split()[0]: line.split() for line in git(store, "verify-pack", "-v", str(index)).splitlines() if " blob " in line}
    versions = {name: [git(store, "rev-parse", f"{at}:{name}") for at in ("main", "main~1")] for name in ("ledger.csv", "notes.txt")}
    # Whole, as the cloud's: a file over a mebibyte is never a change of another.
    assert [len(packed[blob]) for blob in versions["ledger.csv"]] == [5, 5]
    assert sorted(len(packed[blob]) for blob in versions["notes.txt"]) == [5, 7]


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


@pytest.mark.parametrize("theirs", ["a tag of a repository of your own", "a HEAD on no branch"])
def test_a_history_not_the_platforms_own_refuses_the_open_before_a_repository_or_a_copy_is_set_aside(tmp_path, folder, theirs):
    one = a_copy(tmp_path, folder)
    (one.copy / "Draft.md").write_text("in a snapshot\n")
    # A request after the first open's: the place is put right already, as every later request finds it.
    tip = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").snapshot("before a step")
    (one.copy / "notes.txt").write_text("the thread's notes, in no snapshot\n")
    # The repository's first open did not end, so the next sets it aside with its copy: unless the history refuses first.
    (one.repo / "made").unlink()
    store = tmp_path / "store" / "history.git"
    if theirs == "a tag of a repository of your own":
        (store / "packed-refs").write_text(f"{(store / 'packed-refs').read_text()}{tip} refs/tags/v1.0\n")
    else:
        (store / "HEAD").write_text(f"{tip}\n")
    place = as_it_is(tmp_path / "store")
    with refused("history_refused", "^refused the project's history: "):
        LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").open()
    # Nothing of the place was renamed, made or written: every name, mode, time and byte, the place's own folder too.
    assert as_it_is(tmp_path / "store") == place and set_aside_whole(tmp_path / "store") == []


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
    open moved it to the folder as you have changed it since: a file in a folder, a program, a file that
    holds nothing, and a link to a file and one to a folder, which no history holds yet."""
    one = a_copy(tmp_path, folder)
    (one.copy / "A.md").write_text("A's, landed\n")
    land(one, "saga:1")
    (folder / "sub").mkdir()
    (folder / "sub" / "yours.txt").write_text("saved by you since\n")
    (folder / "run.sh").write_text("#!/bin/sh\n")
    (folder / "run.sh").chmod(0o755)
    (folder / "empty.txt").write_text("")
    (folder / "latest").symlink_to("Report.docx")
    (folder / "also").symlink_to("sub")
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    assert again.open() == {"copy": "moved"}
    return again


#: What a thread's place can lose that has its next open make its copy again: with the first its repository too.
LOST = {
    "the mark of its first open's end": lambda one: (one.repo / "made").unlink(),
    "the mark of its first open's end, its refs packed": lambda one: (git(one.repo, "pack-refs", "--all"), (one.repo / "made").unlink()),
    "its index": lambda one: (one.repo / "worktrees" / one.thread / "index").unlink(),
}


@pytest.mark.parametrize("lost", list(LOST))
def test_a_copy_made_again_that_is_what_it_is_made_again_from_and_no_more_is_not_kept(tmp_path, folder, lost):
    one = a_copy_with_nothing_of_its_own(tmp_path, folder)
    # A folder that holds nothing is nothing the copy holds.
    (one.copy / "empty").mkdir()
    LOST[lost](one)
    # Nor is a file the folder holds as the copy does, though no history holds it yet.
    (folder / "later.txt").write_text("saved by you, and not picked up yet\n")
    if lost.startswith("the mark"):
        (one.copy / "later.txt").write_text("saved by you, and not picked up yet\n")
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    assert again.open() == {"copy": "made" if lost.startswith("the mark") else "moved"}
    assert set_aside_whole(tmp_path / "store") == []
    assert files_of(again.copy) == files_of(folder)
    # Nor is anything left of what was removed for it, under any name.
    assert sorted(p.name for p in (tmp_path / "store" / "threads").iterdir()) == ["t1"] == sorted(p.name for p in (tmp_path / "store" / "clones").iterdir())


def test_a_copy_that_lacks_only_what_history_records_none_of_in_the_folder_is_not_kept(tmp_path, folder):
    # In the folder: a repository of its own, a pipe, and files history leaves out.  No copy holds any of them.
    subprocess.run(["git", "init", "-q", str(folder / "proj")], check=True, env=HERMETIC)
    (folder / "proj" / "main.py").write_text("print('x')\n")
    os.mkfifo(folder / "pipe")
    (folder / "node_modules").mkdir()
    (folder / "node_modules" / "left.js").write_text("// left out")
    (folder / ".env").write_text("SECRET=1\n")
    one = a_copy(tmp_path, folder)
    assert sorted(p.name for p in one.copy.iterdir()) == ["Report.docx", "notes.txt"]
    (one.repo / "made").unlink()
    assert LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").open() == {"copy": "made"}
    assert set_aside_whole(tmp_path / "store") == []


#: What a thread can leave its copy as, that what the copy is made again from is not.
HELD = {
    "a file it took away": lambda copy: (copy / "notes.txt").unlink(),
    "a folder of files it took away": lambda copy: shutil.rmtree(copy / "sub"),
    "a link to a folder it took away": lambda copy: (copy / "also").unlink(),
    "a pipe it made": lambda copy: os.mkfifo(copy / "pipe"),
    "a file it changed": lambda copy: (copy / "notes.txt").write_text("the thread's notes\n"),
    "a file it made": lambda copy: (copy / "sub" / "new.md").write_text("new\n"),
    "a file history leaves out": lambda copy: (copy / "scratch.tmp").write_text("left out of history\n"),
    "a file it made a program": lambda copy: (copy / "notes.txt").chmod(0o755),
    "a program it made a file": lambda copy: (copy / "run.sh").chmod(0o644),
    "a link it put where a file was": lambda copy: ((copy / "notes.txt").unlink(), (copy / "notes.txt").symlink_to("A.md")),
    "a link it pointed elsewhere": lambda copy: ((copy / "latest").unlink(), (copy / "latest").symlink_to("A.md")),
    "a pipe it put where a file that holds nothing was": lambda copy: ((copy / "empty.txt").unlink(), os.mkfifo(copy / "empty.txt")),
    "a file named as git's own in a copy": lambda copy: (copy / ".git").write_text("gitdir: /elsewhere\n"),
    "a folder it made where a link to one was": lambda copy: (
        (copy / "also").unlink(), (copy / "also").mkdir(), (copy / "also" / "yours.txt").write_text("saved by you since\n"),
    ),
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


def test_a_copy_beside_a_repository_that_is_not_whole_is_not_read_against_that_repositorys_branch(tmp_path, folder):
    one = a_copy_with_nothing_of_its_own(tmp_path, folder)
    # Every file of the copy is one its branch holds, in a snapshot: by the word of a repository that is not whole.
    (one.copy / "Draft.md").write_text("in a snapshot\n")
    one.snapshot("before a step")
    (one.repo / "made").unlink()
    was = as_it_is(one.copy)
    opened = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").open()
    assert set(opened) == {"copy", "set_aside_folders"} and opened["copy"] == "made", opened
    copy, repository = opened["set_aside_folders"]
    assert (copy[-5:], repository[-11:]) == (".copy", ".repository")
    assert as_it_is(tmp_path / "store" / "set-aside" / copy) == was


@pytest.mark.parametrize("holds", [
    "the kept turn's files and no other", "a file the kept turn removed, put back as the folder has it",
    "a file the kept turn made, taken away since", "the kept turn's files, where the history has lost the thread's base",
])
def test_a_copy_made_again_from_a_turn_kept_in_the_history_is_read_against_that_turn(tmp_path, folder, holds):
    one = a_copy(tmp_path, folder)
    # A failed turn's work, kept in the folder's history for the thread's next landing: a file removed, and one made.
    (one.copy / "notes.txt").unlink()
    (one.copy / "Draft.md").write_text("kept\n")
    LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").keep(author=A, trailers=[["Surogate-Kind", "turn"]], base=True)
    if holds.startswith("a file the kept turn removed"):
        # The thread's own, though the folder holds the same: made again from the kept turn, the copy would not.
        (one.copy / "notes.txt").write_text("v1 notes\n")
    if holds.startswith("a file the kept turn made"):
        # The thread's own too: made again from the kept turn, the copy would hold the file again.
        (one.copy / "Draft.md").unlink()
    if holds.endswith("lost the thread's base"):
        # A history no request wrote: with no base the copy is made again from the folder, and not from the kept turn.
        packed = tmp_path / "store" / "history.git" / "packed-refs"
        packed.write_text("".join(line for line in packed.read_text().splitlines(keepends=True) if not line.endswith(" refs/bases/t1\n")))
    (one.repo / "made").unlink()
    was = as_it_is(one.copy)
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    opened = again.open()
    # Its repository names the kept turn and nothing the history lacks: that is not kept either way.
    if holds == "the kept turn's files and no other":
        assert opened == {"copy": "made"} and set_aside_whole(tmp_path / "store") == []
    else:
        [copy] = opened.get("set_aside_folders", [])
        assert opened == {"copy": "made", "set_aside_folders": [copy]} and copy.endswith("-t1.copy")
        assert as_it_is(tmp_path / "store" / "set-aside" / copy) == was
    if holds.endswith("lost the thread's base"):
        assert files_of(again.copy) == files_of(folder)
    else:
        assert files_of(again.copy) == [("Draft.md", b"kept\n"), ("Report.docx", b"PK\x03\x04 report v1")]
        assert sorted((c["path"], c["after"] is None) for c in land(again, "saga:1")["changes"]) == [("Draft.md", False), ("notes.txt", True)]


#: What a thread can do to its copy after a snapshot, and the files a copy made again then holds as its work.
SINCE = {
    "a file a snapshot took, taken away since, and a change undone": (
        lambda one, start: ((one.copy / "wrong.md").unlink(), (one.copy / "notes.txt").write_text("v1 notes\n")), ["kept.md"],
    ),
    "a step put back, with no snapshot since": (lambda one, start: one.restore(start), ["kept.md"]),
    "a file of the folder's, taken away": (lambda one, start: (one.copy / "Report.docx").unlink(), ["kept.md", "notes.txt", "wrong.md"]),
    "a file it changed again": (lambda one, start: (one.copy / "wrong.md").write_text("changed again, in no snapshot\n"), ["kept.md", "notes.txt"]),
}


@pytest.mark.parametrize("since", list(SINCE))
def test_a_copy_made_again_from_its_branch_holds_as_the_threads_work_only_what_the_copy_and_the_branch_both_held(tmp_path, folder, since):
    one = a_copy(tmp_path, folder)
    place = tmp_path / "store"
    (one.copy / "kept.md").write_text("the turn's, and still there\n")
    start = LocalHistory.at(place, folder, thread="t1", user="u1").snapshot("a turn's start")
    (one.copy / "wrong.md").write_text("what a step made\n")
    (one.copy / "notes.txt").write_text("changed by the step\n")
    LocalHistory.at(place, folder, thread="t1", user="u1").snapshot("before a step")
    then, lands = SINCE[since]
    then(LocalHistory.at(place, folder, thread="t1", user="u1"), start)
    (one.repo / "worktrees" / "t1" / "index").unlink()
    was, theirs = as_it_is(one.copy), as_it_is(folder)
    again = LocalHistory.at(place, folder, thread="t1", user="u1")
    opened = again.open()
    # The copy is set aside as it was, what it no longer held with the rest.
    assert set(opened) == {"copy", "set_aside_folders"} and opened["copy"] == "kept", opened
    [copy] = opened["set_aside_folders"]
    assert (as_it_is(place / "set-aside" / copy), as_it_is(folder)) == (was, theirs)
    # In the copy made again, a file the two did not hold alike is as the thread's base has it: nothing the
    # copy no longer held comes back as the thread's work, and nothing it held is taken away as its work.
    assert again.changed() == {"paths": lands}
    assert (again.copy / "Report.docx").read_bytes() == b"PK\x03\x04 report v1"
    assert [c["path"] for c in land(again, "saga:1")["changes"]] == lands
    assert sorted(p.name for p in folder.iterdir()) == sorted(["Report.docx", "notes.txt", *(name for name in lands if name != "notes.txt")])


def test_a_file_a_thread_put_back_is_not_taken_away_as_its_work_by_the_copy_made_again(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    place = tmp_path / "store"
    # A snapshot took the file's removal; the thread has written it again since, and no snapshot took that.
    (one.copy / "notes.txt").unlink()
    LocalHistory.at(place, folder, thread="t1", user="u1").snapshot("before a step")
    (one.copy / "notes.txt").write_text("written again by the thread\n")
    (one.repo / "worktrees" / "t1" / "index").unlink()
    was = as_it_is(one.copy)
    again = LocalHistory.at(place, folder, thread="t1", user="u1")
    opened = again.open()
    [copy] = opened.get("set_aside_folders", [])
    assert opened == {"copy": "moved", "set_aside_folders": [copy]} and as_it_is(place / "set-aside" / copy) == was
    # The copy made again has the file as its base has it: its landing removes nothing from the folder.
    assert (again.copy / "notes.txt").read_text() == "v1 notes\n" and again.changed() == {"paths": []}
    assert land(again, "saga:1")["commit"] is None and (folder / "notes.txt").read_text() == "v1 notes\n"


def test_a_file_the_folder_holds_is_the_threads_own_where_its_copy_is_made_again_from_its_branch(tmp_path, folder):
    one = a_copy_with_nothing_of_its_own(tmp_path, folder)
    # You saved a file the thread's branch does not hold yet, and the thread wrote the same.
    (folder / "later.txt").write_text("the same from both\n")
    (one.copy / "later.txt").write_text("the same from both\n")
    (one.repo / "worktrees" / "t1" / "index").unlink()
    was = as_it_is(one.copy)
    opened = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").open()
    [copy] = opened.get("set_aside_folders", [])
    assert as_it_is(tmp_path / "store" / "set-aside" / copy) == was


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file nobody may read")
@pytest.mark.parametrize("lost", ["the mark of its first open's end", "its index"])
def test_a_copy_that_cannot_be_read_whole_is_set_aside(tmp_path, folder, lost):
    one = a_copy_with_nothing_of_its_own(tmp_path, folder)
    (one.copy / "notes.txt").chmod(0)
    LOST[lost](one)
    opened = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").open()
    [copy] = opened.get("set_aside_folders", [])
    kept = tmp_path / "store" / "set-aside" / copy
    kept.joinpath("notes.txt").chmod(0o644)
    assert files_of(kept) == files_of(folder)


@pytest.mark.parametrize("lost, failed", [("refs/heads/threads/t1", "^git worktree failed: "), ("refs/bases/t1", "^git rev-parse failed: ")])
def test_a_copy_whose_branch_cannot_be_read_is_set_aside_before_its_open_fails(tmp_path, folder, lost, failed):
    one = a_copy_with_nothing_of_its_own(tmp_path, folder)
    was = as_it_is(one.copy)
    # What no open can make a copy from: the thread's repository has lost its branch or its base, and its copy its index.
    git(one.repo, "update-ref", "-d", lost)
    (one.repo / "worktrees" / "t1" / "index").unlink()
    with refused("failed", failed):
        LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").open()
    [copy] = set_aside_whole(tmp_path / "store")
    assert copy.endswith("-t1.copy") and as_it_is(tmp_path / "store" / "set-aside" / copy) == was


@pytest.mark.parametrize("holds", [
    "a snapshot", "a snapshot, its refs packed", "what a record set aside", "a ref that names no commit", "a ref that cannot be read",
])
def test_a_repository_made_again_that_holds_a_commit_of_the_threads_own_is_set_aside_though_its_copy_is_not(tmp_path, folder, holds):
    one = a_copy_with_nothing_of_its_own(tmp_path, folder)
    if holds == "a ref that names no commit":
        (one.repo / "refs" / "heads" / "odd").write_text("what is no commit's id\n")
    elif holds == "a ref that cannot be read":
        (one.repo / "refs" / "heads" / "odd").write_bytes(b"\xff\xfe not text\n")
    else:
        # The thread wrote a file, a snapshot took it, and the file is gone from the copy again.
        (one.copy / "Draft.md").write_text("in a snapshot\n")
        tip = one.snapshot("before a step")
        (one.copy / "Draft.md").unlink()
    if holds == "what a record set aside":
        # As a record leaves it: the branch on the folder's files again, and the snapshot on a ref of its own.
        git(one.repo, "update-ref", f"refs/set-aside/t1/00000001-{tip}", tip)
        git(one.repo, "update-ref", "refs/heads/threads/t1", git(one.repo, "rev-parse", "refs/heads/main"))
    if holds.endswith("its refs packed"):
        git(one.repo, "pack-refs", "--all")
    (one.repo / "made").unlink()
    kept = as_it_is(one.repo)
    opened = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").open()
    # What a record set aside it holds as the thread's, and the open names it.
    named = {"set_asides"} if holds == "what a record set aside" else set()
    assert set(opened) == {"copy", "set_aside_folders", *named} and opened["copy"] == "made", opened
    if named:
        assert opened["set_asides"] == [tip]
    [repository] = opened["set_aside_folders"]
    assert repository.endswith("-t1.repository") and set_aside_whole(tmp_path / "store") == [repository]
    assert as_it_is(tmp_path / "store" / "set-aside" / repository) == kept
    if not holds.startswith("a ref"):
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


@pytest.mark.parametrize("left", ["a link out of the place", "a folder"])
def test_what_an_earlier_guest_left_where_a_copys_making_is_marked_is_no_mark_and_is_written_through_by_none(tmp_path, folder, left):
    one = a_copy(tmp_path, folder)
    (one.copy / "notes.txt").write_text("the thread's notes, in no snapshot\n")
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text("another's\n")
    mark = tmp_path / "store" / "threads" / "t1.making"

    def planted() -> None:
        mark.symlink_to(elsewhere) if left.startswith("a link") else mark.mkdir()

    planted()
    was = as_it_is(one.copy)
    # No mark of this history's: the copy is one the thread worked in, and stays as it is.
    assert LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").open() == {"copy": "kept"}
    assert as_it_is(one.copy) == was and not os.path.lexists(mark)
    # Nor is the mark of a copy that is made written through it.
    shutil.rmtree(one.copy)
    planted()
    assert LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").open() == {"copy": "kept"}
    assert elsewhere.read_text() == "another's\n" and not os.path.lexists(mark)


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
    # And nothing left there was gone through, written over or taken away: the folder itself holds one more.
    assert [entry for entry in as_it_is(aside) if entry[0].split("/")[0] not in (copy, ".")] == [entry for entry in left if entry[0] != "."]
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

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "theirs.txt").write_text("another's\n")
    theirs = as_it_is(elsewhere)
    # The first thread's first: its repository with its copy, one time set aside under one count.  In the
    # copy is a link out of the place.
    (one.copy / "Draft.md").write_text("in a snapshot\n")
    (one.copy / "out").symlink_to(elsewhere)
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
    assert as_it_is(elsewhere) == theirs
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


def test_a_thread_whose_own_oldest_goes_lets_no_other_threads_go_where_the_place_then_keeps_no_more_than_it_may(tmp_path, folder, monkeypatch):
    monkeypatch.setattr(local_history, "_ASIDE_WHOLE", 2)
    monkeypatch.setattr(local_history, "_ASIDE_WHOLE_IN_ALL", 4)
    place = tmp_path / "store"
    one, two = a_copy(tmp_path, folder, "t1"), a_copy(tmp_path, folder, "t2")
    # The place keeps as many as it may, each thread as many as it may: the other thread's are the oldest.
    theirs = [set_aside_once_more(two, f"two's, {count}\n")["set_aside_folders"][-1] for count in (1, 2)]
    ours = [set_aside_once_more(one, f"one's, {count}\n")["set_aside_folders"][-1] for count in (3, 4, 5)]
    # The thread's own oldest went for its third, and that was enough: the place keeps four again.
    assert [name for name in set_aside_whole(place) if not name.endswith(".gone")] == [*theirs, *ours[1:]]
    assert [(place / "set-aside" / name / "unlanded.md").read_text() for name in theirs] == ["two's, 1\n", "two's, 2\n"]
    assert LocalHistory.at(place, folder, thread="t2", user="u1").open() == {"copy": "moved", "set_aside_folders": theirs}


def test_two_threads_set_aside_under_one_count_are_two_times_and_one_threads_going_takes_nothing_of_the_others(tmp_path, folder, monkeypatch):
    monkeypatch.setattr(local_history, "_ASIDE_WHOLE", 1)
    place = tmp_path / "store"
    one, two = a_copy(tmp_path, folder, "t1"), a_copy(tmp_path, folder, "t2")
    [ours] = set_aside_once_more(one, "one's\n")["set_aside_folders"]
    # Two threads' requests at once count alike: the other thread's first is under the first thread's count.
    theirs = ours.replace("-t1.", "-t2.")
    (place / "set-aside" / theirs).mkdir()
    (place / "set-aside" / theirs / "unlanded.md").write_text("two's, 1\n")
    # The other thread's second takes its own first, and nothing of the first thread's, which is that thread's newest.
    assert set_aside_once_more(two, "two's, 2\n")["set_aside_gone"] == [theirs]
    assert (place / "set-aside" / ours / "unlanded.md").read_text() == "one's\n"
    assert LocalHistory.at(place, folder, thread="t1", user="u1").open() == {"copy": "moved", "set_aside_folders": [ours]}


def test_two_threads_set_aside_under_one_count_are_two_times_for_room_too_and_one_threads_going_takes_nothing_of_the_others(tmp_path, folder, monkeypatch):
    # A place of one byte: room for what is set aside now lets go of every time that is no thread's newest.
    monkeypatch.setattr(local_history, "_ASIDE_BYTES", 1)
    place = tmp_path / "store"
    one, two = a_copy(tmp_path, folder, "t1"), a_copy(tmp_path, folder, "t2")
    [ours] = set_aside_once_more(one, "one's\n")["set_aside_folders"]
    theirs = ours.replace("-t1.", "-t2.")
    (place / "set-aside" / theirs).mkdir()
    (place / "set-aside" / theirs / "unlanded.md").write_text("two's, 1\n")
    # The other thread's second makes room by its own first, and takes nothing of the first thread's newest.
    assert set_aside_once_more(two, "two's, 2\n")["set_aside_gone"] == [theirs]
    assert (place / "set-aside" / ours / "unlanded.md").read_text() == "one's\n"


@pytest.mark.parametrize("bound", ["_ASIDE_WHOLE_IN_ALL", "_ASIDE_BYTES"])
def test_what_a_dropped_thread_set_aside_whole_goes_at_the_places_bounds_as_any_other_and_is_no_threads_newest(tmp_path, folder, monkeypatch, bound):
    # A place of one time in all, or of one byte.
    monkeypatch.setattr(local_history, bound, 1)
    place = tmp_path / "store"
    one, two, three = (a_copy(tmp_path, folder, thread) for thread in ("t1", "t2", "t3"))
    [ours] = set_aside_once_more(one, "one's, deleted since\n")["set_aside_folders"]
    # A thread whose copy was let go since, its repository kept: it is the place's still.
    [idle] = set_aside_once_more(three, "three's\n")["set_aside_folders"]
    assert LocalHistory.at(place, folder, thread="t3", user="u1").close() == {"copy": "removed"}
    assert LocalHistory.at(place, folder, thread="t1", user="u1").drop() == {"thread": "dropped"}
    # Another thread's first set aside takes the deleted thread's: that thread has no newest any more.  The idle
    # thread's newest stays.
    [theirs] = set_aside_once_more(two, "two's\n")["set_aside_folders"]
    assert [name for name in set_aside_whole(place) if not name.endswith(".gone")] == [idle, theirs]
    assert not any((place / "set-aside" / f"{ours}.gone").iterdir())


def test_a_repository_and_its_copy_set_aside_by_one_request_are_one_time_and_go_together(tmp_path, folder, monkeypatch):
    monkeypatch.setattr(local_history, "_ASIDE_WHOLE", 2)
    one = a_copy(tmp_path, folder)
    # The thread's work in its copy, and a link an earlier guest left in its repository: the request's first step sets
    # the repository aside, and its open the copy after it.
    (one.copy / "notes.txt").write_text("the thread's notes\n")
    (one.repo / "refs" / "elsewhere").symlink_to(tmp_path)
    first = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").open()["set_aside_folders"]
    assert [name.rpartition(".")[2] for name in first] == ["copy", "repository"]
    assert first[0].removesuffix(".copy") == first[1].removesuffix(".repository")
    # One time more, and the thread keeps both times whole: none goes without the other.
    opened = set_aside_once_more(one, "one's, 2\n")
    second = opened["set_aside_folders"][-1]
    assert opened == {"copy": "moved", "set_aside_folders": [*first, second]}
    # Another, and the oldest goes, both its folders.
    opened = set_aside_once_more(one, "one's, 3\n")
    assert opened == {"copy": "moved", "set_aside_folders": [second, opened["set_aside_folders"][-1]], "set_aside_gone": first}


def cut_once_its_repository_went(history: LocalHistory, monkeypatch, *, work: bool = True) -> str:
    """*history*'s open cut once its first step set the thread's repository aside whole, for a link an earlier guest
    left in it, before the open set the copy beside it aside; with *work*, the copy holds the thread's.  The
    repository's name in the place's set-aside folder."""
    if work:
        (history.copy / "notes.txt").write_text("the thread's notes\n")
    (history.repo / "refs" / "elsewhere").symlink_to(history.project)

    def cut(self):
        raise OSError("cut")

    with monkeypatch.context() as patched:
        patched.setattr(LocalHistory, "_make_way", cut)
        with pytest.raises(OSError, match="cut"):
            LocalHistory.at(history.store.parent, history.project, thread=history.thread, user="u1").open()
    return [name for name in set_aside_whole(history.store.parent) if name.endswith(f"-{history.thread}.repository")][-1]


def test_a_copy_whose_repository_a_cut_request_set_aside_is_set_aside_with_it_as_one_time_and_kept_meanwhile(tmp_path, folder, monkeypatch):
    monkeypatch.setattr(local_history, "_ASIDE_WHOLE_IN_ALL", 1)
    place = tmp_path / "store"
    one, two = a_copy(tmp_path, folder, "t1"), a_copy(tmp_path, folder, "t2")
    repository = cut_once_its_repository_went(one, monkeypatch)
    # Another thread sets aside twice meanwhile, past the place's one: the thread's repository stays, its newest, for
    # the copy is there.
    theirs = [set_aside_once_more(two, f"two's, {n}\n")["set_aside_folders"][-1] for n in (1, 2)]
    assert [name for name in set_aside_whole(place) if not name.endswith(".gone")] == [repository, theirs[1]]
    # The thread's next open sets its copy aside under the repository's count, one time with it, and keeps both, though
    # the thread had neither until it made them again.
    copy = f"{repository.removesuffix('.repository')}.copy"
    assert LocalHistory.at(place, folder, thread="t1", user="u1").open() == {"copy": "made", "set_aside_folders": [copy, repository]}
    assert (place / "set-aside" / copy / "notes.txt").read_text() == "the thread's notes\n"


def test_a_copy_is_set_aside_under_a_count_of_its_own_where_the_threads_newest_is_no_repository_alone_that_was_beside_it(tmp_path, folder, monkeypatch):
    place = tmp_path / "store"
    one = a_copy(tmp_path, folder)
    # Its repository set aside alone, the copy beside it holding nothing of the thread's: the copy was let go, and both
    # were made again.
    alone = cut_once_its_repository_went(one, monkeypatch, work=False)
    assert LocalHistory.at(place, folder, thread="t1", user="u1").open() == {"copy": "made", "set_aside_folders": [alone]}

    def set_aside(why) -> list[str]:
        (one.copy / "unlanded.md").write_text(f"one's, {why}\n")
        why(one)
        return LocalHistory.at(place, folder, thread="t1", user="u1").open()["set_aside_folders"]

    # A copy beside a repository whose first open did not end, which holds nothing of the thread's and goes.
    first = set_aside(lambda one: (one.repo / "made").unlink())
    # A copy beside no repository, one that went holding nothing of the thread's, the thread's newest a copy alone.
    second = set_aside(lambda one: shutil.rmtree(one.repo))
    assert first[:-1] == [alone] and second[:-1] == [alone, first[-1]]
    assert len({name[:8] for name in second}) == 3 and all(name.endswith(".copy") for name in second[1:])


def test_a_copy_whose_repository_a_cut_request_set_aside_is_set_aside_under_a_count_of_its_own_where_anything_else_is_under_that_count(tmp_path, folder, monkeypatch):
    place = tmp_path / "store"
    one = a_copy(tmp_path, folder)
    repository = cut_once_its_repository_went(one, monkeypatch)
    # An earlier guest left a file where the copy would go.
    (place / "set-aside" / f"{repository.removesuffix('.repository')}.copy").write_text("an earlier guest's\n")
    opened = LocalHistory.at(place, folder, thread="t1", user="u1").open()
    assert opened["copy"] == "made" and opened["set_aside_folders"][0] == repository
    [copy] = opened["set_aside_folders"][1:]
    assert copy[:8] != repository[:8] and (place / "set-aside" / copy / "notes.txt").read_text() == "the thread's notes\n"


def test_what_a_place_keeps_set_aside_whole_takes_ten_gibibytes_of_its_disk_at_most_the_oldest_going_first_and_never_a_threads_newest(tmp_path, folder, monkeypatch):
    assert local_history._ASIDE_BYTES == 10 << 30
    # A place of a few mebibytes, with copies of one or two each.
    monkeypatch.setattr(local_history, "_ASIDE_BYTES", 7 << 19)
    place = tmp_path / "store"
    one, two = a_copy(tmp_path, folder, "t1"), a_copy(tmp_path, folder, "t2")

    def heavy(history: LocalHistory, mib: int, text: str) -> dict:
        # A file that does not compress, in no snapshot: the copy set aside holds it, and the copy made again does not.
        (history.copy / "Film.bin").write_bytes(os.urandom(mib << 20))
        return set_aside_once_more(history, text)

    def kept() -> list[str]:
        return [name for name in set_aside_whole(place) if not name.endswith(".gone")]

    # What holds next to nothing on the disk counts as little: a file of 64 MiB with nothing written in it.
    with open(one.copy / "Sparse.img", "wb") as sparse:
        sparse.truncate(64 << 20)
    names = [set_aside_once_more(one, "one's, sparse\n")["set_aside_folders"][-1]]
    names += [heavy(history, 1, f"{n}\n")["set_aside_folders"][-1] for n, history in enumerate((two, one, two))]
    assert kept() == names
    # One more mebibyte is past the bound: the oldest go first, until what is kept is within it.
    names.append(heavy(one, 1, "one's, 4\n")["set_aside_folders"][-1])
    assert kept() == names[2:]
    assert not any(entry for name in names[:2] for entry in (place / "set-aside" / f"{name}.gone").iterdir())
    # A thread's newest never goes, but the place keeps no more than the bound past them: one of two mebibytes takes
    # the two oldest, and the other thread's newest stays.
    opened = heavy(two, 2, "two's, 5\n")
    names.append(opened["set_aside_folders"][-1])
    assert kept() == [names[4], names[5]]
    assert opened == {"copy": "moved", "set_aside_folders": [names[5]], "set_aside_gone": [names[1], names[3]]}
    # One larger than the bound by itself is kept whole, as each thread's newest is, and the rest goes.
    names.append(heavy(one, 4, "one's, 6\n")["set_aside_folders"][-1])
    assert kept() == [names[5], names[6]]
    assert (place / "set-aside" / names[6] / "unlanded.md").read_text() == "one's, 6\n"
    assert LocalHistory.at(place, folder, thread="t1", user="u1").open() == {
        "copy": "moved", "set_aside_folders": [names[6]], "set_aside_gone": [names[0], names[2], names[4]],
    }


@pytest.mark.skipif(os.geteuid() == 0, reason="root removes from a folder nothing may be removed from")
def test_what_cannot_be_let_go_for_room_is_kept_whole_and_the_copy_is_set_aside_all_the_same(tmp_path, folder, monkeypatch, caplog):
    monkeypatch.setattr(local_history, "_ASIDE_BYTES", 3 << 19)
    one = a_copy(tmp_path, folder)
    aside = tmp_path / "store" / "set-aside"

    def heavy(text: str) -> dict:
        (one.copy / "Film.bin").write_bytes(os.urandom(1 << 20))
        return set_aside_once_more(one, text)

    [first] = heavy("one's, 1\n")["set_aside_folders"]
    # The name it would go by is taken, by what an earlier guest left there: a folder that cannot be emptied.
    taken = aside / f"{first}.gone"
    (taken / "locked").mkdir(parents=True)
    (taken / "locked" / "theirs.md").write_text("another's\n")
    (taken / "locked").chmod(0o555)
    try:
        was = as_it_is(aside / first)
        opened = heavy("one's, 2\n")
        # Letting go is upkeep: the copy is set aside and the turn starts, and what did not go is whole and still kept.
        assert opened["copy"] == "moved" and opened["set_aside_folders"][0] == first and len(opened["set_aside_folders"]) == 2
        assert "could not be let go, and is kept" in caplog.text
        assert as_it_is(aside / first) == was
    finally:
        (taken / "locked").chmod(0o755)


def test_a_repository_set_aside_by_a_request_that_is_no_open_lets_the_oldest_go_at_once(tmp_path, folder, monkeypatch):
    monkeypatch.setattr(local_history, "_ASIDE_WHOLE", 1)
    one = a_copy(tmp_path, folder)
    [first] = set_aside_once_more(one, "one's, 1\n")["set_aside_folders"]
    # An earlier guest left a link in the thread's repository, and the first request after it is no open.
    (one.repo / "refs" / "elsewhere").symlink_to(tmp_path)
    with refused("no_whole_copy"):
        LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").changed()
    # The repository is set aside by that request, and the thread keeps no more for it than it may until its next open.
    [gone, kept] = set_aside_whole(tmp_path / "store")
    assert gone == f"{first}.gone" and kept.endswith("-t1.repository")


@pytest.mark.skipif(os.geteuid() == 0, reason="root removes from a folder nothing may be removed from")
def test_what_cannot_be_renamed_to_go_at_the_bound_is_kept_whole_and_the_open_goes_on(tmp_path, folder, monkeypatch, caplog):
    monkeypatch.setattr(local_history, "_ASIDE_WHOLE", 1)
    one = a_copy(tmp_path, folder)
    [first] = set_aside_once_more(one, "one's, 1\n")["set_aside_folders"]
    # The name it would go by is taken, by what an earlier guest left there: a folder that cannot be emptied.
    taken = tmp_path / "store" / "set-aside" / f"{first}.gone"
    (taken / "locked").mkdir(parents=True)
    (taken / "locked" / "theirs.md").write_text("another's\n")
    (taken / "locked").chmod(0o555)
    was = as_it_is(tmp_path / "store" / "set-aside" / first)
    opened = set_aside_once_more(one, "one's, 2\n")
    # Letting go is upkeep: the turn starts all the same, and what did not go is whole and still said to be kept.
    assert opened["copy"] == "moved" and opened["set_aside_folders"][0] == first and len(opened["set_aside_folders"]) == 2
    assert "could not be let go, and is kept" in caplog.text
    assert as_it_is(tmp_path / "store" / "set-aside" / first) == was
    # It goes at the next open that finds the name free, and is said once to have gone.
    (taken / "locked").chmod(0o755)
    [second] = opened["set_aside_folders"][1:]
    assert LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").open() == {
        "copy": "moved", "set_aside_folders": [second], "set_aside_gone": [first],
    }
    assert not any(taken.iterdir())


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


def platform_folders_in(copy: Path) -> None:
    """The harness's own files, the whiteboard and a coding tool's checkout in *copy*: no file of a branch's."""
    (copy / ".surogates-results").mkdir()
    (copy / ".surogates-results" / "call-1.txt").write_text("a long result\n")
    (copy / "_whiteboard").mkdir()
    (copy / "_whiteboard" / "canvas.json").write_text("{}")
    (copy / ".threads" / "site").mkdir(parents=True)
    (copy / ".threads" / "site" / "index.html").write_text("<p>checked out</p>\n")


def platform_folders(top: Path) -> list[tuple[str, int, int, bytes]]:
    return [entry for entry in as_it_is(top) if entry[0].split("/")[0] in (".surogates-results", "_whiteboard", ".threads")]


def test_a_copy_its_root_let_go_is_removed_only_where_its_branch_holds_all_of_it(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    waiting = tmp_path / "store" / "waiting" / "t1"
    platform_folders_in(one.copy)
    platform = platform_folders(one.copy)
    (one.copy / "notes.txt").write_text("v2 notes\n")
    # A change no snapshot holds yet.
    assert one.close() == {"copy": "kept"}
    first = one.snapshot("before a step")
    # What history leaves out, which the thread made: its commands may need it again.  A folder with nothing in it,
    # which no branch holds.  A repository of the thread's own in it, and one at the copy's top, whose folder git
    # never lists.
    for made, name in (("node_modules", "left-pad.js"), ("an empty folder", None), ("a checkout", "main.py"), (".git", "HEAD")):
        (one.copy / made).mkdir()
        if made == "a checkout":
            subprocess.run(["git", "init", "-q", str(one.copy / made)], check=True, env=HERMETIC)
        if name:
            (one.copy / made / name).write_text("the thread's\n")
        assert one.close() == {"copy": "kept"}, made
        shutil.rmtree(one.copy / made)
    # A copy a stop put back: its files are older than its branch's tip, which holds the turn it undid.
    (one.copy / "notes.txt").write_text("v3 notes\n")
    one.snapshot("before a step")
    one.restore(first)
    assert one.close() == {"copy": "kept"}
    assert (one.copy / "notes.txt").read_text() == "v2 notes\n"
    # Its branch holds all of it: it goes, its index with it, and the platform's folders wait beside the copies.
    one.snapshot("before a step")
    assert one.close() == {"copy": "removed"}
    assert not one.copy.exists() and not (one.repo / "worktrees" / "t1").exists()
    assert sorted(p.name for p in (tmp_path / "store" / "threads").iterdir()) == []
    assert platform_folders(waiting) == platform
    assert one.close() == {"copy": "gone"}
    assert files_of(folder) == [("Report.docx", b"PK\x03\x04 report v1"), ("notes.txt", b"v1 notes\n")]
    # Made again from its branch at its root's next operation, the platform's folders back in it as they were.
    assert one.open(moves=False) == {"copy": "kept"}
    assert (one.copy / "notes.txt").read_text() == "v2 notes\n"
    assert platform_folders(one.copy) == platform
    assert not waiting.exists() and not (one.copy / ".git").exists()
    assert one.changed() == {"paths": ["notes.txt"]}


def a_repository_with_a_commit(at: Path) -> str:
    """A repository of the thread's own at *at*, its files in a commit of its own: the commit."""
    subprocess.run(["git", "init", "-q", str(at)], check=True, env=HERMETIC)
    subprocess.run(["git", "-C", str(at), "add", "-A"], check=True, env=HERMETIC)
    subprocess.run(
        ["git", "-C", str(at), "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q", "-m", "the thread's own commit"],
        check=True, env=HERMETIC,
    )
    return git(at / ".git", "rev-parse", "HEAD")


@pytest.mark.parametrize("held", [
    "a repository two folders down, with a commit, among files its branch holds",
    "a git file naming a repository elsewhere",
    "a git folder that is no repository",
    "a folder of the thread's where the user's folder keeps a repository",
])
def test_a_copy_that_holds_a_name_git_never_lists_anywhere_is_kept_with_all_of_it(tmp_path, folder, held):
    if held.startswith("a folder of the thread's"):
        # A repository in the user's folder, which history leaves out: the copy's folder of that name is the thread's.
        (folder / "vendor").mkdir()
        (folder / "vendor" / "lib.js").write_text("the user's\n")
        a_repository_with_a_commit(folder / "vendor")
    one = a_copy(tmp_path, folder)
    if held.startswith("a repository two folders down"):
        (one.copy / "lib" / "sub").mkdir(parents=True)
        (one.copy / "lib" / "sub" / "mod.py").write_text("x = 1\n")
        one.snapshot("before a step")
        a_repository_with_a_commit(one.copy / "lib" / "sub")
    elif held.startswith("a git file"):
        (one.copy / "site").mkdir()
        (one.copy / "site" / "index.html").write_text("<p>a submodule's checkout, copied in</p>\n")
        (one.copy / "site" / ".git").write_text("gitdir: ../.git/modules/site\n")
    elif held.startswith("a git folder"):
        (one.copy / "notes" / ".git").mkdir(parents=True)
        (one.copy / "notes" / "todo.md").write_text("in a snapshot\n")
        (one.copy / "notes" / ".git" / "NOTES.md").write_text("the thread's own\n")
    else:
        (one.copy / "vendor").mkdir()
        (one.copy / "vendor" / "mine.js").write_text("the thread's own\n")
    # Every file git lists is in a snapshot.
    one.snapshot("before a step")
    held_now = as_it_is(one.copy)
    assert LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").close() == {"copy": "kept"}
    assert as_it_is(one.copy) == held_now


def test_a_checkout_in_the_platforms_own_folder_waits_whole_beside_the_copies_as_the_copy_goes(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    checkout = one.copy / ".threads" / "site"
    checkout.mkdir(parents=True)
    (checkout / "index.html").write_text("<p>checked out</p>\n")
    commit = a_repository_with_a_commit(checkout)
    assert one.close() == {"copy": "removed"}
    assert git(tmp_path / "store" / "waiting" / "t1" / ".threads" / "site" / ".git", "rev-parse", "HEAD") == commit
    assert one.open(moves=False) == {"copy": "kept"}
    assert git(checkout / ".git", "rev-parse", "HEAD") == commit


@pytest.mark.parametrize("how", ["from its branch", "moved at its turn's start", "with its repository made again"])
def test_what_waited_beside_the_copies_comes_back_whichever_way_the_copy_is_made_again(tmp_path, folder, how):
    one = a_copy(tmp_path, folder)
    platform_folders_in(one.copy)
    platform = platform_folders(one.copy)
    assert one.close() == {"copy": "removed"}
    if how == "with its repository made again":
        (one.repo / "made").unlink()
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    assert again.open(moves=how == "moved at its turn's start") == {
        "from its branch": {"copy": "kept"}, "moved at its turn's start": {"copy": "moved"}, "with its repository made again": {"copy": "made"},
    }[how]
    assert platform_folders(again.copy) == platform
    assert not os.path.lexists(tmp_path / "store" / "waiting" / "t1")
    # Nothing of it lands, and nothing of it is said to be unsaved.
    assert again.changed() == {"paths": []}


def test_what_waits_beside_the_copies_is_never_put_over_what_the_copy_holds_at_its_name(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    platform_folders_in(one.copy)
    assert one.close() == {"copy": "removed"}
    waiting = tmp_path / "store" / "waiting" / "t1"
    held = [entry for entry in platform_folders(waiting) if entry[0].startswith(".surogates-results")]
    # As no act of this computer's leaves it: the copy made again with nothing waiting beside it, and then a folder
    # of the harness's at that name in the copy as the folders that waited are there again.
    waiting.rename(tmp_path / "hidden")
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    assert again.open(moves=False) == {"copy": "kept"}
    (tmp_path / "hidden").rename(waiting)
    (again.copy / ".surogates-results").mkdir()
    (again.copy / ".surogates-results" / "call-2.txt").write_text("a later result\n")
    assert LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").open(moves=False) == {"copy": "kept"}
    # Each stays where it is, whole: the one beside the copies is not put over it, nor let go; the others go back.
    assert sorted(p.name for p in (again.copy / ".surogates-results").iterdir()) == ["call-2.txt"]
    assert platform_folders(waiting) == held
    assert (again.copy / "_whiteboard" / "canvas.json").read_text() == "{}"
    # And the copy is not let go while the folder beside it would be put over: it is kept, both whole.
    assert LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").close() == {"copy": "kept"}
    assert (again.copy / ".surogates-results" / "call-2.txt").read_text() == "a later result\n"
    assert (waiting / ".surogates-results" / "call-1.txt").read_text() == "a long result\n"


def test_a_copy_let_go_inside_a_turn_is_made_again_where_its_branch_is_and_moves_at_its_next_turn(tmp_path, folder):
    one, two = a_copy(tmp_path, folder, "t1"), a_copy(tmp_path, folder, "t2")
    (two.copy / "notes.txt").write_text("v2 notes, by B\n")
    land(two, "saga:1", B)
    assert one.close() == {"copy": "removed"}
    # Made again for an operation inside its turn: from its branch, not moved to what landed meanwhile.
    again = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1")
    assert again.open(moves=False) == {"copy": "kept"}
    assert (again.copy / "notes.txt").read_text() == "v1 notes\n"
    # Its next turn starts on the folder as it is now.
    assert again.open() == {"copy": "moved"}
    assert (again.copy / "notes.txt").read_text() == "v2 notes, by B\n"


def test_a_copy_whose_making_did_not_end_or_whose_index_is_gone_is_let_go_as_an_open_lets_it_go(tmp_path, folder):
    one = a_copy(tmp_path, folder)
    place = tmp_path / "store"
    # Its making again cut, as a quit leaves it: marked, and half its files written.  No thread worked in it.
    shutil.rmtree(one.copy)
    (place / "threads" / "t1.making").write_bytes(b"")
    (one.copy / "half").mkdir(parents=True)
    assert LocalHistory.at(place, folder, thread="t1", user="u1").close() == {"copy": "removed"}
    assert not os.path.lexists(one.copy) and not os.path.lexists(place / "threads" / "t1.making") and set_aside_whole(place) == []
    # Whole once, with the thread's work in it, and its index gone as no removal of this computer's leaves it: set aside
    # whole, never removed.
    again = LocalHistory.at(place, folder, thread="t1", user="u1")
    assert again.open(moves=False) == {"copy": "kept"}
    (again.copy / "Draft.md").write_text("in no snapshot\n")
    (again.repo / "worktrees" / "t1" / "index").unlink()
    held = as_it_is(again.copy)
    assert LocalHistory.at(place, folder, thread="t1", user="u1").close() == {"copy": "removed"}
    [aside] = set_aside_whole(place)
    assert as_it_is(place / "set-aside" / aside) == held and not os.path.lexists(again.copy)
    assert LocalHistory.at(place, folder, thread="t1", user="u1").open(moves=False) == {"copy": "kept", "set_aside_folders": [aside]}
    assert files_of(again.copy) == files_of(folder)
    # One that is its branch's files and no more holds nothing of the thread's own: it is removed, and nothing more is kept.
    (again.repo / "worktrees" / "t1" / "index").unlink()
    assert LocalHistory.at(place, folder, thread="t1", user="u1").close() == {"copy": "removed"}
    assert set_aside_whole(place) == [aside] and not os.path.lexists(again.copy)


@pytest.mark.parametrize("cut", ["a record cut after its push", "a move cut part way through its files"])
def test_a_copy_is_let_go_only_once_what_a_request_of_its_thread_was_cut_in_the_middle_of_is_finished(tmp_path, folder, cut):
    if cut == "a record cut after its push":
        again, step = a_record_cut_after_its_push(tmp_path, folder)
        assert again.close() == {"copy": "removed"}
        assert git(again.repo, "rev-parse", "refs/landed/t2") == step["landing"]
        # Made again, it is the landing's files: the newer report, and the other thread's new file.
        made = LocalHistory.at(tmp_path / "store", folder, thread="t2", user="u1")
        assert made.open(moves=False) == {"copy": "kept"}
        assert (made.copy / "Report.docx").read_bytes() == b"PK\x03\x04 A's report"
        assert (made.copy / "A-new.md").read_text() == "A's new file\n"
        assert made.changed() == {"paths": []}
        return
    root = tmp_path / "whole"
    a_clean_copy_whose_main_moved(root, False)
    stepped(root, OURS, "open", {}, before="read-tree -u -m")
    ours = LocalHistory.at(root / "store", root / "Documents", thread=OURS, user="u1")
    (ours.copy / "g.txt").write_text("g by them\n")
    (ours.copy / "e.txt").unlink()
    assert ours.close() == {"copy": "removed"}
    assert not git(ours.repo, "for-each-ref", "refs/moving/")
    assert LocalHistory.at(root / "store", root / "Documents", thread=OURS, user="u1").open(moves=False) == {"copy": "kept"}
    the_folder_goes_back_and_the_thread_lands_its_own_file_alone(root, cut)


def test_a_copys_letting_go_follows_no_link_an_earlier_guest_left(tmp_path, folder):
    assert request(tmp_path, folder, THREAD, "open") == {"copy": "made"}
    assert request(tmp_path, folder, OTHER, "open") == {"copy": "made"}
    place = tmp_path / "store"
    (place / "threads" / OTHER / ".surogates-results").mkdir()
    (place / "threads" / OTHER / ".surogates-results" / "call-1.txt").write_text("a long result\n")
    # Where a let-go copy's own folders wait, made the copies' folder itself: the copy would wait in its own place.
    (place / "waiting").symlink_to(place / "threads")
    assert request(tmp_path, folder, OTHER, "close") == {"copy": "removed"}
    assert not (place / "waiting").is_symlink()
    assert (place / "waiting" / OTHER / ".surogates-results" / "call-1.txt").read_text() == "a long result\n"
    assert sorted(p.name for p in (place / "threads").iterdir()) == [THREAD]
    # Where this thread's own wait, a link to another folder, which holds a folder of the harness's name: the copy
    # made again takes nothing from it.
    assert request(tmp_path, folder, THREAD, "close") == {"copy": "removed"}
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / ".surogates-results").mkdir(parents=True)
    (elsewhere / ".surogates-results" / "theirs.txt").write_text("another folder's file\n")
    (place / "waiting" / THREAD).symlink_to(elsewhere)
    assert request(tmp_path, folder, THREAD, "open", moves=False) == {"copy": "kept"}
    assert files_of(elsewhere) == [(".surogates-results/theirs.txt", b"another folder's file\n")]
    assert not os.path.lexists(place / "threads" / THREAD / ".surogates-results")
    # The copies' folder made a link to another's, where this thread's name is a folder: what it led to stays.
    (elsewhere / THREAD).mkdir()
    (elsewhere / THREAD / "theirs.docx").write_text("another folder's file\n")
    shutil.rmtree(place / "threads")
    (place / "threads").symlink_to(elsewhere)
    assert request(tmp_path, folder, THREAD, "close") == {"copy": "gone"}
    assert not (place / "threads").is_symlink()
    assert (elsewhere / THREAD / "theirs.docx").read_text() == "another folder's file\n"


def test_a_copys_letting_go_and_a_deleted_threads_dropping_are_ones_the_agent_runs(tmp_path, folder, tree):
    place = {"store": str(tmp_path / "store"), "folder": str(folder), "thread": THREAD, "user": "u1"}
    assert ask(tree, {**place, "action": "open", "args": {"moves": False}}) == {"copy": "made"}
    assert ask(tree, {**place, "action": "close", "args": {}}) == {"copy": "removed"}
    assert ask(tree, {**place, "action": "close", "args": {}}) == {"copy": "gone"}
    assert ask(tree, {**place, "action": "open", "args": {"moves": False}}) == {"copy": "kept"}
    assert ask(tree, {**place, "action": "close", "args": {"force": True}}) == {"error": {
        "code": "not_a_request", "message": "refused the request: it names no action this computer's history takes",
    }}
    # A deleted thread's copy and repository go, and the folder's history stays.
    assert ask(tree, {**place, "action": "drop", "args": {}}) == {"thread": "dropped"}
    assert ask(tree, {**place, "action": "drop", "args": {}}) == {"thread": "gone"}
    assert [(p.name, sorted(q.name for q in p.iterdir())) for p in sorted((tmp_path / "store").iterdir()) if p.name != "history.git"] == [
        ("clones", []), ("threads", []),
    ]


def test_a_deleted_threads_dropping_takes_its_copy_what_waits_beside_it_and_its_repository_and_nothing_set_aside_or_another_threads(tmp_path, folder):
    one, two = a_copy(tmp_path, folder, "t1"), a_copy(tmp_path, folder, "t2")
    place = tmp_path / "store"
    # What the thread kept: a snapshot set aside by a record, a copy set aside whole, the platform's folders waiting
    # for its copy, in its copy a file no branch holds, and the mark of a making an open cut.
    recorded_aside(two, one, 0)
    set_aside_once_more(one, "one's\n")
    (place / "waiting" / "t1" / "_whiteboard").mkdir(parents=True)
    (place / "waiting" / "t1" / "_whiteboard" / "canvas.json").write_text("{}")
    (one.copy / "unlanded.md").write_text("the thread's, in no snapshot\n")
    (place / "threads" / "t1.making").write_bytes(b"")
    kept = [as_it_is(at) for at in (place / "set-aside", place / "history.git", two.copy, two.repo, folder)]
    assert LocalHistory.at(place, folder, thread="t1", user="u1").drop() == {"thread": "dropped"}
    assert [sorted(p.name for p in (place / at).iterdir()) for at in ("threads", "clones", "waiting")] == [["t2"], ["t2"], []]
    assert [as_it_is(at) for at in (place / "set-aside", place / "history.git", two.copy, two.repo, folder)] == kept
    # What a dropping cut part way left under the name it went by goes with the next, and there is nothing more.
    for at in ("threads/t1.going", "clones/t1.going", "waiting/t1.going"):
        (place / at / "left").mkdir(parents=True)
    assert LocalHistory.at(place, folder, thread="t1", user="u1").drop() == {"thread": "gone"}
    assert [sorted(p.name for p in (place / at).iterdir()) for at in ("threads", "clones", "waiting")] == [["t2"], ["t2"], []]
    assert [as_it_is(at) for at in (place / "set-aside", place / "history.git", two.copy, two.repo, folder)] == kept
    # Through no link an earlier guest left where the place keeps what waits.
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "t1").mkdir(parents=True)
    (elsewhere / "t1" / "theirs.txt").write_text("another's\n")
    theirs = as_it_is(elsewhere)
    (place / "waiting").rmdir()
    (place / "waiting").symlink_to(elsewhere)
    assert LocalHistory.at(place, folder, thread="t1", user="u1").drop() == {"thread": "gone"}
    assert as_it_is(elsewhere) == theirs and not os.path.lexists(place / "waiting")


#: One request, as its runner takes it, run to its end or killed at one of its steps: each git it
#: runs, each file it writes or puts in its place, and each folder it renames; where it is asked
#: to, each file and folder it removes too.  Killed, every process of it goes at once: before the
#: step, or a while after its git started.
STEPPED = """
import json, os, pathlib, shutil, signal, subprocess, sys, time
at, before, count, gits, inside = int(sys.argv[2]), sys.argv[3], [0], [], float(sys.argv[5] or 0)
def stepped(real):
    def step(*args, **kwargs):
        count[0] += 1
        if real is run:
            gits.append(count[0])
        if count[0] == at and inside and real is run:
            git = subprocess.Popen(
                args[0], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=kwargs.get("env"), cwd=kwargs.get("cwd"),
            )
            try:
                git.stdin.write((kwargs.get("input") or "").encode("utf-8", "surrogateescape"))
                git.stdin.close()
            except OSError:
                pass
            time.sleep(inside)
            os.killpg(0, signal.SIGKILL)
        if count[0] == at or before and args and (before in " ".join(args[0]) if isinstance(args[0], list) else str(args[0]).endswith(before)):
            os.killpg(0, signal.SIGKILL)
        return real(*args, **kwargs)
    return step
run = subprocess.run
subprocess.run, os.replace, os.rename = stepped(subprocess.run), stepped(os.replace), stepped(os.rename)
pathlib.Path.write_bytes = stepped(pathlib.Path.write_bytes)
if sys.argv[4]:
    os.unlink, os.rmdir = stepped(os.unlink), stepped(os.rmdir)
from surogates.sandbox import local_history
try:
    answer = local_history.run(json.loads(sys.argv[1]))
except local_history.HistoryError as refusal:
    answer = {"error": refusal.code}
print(json.dumps({"steps": count[0], "gits": gits, "answer": answer}))
"""
OURS, THEIRS, NEW = THREAD, THREAD.replace("0b6c", "1b6c"), THREAD.replace("0b6c", "2b6c")
SAGA = [["Surogate-Saga", "saga:ours"]]


def stepped(
    root: Path, thread: str, action: str, args: dict, at: int = 0, before: str = "", removals: bool = False, inside: float = 0,
) -> dict | None:
    """*thread*'s request on the folder under *root*, killed at its step *at*, or right before the git that is
    *before*, or the step on the path that ends as *before* does; its steps, which of them are gits, and its
    answer when it ran to its end.  With *removals*, each file and each folder it removes is a step too.
    With *inside*, the git at *at* is killed with its request that many seconds after it started."""
    request = {"store": str(root / "store"), "folder": str(root / "Documents"), "thread": thread, "user": "u1", "action": action, "args": args}
    ran = subprocess.run(
        [sys.executable, "-c", STEPPED, json.dumps(request), str(at), before, "removals" * removals, str(inside or "")],
        capture_output=True, text=True, cwd=Path(__file__).parents[1], start_new_session=True, timeout=300,
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


def each_cut(tmp_path: Path, thread: str, action: str, args: dict, removals: bool = False, inside: float = 0):
    """The folder under ``tmp_path/whole`` copied for each step of *thread*'s request, and the request killed at that
    step; with *inside*, at each of its gits, that many seconds after it started."""
    whole = tmp_path / "whole"
    shutil.copytree(whole, tmp_path / "counted", symlinks=True)
    counted = stepped(tmp_path / "counted", thread, action, args, removals=removals)
    assert counted["steps"] > 1
    for at in counted["gits"] if inside else range(1, counted["steps"] + 1):
        root = tmp_path / f"cut-{at}"
        shutil.copytree(whole, root, symlinks=True)
        before = as_it_is(root / "Documents")
        stepped(root, thread, action, args, at, removals=removals, inside=inside)
        # The history writes no file of the folder, at any step: every name, mode, time and byte is as it was.
        assert as_it_is(root / "Documents") == before, at
        # And no thread's repository is left naming what neither it nor the history holds.
        connected(root / "store")
        yield at, root


def test_a_first_open_killed_at_any_step_leaves_the_folder_as_it_was_and_the_history_whole(tmp_path):
    _, kept = a_folder_two_threads_work_on(tmp_path / "whole")
    for _at, root in each_cut(tmp_path, NEW, "open", {}):
        again = LocalHistory.at(root / "store", root / "Documents", thread=NEW, user="u1")
        assert again.open() == {"copy": "made"}
        assert files_of(again.copy) == files_of(root / "Documents")
        assert land(again, "saga:new")["commit"] is None
        the_next_turn_lands_whole(root, kept, landed=False, new=THREAD.replace("0b6c", "3b6c"))


@pytest.mark.parametrize("action", ["open", "changed", "pickup", "picked", "commit", "keep"])
def test_a_request_killed_at_any_step_before_a_landing_applies_leaves_the_folder_as_it_was_and_the_history_whole(tmp_path, action):
    ours, kept = a_folder_two_threads_work_on(tmp_path / "whole")
    args = {
        "open": {}, "changed": {}, "pickup": {"author": YOURS, "trailers": SAGA}, "picked": {"pickup": None, "after": "Report.docx"},
        "commit": {"author": B, "trailers": SAGA, "pickup": None}, "keep": {"author": B, "trailers": SAGA, "base": True},
    }[action]
    if action in ("commit", "picked"):
        args["pickup"] = ours.pickup(author=YOURS, trailers=SAGA)["commit"]
    for _at, root in each_cut(tmp_path, OURS, action, args):
        the_next_turn_lands_whole(root, kept, landed=False)


@pytest.mark.parametrize("inside", [0, 0.003, 0.012])
def test_a_folders_first_copy_killed_at_any_step_or_inside_any_git_is_made_again_whole_and_its_history_holds_it(tmp_path, inside):
    folder = tmp_path / "whole" / "Documents"
    (folder / "sub").mkdir(parents=True)
    for name, text in (("Report.docx", "report v1"), ("notes.txt", "v1 notes"), ("sub/deep.txt", "deep")):
        (folder / name).write_text(f"{text}\n")
        os.utime(folder / name, (time.time() - 60, time.time() - 60))
    for at, root in each_cut(tmp_path, OURS, "open", {}, inside=inside):
        place, store = root / "store", root / "store" / "history.git"
        # Whatever the cut left, the first open had not ended: the next makes all of it again, and keeps nothing of it.
        again = LocalHistory.at(place, root / "Documents", thread=OURS, user="u1")
        assert again.open() == {"copy": "made"}, at
        assert files_of(again.copy) == files_of(root / "Documents") and set_aside_whole(place) == [], at
        # The folder's first commit is its history's, which the repository borrows: it holds no second copy of the folder.
        main = git(store, "rev-parse", "refs/heads/main")
        assert main == git(again.repo, "rev-parse", "refs/heads/main") and main not in held_here(again.repo), at
        assert git(store, "fsck", "--no-dangling") == "", at
        connected(place)
        (again.copy / "A.md").write_text("A\n")
        assert [c["path"] for c in land(again, "saga:1", B)["changes"]] == ["A.md"], at
        fresh = LocalHistory.at(place, root / "Documents", thread=NEW, user="u1")
        assert fresh.open() == {"copy": "made"} and files_of(fresh.copy) == files_of(root / "Documents"), at
        connected(place)


@pytest.mark.parametrize("inside", [0.003, 0.012])
@pytest.mark.parametrize("action", ["commit", "keep"])
def test_a_push_killed_inside_any_of_its_gits_leaves_the_folder_as_it_was_and_the_history_whole(tmp_path, action, inside):
    ours, kept = a_folder_two_threads_work_on(tmp_path / "whole")
    args = {"author": B, "trailers": SAGA, "pickup": ours.pickup(author=YOURS, trailers=SAGA)["commit"]} if action == "commit" else {
        "author": B, "trailers": SAGA, "base": True,
    }
    for _at, root in each_cut(tmp_path, OURS, action, args, inside=inside):
        the_next_turn_lands_whole(root, kept, landed=False)


@pytest.mark.parametrize("inside", [0, 0.003, 0.012])
def test_an_open_that_packs_its_repository_again_killed_at_any_step_or_inside_any_git_leaves_it_whole(tmp_path, inside):
    ours, kept = a_folder_two_threads_work_on(tmp_path / "whole")
    # More packs than a turn's start leaves, each a snapshot's: the thread's notes drafted again and again.
    for n in range(local_history._PACKS + 1):
        (ours.copy / "notes.txt").write_text(f"our notes, draft {n}\n")
        LocalHistory.at(tmp_path / "whole" / "store", tmp_path / "whole" / "Documents", thread=OURS, user="u1").snapshot("before a step")
    (ours.copy / "notes.txt").write_text("our notes\n")
    assert len(list((ours.repo / "objects" / "pack").glob("*.pack"))) > local_history._PACKS
    for _at, root in each_cut(tmp_path, OURS, "open", {}, inside=inside):
        the_next_turn_lands_whole(root, kept, landed=False)


@pytest.mark.parametrize("inside", [0, 0.003, 0.012])
def test_a_fetch_killed_at_any_step_or_inside_any_git_leaves_the_folder_as_it_was_and_the_history_whole(tmp_path, inside):
    ours, kept = a_folder_two_threads_work_on(tmp_path / "whole")
    theirs = git(tmp_path / "whole" / "store" / "history.git", "rev-parse", "refs/heads/main")
    for _at, root in each_cut(tmp_path, OURS, "fetch", {"saga": "saga:theirs", "commits": [theirs]}, inside=inside):
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


@pytest.mark.parametrize("first", ["open", "open that does not move", "changed", "snapshot", "commit", "keep"])
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
        "open": ours.open, "open that does not move": lambda: ours.open(moves=False),
        "changed": ours.changed, "snapshot": lambda: ours.snapshot("before a step"),
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


#: What an earlier guest, a cut or a fault can leave of a thread's place, that has its next open make its copy again.
BROKEN = {
    "its repository's first open is not marked as ended": lambda ours: (ours.repo / "made").unlink(),
    "a link is in its repository": lambda ours: (ours.repo / "refs" / "elsewhere").symlink_to(ours.repo.parent),
    "its repository's alternates are gone": lambda ours: (ours.repo / "objects" / "info" / "alternates").unlink(),
    "its repository read from a pack the history no longer holds": lambda ours: (ours.repo / "borrowed").write_text(
        f"{(ours.repo / 'borrowed').read_text()}{'0' * 40}\n",
    ),
    "its copy's index is gone": lambda ours: (ours.repo / "worktrees" / ours.thread / "index").unlink(),
}


def held_in(top: Path) -> list[tuple[str, int, bytes]]:
    """Every name under *top* with its mode and what it holds, but where a copy's git folder names the copy's
    path: that is another's under each cut's own folder, and is written again there, staged first; and a
    repository's alternates and its note of the history's packs, which this computer writes, and takes away, as
    it sets a repository aside."""
    return [
        (name, mode, held) for name, mode, _, held in as_it_is(top)
        if Path(name).name not in ("gitdir", ".~gitdir", "alternates", ".~alternates", "borrowed")
    ]


@pytest.mark.parametrize("broken", list(BROKEN))
def test_an_open_that_sets_a_copy_aside_killed_at_any_step_leaves_it_whole_under_one_name_and_the_next_makes_a_whole_copy(tmp_path, broken):
    ours, kept = a_folder_two_threads_work_on(tmp_path / "whole")
    # The thread's work is in a snapshot, and in its copy since: a file in no snapshot, and one history leaves out.
    LocalHistory.at(tmp_path / "whole" / "store", tmp_path / "whole" / "Documents", thread=OURS, user="u1").snapshot("before a step")
    (ours.copy / "late.md").write_text("in no snapshot\n")
    (ours.copy / "scratch.tmp").write_text("left out of history\n")
    BROKEN[broken](ours)
    copy, repository = as_it_is(ours.copy), held_in(ours.repo)
    with_repository = not broken.startswith("its copy")
    for at, root in each_cut(tmp_path, OURS, "open", {}, removals=True):
        place, aside = root / "store", root / "store" / "set-aside"
        # Cut anywhere, each is whole under one name, its own or the one it was set aside under: never part
        # of it under each, and never under neither.
        assert [as_it_is(at_) == copy for at_ in (place / "threads" / OURS, *aside.glob("*.copy"))].count(True) == 1, at
        if with_repository:
            assert [held_in(at_) == repository for at_ in (place / "clones" / OURS, *aside.glob("*.repository"))].count(True) == 1, at
        # The thread's next turn opens on a whole copy and lands, and nothing of anyone's goes with it.
        if with_repository:
            # Made again from the folder's history: the thread's own work is what was set aside, and no part of it lands.
            again = LocalHistory.at(place, root / "Documents", thread=OURS, user="u1")
            assert again.open()["copy"] in ("made", "moved") and again.changed() == {"paths": []}, at
            (again.copy / "after.md").write_text("the turn after\n")
            assert [c["path"] for c in land(again, "saga:after", B)["changes"]] == ["after.md"], at
            assert {name: dict(files_of(root / "Documents")).get(name) for name in kept} == kept, at
        else:
            the_next_turn_lands_whole(root, kept, landed=False)
        # From then on what was set aside is told, and is all the place keeps so: the copy once, as it was,
        # and the repository where that was made again too.
        opened = LocalHistory.at(place, root / "Documents", thread=OURS, user="u1").open()
        assert opened.get("set_aside_folders") == set_aside_whole(place), at
        # One time, whichever request set each aside.
        assert len({name.rpartition(".")[0] for name in opened["set_aside_folders"]}) == 1, at
        assert [as_it_is(aside / name) for name in opened["set_aside_folders"] if name.endswith(".copy")] == [copy], at
        assert [held_in(aside / name) for name in opened["set_aside_folders"] if name.endswith(".repository")] == (
            [repository] if with_repository else []
        ), at
        # A repository set aside borrows the history it was made from by the one line, and reads all it names
        # through it as it is kept (each cut's connectivity check reads it so).
        assert all(
            (aside / name / "objects" / "info" / "alternates").read_text() == "../../../history.git/objects\n"
            for name in opened["set_aside_folders"] if name.endswith(".repository")
        ), at
        connected(place)


def test_an_open_that_lets_the_oldest_set_aside_go_killed_at_any_step_keeps_the_rest_whole_and_none_half_gone(tmp_path):
    ours, kept = a_folder_two_threads_work_on(tmp_path / "whole")
    # The thread's copy was set aside whole four times, as many as a thread keeps, and is about to be once more.
    for count in range(4):
        names = set_aside_once_more(ours, f"unlanded, {count}\n")["set_aside_folders"]
    assert len(names) == 4
    (ours.copy / "unlanded.md").write_text("unlanded, 4\n")
    (ours.repo / "worktrees" / OURS / "index").unlink()
    held = [*(as_it_is(tmp_path / "whole" / "store" / "set-aside" / name) for name in names), as_it_is(ours.copy)]
    for at, root in each_cut(tmp_path, OURS, "open", {}, removals=True):
        folder, aside = root / "Documents", root / "store" / "set-aside"
        # Cut anywhere: only the oldest is ever going, and each of the others is whole under its name.
        assert [name for name in names if not (aside / name).exists()] in ([], names[:1]), at
        assert all(as_it_is(aside / name) == was for name, was in zip(names, held) if (aside / name).exists()), at
        # An open that is cut in its turn, before it reads the copy, has by then let go what the first kept past the bound.
        stepped(root, OURS, "open", {}, before="ls-files")
        assert len([name for name in set_aside_whole(root / "store") if not name.endswith(".gone")]) <= 4, at
        again = LocalHistory.at(root / "store", folder, thread=OURS, user="u1")
        opened = again.open()
        # After the next open the thread keeps its last four, each whole, the one just set aside among them;
        # the oldest is gone whole, and said to be.
        assert opened["set_aside_folders"][:3] == names[1:] and opened["set_aside_gone"] == names[:1], at
        assert [as_it_is(aside / name) for name in opened["set_aside_folders"]] == held[1:], at
        assert set_aside_whole(root / "store") == sorted([f"{names[0]}.gone", *opened["set_aside_folders"]]), at
        assert not any((aside / f"{names[0]}.gone").iterdir()), at
        # And the thread's turn lands from the copy made again.
        (again.copy / "after.md").write_text("the turn after\n")
        assert [c["path"] for c in land(again, "saga:after", B)["changes"]] == ["after.md"], at
        assert {name: dict(files_of(folder)).get(name) for name in kept} == kept, at


@pytest.mark.parametrize("which", ["a first open", "a making again"])
def test_an_open_cut_before_gits_own_file_is_taken_out_of_the_copy_leaves_no_copy_a_thread_works_in_with_it(tmp_path, which):
    root = tmp_path / "whole"
    folder, place, copy = root / "Documents", root / "store", root / "store" / "threads" / OURS
    folder.mkdir(parents=True)
    for name in ("Report.docx", "notes.txt"):
        (folder / name).write_text(f"{name} v1\n")
        os.utime(folder / name, (time.time() - 60, time.time() - 60))
    if which == "a making again":
        a_copy(root, folder, OURS)
        shutil.rmtree(copy)
    stepped(root, OURS, "open", {}, before="/.git", removals=True)
    # As the cut leaves it: every file of the copy written, its index too, and git's own file still in it.
    assert (copy / ".git").is_file() and (place / "clones" / OURS / "worktrees" / OURS / "index").is_file()
    # Its making has not ended.  No request reads it, and its next open makes it again, with no file of git's.
    with refused("no_whole_copy"):
        LocalHistory.at(place, folder, thread=OURS, user="u1").changed()
    again = LocalHistory.at(place, folder, thread=OURS, user="u1")
    assert again.open() == {"copy": "made" if which == "a first open" else "moved"}
    assert files_of(again.copy) == files_of(folder) and not (copy / ".git").exists()
    assert set_aside_whole(place) == []
    assert LocalHistory.at(place, folder, thread=OURS, user="u1").open() == {"copy": "moved"} and not (copy / ".git").exists()


def test_a_first_open_cut_before_its_end_leaves_no_copy_of_the_threads_own_whatever_you_save_since(tmp_path):
    root = tmp_path / "whole"
    folder, place = root / "Documents", root / "store"
    folder.mkdir(parents=True)
    for name in ("Report.docx", "notes.txt"):
        (folder / name).write_text(f"{name} v1\n")
        os.utime(folder / name, (time.time() - 60, time.time() - 60))
    # Every file of the copy is written, and its index, and git's own file is out of it: the repository is not yet marked as made.
    stepped(root, OURS, "open", {}, before="/made")
    copy = place / "threads" / OURS
    assert files_of(copy) == files_of(folder) and (place / "clones" / OURS / "worktrees" / OURS / "index").is_file()
    # You save a file, and take one away: the copy that was being made now differs from the folder both ways.
    (folder / "notes.txt").write_text("saved by you since\n")
    (folder / "Report.docx").unlink()
    with refused("no_whole_copy"):
        LocalHistory.at(place, folder, thread=OURS, user="u1").changed()
    again = LocalHistory.at(place, folder, thread=OURS, user="u1")
    # No thread worked in it: it is made again, and nothing is set aside or told.
    assert again.open() == {"copy": "made"} and set_aside_whole(place) == []
    assert files_of(again.copy) == files_of(folder) == [("notes.txt", b"saved by you since\n")]


def test_an_open_that_removes_what_holds_nothing_of_the_threads_own_killed_at_any_step_leaves_nothing_the_next_sets_aside(tmp_path):
    root = tmp_path / "whole"
    folder = root / "Documents"
    (folder / "sub").mkdir(parents=True)
    for name in ("Report.docx", "notes.txt", "sub/deep.txt"):
        (folder / name).write_text(f"{name} v1\n")
        os.utime(folder / name, (time.time() - 60, time.time() - 60))
    ours = a_copy(root, folder, OURS)
    # As its next request finds it: a copy that is the folder's files, a repository whose refs name those
    # alone, and the mark of its first open's end gone.
    assert LocalHistory.at(root / "store", folder, thread=OURS, user="u1").changed() == {"paths": []}
    (ours.repo / "made").unlink()
    for at, cut in each_cut(tmp_path, OURS, "open", {}, removals=True):
        place = cut / "store"
        again = LocalHistory.at(place, cut / "Documents", thread=OURS, user="u1")
        # Whatever part of either the cut left, it is none of the thread's own: nothing is kept, under any name.
        opened = again.open()
        assert set(opened) == {"copy"} and opened["copy"] in ("made", "moved"), (at, opened)
        assert set_aside_whole(place) == [], at
        assert files_of(again.copy) == files_of(cut / "Documents"), at
        assert sorted(p.name for p in (place / "threads").iterdir()) == [OURS] == sorted(p.name for p in (place / "clones").iterdir()), at


def test_a_threads_dropping_killed_at_any_step_leaves_each_of_its_folders_whole_or_gone_and_the_next_finishes_it(tmp_path):
    root = tmp_path / "whole"
    folder = root / "Documents"
    folder.mkdir(parents=True)
    for name in ("Report.docx", "notes.txt"):
        (folder / name).write_text(f"{name} v1\n")
        os.utime(folder / name, (time.time() - 60, time.time() - 60))
    ours, theirs = a_copy(root, folder, OURS), a_copy(root, folder, THEIRS)
    (ours.copy / "unlanded.md").write_text("ours, in no snapshot\n")
    (root / "store" / "waiting" / OURS / "_whiteboard").mkdir(parents=True)
    (root / "store" / "waiting" / OURS / "_whiteboard" / "canvas.json").write_text("{}")
    copy, waiting = as_it_is(ours.copy), as_it_is(root / "store" / "waiting" / OURS)
    for at, cut in each_cut(tmp_path, OURS, "drop", {}, removals=True):
        place = cut / "store"
        # Each of what a cut left is whole at its name, the copy only beside its repository; what was half gone is under a
        # name no thread has.
        assert as_it_is(place / "threads" / OURS) in ([], copy), at
        assert as_it_is(place / "waiting" / OURS) in ([], waiting), at
        assert not os.path.lexists(place / "threads" / OURS) or (place / "clones" / OURS / "made").is_file(), at
        # The next finishes it, and nothing of another thread's goes.
        assert LocalHistory.at(place, cut / "Documents", thread=OURS, user="u1").drop()["thread"] in ("dropped", "gone"), at
        assert [sorted(p.name for p in (place / name).iterdir()) for name in ("threads", "clones")] == [[THEIRS], [THEIRS]], at
        assert not any((place / "waiting").iterdir()), at
        assert files_of(place / "threads" / THEIRS) == files_of(cut / "Documents"), at


#: What our thread's landing leaves out, which no landing writes: a name that runs code, which it made, and a
#: file of the folder's with a second name, which it deleted.
LEFT = (".vscode/tasks.json", "gone.txt")


def a_landing_that_leaves_files_out(root: Path, many: int = 0) -> tuple[LocalHistory, dict, dict]:
    """A folder under *root* with our thread's landing applied, its record next; the record's step, and what the
    folder must hold whatever is cut.

    Another thread has landed a change to the report, a new file and a change to each of *many* more, and you have
    saved a file since.  Our thread changed the notes and made a file, which are applied; made a name that runs code
    and deleted a file the folder has under a second name, which no landing writes; and a command of its turn still
    running wrote a file after its turn was committed.
    """
    folder = root / "Documents"
    (folder / "sub").mkdir(parents=True)
    named = [("Report.docx", "report v1"), ("notes.txt", "v1 notes"), ("gone.txt", "linked v1"), ("sub/deep.txt", "deep")]
    for name, text in [*named, *((f"sub/many-{n:04d}.txt", f"many {n}") for n in range(many))]:
        (folder / name).write_text(f"{text}\n")
        os.utime(folder / name, (time.time() - 60, time.time() - 60))
    ours, theirs = a_copy(root, folder, OURS), a_copy(root, folder, THEIRS)
    (theirs.copy / "Report.docx").write_text("their report\n")
    (theirs.copy / "A-new.md").write_text("their new file\n")
    for n in range(many):
        (theirs.copy / f"sub/many-{n:04d}.txt").write_text(f"many {n}, by them\n")
    land(theirs, "saga:theirs")
    (folder / "yours.txt").write_text("saved by you since\n")
    (ours.copy / "notes.txt").write_text("our notes\n")
    (ours.copy / "B.md").write_text("our own\n")
    (ours.copy / ".vscode").mkdir()
    (ours.copy / ".vscode" / "tasks.json").write_text("{}\n")
    (ours.copy / "gone.txt").unlink()
    step = up_to_its_record(ours, "saga:ours", B, LEFT)
    assert [c["path"] for c in step["applied"]] == ["B.md", "notes.txt"]
    (ours.copy / "late.md").write_text("written after the turn was committed\n")
    kept = {name: (folder / name).read_bytes() for name in ("Report.docx", "A-new.md", "yours.txt", "sub/deep.txt", "gone.txt", "B.md", "notes.txt")}
    return ours, step, kept


def left_as_the_thread_has_them(root: Path, kept: dict, at: object) -> None:
    """After our thread's record that left files out is finished, by whichever act: the copy is the landing's files
    but those, which are as the thread has them; the folder's history records nothing of them as landed; and the
    thread's next turn lands them, where a landing then writes them, and the turn after lands its own file alone."""
    folder, store = root / "Documents", root / "store" / "history.git"
    ours = LocalHistory.at(root / "store", folder, thread=OURS, user="u1")
    # They are work the thread has not landed: its turn's open keeps the copy where it is.
    assert ours.open()["copy"] == "kept", at
    assert ours.changed() == {"paths": list(LEFT)}, at
    held = {**dict(files_of(folder)), ".vscode/tasks.json": b"{}\n"}
    del held["gone.txt"]
    assert dict(files_of(ours.copy)) == held, at
    assert {name: dict(files_of(folder)).get(name) for name in kept} == kept and not (folder / ".vscode").exists(), at
    main = git(store, "rev-parse", "refs/heads/main")
    assert git(store, "cat-file", "-p", f"{main}:gone.txt") == "linked v1", at
    assert ".vscode/tasks.json" not in git(store, "ls-tree", "-r", "--name-only", main).split(), at
    after = land(ours, "saga:next", B)
    assert [(c["path"], c["after"] is None) for c in after["changes"]] == [(".vscode/tasks.json", False), ("gone.txt", True)], at
    assert (folder / ".vscode" / "tasks.json").read_bytes() == b"{}\n" and not (folder / "gone.txt").exists(), at
    (ours.copy / "after.md").write_text("the turn after\n")
    assert [c["path"] for c in land(ours, "saga:after", B)["changes"]] == ["after.md"], at
    assert git(store, "fsck", "--no-dangling") == "", at


def never_recorded(root: Path, step: dict, kept: dict, at: object) -> None:
    """After our thread's record was cut before its push and never asked again: the landing never counted, and what
    it kept is not to be forgotten.  The thread's next turn lands the whole of its work, and nothing of anyone else's goes."""
    folder = root / "Documents"
    ours = LocalHistory.at(root / "store", folder, thread=OURS, user="u1")
    with refused("landing_unsettled"):
        ours.forget(saga="saga:ours", applied=step["applied"])
    assert ours.open()["copy"] == "kept", at
    after = land(ours, "saga:next", B)
    assert {c["path"] for c in after["changes"]} <= {"B.md", "notes.txt", "late.md", *LEFT}, at
    assert {name: dict(files_of(folder)).get(name) for name in kept if name != "gone.txt"} == {
        name: text for name, text in kept.items() if name != "gone.txt"
    }, at


@pytest.mark.parametrize("asked_again", [False, True])
def test_a_record_that_leaves_files_out_killed_at_any_step_leaves_them_as_the_thread_has_them_once_it_is_finished(tmp_path, asked_again):
    _, step, kept = a_landing_that_leaves_files_out(tmp_path / "whole")
    pushed = 0
    for at, root in each_cut(tmp_path, OURS, "record", step):
        store = root / "store" / "history.git"
        recorded = "Surogate-Saga: saga:ours" in git(store, "log", "-1", "--format=%B", "refs/heads/main")
        pushed += recorded
        again = LocalHistory.at(root / "store", root / "Documents", thread=OURS, user="u1")
        if asked_again:
            answer = again.record(**step)
            assert git(again.repo, "cat-file", "-p", f"{answer['set_aside']}:late.md") == "written after the turn was committed", at
        if asked_again or recorded:
            left_as_the_thread_has_them(root, kept, at)
        else:
            never_recorded(root, step, kept, at)
    # The cuts fall on both sides of the push.
    assert 0 < pushed < at


def test_an_open_after_a_record_that_left_files_out_was_cut_after_its_push_killed_at_any_step_leaves_them_as_the_thread_has_them(tmp_path):
    _, step, kept = a_landing_that_leaves_files_out(tmp_path / "whole")
    stepped(tmp_path / "whole", OURS, "record", step, before="update-ref refs/heads/main")
    for at, root in each_cut(tmp_path, OURS, "open", {}):
        left_as_the_thread_has_them(root, kept, at)


@pytest.mark.parametrize("inside", [0.003, 0.012])
@pytest.mark.parametrize("asked_again", [False, True])
def test_a_record_that_leaves_files_out_killed_inside_any_of_its_gits_leaves_them_as_the_thread_has_them_once_it_is_finished(tmp_path, asked_again, inside):
    _, step, kept = a_landing_that_leaves_files_out(tmp_path / "whole", many=300)
    for at, root in each_cut(tmp_path, OURS, "record", step, inside=inside):
        store = root / "store" / "history.git"
        recorded = "Surogate-Saga: saga:ours" in git(store, "log", "-1", "--format=%B", "refs/heads/main")
        if asked_again:
            LocalHistory.at(root / "store", root / "Documents", thread=OURS, user="u1").record(**step)
        if asked_again or recorded:
            left_as_the_thread_has_them(root, kept, at)
        else:
            never_recorded(root, step, kept, at)


@pytest.mark.parametrize("inside", [0.003, 0.012])
def test_an_open_after_a_record_that_left_files_out_was_cut_after_its_push_killed_inside_any_of_its_gits_leaves_them_as_the_thread_has_them(tmp_path, inside):
    _, step, kept = a_landing_that_leaves_files_out(tmp_path / "whole", many=300)
    stepped(tmp_path / "whole", OURS, "record", step, before="update-ref refs/heads/main")
    for at, root in each_cut(tmp_path, OURS, "open", {}, inside=inside):
        left_as_the_thread_has_them(root, kept, at)


def a_copy_to_let_go(root: Path) -> tuple[dict, list[tuple[str, bytes]], list[tuple[str, int, int, bytes]]]:
    """A folder under *root* whose thread's work is all in a snapshot, and the platform's folders in its copy: its
    branch holds every other file of the copy, and the copy is let go next.  What the folder must hold whatever is
    cut, every file of the copy, and the platform's folders in it as they lie."""
    ours, kept = a_folder_two_threads_work_on(root)
    LocalHistory.at(root / "store", root / "Documents", thread=OURS, user="u1").snapshot("before a step")
    platform_folders_in(ours.copy)
    return kept, files_of(ours.copy), platform_folders(ours.copy)


def made_whole_again(root: Path, kept: dict, files: list[tuple[str, bytes]], at: object) -> None:
    """After a cut: the thread's next operation has its copy made again from its branch, every file of it as it was and
    the platform's folders back in it; and its next turn lands its work, and nobody else's."""
    place = root / "store"
    again = LocalHistory.at(place, root / "Documents", thread=OURS, user="u1")
    assert again.open(moves=False) == {"copy": "kept"}, at
    assert files_of(again.copy) == files, at
    assert not os.path.lexists(place / "waiting" / OURS) and not os.path.lexists(place / "threads" / f"{OURS}.going"), at
    assert set_aside_whole(place) == [], at
    the_next_turn_lands_whole(root, kept, landed=False)


def test_a_letting_go_killed_at_any_step_leaves_the_whole_copy_at_its_name_or_nothing_and_its_next_open_makes_it_whole(tmp_path):
    kept, files, platform = a_copy_to_let_go(tmp_path / "whole")
    # Every name of the copy but the platform's folders, and the copy's own folder, whose time their leaving changes.
    held = [entry for entry in as_it_is(tmp_path / "whole" / "store" / "threads" / OURS) if entry not in platform and entry[0] != "."]
    for at, root in each_cut(tmp_path, OURS, "close", {}, removals=True):
        copy, waiting = root / "store" / "threads" / OURS, root / "store" / "waiting" / OURS
        # Cut anywhere: at the copy's name is the whole copy, but the platform's folders, or nothing; and each of those
        # is whole, in the copy or beside the copies.
        assert not os.path.lexists(copy) or [entry for entry in as_it_is(copy) if entry not in platform and entry[0] != "."] == held, at
        assert sorted([*platform_folders(copy), *platform_folders(waiting)]) == platform, at
        made_whole_again(root, kept, files, at)


@pytest.mark.parametrize("repository", ["whole", "made again"])
def test_an_open_after_a_letting_go_killed_at_any_step_loses_nothing_that_waited_beside_the_copies(tmp_path, repository):
    kept, files, platform = a_copy_to_let_go(tmp_path / "whole")
    ours = LocalHistory.at(tmp_path / "whole" / "store", tmp_path / "whole" / "Documents", thread=OURS, user="u1")
    assert ours.close() == {"copy": "removed"}
    if repository == "made again":
        # Its first open, as it is not marked as ended: what the thread did is set aside with its repository.
        (ours.repo / "made").unlink()
    for at, root in each_cut(tmp_path, OURS, "open", {"moves": False}, removals=True):
        copy, waiting = root / "store" / "threads" / OURS, root / "store" / "waiting" / OURS
        # Cut anywhere, each of the platform's folders is whole, beside the copies or in a whole copy.
        assert sorted([*platform_folders(copy), *platform_folders(waiting)]) == platform, at
        if repository == "whole":
            made_whole_again(root, kept, files, at)
            continue
        again = LocalHistory.at(root / "store", root / "Documents", thread=OURS, user="u1")
        assert again.open(moves=False)["copy"] in ("made", "kept"), at
        assert platform_folders(again.copy) == platform and not os.path.lexists(waiting), at


IDLE = THREAD.replace("0b6c", "4b6c")
#: What a pruning a hundred days on is asked: the busy thread's refs alone.
PRUNED = {"keep": [f"refs/heads/threads/{THEIRS}", f"refs/bases/{THEIRS}"], "now": time.time() + LATER}


def a_folder_to_prune(root: Path) -> None:
    """A folder under *root* whose history a pruning a hundred days on cuts, and every way a repository there reads it.

    Another thread has landed twenty-two times; an idle one's base is the
    folder's first commit; ours has a snapshot on a base the window lets go,
    in its reflog alone, and an index naming a version no commit of the window
    holds; and a repository of a thread's own was set aside whole, its work on
    the folder's first commit.  The history was pruned once already, on the
    day it was all within the window: it is one pack.
    """
    folder = root / "Documents"
    folder.mkdir(parents=True)
    for name, text in (("Report.docx", "report v1"), ("notes.txt", "v1 notes")):
        (folder / name).write_text(f"{text}\n")
        os.utime(folder / name, (time.time() - 60, time.time() - 60))
    place, store = root / "store", root / "store" / "history.git"
    gone = a_copy(root, folder, NEW)
    (gone.copy / "Plan.md").write_text("its plan\n")
    gone.snapshot("before a step")
    a_copy(root, folder, IDLE)
    a_copy(root, folder, OURS)
    theirs = a_copy(root, folder, THEIRS)
    lands_often(theirs, "notes.txt", 1)
    ours = LocalHistory.at(place, folder, thread=OURS, user="u1")
    assert ours.open() == {"copy": "moved"}
    untold = git(store, "rev-parse", "refs/heads/main:notes.txt")
    (ours.copy / "Draft.md").write_text("drafted then\n")
    put_back(ours, git(store, "rev-parse", "refs/heads/main"))
    lands_often(theirs, "notes.txt", 1)
    (gone.repo / "made").unlink()
    LocalHistory.at(place, folder, thread=NEW, user="u1").open()
    lands_often(theirs, "notes.txt", 20)
    assert LocalHistory.at(place, folder, thread=OURS, user="u1").open() == {"copy": "moved"}
    subprocess.run(
        ["git", f"--git-dir={ours.repo}", f"--work-tree={folder}", "update-index", "--add", "--cacheinfo", f"100644,{untold},then.txt"],
        check=True, env=HERMETIC,
    )
    assert LocalHistory.at(place, folder, thread=THEIRS, user="u1").prune(keep=[], now=time.time())["pruned"] is True
    assert len(packs_of(place)) == 1 and len(set_aside_whole(place)) == 2


def pruned_whole(root: Path, at: object) -> None:
    """After a pruning cut at *at*: no repository is astray, all read what they name, and the next pruning finishes the cut one.

    Then every repository notes the history's one pack, each live thread's copy opens, and ours lands again.
    """
    place, folder = root / "store", root / "Documents"
    threads = sorted(repo.name for repo in (place / "clones").iterdir())
    for thread in threads:
        history = LocalHistory.at(place, folder, thread=thread, user="u1")
        history._pin()
        assert not history.astray, (at, thread)
    # The work a cut pruning left, which links the packs it let go, is gone with the next request.
    assert not os.path.lexists(place / "pruning.git"), at
    connected(place)
    pruned = LocalHistory.at(place, folder, thread=OURS, user="u1").prune(**{**PRUNED, "now": PRUNED["now"] + 2 * 86_400})
    assert pruned["pruned"] is True, at
    [one] = packs_of(place)
    assert all(noted(repo) == {one} for repo in [*(place / "clones").iterdir(), *(place / "set-aside").glob("*.repository")]), at
    connected(place)
    for thread in threads:
        assert LocalHistory.at(place, folder, thread=thread, user="u1").open()["copy"] in ("moved", "kept"), (at, thread)
    ours = LocalHistory.at(place, folder, thread=OURS, user="u1")
    (ours.copy / "after.md").write_text("after the pruning\n")
    assert [c["path"] for c in land(ours, "saga:after")["changes"]] == ["after.md"], at
    connected(place)


@pytest.mark.parametrize("inside", [0, 0.012])
def test_a_pruning_killed_at_any_step_leaves_every_repository_whole_and_the_next_pruning_finishes_it(tmp_path, inside):
    a_folder_to_prune(tmp_path / "whole")
    cuts = 0
    for at, root in each_cut(tmp_path, THEIRS, "prune", PRUNED, removals=True, inside=inside):
        pruned_whole(root, at)
        cuts += 1
    assert cuts > 10
    # Run to its end, it prunes, and every repository is whole after it.
    root = tmp_path / "ended"
    shutil.copytree(tmp_path / "whole", root, symlinks=True)
    assert stepped(root, THEIRS, "prune", PRUNED)["answer"]["pruned"] is True
    connected(root / "store")


@pytest.mark.parametrize("broken", [name for name in BROKEN if name != "its copy's index is gone"])
def test_a_copy_whose_repository_is_none_a_request_reads_is_kept_as_it_is_for_its_next_open(tmp_path, folder, broken):
    one = a_copy(tmp_path, folder)
    (one.copy / "Draft.md").write_text("the thread's, in no snapshot\n")
    BROKEN[broken](one)
    held = as_it_is(one.copy)
    assert LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").close() == {"copy": "kept"}
    assert as_it_is(one.copy) == held
    # Its next open makes way for the copy made again, as it would have: what the thread made is kept.
    opened = LocalHistory.at(tmp_path / "store", folder, thread="t1", user="u1").open(moves=False)
    [copy] = [name for name in opened.get("set_aside_folders", []) if name.endswith("-t1.copy")]
    assert as_it_is(tmp_path / "store" / "set-aside" / copy) == held
