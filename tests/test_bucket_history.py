"""The api's copy of a project's history, read from its bucket through the storage backend."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import subprocess
import threading
import time
import tracemalloc
from pathlib import Path

import pytest

from surogates.sandbox.history import MAIN, HistoryError
from surogates.storage.backend import LocalBackend
from surogates.workstreams import bucket as module
from surogates.workstreams.bucket import BUSY, TOO_LARGE, Bounds, BucketHistory, said
from tests.test_durable_history import a_pod, cut_history, git, land

pytestmark = pytest.mark.asyncio

PREFIX = "boundaries/workstream:p1/workspace/"


@pytest.fixture()
def storage(tmp_path: Path) -> LocalBackend:
    return LocalBackend(str(tmp_path / "buckets"))


@pytest.fixture()
def project(tmp_path: Path, storage: LocalBackend) -> Path:
    """The project's real files where the storage keeps them, as a pod's geesefs mounts them."""
    real = storage._resolve("agent", PREFIX)
    real.mkdir(parents=True)
    (real / "Report.docx").write_bytes(b"PK\x03\x04 report v1")
    (real / "notes.txt").write_text("v1 notes\n")
    for file in real.rglob("*"):
        os.utime(file, (time.time() - 60, time.time() - 60))
    return real


def bucket(tmp_path: Path, storage: LocalBackend, **bounds: float) -> BucketHistory:
    return BucketHistory(storage, "agent", PREFIX, tmp_path / "api" / "clone", Bounds(**bounds))


def blob_of(data: bytes) -> str:
    """The git blob id of *data*."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def landed(tmp_path: Path, project: Path, saga: str, files: dict[str, bytes]) -> str:
    """A thread's landing of *files*, in a pod of its own; its commit."""
    pod = a_pod(tmp_path, project)
    for name, data in files.items():
        (pod.copy / name).write_bytes(data)
    return land(pod, saga)["commit"]


def packs_of(history: BucketHistory) -> list[Path]:
    return sorted((history.clone / "objects" / "pack").glob("*.pack"))


async def test_the_api_reads_the_history_a_pod_pushed_and_which_versions_it_holds(tmp_path, storage, project):
    one = landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    two = landed(tmp_path, project, "saga:2", {"Report.docx": b"PK\x03\x04 report v3"})
    history = bucket(tmp_path, storage)
    assert (await history.sync())[MAIN] == two
    v2 = git(project / "_history", "rev-parse", f"{one}:Report.docx")
    # A version is a file's bytes: a commit's id names none, nor does an id the history never held.
    assert await history.held([v2, one, "0" * 40, None]) == {v2}
    assert await history.held([]) == set()
    # Its copy is its own repository: none of the bucket's config or HEAD.
    assert (history.clone / "config").read_text() != (project / "_history" / "config").read_text()
    # A project with no landing yet has no history to copy, and holds no version.
    empty = BucketHistory(storage, "agent", "boundaries/workstream:p2/workspace/", tmp_path / "api" / "other")
    assert (await empty.sync(), await empty.held([v2])) == ({}, set())


async def test_a_pruned_version_is_held_no_more(tmp_path, storage, project):
    one = landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    landed(tmp_path, project, "saga:2", {"Report.docx": b"PK\x03\x04 report v3"})
    v2 = git(project / "_history", "rev-parse", f"{one}:Report.docx")
    history = bucket(tmp_path, storage)
    assert await history.held([v2]) == {v2}
    before = {pack.name for pack in packs_of(history)}
    cut_history(tmp_path, project / "_history", kept=1)
    assert await history.held([v2]) == set()
    # The packs the pruning took out of the bucket are gone from the copy too.
    assert before.isdisjoint(pack.name for pack in packs_of(history))
    v3 = blob_of(b"PK\x03\x04 report v3")
    assert await history.held([v3]) == {v3}


