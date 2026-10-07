"""A project's history in the bucket: pods fetch it at depth 1 and the lock holder pushes packs."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from surogates.harness.turn_summarizer import is_platform_path
from surogates.sandbox.history import History, HistoryConflict
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


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", f"--git-dir={repo}", *args], capture_output=True, text=True, check=True).stdout.strip()


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


def test_a_pod_looks_again_at_each_folder_main_has_before_it_reads_the_real_files(tmp_path, project, monkeypatch):
    first = a_pod(tmp_path, project)
    (first.copy / "threads" / "Draft A").mkdir(parents=True)
    (first.copy / "threads" / "Draft A" / "Y.md").write_text("by A")
    land(first)
    asked: list[str] = []
    monkeypatch.setattr(os, "setxattr", lambda path, name, value: asked.append((str(path), name)))
    a_pod(tmp_path, project)
    # geesefs trusts a listing for a second: one taken just before another pod's landing would hide its files.
    folders = {path for path, name in asked if name == ".invalidate"}
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
