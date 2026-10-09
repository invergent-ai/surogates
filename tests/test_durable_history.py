"""A project's history in the bucket: pods fetch it at depth 1 and the lock holder pushes packs."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from surogates.harness.turn_summarizer import is_platform_path
from surogates.sandbox import history as history_module
from surogates.sandbox.history import THREAD_POD_DEADLINE, History, HistoryConflict, HistoryError
from surogates.tools.utils.checkpoint_manager import _shadow_repo_path

A = {"name": "Draft A", "email": "thread:t1@surogate"}


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    real = tmp_path / "project"
    (real / "uploads").mkdir(parents=True)
    (real / "Report.docx").write_bytes(b"PK\x03\x04 report v1")
    (real / "notes.txt").write_text("v1 notes\n")
    for file in real.rglob("*"):
        # Saved a minute ago: a file no older than the index git reads again, as it must.
        os.utime(file, (time.time() - 60, time.time() - 60))
    return real


def a_pod(tmp_path: Path, project: Path, thread: str = "t1", **more) -> History:
    """*thread*'s pod, opened on a disk of its own as each turn's pod is."""
    pod = tmp_path / f"pod-{len(list(tmp_path.glob('pod-*')))}"
    (pod / "workspace").mkdir(parents=True)
    history = History(
        repo=_shadow_repo_path(str(project), base=pod / "home"), project=project,
        copy=pod / "workspace", thread=thread, user="u1", **more,
    )
    history.open()
    return history


def land(history: History, saga: str = "saga:1", author=A) -> dict:
    """*history*'s turn landed whole, as the landing saga runs it."""
    main = history.fetch()["main"]
    turn = history.commit_turn(author=author, trailers=[["Surogate-Saga", saga], ["Surogate-Kind", "turn"]])
    applied = [history.apply(c["path"], c["before"], c["after"]) for c in turn["changes"]]
    return history.record(
        turn=turn["commit"], applied=applied, author=author,
        trailers=[["Surogate-Saga", saga], ["Surogate-Kind", "landing"]], main=main,
    )


def git(repo: Path, *args: str, input: str | None = None) -> str:
    return subprocess.run(
        ["git", f"--git-dir={repo}", *args], capture_output=True, text=True, check=True, input=input,
    ).stdout.strip()


def test_a_landing_reaches_the_bucket_as_packs_and_packed_refs_alone(tmp_path, project):
    history = a_pod(tmp_path, project)
    (history.copy / "Report.docx").write_bytes(b"PK\x03\x04 report v2")
    landed = land(history)
    files = sorted(str(p.relative_to(project / "_history")) for p in (project / "_history").rglob("*") if p.is_file())
    assert [f for f in files if not f.startswith("objects/pack/pack-")] == ["HEAD", "config", "index", "packed-refs"]
    assert {Path(f).suffix for f in files if f.startswith("objects/pack/")} == {".pack", ".idx"}
    # The durable history is a repository git can read, with main at the landing.
    assert git(project / "_history", "rev-parse", "refs/heads/main") == landed["commit"]
    assert git(project / "_history", "fsck", "--no-dangling") == ""
    # The Library and the turn summaries leave it out.
    assert is_platform_path("_history/packed-refs")


def test_the_next_pod_takes_up_the_history_where_the_last_left_it(tmp_path, project):
    first = a_pod(tmp_path, project)
    (first.copy / "threads" / "Draft A").mkdir(parents=True)
    (first.copy / "threads" / "Draft A" / "outline.md").write_text("outline")
    one = land(first)["commit"]

    second = a_pod(tmp_path, project)
    assert (second.copy / "threads" / "Draft A" / "outline.md").read_text() == "outline"
    assert git(second.repo, "rev-parse", "refs/heads/main") == one
    (second.copy / "threads" / "Draft A" / "sources.md").write_text("sources")
    two = land(second, "saga:2")["commit"]

    third = a_pod(tmp_path, project)
    assert sorted(p.name for p in (third.copy / "threads" / "Draft A").iterdir()) == ["outline.md", "sources.md"]
    assert git(project / "_history", "rev-list", "--first-parent", "refs/heads/main").splitlines()[:2] == [two, one]


def test_a_pod_fetches_main_its_branch_and_its_base_at_depth_one(tmp_path, project):
    for n in range(3):
        history = a_pod(tmp_path, project)
        (history.copy / f"{n}.md").write_text(str(n))
        land(history, f"saga:{n}")
    other = a_pod(tmp_path, project, "t2")
    (other.copy / "B.md").write_text("B's, not landed")
    other.commit_turn(author=A, trailers=[["Surogate-Saga", "saga:b"], ["Surogate-Kind", "turn"]])

    fresh = a_pod(tmp_path, project)
    refs = git(fresh.repo, "for-each-ref", "--format=%(refname)").splitlines()
    assert sorted(refs) == ["refs/bases/t1", "refs/heads/main", "refs/heads/threads/t1", "refs/synced/t1"]
    # Depth 1: the tips alone, not the three landings before them.
    assert git(fresh.repo, "rev-list", "--count", "refs/heads/main") == "1"
    assert not (fresh.copy / "B.md").exists()


def test_your_changes_since_the_last_landing_are_in_the_next_copy(tmp_path, project):
    history = a_pod(tmp_path, project)
    (history.copy / "Report.docx").write_bytes(b"PK\x03\x04 report v2")
    one = land(history)["commit"]
    (project / "uploads" / "brief.pdf").write_bytes(b"%PDF uploaded since")
    (project / "notes.txt").write_text("v2 notes, saved by you\n")

    pod = a_pod(tmp_path, project)
    assert (pod.copy / "uploads" / "brief.pdf").read_bytes() == b"%PDF uploaded since"
    assert (pod.copy / "notes.txt").read_text() == "v2 notes, saved by you\n"
    # The pod's own pickup, by you, is its copy's base.
    yours = git(pod.repo, "rev-parse", "refs/bases/t1")
    assert git(pod.repo, "log", "-1", "--format=%an <%ae>|%P", yours) == f"u1 <user:u1@surogate>|{one}"
    (pod.copy / "threads").mkdir()
    (pod.copy / "threads" / "plan.md").write_text("plan")
    two = land(pod, "saga:2")["commit"]
    # Only the landing goes on main, on main's tip: the pickup is no record of yours.
    assert git(project / "_history", "rev-parse", f"{two}^1") == one
    assert (project / "threads" / "plan.md").read_text() == "plan"
    assert (project / "notes.txt").read_text() == "v2 notes, saved by you\n"


def test_a_pod_reads_only_the_real_files_whose_size_or_time_changed(tmp_path, project):
    history = a_pod(tmp_path, project)
    (history.copy / "Report.docx").write_bytes(b"PK\x03\x04 report v2")
    land(history)
    # A file nothing changed since the landing, which this pod could not read.
    (project / "notes.txt").chmod(0)
    try:
        pod = a_pod(tmp_path, project)
    finally:
        (project / "notes.txt").chmod(0o644)
    assert (pod.copy / "notes.txt").read_text() == "v1 notes\n"
    assert (pod.copy / "Report.docx").read_bytes() == b"PK\x03\x04 report v2"


def a_messy_project(project: Path) -> None:
    """Files of every kind an open meets: names git must not misread, files it leaves out, a link, a repository."""
    (project / "Annual report 2025.docx").write_bytes(b"PK\x03\x04 annual")
    (project / "uploads" / "Écran d'accueil.png").write_bytes(b"\x89PNG accueil")
    (project / "uploads" / "two\nlines.txt").write_text("a name with a line break\n")
    (project / "uploads" / "deep" / "er").mkdir(parents=True)
    (project / "uploads" / "deep" / "er" / "notes.md").write_text("deep\n")
    (project / "empty folder").mkdir()
    (project / "empty.md").write_bytes(b"")
    (project / "archive").mkdir()
    (project / "archive" / "old.md").write_text("old\n")
    (project / "scratch.tmp").write_text("left out\n")
    (project / "node_modules" / "x").mkdir(parents=True)
    (project / "node_modules" / "x" / "index.js").write_text("left out\n")
    (project / "latest").symlink_to("notes.txt")
    (project / "run.sh").write_text("#!/bin/sh\n")
    (project / "run.sh").chmod(0o755)
    (project / "plain.sh").write_text("#!/bin/sh\n")
    (project / "vendor").mkdir()
    subprocess.run(["git", "init", "-q", str(project / "vendor" / "lib")], check=True)
    (project / "vendor" / "lib" / "lib.c").write_text("a repository of its own\n")
    for n in range(80):
        (project / "uploads" / f"scan {n:02}.pdf").write_bytes(b"%PDF " + bytes([n]) * 100)
    for file in project.rglob("*"):
        if "vendor" not in file.parts and not file.is_symlink():
            os.utime(file, (time.time() - 60, time.time() - 60))


def changed_since(project: Path) -> None:
    """Every kind of change you can make to the real files between two landings."""
    (project / "notes.txt").write_text("v2 notes, saved by you\n")  # changed
    (project / "Report.docx").unlink()  # removed
    (project / "uploads" / "scan 03.pdf").unlink()
    (project / "uploads" / "scan 03.pdf").mkdir()  # a folder where a file was
    (project / "uploads" / "scan 03.pdf" / "page 1.png").write_bytes(b"\x89PNG page")
    shutil.rmtree(project / "uploads" / "deep")
    (project / "uploads" / "deep").write_text("a file where a folder was\n")
    os.utime(project / "Annual report 2025.docx", (time.time() - 30, time.time() - 30))  # touched, not changed
    (project / "run.sh").chmod(0o644)  # no longer to be run: nothing else of it changed
    (project / "plain.sh").chmod(0o755)  # and one now to be run
    (project / "latest").unlink()
    (project / "latest").symlink_to("Annual report 2025.docx")  # a link that points elsewhere
    (project / "empty.md").write_text("no longer empty\n")
    (project / "archive").rename(project / "moved")
    (project / "archive").symlink_to("moved")  # a link where a folder was: what was under it is under the link's folder now
    for n in range(40, 80):  # saved again in place: the same size with other bytes, and another size
        (project / "uploads" / f"scan {n:02}.pdf").write_bytes(b"%PDF " + bytes([n + 100]) * (100 if n % 2 else 150))
    for n in range(80):
        (project / "new" / f"{n % 4}").mkdir(parents=True, exist_ok=True)
        (project / "new" / f"{n % 4}" / f"upload {n:02}.docx").write_bytes(b"PK\x03\x04 " + bytes([n]) * 200)
    for file in (project / "new").rglob("*"):
        os.utime(file, (time.time() - 20, time.time() - 20))
    for n in range(40, 80):
        os.utime(project / "uploads" / f"scan {n:02}.pdf", (time.time() - 20, time.time() - 20))


def mains_tree(history: History) -> str:
    return git(history.repo, "rev-parse", "refs/bases/t1^{tree}")


def tree_git_alone_makes(project: Path, tmp_path: Path) -> str:
    """The tree one ``git add -A`` makes of the real files, with the history's excludes and attributes."""
    repo = tmp_path / f"alone-{len(list(tmp_path.glob('alone-*')))}.git"
    env = {
        **{name: value for name, value in os.environ.items() if not name.startswith("GIT_")},
        "GIT_DIR": str(repo), "GIT_WORK_TREE": str(project), "GIT_LITERAL_PATHSPECS": "1",
    }

    def alone(*args: str) -> str:
        return subprocess.run(["git", *args], env=env, cwd=project, check=True, capture_output=True, text=True).stdout.strip()

    alone("init", "-q", "-b", "main")
    (repo / "info" / "exclude").write_text("\n".join(history_module.HISTORY_EXCLUDES) + "\n/vendor/lib/\n")
    (repo / "info" / "attributes").write_text(history_module._ATTRIBUTES)
    alone("add", "-A")
    return alone("write-tree")