@pytest.mark.parametrize("crafted", ["id", "peeled", "ref", "shallow", "a version"])
async def test_a_history_whose_ids_or_refs_a_command_wrote_is_refused(tmp_path, storage, project, crafted):
    landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    durable, ran = project / "_history", tmp_path / "ran"
    # A thread's commands can write the history: an option for git where an id goes.
    option = f"--upload-pack=touch${{IFS}}{ran}"
    main = git(durable, "rev-parse", "refs/heads/main")
    refs = (durable / "packed-refs").read_text()
    if crafted == "id":
        (durable / "packed-refs").write_text(refs.replace(f"{main} refs/heads/main", f"{option} refs/heads/main"))
    elif crafted == "peeled":
        (durable / "packed-refs").write_text(f"{refs}^{option}\n")
    elif crafted == "ref":
        (durable / "packed-refs").write_text(f"{refs}{main} refs/heads/../../config\n")
    elif crafted == "shallow":
        (durable / "shallow").write_text(f"{option}\n")
    with pytest.raises(HistoryError, match="refused the project's history"):
        # A row's version is read from the records, which a landing wrote from what a pod answered: checked as an id too.
        await bucket(tmp_path, storage).held([option] if crafted == "a version" else [main])
    assert not ran.exists()


async def test_git_runs_only_in_the_apis_copy_under_its_own_config(tmp_path, storage, project):
    landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    durable, ran = project / "_history", tmp_path / "ran"
    # A config and a hook a command wrote into the bucket's history.
    (durable / "config").write_text(f"[core]\n\tfsmonitor = touch {ran}\n\thooksPath = hooks\n")
    (durable / "hooks").mkdir(exist_ok=True)
    for hook in ("reference-transaction", "post-index-change"):
        (durable / "hooks" / hook).write_text(f"#!/bin/sh\ntouch {ran}\n")
        (durable / "hooks" / hook).chmod(0o755)
    history = bucket(tmp_path, storage)
    v2 = blob_of(b"PK\x03\x04 report v2")
    assert await history.held([v2]) == {v2}
    # Nothing of the bucket's ran, and none of it is in the copy.
    assert not ran.exists()
    assert "fsmonitor" not in (history.clone / "config").read_text()
    assert not (history.clone / "hooks").exists()


async def test_git_runs_under_none_of_the_apis_own_git_settings(tmp_path, storage, project, monkeypatch):
    one = landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    made = {index.name: index.read_bytes() for index in (project / "_history" / "objects" / "pack").glob("*.idx")}
    # The api's own process, as an operator or another feature may leave it: a git config of the user's, and git's
    # variables set for some other repository.
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / ".gitconfig").write_text("[pack]\n\tindexVersion = 1\n")
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    history = bucket(tmp_path, storage)
    assert (await history.sync())[MAIN] == one
    v2 = blob_of(b"PK\x03\x04 report v2")
    assert await history.held([v2]) == {v2}
    assert {index.name: index.read_bytes() for index in (history.clone / "objects" / "pack").glob("*.idx")} == made
    assert list((tmp_path / "elsewhere").iterdir()) == []


async def test_a_pack_never_passes_through_the_apis_memory_whole(tmp_path, storage, project, monkeypatch):
    one = landed(tmp_path, project, "saga:1", {"Report.docx": os.urandom(64 * 2**20)})  # 64 MiB that does not compress
    landed(tmp_path, project, "saga:2", {"Report.docx": os.urandom(48 * 2**20)})
    report = git(project / "_history", "rev-parse", f"{one}:Report.docx")
    whole = []

    async def read(bucket_name, key):
        whole.append(key)  # the backend's read answers an object whole
        return await LocalBackend.read(storage, bucket_name, key)

    monkeypatch.setattr(storage, "read", read)
    history = bucket(tmp_path, storage)
    tracemalloc.start()
    assert await history.held([report]) == {report}
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    # 112 MiB of packs copied and indexed: none of it held whole.
    assert sum(pack.stat().st_size for pack in packs_of(history)) > 112 * 2**20
    assert peak < 8 * 2**20, peak
    assert whole == []


async def test_a_history_past_its_bound_is_refused_in_words_before_any_of_it_is_read(tmp_path, storage, project):
    landed(tmp_path, project, "saga:1", {"Report.docx": os.urandom(2**20)})
    history = bucket(tmp_path, storage, packs=2**19)
    with pytest.raises(HistoryError) as refused:
        await history.held([blob_of(b"any")])
    assert said(refused.value) == TOO_LARGE == "This project's history is larger than Surogate can read here."
    assert list((history.clone / "objects" / "pack").glob("*")) == []
    # A bound's refusal is said to the user as it is; any other failure of the history is not.
    assert said(HistoryError("git cat-file failed: fatal: bad object")) is None
    # Exactly at its bound it is read.
    size = sum(pack.stat().st_size for pack in (project / "_history" / "objects" / "pack").glob("*.pack"))
    assert MAIN in await bucket(tmp_path, storage, packs=size).sync()


async def test_a_history_of_more_packs_than_a_history_holds_is_refused_before_they_are_listed_whole(
    tmp_path, storage, project, monkeypatch,
):
    landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    packs = project / "_history" / "objects" / "pack"
    # A thread's command can write as many names there as it likes.
    for number in range(8):
        (packs / f"pack-{number:040x}.pack").write_bytes(b"")
    monkeypatch.setattr(module, "_PACKS_MOST", 4)
    asked = []
    listing = storage.list_entries

    async def list_entries(bucket_name, prefix="", limit=None):
        asked.append(limit)
        return await listing(bucket_name, prefix, limit)

    monkeypatch.setattr(storage, "list_entries", list_entries)
    history = bucket(tmp_path, storage)
    with pytest.raises(HistoryError) as refused:
        await history.sync()
    assert (said(refused.value), asked) == (TOO_LARGE, [5])
    assert packs_of(history) == []


async def test_a_copy_not_used_for_a_while_is_removed_and_the_copies_stay_within_their_bound(tmp_path, storage, project):
    landed(tmp_path, project, "saga:1", {"Report.docx": os.urandom(2**20)})
    copies = tmp_path / "api"

    def copy_of(name: str, **bounds: float) -> BucketHistory:
        return BucketHistory(storage, "agent", PREFIX, copies / name, Bounds(**bounds))

    def there() -> list[str]:
        return sorted(p.name for p in copies.iterdir() if p.is_dir())

    idle, recent, old = copy_of("idle"), copy_of("recent"), copy_of("old")
    for history in (idle, recent, old):
        await history.sync()
    ago = time.time() - 700
    os.utime(idle.clone / "HEAD", (ago, ago))
    await copy_of("one").sync()
    # Not used for ten minutes: gone.  The others stay: together they are within the bound.
    assert there() == ["old", "one", "recent"]
    for history, seconds in ((recent, 90), (old, 300)):
        os.utime(history.clone / "HEAD", (time.time() - seconds, time.time() - seconds))
    # Three copies of a mebibyte and a fourth to come, within three: the least recently used goes.
    await copy_of("two", copies=3 * 2**20 + 2**19).sync()
    assert there() == ["one", "recent", "two"]
    # One used within the last minute is never removed, whatever the bound.
    await copy_of("three", copies=2**20).sync()
    assert {"one", "two", "three"} <= set(there())
    # A copy removed is made anew when it is asked again.
    assert (await old.sync())[MAIN] == (await idle.sync())[MAIN]


async def test_a_copy_asked_again_was_used_just_now_however_long_ago_it_was_made(tmp_path, storage, project):
    landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    copies = tmp_path / "api"
    asked, left = (BucketHistory(storage, "agent", PREFIX, copies / name) for name in ("asked", "left"))
    for history in (asked, left):
        await history.sync()
    for history in (asked, left):
        os.utime(history.clone / "HEAD", (time.time() - 700, time.time() - 700))
    assert await asked.held([blob_of(b"never held")]) == set()
    await BucketHistory(storage, "agent", PREFIX, copies / "new").sync()
    assert sorted(p.name for p in copies.iterdir() if p.is_dir()) == ["asked", "new"]


async def test_a_copy_another_request_is_reading_or_bringing_in_is_never_removed(tmp_path, storage, project):
    landed(tmp_path, project, "saga:1", {"Report.docx": os.urandom(2**20)})
    copies = tmp_path / "api"
    read, brought = (BucketHistory(storage, "agent", PREFIX, copies / name) for name in ("read", "brought"))
    for history in (read, brought):
        await history.sync()
    for history in (read, brought):
        os.utime(history.clone / "HEAD", (time.time() - 700, time.time() - 700))
    # One a request of this process reads now; one another process brings in, which holds its lock.
    module._USING[read.clone] += 1
    other = os.open(module._lock_of(brought.clone), os.O_RDWR)
    fcntl.flock(other, fcntl.LOCK_EX)
    try:
        await BucketHistory(storage, "agent", PREFIX, copies / "new").sync()
        assert {"read", "brought"} <= {p.name for p in copies.iterdir()}
    finally:
        os.close(other)
        del module._USING[read.clone]
    await BucketHistory(storage, "agent", PREFIX, copies / "newer").sync()
    assert {"read", "brought"}.isdisjoint(p.name for p in copies.iterdir())