def read_by(monkeypatch) -> dict:
    """Which gits read the real files from here on: each reader's files, and every ``git add -A`` over the real files."""
    seen, run = {"readers": [], "alone": 0}, subprocess.run

    def spied(args, **kwargs):
        if args[:2] == ["git", "update-index"] and "--stdin" in args:
            seen["readers"].append(sorted(n for n in kwargs["input"].split("\0") if n))
        if args[:3] == ["git", "add", "-A"] and (kwargs.get("env") or {}).get("GIT_WORK_TREE", "").endswith("/project"):
            seen["alone"] += 1
        return run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spied)
    return seen


@pytest.mark.parametrize("since", ["no history yet", "a landing", "a landing of many files", "files saved again in place"])
def test_an_open_reads_many_real_files_several_at_once_and_makes_the_commit_git_alone_would(tmp_path, project, monkeypatch, since):
    a_messy_project(project)
    if since == "a landing":
        first = a_pod(tmp_path, project)
        (first.copy / "a.md").write_text("a")
        land(first)
        changed_since(project)
    elif since == "a landing of many files":
        # The history's index knows a file a landing wrote by no size or time: the next pod reads each of them.
        first = a_pod(tmp_path, project)
        for n in range(80):
            (first.copy / f"part {n:02}.md").write_text(f"part {n}\n")
        land(first)
        (project / "Report.docx").unlink()  # and you removed a file since
    elif since == "files saved again in place":
        # No new file and none a landing wrote: every one is in the history's index, by another size or time.
        first = a_pod(tmp_path, project)
        (first.copy / "a.md").write_text("a")
        land(first)
        for n in range(80):
            (project / "uploads" / f"scan {n:02}.pdf").write_bytes(b"%PDF " + bytes([n + 100]) * (100 if n % 2 else 150))
            os.utime(project / "uploads" / f"scan {n:02}.pdf", (time.time() - 20, time.time() - 20))
    time.sleep(1.1)  # saved before the second the pod opens in: git reads again a file saved in that second
    seen = read_by(monkeypatch)
    pod = a_pod(tmp_path, project)
    # Read by several gits at once, never more than the bound, each file by one of them: and by no git alone.
    read = [name for names in seen["readers"] for name in names]
    assert 1 < len(seen["readers"]) <= history_module._READERS and len(read) == len(set(read)) > history_module._READ_ALONE
    assert seen["alone"] == 0
    assert not [name for name in read if name.startswith(("node_modules/", "vendor/")) or name.endswith(".tmp")]
    if since == "a landing":
        # Those saved again in place, the one whose mode alone changed each way, the link and the file no longer
        # empty are read; one only touched too; one neither saved nor touched is not.
        assert {"uploads/scan 41.pdf", "uploads/scan 42.pdf", "run.sh", "plain.sh", "latest", "empty.md", "Annual report 2025.docx"} <= set(read)
        assert "uploads/scan 05.pdf" not in read and "Report.docx" not in read and "uploads/scan 03.pdf" not in read
    # The pod's base is the commit git alone makes of the same files.
    assert mains_tree(pod) == tree_git_alone_makes(project, tmp_path)
    names = sorted(str(f.relative_to(pod.copy)) for f in pod.copy.rglob("*") if not f.is_dir())
    assert "uploads/two\nlines.txt" in names and "latest" in names and "scratch.tmp" not in names
    # Each file is known by its size and time from then on: a later look finds none to read again.
    assert git(pod.repo, f"--work-tree={project}", "diff-files", "--name-only") == ""


def test_an_open_decides_what_to_read_after_its_look_and_reads_a_few_files_with_one_git(tmp_path, project, monkeypatch):
    a_messy_project(project)
    first = a_pod(tmp_path, project)
    (first.copy / "a.md").write_text("a")
    land(first)
    time.sleep(1.1)
    seen = read_by(monkeypatch)
    again = a_pod(tmp_path, project)
    # A file the landing wrote is one the history's index does not know by size and time, as an empty
    # file is, to git too: one git reads the two, and no git looks at the rest.
    assert (seen["readers"], seen["alone"]) == ([["a.md", "empty.md"]], 0)
    seen["readers"].clear()
    (project / "notes.txt").write_text("v2 notes, saved by you\n")
    (project / "uploads" / "scan 07.pdf").unlink()
    time.sleep(1.1)
    pod = a_pod(tmp_path, project)
    assert (seen["readers"], seen["alone"]) == ([["a.md", "empty.md", "notes.txt"]], 0)
    assert mains_tree(pod) == tree_git_alone_makes(project, tmp_path) != mains_tree(again)
    assert not (pod.copy / "uploads" / "scan 07.pdf").exists() and (pod.copy / "notes.txt").read_text() == "v2 notes, saved by you\n"


def test_a_file_no_older_than_the_index_is_read_however_its_size_and_time_match(tmp_path, project, monkeypatch):
    # Saved in a second the clock has not passed: no index written now is newer than it.
    second = int(time.time()) + 3600
    os.utime(project / "notes.txt", (second, second))
    first = a_pod(tmp_path, project)
    (first.copy / "a.md").write_text("a")
    land(first)  # the history's index knows notes.txt by that second and its size
    look = History._look

    def saved_again_first(self, paths):
        (project / "notes.txt").write_text("v9 notes\n")  # again, in that second, at the same size
        os.utime(project / "notes.txt", (second, second))
        return look(self, paths)

    monkeypatch.setattr(History, "_look", saved_again_first)
    # Its size and time are the index's, and it may have been saved again unseen: it is read.
    assert (a_pod(tmp_path, project).copy / "notes.txt").read_text() == "v9 notes\n"


def test_a_file_that_changes_while_many_are_read_leaves_the_read_to_git_alone(tmp_path, project, monkeypatch):
    a_messy_project(project)
    run = subprocess.run

    def a_folder_takes_a_files_place(args, **kwargs):
        if args[:2] == ["git", "update-index"] and "--stdin" in args and "uploads/scan 07.pdf" in kwargs["input"].split("\0"):
            (project / "uploads" / "scan 07.pdf").unlink()
            (project / "uploads" / "scan 07.pdf").mkdir()
            (project / "uploads" / "scan 07.pdf" / "page.png").write_bytes(b"\x89PNG")
        return run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", a_folder_takes_a_files_place)
    pod = a_pod(tmp_path, project)
    # Whatever the several readers could not finish, the copy is the real files as they are.
    assert (pod.copy / "uploads" / "scan 07.pdf" / "page.png").read_bytes() == b"\x89PNG"
    assert (pod.copy / "uploads" / "scan 08.pdf").exists()


def a_reader_fails(monkeypatch, name: str, *, times: int) -> dict:
    """The reader given the file *name* fails its first *times* tries, as a read the mount refused does; what each git did from here on."""
    seen, run = {"tries": 0, "alone": 0, "readers": 0}, subprocess.run

    def refused(args, **kwargs):
        if args[:2] == ["git", "update-index"] and "--stdin" in args:
            seen["readers"] += 1
            if name in kwargs["input"].split("\0"):
                seen["tries"] += 1
                if seen["tries"] <= times:
                    return subprocess.CompletedProcess(args, 128, "", f"error: unable to read {name}: Input/output error")
        if args[:3] == ["git", "add", "-A"] and (kwargs.get("env") or {}).get("GIT_WORK_TREE", "").endswith("/project"):
            seen["alone"] += 1
        return run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", refused)
    return seen


def test_a_reader_that_fails_is_tried_once_more_before_the_read_is_left_to_one_git(tmp_path, project, monkeypatch, caplog):
    a_messy_project(project)
    time.sleep(1.1)
    seen = a_reader_fails(monkeypatch, "uploads/scan 07.pdf", times=1)
    with caplog.at_level(logging.WARNING, logger=history_module.__name__):
        pod = a_pod(tmp_path, project)
    # One read the mount refused is not sixteen readers' work thrown away: that reader alone reads again.
    assert (seen["tries"], seen["alone"]) == (2, 0) and seen["readers"] == history_module._READERS + 1
    assert mains_tree(pod) == tree_git_alone_makes(project, tmp_path)
    assert "is tried once more" in caplog.text and "Input/output error" in caplog.text
    assert "read by one git" not in caplog.text


def test_a_reader_that_fails_twice_leaves_the_read_to_one_git_and_the_pods_log_says_why(tmp_path, project, monkeypatch, caplog):
    a_messy_project(project)
    time.sleep(1.1)
    seen = a_reader_fails(monkeypatch, "uploads/scan 07.pdf", times=2)
    with caplog.at_level(logging.WARNING, logger=history_module.__name__):
        pod = a_pod(tmp_path, project)
    assert (seen["tries"], seen["alone"]) == (2, 1)
    assert mains_tree(pod) == tree_git_alone_makes(project, tmp_path)
    # Whoever reads the pod's log to learn why an open was slow finds the fallback, and its reason.
    [said] = [r.getMessage() for r in caplog.records if "read by one git" in r.getMessage()]
    assert "unable to read uploads/scan 07.pdf: Input/output error" in said