async def test_the_copys_index_is_made_from_the_pack_and_a_pack_git_cannot_read_is_left_out(tmp_path, storage, project):
    one = landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    packs = project / "_history" / "objects" / "pack"
    made = {index.name: index.read_bytes() for index in packs.glob("*.idx")}
    # A thread's command can write the history: an index of its own for each pack, and a pack that is none.
    for name, index in made.items():
        (packs / name).write_bytes(os.urandom(len(index)))
    for kind in ("pack", "idx"):
        (packs / f"pack-{'ab' * 20}.{kind}").write_bytes(os.urandom(4096))
    history = bucket(tmp_path, storage)
    assert (await history.sync())[MAIN] == one
    copy = history.clone / "objects" / "pack"
    assert {index.name: index.read_bytes() for index in copy.glob("*.idx")} == made
    assert sorted(p.suffix for p in copy.glob(f"pack-{'ab' * 20}.*")) == [".bad"]
    v2 = blob_of(b"PK\x03\x04 report v2")
    assert await history.held([v2]) == {v2}
    # Neither the pack that is none nor one the copy has is read again.
    downloads = []
    read = storage.download

    async def download(bucket_name, key, target, **limit):
        downloads.append(key)
        return await read(bucket_name, key, target, **limit)

    storage.download = download
    await history.sync()
    assert [key for key in downloads if key.endswith((".pack", ".idx"))] == []


async def test_a_pack_whose_push_has_not_finished_or_that_is_no_packs_name_is_not_taken(tmp_path, storage, project):
    landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    packs = project / "_history" / "objects" / "pack"
    pushed = sorted(packs.glob("*.pack"))
    real = pushed[0]
    # A push writes its pack before its index: one with no index yet is not whole.  A name that is no pack's is no pack.
    (packs / f"pack-{'cd' * 20}.pack").write_bytes(real.read_bytes())
    for kind in ("pack", "idx"):
        (packs / f"pack-mine.{kind}").write_bytes(real.with_suffix(f".{kind}").read_bytes())
    (packs / "tmp_pack_Ab12").write_bytes(real.read_bytes())
    history = bucket(tmp_path, storage)
    await history.sync()
    assert [pack.name for pack in packs_of(history)] == [pack.name for pack in pushed]


async def test_a_pack_a_pruning_took_between_the_listing_and_its_read_is_left_out(tmp_path, storage, project, monkeypatch):
    landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    gone, *others = sorted((project / "_history" / "objects" / "pack").glob("*.pack"))
    read = storage.download

    async def download(bucket_name, key, target, **limit):
        if key.endswith(gone.name):
            raise KeyError(key)
        return await read(bucket_name, key, target, **limit)

    monkeypatch.setattr(storage, "download", download)
    history = bucket(tmp_path, storage)
    assert MAIN in await history.sync()
    assert [pack.name for pack in packs_of(history)] == [pack.name for pack in others]
    # It is asked for again at the next read: nothing marks it as the copy's.
    monkeypatch.setattr(storage, "download", read)
    await history.sync()
    assert gone.name in [pack.name for pack in packs_of(history)]


async def test_each_new_pack_is_indexed_beside_no_other_so_a_history_of_many_packs_is_not_read_as_many_times_over(
    tmp_path, storage, project, monkeypatch,
):
    landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    run, indexed = subprocess.run, []

    def spied(command, **how):
        if "index-pack" in command:
            indexed.append(how["env"].get("GIT_OBJECT_DIRECTORY"))
        return run(command, **how)

    monkeypatch.setattr(module.subprocess, "run", spied)
    history = bucket(tmp_path, storage)
    await history.sync()
    # Git opens every pack of the objects it runs over: each new pack is indexed over a folder that holds it alone.
    assert len(indexed) == 2 and all(folder and Path(folder).parent == history.clone for folder in indexed)
    assert len(set(indexed)) == 2


async def test_a_pack_that_grew_since_it_was_listed_is_refused(tmp_path, storage, project, monkeypatch):
    landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    listing = storage.list_entries

    async def list_entries(bucket_name, prefix="", limit=None):
        entries = await listing(bucket_name, prefix, limit)
        # A thread's command writes more into the pack between the listing, which the bound was checked by, and the read.
        for pack in (project / "_history" / "objects" / "pack").glob("*.pack"):
            pack.chmod(0o644)
            with open(pack, "ab") as more:
                more.write(b"x" * 64)
        return entries

    monkeypatch.setattr(storage, "list_entries", list_entries)
    history = bucket(tmp_path, storage)
    with pytest.raises(HistoryError, match="refused the project's history: a pack grew while it was read"):
        await history.sync()
    assert packs_of(history) == []


async def test_a_refs_file_larger_than_a_historys_is_refused(tmp_path, storage, project):
    landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    with open(project / "_history" / "packed-refs", "ab") as refs:
        refs.write(b"# " + b"x" * (16 * 2**20) + b"\n")
    with pytest.raises(HistoryError, match="refused the project's history: its packed-refs is larger than a history's"):
        await bucket(tmp_path, storage).sync()


async def test_a_request_waits_for_the_copy_another_is_bringing_in_and_is_told_to_try_again_past_its_patience(
    tmp_path, storage, project, monkeypatch,
):
    landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    history = bucket(tmp_path, storage)
    await history.sync()
    v2 = blob_of(b"PK\x03\x04 report v2")
    monkeypatch.setattr(module, "_PATIENCE", 0.4)
    # Another request, in this process or another on this disk, is bringing the copy in.
    other = os.open(module._lock_of(history.clone), os.O_RDWR)
    fcntl.flock(other, fcntl.LOCK_EX)
    try:
        began = time.monotonic()
        with pytest.raises(HistoryError) as refused:
            await history.held([v2])
        assert 0.4 <= time.monotonic() - began < 3
        assert said(refused.value) is None
        assert str(refused.value) == BUSY == "This project's history is being read just now. Try again in a moment."
        # Let go within its patience, the request is answered.
        waiting = asyncio.create_task(history.held([v2]))
        await asyncio.sleep(0.1)
        assert not waiting.done()
    finally:
        os.close(other)
    assert await waiting == {v2}


async def test_two_requests_for_one_project_copy_its_packs_once(tmp_path, storage, project, monkeypatch):
    landed(tmp_path, project, "saga:1", {"Report.docx": os.urandom(2**20)})
    downloads = []
    read = storage.download

    async def download(bucket_name, key, target, **limit):
        downloads.append(key)
        await asyncio.sleep(0.05)  # the second request arrives while the first reads
        return await read(bucket_name, key, target, **limit)

    monkeypatch.setattr(storage, "download", download)
    first, second = bucket(tmp_path, storage), bucket(tmp_path, storage)
    refs = await asyncio.gather(first.sync(), second.sync())
    assert refs[0] == refs[1]
    taken = [key for key in downloads if key.endswith(".pack")]
    assert len(taken) == len(set(taken)) == len(packs_of(first)) > 0


async def test_a_copy_on_its_way_in_is_not_given_up_with_the_request_that_began_it(tmp_path, storage, project, monkeypatch):
    landed(tmp_path, project, "saga:1", {"Report.docx": os.urandom(2**20)})
    reading, taken, read = asyncio.Event(), [], storage.download

    async def download(bucket_name, key, target, **limit):
        if key.endswith(".pack"):
            taken.append(key)
            reading.set()
            await asyncio.sleep(0.2)
        return await read(bucket_name, key, target, **limit)

    monkeypatch.setattr(storage, "download", download)
    history = bucket(tmp_path, storage)
    request = asyncio.create_task(history.sync())
    await reading.wait()
    # Its client went away, as a page does that waited its ten seconds.
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert packs_of(history) == []
    # The next request waits for the copy that one began, and reads no pack a second time.
    assert MAIN in await history.sync()
    assert len(packs_of(history)) == len(taken) == len(set(taken)) == 2