@pytest.mark.parametrize("index", ["of another version", "with a merge's entries"])
def test_a_kept_index_the_readers_cannot_use_is_made_again_from_main_and_no_git_reads_alone(tmp_path, project, monkeypatch, caplog, index):
    a_messy_project(project)
    first = a_pod(tmp_path, project)
    (first.copy / "a.md").write_text("a")
    land(first)
    kept = project / "_history" / "index"
    env = {**os.environ, "GIT_INDEX_FILE": str(kept), "GIT_DIR": str(first.repo), "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    if index == "of another version":
        # As a thread's command can leave it: git reads it, and keeps its version at every write.
        subprocess.run(["git", "update-index", "--index-version", "4"], env=env, check=True)
    else:
        blob = git(first.repo, "rev-parse", "refs/heads/main:notes.txt")
        staged = "".join(f"100644 {blob} {stage}\tnotes.txt\n" for stage in (1, 2, 3))
        subprocess.run(["git", "update-index", "--index-info"], env=env, input=staged, text=True, check=True)
    changed_since(project)
    time.sleep(1.1)
    seen = read_by(monkeypatch)
    with caplog.at_level(logging.WARNING, logger=history_module.__name__):
        pod = a_pod(tmp_path, project)
    # Not left to one git, which a large project's pod would not outlast at every open from then on:
    # the index is made again from main, and every file read by the readers.
    assert seen["alone"] == 0 and len(seen["readers"]) == history_module._READERS
    assert mains_tree(pod) == tree_git_alone_makes(project, tmp_path)
    assert "made again from main" in caplog.text
    # And the index the pod leaves is one the next open reads as it is.
    assert history_module._entries(pod.repo / "index") is not None
    assert git(pod.repo, f"--work-tree={project}", "diff-files", "--name-only") == ""


def test_a_pod_looks_again_at_each_folder_main_has_before_it_reads_the_real_files(tmp_path, project, monkeypatch):
    first = a_pod(tmp_path, project)
    (first.copy / "threads" / "Draft A").mkdir(parents=True)
    (first.copy / "threads" / "Draft A" / "Y.md").write_text("by A")
    land(first)
    asked: list[tuple[str, str]] = []
    add_all = History._add_all
    monkeypatch.setattr(os, "setxattr", lambda path, name, value: asked.append((str(path), name)))
    monkeypatch.setattr(History, "_add_all", lambda self, git: (asked.append(("the real files", "read")), add_all(self, git))[1])
    a_pod(tmp_path, project)
    # geesefs trusts a listing for a second: one taken just before another pod's landing would hide its files.
    read = asked.index(("the real files", "read"))
    folders = {path for path, name in asked[:read] if name == ".invalidate"}
    assert {str(project), str(project / "threads"), str(project / "threads" / "Draft A")} <= folders


def test_a_branch_with_nothing_unlanded_starts_again_at_main_with_its_base(tmp_path, project):
    first = a_pod(tmp_path, project, "t1")
    (first.copy / "A.md").write_text("by A")
    land(first, "saga:a")
    other = a_pod(tmp_path, project, "t2")
    (other.copy / "B.md").write_text("by B")
    by_b = land(other, "saga:b", author={"name": "Draft B", "email": "thread:t2@surogate"})["commit"]

    pod = a_pod(tmp_path, project, "t1")
    assert (pod.copy / "B.md").read_text() == "by B"
    for ref in ("refs/heads/threads/t1", "refs/bases/t1"):
        assert git(pod.repo, "rev-parse", ref) == by_b


def test_a_branch_with_unlanded_work_keeps_it_and_its_base_and_lands_it_later(tmp_path, project):
    history = a_pod(tmp_path, project)
    (history.copy / "Report.docx").write_bytes(b"PK\x03\x04 report v2")
    turn = history.commit_turn(author=A, trailers=[["Surogate-Saga", "saga:1"], ["Surogate-Kind", "turn"]])
    # The landing rolled back: the real files are as they were, the turn is on the branch.
    assert (project / "Report.docx").read_bytes() == b"PK\x03\x04 report v1"

    pod = a_pod(tmp_path, project)
    assert (pod.copy / "Report.docx").read_bytes() == b"PK\x03\x04 report v2"
    assert git(pod.repo, "rev-parse", "refs/bases/t1") == turn["base"]
    land(pod, "saga:2")
    assert (project / "Report.docx").read_bytes() == b"PK\x03\x04 report v2"


def test_a_threads_version_that_did_not_land_is_in_the_history_after_its_pod(tmp_path, project):
    history = a_pod(tmp_path, project)
    (history.copy / "Report.docx").write_bytes(b"PK\x03\x04 report by A")
    (project / "Report.docx").write_bytes(b"PK\x03\x04 report by you")
    landed = land(history)["commit"]
    shutil.rmtree(history.repo.parent.parent)  # the pod goes, its clone and copy with it
    # The newer file stays; the thread's version is the landing's second parent, in the bucket.
    assert (project / "Report.docx").read_bytes() == b"PK\x03\x04 report by you"
    assert git(project / "_history", "show", f"{landed}^2:Report.docx") == "PK\x03\x04 report by A"
    # The thread's next copy has the newer file to redo its edit on.
    assert (a_pod(tmp_path, project).copy / "Report.docx").read_bytes() == b"PK\x03\x04 report by you"


def test_a_record_repeated_after_a_lost_reply_finds_its_landing(tmp_path, project):
    history = a_pod(tmp_path, project)
    (history.copy / "Report.docx").write_bytes(b"PK\x03\x04 report v2")
    main = history.fetch()["main"]
    trailers = [["Surogate-Saga", "saga:1"], ["Surogate-Kind", "turn"]]
    turn = history.commit_turn(author=A, trailers=trailers)
    # So is the commit step's: the turn is committed and pushed once.
    assert history.commit_turn(author=A, trailers=trailers)["commit"] == turn["commit"]
    applied = [history.apply(c["path"], c["before"], c["after"]) for c in turn["changes"]]
    record = {"turn": turn["commit"], "applied": applied, "author": A, "trailers": trailers, "main": main}
    first = history.record(**record)
    assert history.record(**record) == first
    assert git(project / "_history", "rev-list", "--count", "--first-parent", "refs/heads/main") == "2"


def test_a_landing_whose_main_moved_since_it_began_is_refused(tmp_path, project):
    first, second = a_pod(tmp_path, project, "t1"), a_pod(tmp_path, project, "t2")
    (second.copy / "B.md").write_text("by B")
    main = second.fetch()["main"]
    (first.copy / "A.md").write_text("by A")
    land(first, "saga:a")
    turn = second.commit_turn(author=A, trailers=[["Surogate-Saga", "saga:b"], ["Surogate-Kind", "turn"]])
    applied = [second.apply(c["path"], c["before"], c["after"]) for c in turn["changes"]]
    with pytest.raises(HistoryConflict, match="main moved"):
        second.record(
            turn=turn["commit"], applied=applied, author=A,
            trailers=[["Surogate-Saga", "saga:b"], ["Surogate-Kind", "landing"]], main=main,
        )


def test_a_landing_after_another_moved_main_goes_on_top_of_it(tmp_path, project):
    first, second = a_pod(tmp_path, project, "t1"), a_pod(tmp_path, project, "t2")
    (first.copy / "A.md").write_text("by A")
    by_a = land(first, "saga:a")["commit"]
    (second.copy / "B.md").write_text("by B")
    by_b = land(second, "saga:b")["commit"]
    assert git(project / "_history", "rev-parse", f"{by_b}^1") == by_a
    # B's landing keeps A's: it is B's files on main as A left it.
    assert git(project / "_history", "ls-tree", "--name-only", by_b).splitlines() == ["A.md", "B.md", "Report.docx", "notes.txt"]


def test_the_first_look_says_whether_main_carries_a_landings_saga(tmp_path, project):
    history = a_pod(tmp_path, project)
    (history.copy / "Report.docx").write_bytes(b"PK\x03\x04 report v2")
    landed = land(history, "saga:mine")
    looked = history.fetch(saga="saga:mine")
    assert (looked["main"], looked["has_saga"]) == (landed["commit"], True)
    assert history.fetch(saga="saga:another")["has_saga"] is False


def test_a_look_fetches_the_commits_it_is_asked_for_and_names_those_the_history_no_longer_has(tmp_path, project):
    first = a_pod(tmp_path, project)
    (first.copy / "a.md").write_text("a")
    turn = first.commit_turn(author=A, trailers=[["Surogate-Saga", "saga:1"], ["Surogate-Kind", "turn"]])
    wanted = [turn["commit"], turn["base"]]
    holder = a_pod(tmp_path, project, "t2")  # the next lock holder's pod: it never had the turn or its base
    assert holder.fetch(commits=wanted)["missing"] == []
    assert git(holder.repo, "show", f"{turn['commit']}:a.md") == "a"
    # The history gone, as the master's own tools can delete it: no fetch can bring them.
    shutil.rmtree(project / "_history")
    (project / "later.md").write_text("saved since")  # so a new pod's first commit is not the base again, made in the same second
    assert a_pod(tmp_path, project, "t3").fetch(commits=wanted)["missing"] == wanted
    # A pod that fetched them before still has them.
    assert holder.fetch(commits=wanted)["missing"] == []


KEPT = [["Surogate-Kind", "kept"]]


def test_a_failed_turns_work_is_kept_on_its_branch_for_the_next_pod(tmp_path, project):
    history = a_pod(tmp_path, project)
    (history.copy / "Report.docx").write_bytes(b"PK\x03\x04 half made")
    history.keep(author=A, trailers=KEPT, base=True)
    pod = a_pod(tmp_path, project)
    assert (pod.copy / "Report.docx").read_bytes() == b"PK\x03\x04 half made"
    assert (project / "Report.docx").read_bytes() == b"PK\x03\x04 report v1"
    land(pod, "saga:2")
    assert (project / "Report.docx").read_bytes() == b"PK\x03\x04 half made"


#: What a thread's commands can write into the history's files: each file, and its text from main's id and an option for git.
CRAFTED = {
    "id": ("packed-refs", lambda main, option: f"{option} refs/heads/main\n"),
    "ref": ("packed-refs", lambda main, option: f"{main} refs/heads/main\n{main} refs/heads/../../config\n"),
    "ref-dash": ("packed-refs", lambda main, option: f"{main} refs/heads/main\n{main} refs/heads/-x\n"),
    "ref-other-prefix": ("packed-refs", lambda main, option: f"{main} refs/heads/main\n{main} refs/tags/x\n"),
    "ref-control": ("packed-refs", lambda main, option: f"{main} refs/heads/main\n{main} refs/heads/a\x1bb\n"),
    "peeled": ("packed-refs", lambda main, option: f"{main} refs/heads/main\n^{option}\n"),
    "shallow": ("shallow", lambda main, option: f"{option}\n"),
    "short": ("shallow", lambda main, option: f"{main[:39]}\n"),
    "upper": ("shallow", lambda main, option: f"{main.upper()}\n"),
    "HEAD": ("HEAD", lambda main, option: f"ref: {option}\n"),
    "HEAD-shape": ("HEAD", lambda main, option: f"{option}\n"),
    "config": ("config", lambda main, option: f"[core]\n\tbare = true\n\tfsmonitor = {option}\n"),
}


@pytest.mark.parametrize("crafted", list(CRAFTED))
def test_a_history_a_command_wrote_runs_nothing_in_a_pod_and_refuses_its_open(tmp_path, project, crafted):
    first = a_pod(tmp_path, project)
    (first.copy / "A.md").write_text("by A")
    land(first, "saga:1")
    holder = a_pod(tmp_path, project, "t3")  # a pod already open: the next lock holder's
    durable, ran = project / "_history", tmp_path / "ran"
    # A thread's commands can write the history: an option for git where an id goes.
    option = f"--upload-pack=touch${{IFS}}{ran}"
    name, text = CRAFTED[crafted]
    header = "# pack-refs with: peeled fully-peeled sorted \n" if name == "packed-refs" else ""
    (durable / name).write_text(header + text(git(durable, "rev-parse", "refs/heads/main"), option))
    with pytest.raises(HistoryError, match="refused the project's history") as refused:
        a_pod(tmp_path, project, "t2")
    assert "upload-pack" not in str(refused.value)
    if name not in ("HEAD", "config"):
        # Nor at a landing's first look, in a pod opened before.
        with pytest.raises(HistoryError, match="refused the project's history"):
            holder.fetch()
    with pytest.raises(HistoryError, match="project's history"):
        holder.fetch(commits=[option])
    assert not ran.exists()


def cut_history(tmp_path: Path, durable: Path, *, kept: int) -> None:
    """A pruning's cut, made by hand: ``main``'s last *kept* landings stay, its other threads gone, with ``shallow`` below."""
    work = tmp_path / f"cut-{time.monotonic_ns()}.git"
    subprocess.run(["git", "clone", "-q", "--mirror", "--no-hardlinks", str(durable), str(work)], check=True, capture_output=True)
    for ref in git(work, "for-each-ref", "--format=%(refname)").splitlines():
        if ref != "refs/heads/main":
            git(work, "update-ref", "-d", ref)
    mains = git(work, "log", "--first-parent", "--format=%H", "refs/heads/main").split()
    tips = set(git(work, "for-each-ref", "--format=%(objectname)").split())
    parents = {line.split()[0]: line.split()[1:] for line in git(work, "rev-list", "--all", "--parents").splitlines()}
    stays = set(git(work, "rev-list", "--all", f"^{mains[kept]}").split()) | tips
    (work / "shallow").write_text("".join(f"{c}\n" for c in sorted(c for c in stays if any(p not in stays for p in parents[c]))))
    git(work, "reflog", "expire", "--expire=now", "--all")
    git(work, "gc", "-q", "--prune=now")
    git(work, "pack-refs", "--all", "--prune")
    for old in (durable / "objects" / "pack").iterdir():
        old.unlink()
    for name in ("shallow", "packed-refs", *(f"objects/pack/{p.name}" for p in (work / "objects" / "pack").iterdir())):
        shutil.copyfile(work / name, durable / name)


def whole(repo: Path, *args: str) -> str:
    """What git says is wrong with *repo* when it runs *args*: nothing, for a whole history."""
    out = subprocess.run(["git", f"--git-dir={repo}", *args], capture_output=True, text=True)
    return f"{out.stdout}{out.stderr}".strip() or f"exit {out.returncode}" if out.returncode else ""


def test_a_push_retried_after_its_pack_went_up_still_marks_the_commit_a_pruning_cut_below(tmp_path, project, monkeypatch):
    first = a_pod(tmp_path, project, "early")
    (first.copy / "start.md").write_text("x")
    land(first, "saga:start")
    slow = a_pod(tmp_path, project, "slow")  # a long turn's pod, open across a pruning
    (slow.copy / "slow.md").write_text("the slow thread's work")
    for n in range(4):
        other = a_pod(tmp_path, project, f"o{n}")
        (other.copy / "Budget.xlsx").write_bytes(os.urandom(5_000))
        land(other, f"saga:o{n}", author={"name": f"O{n}", "email": f"thread:o{n}@surogate"})
    durable = project / "_history"
    cut_history(tmp_path, durable, kept=2)
    assert subprocess.run(["git", f"--git-dir={durable}", "cat-file", "-e", git(slow.repo, "rev-parse", "refs/bases/slow")]).returncode
    main = slow.fetch()["main"]
    trailers = [["Surogate-Saga", "saga:slow"], ["Surogate-Kind", "turn"]]
    put, failed = History._put_durable, []

    def shallow_fails_once(self, name, source):
        if name == "shallow" and not failed:
            failed.append(name)
            raise OSError(5, "Input/output error")  # the pack is up; its shallow line is not
        return put(self, name, source)

    monkeypatch.setattr(History, "_put_durable", shallow_fails_once)
    with pytest.raises(OSError):
        slow.commit_turn(author=A, trailers=trailers)
    turn = slow.commit_turn(author=A, trailers=trailers)  # the step's retry
    # The pod's own boundary, in the pack since the first try, is marked all the same.
    assert whole(durable, "fsck", "--no-dangling") == whole(durable, "rev-list", "--all", "--objects", "--quiet") == ""
    applied = [slow.apply(c["path"], c["before"], c["after"]) for c in turn["changes"]]
    slow.record(turn=turn["commit"], applied=applied, author=A, trailers=[["Surogate-Saga", "saga:slow"], ["Surogate-Kind", "landing"]], main=main)
    assert (project / "slow.md").read_text() == "the slow thread's work"
    assert whole(durable, "fsck", "--no-dangling") == ""


def test_the_opens_git_has_the_pods_ready_bound_and_a_steps_git_its_own(tmp_path, project, monkeypatch):
    first = a_pod(tmp_path, project)
    (first.copy / "A.md").write_text("by A")
    land(first)
    run, timeouts = subprocess.run, []

    def timed(args, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        return run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", timed)
    pod = a_pod(tmp_path, project)
    # Through geesefs, a large project's fetch is bound by request latency: ten minutes, less a margin.
    opened = set(timeouts)
    assert len(opened) == 1 and 540 <= opened.pop() < 600
    timeouts.clear()
    (pod.copy / "B.md").write_text("b")
    pod.commit_turn(author=A, trailers=[["Surogate-Saga", "saga:2"], ["Surogate-Kind", "turn"]])
    assert set(timeouts) == {120}


def test_a_save_in_the_second_a_pod_read_the_file_reaches_the_next_copy(tmp_path, project, monkeypatch):
    first = a_pod(tmp_path, project)
    (first.copy / "A.md").write_text("by A")
    land(first, "saga:a")
    add_all = History._add_all

    def a_large_projects_read(self, git):
        add_all(self, git)
        time.sleep(1.1)
        os.utime(self.repo / "index")  # git writes the index seconds after it read notes.txt

    time.sleep(1 - time.time() % 1)  # the start of a second
    second = int(time.time())
    os.utime(project / "notes.txt", (second, second))  # geesefs shows whole seconds
    with monkeypatch.context() as patch:
        patch.setattr(History, "_add_all", a_large_projects_read)
        pod = a_pod(tmp_path, project)  # its open reads notes.txt in that second
    (project / "notes.txt").write_text("v9 notes\n")  # saved again in that second, the same size
    os.utime(project / "notes.txt", (second, second))
    (pod.copy / "B.md").write_text("by B")
    time.sleep(1.1)  # the landing comes later
    land(pod, "saga:b")
    assert (a_pod(tmp_path, project).copy / "notes.txt").read_text() == "v9 notes\n"


def test_a_kept_index_git_cannot_read_is_left_out_and_the_pod_opens(tmp_path, project):
    first = a_pod(tmp_path, project)
    (first.copy / "A.md").write_text("by A")
    land(first, "saga:a")
    (project / "_history" / "index").write_bytes(b"DIRC\x00\x00\x00\x02 not an index")
    pod = a_pod(tmp_path, project)
    assert sorted(p.name for p in pod.copy.iterdir()) == ["A.md", "Report.docx", "notes.txt"]


def test_a_record_found_by_its_saga_leaves_its_pod_ready_for_the_next_landing(tmp_path, project, monkeypatch):
    history = a_pod(tmp_path, project)
    (history.copy / "a.md").write_text("a")
    main = history.fetch()["main"]
    turn = history.commit_turn(author=A, trailers=[["Surogate-Saga", "saga:1"], ["Surogate-Kind", "turn"]])
    applied = [history.apply(c["path"], c["before"], c["after"]) for c in turn["changes"]]
    record = {"turn": turn["commit"], "applied": applied, "author": A, "trailers": [["Surogate-Saga", "saga:1"]], "main": main}
    put = History._put_durable

    def written_then_failed(self, name, source):
        put(self, name, source)
        if name == "packed-refs":
            raise OSError(5, "Input/output error")  # the push counts; its answer is lost

    with monkeypatch.context() as patch:
        patch.setattr(History, "_put_durable", written_then_failed)
        with pytest.raises(OSError):
            history.record(**record)
    landed = history.record(**record)  # the step's retry finds it by its saga
    assert git(project / "_history", "rev-parse", "refs/heads/main") == landed["commit"]
    (history.copy / "b.md").write_text("b")
    land(history, "saga:2")
    assert (project / "b.md").read_text() == "b"


def test_the_cap_counts_the_files_history_would_track():
    from surogates.sandbox.history import tracked

    cases = {
        "Report.docx": True, "uploads/brief.pdf": True, "server.log": True, "a/_history/x": True, "node_modules": True,
        "node_modules/x.js": False, "a/node_modules/x.js": False, "x/~$Report.docx": False, "notes.tmp": False,
        "a.tmp/b.docx": False, "_history/packed-refs": False, ".env": False, "conf/.env.prod": False,
        "proj/.git/HEAD": False, "Thumbs.db": False, "d/._x": False, ".threads/k/r": False, "coverage/a.docx": False,
    }
    assert {path: tracked(path) for path in cases} == cases


#: A day and more after the landings: no pod alive then opened on any of them.
LATER = THREAD_POD_DEADLINE + 3600


def in_history(durable: Path, commit: str) -> bool:
    return subprocess.run(["git", f"--git-dir={durable}", "cat-file", "-e", commit]).returncode == 0


def test_pruning_keeps_the_window_and_cuts_at_the_size_rule_and_kept_ids_still_open(tmp_path, project):
    live, gone = a_pod(tmp_path, project, "live"), a_pod(tmp_path, project, "gone")
    for thread in (live, gone):
        (thread.copy / f"{thread.thread}.md").write_text("unlanded")
        thread.keep(author=A, trailers=KEPT, base=True)
    landings = []
    for n in range(40):
        history = a_pod(tmp_path, project)
        (history.copy / "Budget.xlsx").write_bytes(os.urandom(50_000))  # an office file: no delta between versions
        landings.append(land(history, f"saga:{n}")["commit"])
    durable = project / "_history"
    # The days pass in what the pruning is told: its packs are seconds old, so it is told to spare none.
    out = a_pod(tmp_path, project).prune(keep=["refs/heads/threads/live", "refs/bases/live"], now=time.time() + LATER, spare=0)
    # All forty are inside 90 days, but forty versions are more than twice the files: cut, never below twenty.
    assert (out["pruned"], out["commits"]) == (True, 20)
    assert git(durable, "rev-list", "--first-parent", "--count", "refs/heads/main") == "20"
    # The commits that stay keep their ids, and their files still open.
    assert git(durable, "rev-parse", "refs/heads/main") == landings[-1]
    assert git(durable, "cat-file", "-s", f"{landings[-20]}:Budget.xlsx") == "50000"
    assert not in_history(durable, landings[0])
    # A live thread's branch stays, with its base though it is older; an ended one's goes.
    refs = git(durable, "for-each-ref", "--format=%(refname)").splitlines()
    assert {"refs/heads/threads/live", "refs/bases/live"} <= set(refs) and "refs/heads/threads/gone" not in refs
    assert len(list((durable / "objects" / "pack").glob("*.pack"))) == 1
    assert git(durable, "fsck", "--no-dangling") == ""
    # Pruned at most once a day; and pods go on fetching and landing.
    assert a_pod(tmp_path, project).prune(keep=[], now=time.time()) == {"pruned": False}
    pod = a_pod(tmp_path, project, "live")
    assert (pod.copy / "live.md").read_text() == "unlanded"
    land(pod, "saga:after")
    # Work older than the window, on a base the cut went below, lands all the same.
    assert (project / "live.md").read_text() == "unlanded"
    assert git(durable, "fsck", "--no-dangling") == ""


def test_pruning_keeps_ninety_days_when_they_are_small(tmp_path, project):
    (project / "Annual report.pdf").write_bytes(os.urandom(2_000_000))  # the project is large, its edits small
    for n in range(25):
        history = a_pod(tmp_path, project)
        (history.copy / "notes.txt").write_text(f"v{n}\n")
        land(history, f"saga:{n}")
    durable = project / "_history"
    out = a_pod(tmp_path, project).prune(keep=[], now=time.time())
    assert out["commits"] == 26  # every landing, and the project's first commit
    later = a_pod(tmp_path, project).prune(keep=[], now=time.time() + 100 * 86_400)
    assert (later["pruned"], later["commits"]) == (True, 20)
    assert git(durable, "rev-list", "--first-parent", "--count", "refs/heads/main") == "20"


def test_a_pod_open_across_a_pruning_lands_and_the_history_stays_whole(tmp_path, project):
    durable = project / "_history"
    first = a_pod(tmp_path, project, "early")
    (first.copy / "start.md").write_text("x")
    land(first, "saga:start")
    slow = a_pod(tmp_path, project, "slow")  # a long turn: its pod opened on main as it was then
    (slow.copy / "slow.md").write_text("the slow thread's work")
    for n in range(25):  # other threads land meanwhile
        other = a_pod(tmp_path, project, f"o{n}")
        (other.copy / "Budget.xlsx").write_bytes(os.urandom(50_000))
        land(other, f"saga:o{n}", author={"name": f"O{n}", "email": f"thread:o{n}@surogate"})
    base = git(slow.repo, "rev-parse", "refs/bases/slow")
    # While a pod opened on them may be alive, main's commits stay.
    assert a_pod(tmp_path, project, "p1").prune(keep=[], now=time.time(), spare=0)["commits"] == 27
    assert in_history(durable, base)
    # Past that, a pruning cuts the slow pod's base; the pod, left alive, still lands.
    assert a_pod(tmp_path, project, "p2").prune(keep=[], now=time.time() + LATER + 86_400, spare=0)["commits"] == 20
    assert not in_history(durable, base)
    land(slow, "saga:slow")
    assert (project / "slow.md").read_text() == "the slow thread's work"
    # Its base joined the history's cut: the history reads whole, and prunes again.
    assert git(durable, "fsck", "--no-dangling") == ""
    assert a_pod(tmp_path, project, "p3").prune(keep=[], now=time.time() + LATER + 3 * 86_400, spare=0)["pruned"] is True
    assert git(durable, "fsck", "--no-dangling") == ""


def test_a_pruning_cut_off_waits_a_day_and_writes_cut_off_are_swept(tmp_path, project, monkeypatch):
    history = a_pod(tmp_path, project)
    (history.copy / "a.md").write_text("a")
    land(history, "saga:1")
    durable = project / "_history"
    left = [durable / ".~dead.landing~", durable / "objects" / "pack" / ".~dead.landing~"]
    for path in left:
        path.write_bytes(b"half a pack")

    def cut_off(*_, **__):
        raise TimeoutError("killed at its bound")

    monkeypatch.setattr(History, "_cut", cut_off)
    with pytest.raises(TimeoutError):
        a_pod(tmp_path, project).prune(keep=[], now=time.time())
    # Swept as it began; and marked before it pruned: not tried again the same day.
    assert not any(path.exists() for path in left)
    assert a_pod(tmp_path, project).prune(keep=[], now=time.time()) == {"pruned": False}
    # A push sweeps them too.
    for path in left:
        path.write_bytes(b"half a pack")
    pod = a_pod(tmp_path, project)
    (pod.copy / "b.md").write_text("b")
    land(pod, "saga:2")
    assert not any(path.exists() for path in left)


def test_a_pruning_whose_lock_was_lost_leaves_the_landing_made_meanwhile(tmp_path, project, monkeypatch):
    first = a_pod(tmp_path, project)
    (first.copy / "a.md").write_text("a")
    land(first, "saga:1")
    other = a_pod(tmp_path, project, "t2")
    (other.copy / "b.md").write_text("b")
    cut, landed = History._cut, []

    def another_lands_meanwhile(self, *args, **kwargs):
        packed = cut(self, *args, **kwargs)
        # A failover freed the lock: its next holder lands while the pruning runs.
        landed.append(land(other, "saga:2", author={"name": "Draft B", "email": "thread:t2@surogate"})["commit"])
        return packed

    monkeypatch.setattr(History, "_cut", another_lands_meanwhile)
    with pytest.raises(HistoryConflict, match="moved while it was pruned"):
        a_pod(tmp_path, project).prune(keep=[], now=time.time())
    durable = project / "_history"
    assert git(durable, "rev-parse", "refs/heads/main") == landed[0]
    assert git(durable, "fsck", "--no-dangling") == ""


def test_a_keep_whose_history_moved_while_its_pack_went_up_writes_over_no_landing(tmp_path, project, monkeypatch):
    first = a_pod(tmp_path, project)
    (first.copy / "a.md").write_text("a")
    land(first, "saga:1")
    keeper, other = a_pod(tmp_path, project, "t2"), a_pod(tmp_path, project, "t3")
    (keeper.copy / "kept.md").write_text("kept")
    (other.copy / "b.md").write_text("b")
    put, landed = History._put_durable, []

    def another_lands_meanwhile(self, name, source):
        put(self, name, source)
        if self.thread == "t2" and name.endswith(".idx") and not landed:
            # A lock lost unseen, or a keep that did not wait out a landing still at work: it records now,
            # between this push's first look at the refs and its write of them.
            landed.append(land(other, "saga:2", author={"name": "Draft C", "email": "thread:t3@surogate"})["commit"])

    monkeypatch.setattr(History, "_put_durable", another_lands_meanwhile)
    with pytest.raises(HistoryConflict, match="moved while it was pushed"):
        keeper.keep(author=A, trailers=KEPT, base=True)
    durable = project / "_history"
    # The landing's refs stand: the keep wrote none of its own over them.
    assert git(durable, "rev-parse", "refs/heads/main") == git(durable, "rev-parse", "refs/heads/threads/t3") == landed[0]
    assert (project / "b.md").read_text() == "b"
    # Tried again, it goes in beside the landing.
    keeper.keep(author=A, trailers=KEPT, base=True)
    assert git(durable, "rev-parse", "refs/heads/main") == landed[0]
    assert git(durable, "show", "refs/heads/threads/t2:kept.md") == "kept"
    assert git(durable, "fsck", "--no-dangling") == ""


def test_a_pruning_leaves_the_packs_younger_than_the_fence_so_a_push_it_ran_under_names_no_commit_it_took(tmp_path, project, monkeypatch):
    for n in range(3):
        seed = a_pod(tmp_path, project, "t9")
        (seed.copy / f"seed{n}.md").write_text("seed")
        land(seed, f"saga:{n}")
    durable = project / "_history"
    old = {pack.name for pack in (durable / "objects" / "pack").iterdir()}
    for name in old:  # written long ago: no push still at work wrote them
        os.utime(durable / "objects" / "pack" / name, (time.time() - 3600, time.time() - 3600))
    pusher, pruner = a_pod(tmp_path, project, "t1"), a_pod(tmp_path, project, "t2")
    (pusher.copy / "a.md").write_text("a turn")
    put, pruned = History._put_durable, []
    keep = [f"refs/{kind}/{thread}" for thread in ("t1", "t2", "t9") for kind in ("heads/threads", "bases")]

    def a_pruning_runs_whole(self, name, source):
        put(self, name, source)
        if self.thread == "t1" and name.endswith(".idx") and not pruned:
            # This push lost its lock unseen: its pack is up, its refs are not, and another holder prunes.
            # Asked as a caller that names no fence asks, a day on by its own count: the pod's clock decides.
            pruned.append(pruner.prune(keep=keep, now=time.time() + 90_000))

    monkeypatch.setattr(History, "_put_durable", a_pruning_runs_whole)
    turn = pusher.commit_turn(author=A, trailers=[["Surogate-Saga", "saga:a"], ["Surogate-Kind", "turn"]])
    monkeypatch.setattr(History, "_put_durable", put)
    assert pruned[0]["pruned"] is True
    # The pruning moved no ref, so the push passed its last look: the commit its ref names is still there.
    assert git(durable, "rev-parse", "refs/heads/threads/t1") == turn["commit"] and in_history(durable, turn["commit"])
    # What was older than the fence went into the pruning's one pack; the push's pack stayed beside it.
    packs = {pack.name for pack in (durable / "objects" / "pack").iterdir()}
    assert not old & packs and len([name for name in packs if name.endswith(".pack")]) == 2
    assert git(durable, "fsck", "--no-dangling") == ""
    assert (a_pod(tmp_path, project, "t1").copy / "a.md").read_text() == "a turn"  # and the thread's next pod opens


def a_main_whose_parent_the_history_lacks(durable: Path) -> str:
    """``main`` made a commit whose parent the history lacks, as after a pruning or as a command can make it; in a pack."""
    main = git(durable, "rev-parse", "refs/heads/main")
    body = (
        f"tree {git(durable, 'rev-parse', f'{main}^{{tree}}')}\nparent {'1' * 40}\n"
        "author X <x@x> 1700000000 +0000\ncommitter X <x@x> 1700000000 +0000\n\ncut\n"
    )
    crafted = git(durable, "hash-object", "-t", "commit", "--literally", "-w", "--stdin", input=body)
    (durable / "packed-refs").write_text(f"# pack-refs with: peeled fully-peeled sorted \n{crafted} refs/heads/main\n")
    (durable / "shallow").write_text(f"{crafted}\n")
    git(durable, "repack", "-q", "-d")  # pods read only packs
    return crafted


def test_a_history_whose_config_a_command_wrote_runs_nothing_at_a_push_or_a_pruning_and_refuses_an_open(tmp_path, project):
    first = a_pod(tmp_path, project, "early")
    (first.copy / "start.md").write_text("x")
    land(first, "saga:start")
    durable, ran = project / "_history", tmp_path / "ran"
    a_main_whose_parent_the_history_lacks(durable)
    slow, pruner = a_pod(tmp_path, project, "slow"), a_pod(tmp_path, project, "t2")
    other = a_pod(tmp_path, project, "o1")
    (other.copy / "o.md").write_text("o")
    land(other, "saga:o1", author={"name": "O", "email": "thread:o1@surogate"})
    # A partial clone's config: a git in this repository that misses an object fetches it with this command.
    (durable / "config").write_text(
        "[core]\n\trepositoryformatversion = 1\n\tbare = true\n[extensions]\n\tpartialClone = evil\n"
        f"[remote \"evil\"]\n\turl = {durable}\n\tpromisor = true\n\tuploadpack = touch {ran}; false\n"
    )
    # A push whose boundary's parent the history lacks looks for it there.  A pod opened before
    # the config was written never reads it: its landing and its pruning go on, and run nothing.
    (slow.copy / "slow.md").write_text("the slow thread's work")
    land(slow, "saga:slow")
    assert pruner.prune(keep=[], now=time.time())["pruned"] is True
    assert (project / "slow.md").read_text() == "the slow thread's work"
    # A pod opened after it is refused: the config is not the platform's own, whatever git would make of it.
    with pytest.raises(HistoryError, match="refused the project's history: its config is not the platform's own"):
        a_pod(tmp_path, project, "t3")
    assert not ran.exists()


def test_git_never_runs_in_the_buckets_history(tmp_path, project, monkeypatch):
    first = a_pod(tmp_path, project, "early")
    (first.copy / "a.md").write_text("a")
    land(first, "saga:1")
    durable, ran = project / "_history", tmp_path / "ran"
    # A main whose parent the history lacks, and a landing after a pod opened on it: that pod's
    # push then has to ask whether the history holds its boundary's parent.
    a_main_whose_parent_the_history_lacks(durable)
    slow = a_pod(tmp_path, project, "slow")
    other = a_pod(tmp_path, project, "o1")
    (other.copy / "o.md").write_text("o")
    land(other, "saga:o1", author={"name": "O", "email": "thread:o1@surogate"})
    # What a thread's commands can write there: hooks, other object stores for git to read, marks beside a pack.
    for hook in ("reference-transaction", "post-checkout", "pre-auto-gc", "post-index-change"):
        (durable / "hooks").mkdir(exist_ok=True)
        (durable / "hooks" / hook).write_text(f"#!/bin/sh\ntouch {ran}-{hook}\n")
        (durable / "hooks" / hook).chmod(0o755)
    (durable / "objects" / "info").mkdir(exist_ok=True)
    (durable / "objects" / "info" / "alternates").write_text(f"{tmp_path / 'another' / 'objects'}\n")
    for pack in list((durable / "objects" / "pack").glob("*.pack")):
        pack.with_suffix(".promisor").write_text("")
        pack.with_suffix(".keep").write_text("")
    (durable / "objects" / "pack" / "multi-pack-index").write_bytes(b"MIDX not one")
    run, gits = subprocess.run, []

    def recorded(args, **kwargs):
        gits.append((args, (kwargs.get("env") or {}).get("GIT_DIR"), kwargs.get("cwd")))
        return run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recorded)
    pod = a_pod(tmp_path, project, "t2")
    (pod.copy / "b.md").write_text("b")
    land(pod, "saga:2")
    (slow.copy / "slow.md").write_text("slow")
    land(slow, "saga:slow")  # across the missing parent
    a_pod(tmp_path, project, "t3").prune(keep=[], now=time.time())
    # Neither its repository, nor its folder, nor a remote: the pod copies the history's files and reads them as data.
    there = [args for args, repo, cwd in gits if any(str(durable) in str(v) for v in (*args, repo, cwd))]
    assert there == [] and not list(tmp_path.glob("ran*"))
    assert (project / "b.md").read_text() == "b" and (project / "slow.md").read_text() == "slow"
    # And only its packs, its refs and its cut: the copy's HEAD and config are the pod's own, and nothing
    # else of the bucket's comes with them, to lead git to another store or have it run a command.
    taken = pod.repo / "durable.git"
    files = sorted(str(f.relative_to(taken)) for f in taken.rglob("*") if f.is_file() and "hooks" not in f.parts)
    assert [f for f in files if not f.startswith("objects/pack/pack-")] == [
        "HEAD", "config", "description", "info/exclude", "packed-refs", "shallow",
    ]
    assert {Path(f).suffix for f in files if f.startswith("objects/pack/")} == {".pack", ".idx"}
    assert not [hook for hook in ("reference-transaction", "post-checkout", "pre-auto-gc", "post-index-change") if (taken / "hooks" / hook).exists()]


@pytest.mark.parametrize("name", ["packed-refs", "HEAD", "shallow", "index", "a pack"])
def test_a_history_file_made_a_link_is_refused_and_the_refusal_quotes_nothing(tmp_path, project, name):
    first = a_pod(tmp_path, project)
    (first.copy / "a.md").write_text("a")
    land(first, "saga:1")
    durable = project / "_history"
    # A file of the pod's own, which a link in the bucket would have it read.
    secret = tmp_path / "token"
    secret.write_text("eyJhbGciOiJSUzI1NiJ9.a-pod-token\n")
    link = durable / "objects" / "pack" / f"pack-{'2' * 40}.pack" if name == "a pack" else durable / name
    link.unlink(missing_ok=True)
    link.symlink_to(secret)
    with pytest.raises(HistoryError, match="refused the project's history") as refused:
        a_pod(tmp_path, project, "t2")
    assert "eyJ" not in str(refused.value) and "token" not in str(refused.value)


@pytest.mark.parametrize("folder", ["_history", "_history/objects", "_history/objects/pack"])
def test_a_history_folder_made_a_link_is_refused_at_every_step_and_nothing_is_written_through_it(tmp_path, project, folder):
    first = a_pod(tmp_path, project)
    (first.copy / "a.md").write_text("a")
    land(first, "saga:1")
    holder = a_pod(tmp_path, project, "t3")  # a pod already open, as the next lock holder's is
    (holder.copy / "h.md").write_text("h")
    # A thread's commands can make the folder a link: here to a copy of itself, a history as good as the real one.
    elsewhere = tmp_path / "elsewhere"
    shutil.copytree(project / folder, elsewhere)
    shutil.rmtree(project / folder)
    (project / folder).symlink_to(elsewhere)
    there = sorted(str(f.relative_to(elsewhere)) for f in elsewhere.rglob("*"))
    steps = {
        "an open": lambda: a_pod(tmp_path, project, "t2"),
        "a landing's first look": holder.fetch,
        "a commit step": lambda: holder.commit_turn(author=A, trailers=[["Surogate-Saga", "saga:2"], ["Surogate-Kind", "turn"]]),
        "a keep": lambda: holder.keep(author=A, trailers=KEPT, base=True),
        "a hand-off": lambda: holder.hand_off(author=A, trailers=KEPT),
        "a pruning": lambda: holder.prune(keep=[], now=time.time() + 2 * 86_400),
    }
    for name, step in steps.items():
        with pytest.raises(HistoryError, match="refused the project's history: a folder of it is a link"):
            step()
        # Refused before anything went through the link: no mark of a pruning, no pack, no file half written.
        assert sorted(str(f.relative_to(elsewhere)) for f in elsewhere.rglob("*")) == there, name


@pytest.mark.parametrize("made", ["a pipe", "a folder"])
def test_a_history_file_that_is_no_file_is_refused_and_never_read(tmp_path, project, made):
    first = a_pod(tmp_path, project)
    (first.copy / "a.md").write_text("a")
    land(first, "saga:1")
    refs = project / "_history" / "packed-refs"
    refs.unlink()
    os.mkfifo(refs) if made == "a pipe" else refs.mkdir()  # a pipe would hold a read for ever; a folder is no history
    with pytest.raises(HistoryError, match="refused the project's history: its packed-refs is not a file"):
        a_pod(tmp_path, project, "t2")


def test_a_commit_whose_parent_is_no_commit_id_is_refused_before_git_sees_it(tmp_path, project):
    first = a_pod(tmp_path, project, "early")
    (first.copy / "start.md").write_text("x")
    land(first, "saga:start")
    durable = project / "_history"
    main = git(durable, "rev-parse", "refs/heads/main")
    # An id in capitals: git reads it as a parent, and it is not one the platform writes.
    body = (
        f"tree {git(durable, 'rev-parse', f'{main}^{{tree}}')}\nparent {main.upper()}\n"
        "author X <x@x> 1700000000 +0000\ncommitter X <x@x> 1700000000 +0000\n\nc\n"
    )
    crafted = git(durable, "hash-object", "-t", "commit", "--literally", "-w", "--stdin", input=body)
    (durable / "packed-refs").write_text(f"# pack-refs with: peeled fully-peeled sorted \n{crafted} refs/heads/main\n")
    git(durable, "repack", "-q", "-d")
    slow = a_pod(tmp_path, project, "slow")  # opens on the crafted main, its depth-1 boundary
    other = a_pod(tmp_path, project, "o1")
    (other.copy / "o.md").write_text("o")
    land(other, "saga:o1", author={"name": "O", "email": "thread:o1@surogate"})
    (slow.copy / "slow.md").write_text("slow")
    with pytest.raises(HistoryError, match="refused the project's history: a commit holds what is not a commit id"):
        slow.commit_turn(author=A, trailers=[["Surogate-Saga", "saga:slow"], ["Surogate-Kind", "turn"]])


def test_a_crafted_commit_puts_nothing_of_its_own_on_gits_command_line(tmp_path, project, monkeypatch):
    first = a_pod(tmp_path, project, "early")
    (first.copy / "start.md").write_text("x")
    land(first, "saga:start")
    durable = project / "_history"
    main = git(durable, "rev-parse", "refs/heads/main")
    # A parent line after the committer: no parent to git, but a line a careless read takes for one.
    body = (
        f"tree {git(durable, 'rev-parse', f'{main}^{{tree}}')}\nparent {main}\n"
        "author X <x@x> 1700000000 +0000\ncommitter X <x@x> 1700000000 +0000\nparent --upload-pack=touch${IFS}ran\n\nc\n"
    )
    crafted = git(durable, "hash-object", "-t", "commit", "--literally", "-w", "--stdin", input=body)
    (durable / "packed-refs").write_text(f"# pack-refs with: peeled fully-peeled sorted \n{crafted} refs/heads/main\n")
    git(durable, "repack", "-q", "-d")
    slow = a_pod(tmp_path, project, "slow")  # opens on the crafted main, its depth-1 boundary
    other = a_pod(tmp_path, project, "o1")
    (other.copy / "o.md").write_text("o")
    land(other, "saga:o1", author={"name": "O", "email": "thread:o1@surogate"})
    run, argv = subprocess.run, []
    monkeypatch.setattr(subprocess, "run", lambda args, **kwargs: (argv.extend(args), run(args, **kwargs))[1])
    (slow.copy / "slow.md").write_text("slow")
    slow.commit_turn(author=A, trailers=[["Surogate-Saga", "saga:slow"], ["Surogate-Kind", "turn"]])
    # Its boundary's parents are read to push it: only the ones git reads, each a commit id.
    assert not [a for a in argv if "upload-pack" in a]


def a_helper(tmp_path, project, helper="h1"):
    """A helper's pod: its own copy, of its thread t1's hand-off."""
    return a_pod(tmp_path, project, "t1", helper=helper)


def test_a_helpers_work_reaches_its_threads_copy_through_the_hand_off(tmp_path, project):
    thread = a_pod(tmp_path, project)
    (thread.copy / "outline.md").write_text("the thread's outline")
    thread.hand_off(author=A, trailers=KEPT)
    # The hand-off moves no branch: a turn stopped now lands none of it.
    assert subprocess.run(["git", f"--git-dir={project / '_history'}", "rev-parse", "-q", "--verify", "refs/heads/threads/t1"],
                          capture_output=True).returncode != 0
    helper = a_helper(tmp_path, project)
    assert (helper.copy / "outline.md").read_text() == "the thread's outline"
    (helper.copy / "notes.txt").write_text("v1 notes, sourced by the helper\n")
    (helper.copy / "sources.md").write_text("sources")
    assert helper.hand_back(author=A, trailers=KEPT)["not_kept"] == []

    assert thread.take_up() == {"not_taken": []}
    assert (thread.copy / "sources.md").read_text() == "sources"
    assert (thread.copy / "notes.txt").read_text() == "v1 notes, sourced by the helper\n"
    land(thread)
    assert (project / "sources.md").read_text() == "sources"
    # The thread's next pod has it all, and takes up nothing twice.
    pod = a_pod(tmp_path, project)
    assert pod.take_up() == {"not_taken": []} and (pod.copy / "sources.md").read_text() == "sources"


def test_two_helpers_that_change_one_file_keep_the_first_and_name_it_to_the_second(tmp_path, project):
    thread = a_pod(tmp_path, project)
    thread.hand_off(author=A, trailers=KEPT)
    first, second = a_helper(tmp_path, project, "h1"), a_helper(tmp_path, project, "h2")
    (first.copy / "notes.txt").write_text("by the first\n")
    (second.copy / "notes.txt").write_text("by the second\n")
    (second.copy / "second.md").write_text("the second's own")
    first.hand_back(author=A, trailers=KEPT)
    assert second.hand_back(author=A, trailers=KEPT)["not_kept"] == ["notes.txt"]
    thread.take_up()
    assert (thread.copy / "notes.txt").read_text() == "by the first\n"
    assert (thread.copy / "second.md").read_text() == "the second's own"


def test_a_thread_keeps_its_own_version_of_a_file_a_helper_also_changed(tmp_path, project):
    thread = a_pod(tmp_path, project)
    thread.hand_off(author=A, trailers=KEPT)
    helper = a_helper(tmp_path, project)
    (helper.copy / "notes.txt").write_text("by the helper\n")
    (helper.copy / "Report.docx").unlink()
    (helper.copy / "Report.docx").mkdir()  # a folder where the thread keeps its file
    (helper.copy / "Report.docx" / "x.md").write_text("x")
    helper.hand_back(author=A, trailers=KEPT)
    (thread.copy / "notes.txt").write_text("by the thread meanwhile\n")
    (thread.copy / "Report.docx").write_bytes(b"PK\x03\x04 report by the thread")
    assert thread.take_up() == {"not_taken": ["Report.docx", "Report.docx/x.md", "notes.txt"]}
    assert (thread.copy / "notes.txt").read_text() == "by the thread meanwhile\n"
    assert (thread.copy / "Report.docx").read_bytes() == b"PK\x03\x04 report by the thread"


def test_a_helpers_file_the_thread_threw_away_stays_away_at_its_next_turn(tmp_path, project):
    thread = a_pod(tmp_path, project)
    thread.hand_off(author=A, trailers=KEPT)
    helper = a_helper(tmp_path, project)
    (helper.copy / "h.md").write_text("the helper's draft")
    helper.hand_back(author=A, trailers=KEPT)
    thread.take_up()
    (thread.copy / "h.md").unlink()
    # The turn changed nothing since its base, but what it took up is taken: the next take-up merges from there.
    assert thread.commit_turn(author=A, trailers=[["Surogate-Saga", "saga:1"], ["Surogate-Kind", "turn"]])["commit"] is None
    assert not (a_pod(tmp_path, project).copy / "h.md").exists()


def test_a_stopped_turns_hand_off_is_dropped_and_none_of_it_lands_later(tmp_path, project):
    thread = a_pod(tmp_path, project)
    (thread.copy / "draft.md").write_text("the stopped turn's draft")
    thread.hand_off(author=A, trailers=KEPT)
    assert thread.drop_hand_off() == {"dropped": True}
    refs = git(project / "_history", "for-each-ref", "--format=%(refname)").splitlines()
    assert not any("handoff" in ref or "threads/t1" in ref for ref in refs)
    pod = a_pod(tmp_path, project)
    assert not (pod.copy / "draft.md").exists()
    # A pod that handed nothing off drops nothing: the hand-off is another turn's.
    assert pod.drop_hand_off() == {"dropped": False}


def test_a_helper_starts_where_its_thread_handed_off_never_from_a_pickup_of_its_own(tmp_path, project):
    thread = a_pod(tmp_path, project, "t1")
    other = a_pod(tmp_path, project, "t2")  # another thread lands meanwhile
    (other.copy / "notes.txt").write_text("by another thread\n")
    land(other, "saga:other", author={"name": "B", "email": "thread:t2@surogate"})
    (project / "uploads" / "late.pdf").write_bytes(b"%PDF uploaded after the thread started")
    helper = a_helper(tmp_path, project)  # the thread handed nothing off: the helper starts at main, as the history has it
    assert not (helper.copy / "uploads" / "late.pdf").exists()
    (helper.copy / "h.md").write_text("the helper's work")
    helper.hand_back(author=A, trailers=KEPT)
    thread.take_up()
    turn = thread.commit_turn(author=A, trailers=[["Surogate-Saga", "saga:t1"], ["Surogate-Kind", "turn"]])
    # Only the helper's own change comes with the thread's turn: not another's landing, not your upload.
    assert [c["path"] for c in turn["changes"]] == ["h.md"]


TURN = [["Surogate-Saga", "saga:x"], ["Surogate-Kind", "turn"]]


def test_a_helper_started_after_its_threads_landing_starts_from_what_landed_and_its_untaken_files_are_named(tmp_path, project):
    thread = a_pod(tmp_path, project)
    (thread.copy / "outline.md").write_text("outline v1")
    thread.hand_off(author=A, trailers=KEPT)
    helper = a_helper(tmp_path, project)
    (helper.copy / "sources.md").write_text("sources")
    helper.hand_back(author=A, trailers=KEPT)
    thread.take_up()
    (thread.copy / "outline.md").write_text("outline v2, the thread's last word")
    land(thread, "saga:1")
    # The landing took the hand-off up: it is gone, so no later helper starts from the copy as it was handed on.
    refs = git(project / "_history", "for-each-ref", "--format=%(refname)").splitlines()
    assert not [ref for ref in refs if "handoff" in ref]
    other = a_pod(tmp_path, project, "t2")  # another thread lands after it
    (other.copy / "notes.txt").write_text("v2 notes, landed by another thread\n")
    land(other, "saga:2", author={"name": "Draft B", "email": "thread:t2@surogate"})
    run = a_helper(tmp_path, project, "h-routine")  # the thread's routine runs, long after
    assert (run.copy / "outline.md").read_text() == "outline v2, the thread's last word"
    assert (run.copy / "sources.md").read_text() == "sources"
    # It starts at its thread's branch, which is the thread's own landing: not what others landed since.
    assert (run.copy / "notes.txt").read_text() == "v1 notes\n"
    (run.copy / "outline.md").write_text("outline v2, with the routine's line")
    (run.copy / "notes.txt").write_text("v1 notes\nthe routine's line\n")
    (run.copy / "routine.md").write_text("the routine's own file")
    assert run.hand_back(author=A, trailers=KEPT)["not_kept"] == []
    pod = a_pod(tmp_path, project)  # the thread's next turn end, with no tool
    turn = pod.commit_turn(author=A, trailers=TURN)
    assert [c["path"] for c in turn["changes"]] == ["outline.md", "routine.md"]
    # Its edit to a file that changed since it started is left out, and the landing can say so.
    assert turn["not_taken"] == ["notes.txt"]


def test_a_helpers_edit_to_a_file_you_saved_since_is_not_taken_and_is_named(tmp_path, project):
    first = a_pod(tmp_path, project)
    (first.copy / "a.md").write_text("a")
    land(first, "saga:1")
    (project / "Report.docx").write_bytes(b"PK\x03\x04 report v2, saved by you")
    run = a_helper(tmp_path, project)  # no hand-off: it starts at its thread's branch, as the history has it
    assert (run.copy / "Report.docx").read_bytes() == b"PK\x03\x04 report v1"  # your save is not in the history
    (run.copy / "Report.docx").write_bytes(b"PK\x03\x04 report v1 + today's numbers")
    assert run.hand_back(author=A, trailers=KEPT)["not_kept"] == []
    pod = a_pod(tmp_path, project)  # its open takes the helper's work up: your version stays
    turn = pod.commit_turn(author=A, trailers=TURN)
    assert (turn["commit"], turn["changes"], turn["overlapped"]) == (None, [], [])
    # Nothing lands, and the helper's edit is not silently gone: the commit step names it.
    assert turn["not_taken"] == ["Report.docx"]
    assert (project / "Report.docx").read_bytes() == b"PK\x03\x04 report v2, saved by you"
    # Taken up and left out once: a later turn of the thread names it no more.
    assert a_pod(tmp_path, project).commit_turn(author=A, trailers=TURN)["not_taken"] == []


def test_a_failed_turns_keep_drops_the_hand_off_its_branch_has_taken_up(tmp_path, project):
    thread = a_pod(tmp_path, project)
    (thread.copy / "outline.md").write_text("outline")
    thread.hand_off(author=A, trailers=KEPT)
    helper, late = a_helper(tmp_path, project, "h1"), a_helper(tmp_path, project, "h2")
    (helper.copy / "sources.md").write_text("sources")
    helper.hand_back(author=A, trailers=KEPT)
    thread.take_up()
    thread.keep(author=A, trailers=KEPT, base=True)  # its turn failed: the copy, with what it took up, is on its branch
    durable = project / "_history"
    assert not [ref for ref in git(durable, "for-each-ref", "--format=%(refname)").splitlines() if "handoff" in ref]
    assert (a_helper(tmp_path, project, "h3").copy / "sources.md").read_text() == "sources"  # from the branch
    # A helper still at work hands back onto no hand-off: what it changed since it started comes with the next turn.
    (late.copy / "late.md").write_text("late")
    late.hand_back(author=A, trailers=KEPT)
    pod = a_pod(tmp_path, project)
    assert sorted(p.name for p in pod.copy.iterdir()) == ["Report.docx", "late.md", "notes.txt", "outline.md", "sources.md"]
    assert pod.commit_turn(author=A, trailers=TURN)["not_taken"] == []


def packed(durable: Path) -> int:
    """The bytes of the history's packs."""
    return sum(p.stat().st_size for p in (durable / "objects" / "pack").glob("*.pack"))


def parents(repo: Path, commit: str) -> list[str]:
    return git(repo, "log", "-1", "--format=%P", commit).split()


def test_a_turn_reaches_the_history_as_one_commit_on_its_base_whatever_its_steps(tmp_path, project):
    first = a_pod(tmp_path, project)
    (first.copy / "Budget.xlsx").write_bytes(os.urandom(300_000))  # an office file: no delta between versions
    one = land(first, "saga:0")["commit"]
    durable = project / "_history"
    before = packed(durable)
    pod = a_pod(tmp_path, project)
    steps = []
    for step in range(10):  # ten steps, each snapshotted first as the harness does, each rewriting the workbook
        steps.append(pod.snapshot(f"before step {step}"))
        (pod.copy / "Budget.xlsx").write_bytes(os.urandom(300_000))
    turn = pod.commit_turn(author=A, trailers=[["Surogate-Saga", "saga:1"], ["Surogate-Kind", "turn"]])
    # One version of the workbook went up, not ten.
    assert 300_000 <= packed(durable) - before < 400_000
    # The turn in the history is one commit, the turn's files on its base, and says whose landing it is.
    assert git(durable, "rev-parse", "refs/heads/threads/t1") == turn["commit"]
    assert parents(durable, turn["commit"]) == [turn["base"]] == [one]
    assert git(durable, "log", "-1", "--format=%s|%an|%(trailers:key=Surogate-Saga,valueonly)", turn["commit"]) == "Turn|Draft A|saga:1"
    # The steps' snapshots stay in the pod, where a stop restores from them; its own branch keeps them.
    assert not any(in_history(durable, step) for step in steps[1:])  # the first found nothing to snapshot: it is the base
    assert git(pod.repo, "rev-list", "--count", "refs/heads/threads/t1", f"^{one}") == "10"  # nine snapshots, and the turn's end
    assert git(pod.repo, "rev-parse", "refs/synced/t1") == turn["commit"]
    pod.restore(steps[1])
    assert (pod.copy / "Budget.xlsx").stat().st_size == 300_000
    assert git(durable, "fsck", "--no-dangling") == ""


def test_a_fresh_pods_open_copies_one_version_of_a_file_for_each_landing_the_history_keeps(tmp_path, project):
    size, landings = 200_000, 6
    for n in range(landings):
        history = a_pod(tmp_path, project)
        for step in range(3):
            history.snapshot(f"before step {step}")
            (history.copy / "Budget.xlsx").write_bytes(os.urandom(size))
        land(history, f"saga:{n}")
    pod = a_pod(tmp_path, project, "fresh")
    copied = packed(pod.repo / "durable.git")
    # The open copies the whole kept history to the pod: one version a landing, never one a step.
    # It is not the project's size: a pruning, not the open, is what bounds it.
    assert landings * size <= copied < (landings + 1) * size


def test_a_commit_step_tried_again_pushes_the_commit_it_pushed_before(tmp_path, project, monkeypatch):
    pod = a_pod(tmp_path, project)
    pod.snapshot("before a step")
    (pod.copy / "a.md").write_text("a")
    trailers = [["Surogate-Saga", "saga:1"], ["Surogate-Kind", "turn"]]
    durable, put, failed = project / "_history", History._put_durable, []

    def the_refs_fail_once(self, name, source):
        if name == "packed-refs" and not failed:
            failed.append(name)
            raise OSError(5, "Input/output error")  # its pack is up; nothing names it yet
        return put(self, name, source)

    with monkeypatch.context() as patch:
        patch.setattr(History, "_put_durable", the_refs_fail_once)
        with pytest.raises(OSError):
            pod.commit_turn(author=A, trailers=trailers)
    assert not (durable / "packed-refs").exists()
    turn = pod.commit_turn(author=A, trailers=trailers)  # the step's retry, after a push that failed
    assert git(durable, "rev-parse", "refs/heads/threads/t1") == turn["commit"]
    # Its answer lost, it is tried again, seconds later: the same commit, which the row then names and the history has.
    time.sleep(1.1)
    assert pod.commit_turn(author=A, trailers=trailers)["commit"] == turn["commit"]
    # Cut off after the push and before the pod noted it: the same commit still, and no refusal.
    git(pod.repo, "update-ref", "-d", "refs/synced/t1")
    assert pod.commit_turn(author=A, trailers=trailers)["commit"] == turn["commit"]
    assert git(durable, "rev-parse", "refs/heads/threads/t1") == turn["commit"]
    assert git(pod.repo, "rev-parse", "refs/synced/t1") == turn["commit"]


def test_the_pods_own_branch_keeps_its_snapshots_until_the_record_moves_it_to_the_landing(tmp_path, project):
    pod = a_pod(tmp_path, project)
    (pod.copy / "a.md").write_text("a draft")
    step = pod.snapshot("before a step")
    (pod.copy / "a.md").write_text("a")
    main = pod.fetch()["main"]
    turn = pod.commit_turn(author=A, trailers=[["Surogate-Saga", "saga:1"], ["Surogate-Kind", "turn"]])
    # Pushed, the turn is a commit of its own; the pod's branch is still its own line of snapshots.
    own = git(pod.repo, "rev-parse", "refs/heads/threads/t1")
    assert own != turn["commit"] and git(pod.repo, "rev-parse", f"{own}~1") == step
    assert git(pod.repo, "rev-parse", f"{own}^{{tree}}") == git(pod.repo, "rev-parse", f"{turn['commit']}^{{tree}}")
    applied = [pod.apply(c["path"], c["before"], c["after"]) for c in turn["changes"]]
    landed = pod.record(turn=turn["commit"], applied=applied, author=A, trailers=[["Surogate-Saga", "saga:1"]], main=main)
    assert git(pod.repo, "rev-parse", "refs/heads/threads/t1") == landed["commit"]
    assert parents(project / "_history", landed["commit"])[1] == turn["commit"]


def test_a_kept_turn_a_hand_off_and_a_helpers_copy_each_reach_the_history_as_one_commit(tmp_path, project):
    durable = project / "_history"

    def rewritten(history: History, name: str, times: int = 4) -> None:
        for step in range(times):
            history.snapshot(f"before step {step}")
            (history.copy / name).write_bytes(os.urandom(100_000))

    thread = a_pod(tmp_path, project)
    base = git(thread.repo, "rev-parse", "refs/bases/t1")
    rewritten(thread, "Draft.docx")
    handed = thread.hand_off(author=A, trailers=KEPT)["commit"]
    assert git(durable, "rev-parse", "refs/handoff/t1") == handed and parents(durable, handed) == [base]
    helper, failing = a_helper(tmp_path, project, "h1"), a_helper(tmp_path, project, "h2")
    rewritten(helper, "Sources.docx")
    kept = helper.hand_back(author=A, trailers=KEPT)["commit"]
    # A helper's copy is one commit on where it started.
    assert git(durable, "rev-parse", "refs/handoff/t1") == kept and parents(durable, kept) == [handed]
    rewritten(failing, "Half.docx")
    apart = failing.keep_apart(author=A, trailers=KEPT)
    assert git(durable, "rev-parse", "refs/helpers/t1/h2") == apart["commit"] and parents(durable, apart["commit"]) == [handed]
    # A failed turn's keep: the copy, with what it took up, on its base; the hand-off it took up its second parent.
    thread.take_up()
    rewritten(thread, "Draft.docx")
    turn = thread.keep(author=A, trailers=KEPT, base=True)["commit"]
    assert git(durable, "rev-parse", "refs/heads/threads/t1") == turn and parents(durable, turn) == [base, kept]
    # Four pushes of files rewritten four times each: four versions went up, and the first commit's files.
    assert packed(durable) < 5 * 100_000
    assert git(durable, "fsck", "--no-dangling") == ""
    # Asked again, each answers the commit it pushed.
    assert thread.keep(author=A, trailers=KEPT, base=True)["commit"] == turn
    assert helper.hand_back(author=A, trailers=KEPT)["commit"] == kept


def test_a_helpers_version_the_thread_did_not_take_stays_in_the_history_with_the_landing(tmp_path, project):
    durable = project / "_history"
    thread = a_pod(tmp_path, project)
    thread.hand_off(author=A, trailers=KEPT)
    helper = a_helper(tmp_path, project)
    (helper.copy / "notes.txt").write_text("the helper's notes\n")
    theirs = git(helper.repo, "hash-object", str(helper.copy / "notes.txt"))
    handed_back = helper.hand_back(author=A, trailers=KEPT)["commit"]
    (thread.copy / "notes.txt").write_text("the thread's notes\n")
    # The landing does not finish: the turn waits on its branch, the hand-off it took up behind it.
    turn = thread.commit_turn(author=A, trailers=[["Surogate-Saga", "saga:1"], ["Surogate-Kind", "turn"]])
    assert turn["not_taken"] == ["notes.txt"] and parents(durable, turn["commit"]) == [turn["base"], handed_back]
    # The thread's next turn, in a new pod, lands it: no hand-off is left to take up, and the helper's version is still named.
    pod = a_pod(tmp_path, project)
    (pod.copy / "more.md").write_text("more")
    landed = land(pod, "saga:2")["commit"]
    assert not [ref for ref in git(durable, "for-each-ref", "--format=%(refname)").splitlines() if "handoff" in ref]
    assert parents(durable, f"{landed}^2") == [turn["base"], handed_back]
    assert theirs in git(durable, "rev-list", "--objects", "refs/heads/main").split()
    assert git(durable, "show", f"{handed_back}:notes.txt") == "the helper's notes"
    # What reads the history is as it was: the thread's version is the landing's second parent's,
    # that commit says whose landing it is, and main's own line is its landings alone.
    assert git(durable, "show", f"{landed}^2:notes.txt") == "the thread's notes"
    assert git(durable, "log", "-1", "--format=%(trailers:key=Surogate-Saga,valueonly)", f"{landed}^2") == "saga:2"
    assert git(durable, "log", "--first-parent", "--format=%s", "refs/heads/main").splitlines() == ["Landing", "The project's files"]
    assert git(durable, "fsck", "--no-dangling") == ""
    # And a pruning keeps it with the landing.
    assert a_pod(tmp_path, project, "t2").prune(keep=[], now=time.time())["pruned"] is True
    assert git(durable, "show", f"{handed_back}:notes.txt") == "the helper's notes"
    assert git(durable, "fsck", "--no-dangling") == ""


def test_a_pushed_copy_names_eight_parents_at_most_the_older_hand_offs_folded_behind_one(tmp_path, project):
    durable = project / "_history"
    other = a_pod(tmp_path, project, "t0")
    (other.copy / "other.md").write_text("another thread's")
    land(other, "saga:0")
    seed = a_pod(tmp_path, project)
    (seed.copy / "seed.md").write_text("seed")
    base = git(seed.repo, "rev-parse", "refs/bases/t1")
    seed.keep(author=A, trailers=KEPT, base=True)
    handed, kept = [], ""
    # Twelve helpers start where the thread is now, so none of their hand-backs has another's behind it.
    helpers = [a_helper(tmp_path, project, f"h{n}") for n in range(12)]
    for n, helper in enumerate(helpers):
        # A helper keeps its work, and the thread's turn takes it up at its open, then fails: kept, unlanded.
        (helper.copy / f"h{n}.md").write_text(f"helper {n}")
        handed.append(helper.hand_back(author=A, trailers=KEPT)["commit"])
        pod = a_pod(tmp_path, project)
        (pod.copy / f"turn{n}.md").write_text(str(n))
        kept = pod.keep(author=A, trailers=KEPT, base=True)["commit"]
        named = parents(durable, kept)
        if n < 7:
            # Its base, and each hand-off it has taken up, the last first.
            assert named == [base, *reversed(handed)]
        else:
            # Past eight, the six it last took up stay, and one commit behind them names the older ones.
            assert len(named) == 8 and named[:7] == [base, *list(reversed(handed))[:6]]
            assert len(parents(durable, named[7])) <= 8
        # A second try pushes nothing new: the same commit, fold and all.
        assert pod.keep(author=A, trailers=KEPT, base=True)["commit"] == kept
        assert a_pod(tmp_path, project).keep(author=A, trailers=KEPT, base=True)["commit"] == kept
    # Nothing a hand-off held is let go: each is behind the kept commit still, through the fold.
    assert set(handed) <= set(git(durable, "rev-list", kept).split())
    assert a_pod(tmp_path, project, "t2").prune(
        keep=["refs/heads/threads/t1", "refs/bases/t1"], now=time.time(),
    )["pruned"] is True
    assert [git(durable, "show", f"{commit}:h{n}.md") for n, commit in enumerate(handed)] == [f"helper {n}" for n in range(12)]
    # And the landing keeps them with the turn, its second parent, bounded the same.
    last = a_pod(tmp_path, project)
    (last.copy / "done.md").write_text("done")
    landed = land(last, "saga:9")["commit"]
    assert len(parents(durable, f"{landed}^2")) == 8
    assert set(handed) <= set(git(durable, "rev-list", "refs/heads/main").split())
    assert git(durable, "fsck", "--no-dangling") == ""


def test_a_keep_names_the_helpers_files_its_pods_take_ups_left_as_the_copy_had_them(tmp_path, project):
    first = a_pod(tmp_path, project)
    (first.copy / "a.md").write_text("a")
    land(first, "saga:1")
    (project / "Report.docx").write_bytes(b"PK\x03\x04 report v2, saved by you")
    helper = a_helper(tmp_path, project)
    (helper.copy / "Report.docx").write_bytes(b"PK\x03\x04 report v1 + the helper's numbers")
    (helper.copy / "h.md").write_text("the helper's own file")
    helper.hand_back(author=A, trailers=KEPT)
    failing = a_pod(tmp_path, project)  # its open takes the hand-off up, leaving Report.docx as the copy has it
    (failing.copy / "half.md").write_text("half")
    # The turn fails, and its keep says what the turn's report must: this pod's list goes with the pod.
    kept = failing.keep(author=A, trailers=KEPT, base=True)
    assert kept["not_taken"] == ["Report.docx"]
    assert failing.keep(author=A, trailers=KEPT, base=True) == kept  # and at a second try, with nothing new to push
    # The next turn's pod took nothing up: it has nothing to name.
    assert a_pod(tmp_path, project).commit_turn(author=A, trailers=TURN)["not_taken"] == []


def test_the_commit_step_takes_up_what_a_helper_kept_since_the_copy_last_did(tmp_path, project):
    thread = a_pod(tmp_path, project)
    (thread.copy / "outline.md").write_text("outline")
    thread.hand_off(author=A, trailers=KEPT)
    helper = a_helper(tmp_path, project)
    (helper.copy / "sources.md").write_text("sources")
    (helper.copy / "outline.md").write_text("the helper's outline")
    (thread.copy / "outline.md").write_text("the thread's outline, since")
    # The helper keeps after the thread's last step: nothing takes its work up before the turn's end.
    helper.hand_back(author=A, trailers=KEPT)
    turn = thread.commit_turn(author=A, trailers=TURN)
    # It lands with the turn all the same, and the file the thread changed too is named.
    assert [c["path"] for c in turn["changes"]] == ["outline.md", "sources.md"]
    assert turn["not_taken"] == ["outline.md"]
    assert (thread.copy / "sources.md").read_text() == "sources"


def test_a_helper_with_no_hand_off_starts_at_its_threads_branch_with_the_work_it_has_not_landed(tmp_path, project):
    first = a_pod(tmp_path, project)
    (first.copy / "a.md").write_text("a")
    land(first)
    thread = a_pod(tmp_path, project)
    (thread.copy / "draft.md").write_text("kept, not landed")
    thread.keep(author=A, trailers=KEPT, base=True)  # a failed turn's work, on its branch
    helper = a_helper(tmp_path, project)
    # Not at main, which lacks it: a helper of the thread goes on from the thread's own work.
    assert (helper.copy / "draft.md").read_text() == "kept, not landed"
    assert not (project / "draft.md").exists()


def test_a_failed_helpers_copy_is_kept_apart_and_never_handed_back(tmp_path, project):
    thread = a_pod(tmp_path, project)
    thread.hand_off(author=A, trailers=KEPT)
    helper = a_helper(tmp_path, project)
    (helper.copy / "Budget.xlsx").write_bytes(b"PK\x03\x04 half made")
    (helper.copy / "outline.md").write_text("half an outline")
    assert helper.keep_apart(author=A, trailers=KEPT)["left"] == ["Budget.xlsx", "outline.md"]
    durable = project / "_history"
    assert git(durable, "ls-tree", "--name-only", "refs/helpers/t1/h1").splitlines() == [
        "Budget.xlsx", "Report.docx", "notes.txt", "outline.md",
    ]
    assert thread.take_up() == {"not_taken": []}
    assert not (thread.copy / "Budget.xlsx").exists()


def test_a_hand_off_with_no_handoff_from_is_taken_up_from_the_threads_base(tmp_path, project):
    thread = a_pod(tmp_path, project)
    (thread.copy / "outline.md").write_text("the thread's outline")
    thread.hand_off(author=A, trailers=KEPT)
    helper = a_helper(tmp_path, project)
    (helper.copy / "sources.md").write_text("sources")
    helper.hand_back(author=A, trailers=KEPT)
    refs = project / "_history" / "packed-refs"
    refs.write_text("".join(line for line in refs.read_text().splitlines(True) if "refs/handoff-from/" not in line))
    # The thread's next pod takes it up all the same, from its base: what the hand-off holds since is its work.
    pod = a_pod(tmp_path, project)
    assert (pod.copy / "outline.md").read_text() == "the thread's outline"
    assert (pod.copy / "sources.md").read_text() == "sources"
    turn = pod.commit_turn(author=A, trailers=[["Surogate-Saga", "saga:1"], ["Surogate-Kind", "turn"]])
    assert [c["path"] for c in turn["changes"]] == ["outline.md", "sources.md"]


def test_a_pruning_keeps_every_ref_under_a_kept_name_ending_in_a_slash(tmp_path, project):
    first = a_pod(tmp_path, project)
    (first.copy / "a.md").write_text("a")
    land(first)
    for thread, helper in (("t1", "h1"), ("t1", "h2"), ("t10", "h3")):
        apart = a_pod(tmp_path, project, thread, helper=helper)
        (apart.copy / f"{helper}.md").write_text("half made")
        apart.keep_apart(author=A, trailers=KEPT)
    durable = project / "_history"
    assert a_pod(tmp_path, project).prune(keep=["refs/helpers/t1/"], now=time.time())["pruned"] is True
    refs = git(durable, "for-each-ref", "--format=%(refname)", "refs/helpers/").splitlines()
    # Every ref under the name; none of a thread whose id only starts with it.
    assert refs == ["refs/helpers/t1/h1", "refs/helpers/t1/h2"]
    assert git(durable, "show", "refs/helpers/t1/h2:h2.md") == "half made"
    assert git(durable, "fsck", "--no-dangling") == ""