async def test_git_runs_as_few_children_at_once_and_a_request_past_its_patience_for_one_is_told_to_try_again(
    tmp_path, storage, project, monkeypatch,
):
    landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    v2 = blob_of(b"PK\x03\x04 report v2")
    copies = [BucketHistory(storage, "agent", PREFIX, tmp_path / "api" / f"copy-{n}") for n in range(module._GIT_SLOTS * 3)]
    running, most, gate = 0, 0, threading.Lock()
    run = subprocess.run

    def counted(*args, **kwargs):
        nonlocal running, most
        with gate:
            running += 1
            most = max(most, running)
        try:
            time.sleep(0.02)
            return run(*args, **kwargs)
        finally:
            with gate:
                running -= 1

    monkeypatch.setattr(module.subprocess, "run", counted)
    # Every project's first read at once: each is answered, and git ran as few at a time as it has turns.
    assert await asyncio.gather(*(copy.held([v2]) for copy in copies)) == [{v2}] * len(copies)
    assert most == module._GIT_SLOTS
    # While every turn is taken for longer than a request waits, it is told so; it holds no turn after.
    release = threading.Event()

    def held_up(*args, **kwargs):
        release.wait(10)
        return run(*args, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", held_up)
    monkeypatch.setattr(module, "_PATIENCE", 0.3)
    taken = [asyncio.create_task(copy.held([v2])) for copy in copies[: module._GIT_SLOTS]]
    await asyncio.sleep(0.1)
    with pytest.raises(HistoryError, match="being read just now"):
        await copies[-1].held([v2])
    release.set()
    assert await asyncio.gather(*taken) == [{v2}] * module._GIT_SLOTS
    assert await copies[-1].held([v2]) == {v2}


async def test_a_history_git_cannot_read_within_its_memory_is_refused_in_words_and_its_pack_is_not_given_up(
    tmp_path, storage, project, monkeypatch,
):
    landed(tmp_path, project, "saga:1", {"Report.docx": os.urandom(24 * 2**20)})
    # Git with 20 MiB to itself, and an object of 24 it must hold whole to index.
    monkeypatch.setattr(module, "_GIT_MEMORY", 20 * 2**20)
    history = bucket(tmp_path, storage)
    with pytest.raises(HistoryError) as refused:
        await history.sync()
    assert said(refused.value) == "This project's history holds a file larger than Surogate can read here."
    assert list((history.clone / "objects" / "pack").glob("*.bad")) == []
    # It is not read again to be refused again: the next request is told at once.
    downloads = []
    read = storage.download

    async def download(bucket_name, key, target, **limit):
        downloads.append(key)
        return await read(bucket_name, key, target, **limit)

    monkeypatch.setattr(storage, "download", download)
    with pytest.raises(HistoryError, match="holds a file larger than Surogate can read here"):
        await history.sync()
    assert [key for key in downloads if key.endswith(".pack")] == []
    # Nor is it a pack that is none: it is read once git has the memory, and what marked it goes.
    monkeypatch.setattr(module, "_GIT_MEMORY", 256 * 2**20)
    assert MAIN in await history.sync()
    assert len(packs_of(history)) == 2
    assert list((history.clone / "objects" / "pack").glob("*.large")) == []


async def test_the_copy_of_a_project_is_one_folder_by_its_bucket_and_prefix_within_the_apis_settings(tmp_path, storage):
    from types import SimpleNamespace
    from uuid import uuid4

    from surogates.config import HistorySettings

    def master(bucket_name: str, project_id) -> SimpleNamespace:
        config = {"storage_bucket": bucket_name, "workspace_boundary": f"workstream:{project_id}"}
        return SimpleNamespace(id=uuid4(), org_id=uuid4(), user_id=uuid4(), agent_id="a", config=config)

    one, two = uuid4(), uuid4()
    settings = HistorySettings(copies_path=str(tmp_path / "copies"), packs_bound=5, copies_bound=7, copy_idle=9)
    first = BucketHistory.of(storage, master("agent", one), settings)
    assert first.clone.parent == tmp_path / "copies"
    assert first.bounds == Bounds(packs=5, copies=7, idle=9)
    assert first.prefix.endswith("/") and str(one) in first.prefix
    # The same project is the same copy, whoever of its sessions asks; another project, or another bucket, is another.
    assert BucketHistory.of(storage, master("agent", one), settings).clone == first.clone
    assert BucketHistory.of(storage, master("agent", two), settings).clone != first.clone
    assert BucketHistory.of(storage, master("other", one), settings).clone != first.clone
    # With no settings, the defaults, in the temp folder.
    plain = BucketHistory.of(storage, master("agent", one))
    assert (plain.clone.parent, plain.bounds) == (module.CLONES, Bounds())
    assert (HistorySettings().packs_bound, HistorySettings().copies_bound, HistorySettings().copy_idle) == (4 * 2**30, 8 * 2**30, 600)
