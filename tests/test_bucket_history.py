"""The api's copy of a project's history, read from its bucket through the storage backend, and what it lands there as a pod does."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import zlib
from collections.abc import Iterable
from pathlib import Path

import pytest

from surogates.sandbox.history import MAIN, HistoryConflict, HistoryError
from surogates.storage.backend import LocalBackend
from surogates.workstreams import bucket as module
from surogates.workstreams.bucket import BUSY, TOO_LARGE, Bounds, BucketHistory, Busy, NotKept, Slow, said
from surogates.workstreams.history import REFS_BOUND
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


def by_hand(storage: LocalBackend, name: str, refs: str = f"{'1' * 40} {MAIN}\n") -> Path:
    """A history written by hand for the project *name*, as a thread's command can write one: its folder."""
    durable = storage._resolve("agent", f"{name}/") / "_history"
    (durable / "objects" / "pack").mkdir(parents=True)
    (durable / "packed-refs").write_text(refs)
    return durable


def copy_of(tmp_path: Path, storage: LocalBackend, name: str, copy: str | None = None, **bounds: float) -> BucketHistory:
    """The api's copy of the project *name*'s history, in a folder of *copy*'s name or its own."""
    return BucketHistory(storage, "agent", f"{name}/", tmp_path / "api" / (copy or name), Bounds(**bounds))


def a_pack(packs: Path, blobs: Iterable[bytes] = (), *, zeros: int = 0) -> str:
    """A pack written by hand into a history's *packs*: of *blobs*, or of one blob of *zeros* zero bytes; its name.

    Its index in the bucket is empty: the api reads only that one is listed.
    """
    def header(size: int) -> bytes:
        first, size, out = (3 << 4) | (size & 0x0F), size >> 4, bytearray()
        while size:
            out.append(first | 0x80)
            first, size = size & 0x7F, size >> 7
        out.append(first)
        return bytes(out)

    blobs = list(blobs)
    digest, staged = hashlib.sha1(), packs / "staged"
    with open(staged, "wb") as out:
        def put(piece: bytes) -> None:
            digest.update(piece)
            out.write(piece)

        put(b"PACK" + struct.pack(">II", 2, len(blobs) or 1))
        for blob in blobs:
            put(header(len(blob)) + zlib.compress(blob))
        if not blobs:
            put(header(zeros))
            packer, block, left = zlib.compressobj(1), bytes(1 << 24), zeros
            while left:
                put(packer.compress(block[: min(left, len(block))]))
                left -= min(left, len(block))
            put(packer.flush())
        out.write(digest.digest())
    name = f"pack-{digest.hexdigest()}"
    staged.rename(packs / f"{name}.pack")
    (packs / f"{name}.idx").write_bytes(b"")
    return name


def refs_of(size: int) -> str:
    """A ``packed-refs`` of *size* bytes at most: ``main``, and before it as many threads' refs as fit, in order."""
    main, lines, left, n = f"{'1' * 40} {MAIN}\n", [], size, 0
    left -= len(main)
    while left >= 63:
        lines.append(f"{n:040x} refs/handoff/{n:08x}\n")
        left, n = left - 63, n + 1
    return "".join(lines) + main


def on_disk(folder: Path) -> int:
    """The bytes of every file under *folder*."""
    return sum(file.stat().st_size for file in folder.rglob("*") if file.is_file())


def staged_in(history: BucketHistory) -> list[Path]:
    """The versions the copy holds on their way to who asked for them."""
    return sorted((history.clone / "out").glob("*"))


async def sent(staged) -> bytes:
    """A version as who asked for it takes it: its pieces, joined."""
    pieces = []

    async def take(piece: bytes) -> None:
        pieces.append(piece)

    await staged.send(take)
    return b"".join(pieces)


def handles_of_versions() -> list[str]:
    """The handles this process holds open on versions written out of any copy."""
    return [fd for fd in os.listdir("/proc/self/fd") if "/out/" in os.path.realpath(f"/proc/self/fd/{fd}")]


def let_go_on_the_loop(monkeypatch) -> list[bool]:
    """Whether each version let go from now on was let go on the loop's own thread, where a large file's going would stall it."""
    where, let_go = [], module._let_go

    def noted(*args) -> None:
        where.append(threading.current_thread() is threading.main_thread())
        let_go(*args)

    monkeypatch.setattr(module, "_let_go", noted)
    return where


def no_ones(history: BucketHistory) -> bool:
    """Whether the copy is no request's: its lock can be taken."""
    held = os.open(module._lock_of(history.clone), os.O_RDWR)
    try:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(held)
    return True


def a_table(megabytes: int, changed: int) -> bytes:
    """About *megabytes* MiB of a table's rows, a few of them *changed*: git stores one such version as a change to another."""
    rows = [f"{n},customer-{n % 9973},{(n * 7919) % 100000},{n * 31 % 977}.{n % 100:02d}\n" for n in range(megabytes * 36_000)]
    for n in range(changed * 50):
        rows[(n * 7919) % len(rows)] = f"changed in version {changed}, row {n}\n"
    return "".join(rows).encode()


def read_from(storage: LocalBackend, monkeypatch) -> list[str]:
    """Each object the copy reads from the bucket from now on, by the last part of its key."""
    read, keys = storage.download, []

    async def download(bucket_name, key, target, **limit):
        keys.append(key.rsplit("/", 1)[-1])
        return await read(bucket_name, key, target, **limit)

    monkeypatch.setattr(storage, "download", download)
    return keys


async def test_the_api_reads_the_history_a_pod_pushed_and_which_versions_it_holds(tmp_path, storage, project):
    one = landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    two = landed(tmp_path, project, "saga:2", {"Report.docx": b"PK\x03\x04 report v3"})
    history = bucket(tmp_path, storage)
    assert await history.sync() == two
    v2 = git(project / "_history", "rev-parse", f"{one}:Report.docx")
    # A version is a file's bytes: a commit's id names none, nor does an id the history never held.
    assert await history.held([v2, one, "0" * 40, None]) == {v2}
    assert await history.held([]) == set()
    # Its copy is its own repository: none of the bucket's config or HEAD.
    assert (history.clone / "config").read_text() != (project / "_history" / "config").read_text()
    # A project with no landing yet has no history to copy, and holds no version.
    empty = BucketHistory(storage, "agent", "boundaries/workstream:p2/workspace/", tmp_path / "api" / "other")
    listed, listing = [], storage.list_entries

    async def list_entries(bucket_name, prefix="", limit=None):
        listed.append(prefix)
        return await listing(bucket_name, prefix, limit)

    storage.list_entries = list_entries
    assert (await empty.sync(), await empty.held([v2])) == (None, set())
    # Nothing of it is listed.
    assert listed == []


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


@pytest.mark.parametrize("crafted, refusal", [
    ("id", "its packed-refs holds what is not a commit id"), ("peeled", "its packed-refs holds what is not a commit id"),
    ("ref", "its packed-refs holds what is not one of its refs"), ("shallow", "its shallow holds what is not a commit id"),
    ("a version", "a version holds what is not a commit id"),
])
async def test_a_history_whose_ids_or_refs_a_command_wrote_is_refused(tmp_path, storage, project, crafted, refusal):
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
    with pytest.raises(HistoryError, match=f"refused the project's history: {refusal}"):
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
    assert await history.sync() == one
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
    # With room for it, it is read.
    assert await bucket(tmp_path, storage, packs=2**21).sync() is not None
    # Two landings that would each fit, and do not together: none of either is read.
    landed(tmp_path, project, "saga:2", {"notes.txt": os.urandom(2**20)})
    short = BucketHistory(storage, "agent", PREFIX, tmp_path / "api" / "short", Bounds(packs=2**20 + 2**19))
    with pytest.raises(HistoryError) as refused:
        await short.sync()
    assert said(refused.value) == TOO_LARGE
    assert list((short.clone / "objects" / "pack").glob("*")) == []


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
    assert await old.sync() == await idle.sync() is not None


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
    assert await history.sync() == one
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
    assert await history.sync() is not None
    assert [pack.name for pack in packs_of(history)] == [pack.name for pack in others]
    # It is asked for again at the next read: nothing marks it as the copy's.
    monkeypatch.setattr(storage, "download", read)
    await history.sync()
    assert gone.name in [pack.name for pack in packs_of(history)]


async def test_each_new_pack_is_indexed_beside_no_other_so_a_history_of_many_packs_is_not_read_as_many_times_over(
    tmp_path, storage, project, monkeypatch,
):
    landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    run, indexed, made = subprocess.run, [], []

    def spied(command, **how):
        done = run(command, **how)
        if "index-pack" in command:
            indexed.append(how["env"].get("GIT_OBJECT_DIRECTORY"))
            made.append(sorted(file.suffix for file in Path(indexed[-1]).iterdir()))
        return done

    monkeypatch.setattr(module.subprocess, "run", spied)
    history = bucket(tmp_path, storage)
    await history.sync()
    # Git opens every pack of the objects it runs over: each new pack is indexed over a folder that holds it alone.
    assert len(indexed) == 2 and all(folder and Path(folder).parent == history.clone for folder in indexed)
    assert len(set(indexed)) == 2
    # And git makes of each the one index the copy keeps and has counted, none of its own beside it.
    assert made == [[".idx", ".pack"]] * 2


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


@pytest.mark.parametrize("name", ["packed-refs", "shallow"])
async def test_a_refs_file_larger_than_a_historys_is_refused_in_words_and_never_read(tmp_path, storage, project, monkeypatch, name):
    landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    history, read = bucket(tmp_path, storage), read_from(storage, monkeypatch)
    # As many lines of a history's own as a turn's end reads into the worker, and one more: each a line git would take.
    with open(project / "_history" / name, "ab") as refs:
        refs.write(f"{'1' * 40}\n".encode() * (REFS_BOUND // 41 + 1) if name == "shallow" else b"#\n" * (REFS_BOUND // 2))
    assert (project / "_history" / name).stat().st_size > REFS_BOUND
    for _ in range(2):
        with pytest.raises(HistoryError) as refused:
            await history.sync()
        assert said(refused.value) == TOO_LARGE
    assert read == []
    # One that grows past it between what the bucket said of it and its read is refused the same.
    said_of = storage.stat

    async def stat(bucket_name, key):
        return {**await said_of(bucket_name, key), "size": 64}

    monkeypatch.setattr(storage, "stat", stat)
    with pytest.raises(HistoryError) as refused:
        await history.sync()
    assert (said(refused.value), name in read) == (TOO_LARGE, True)
    assert not list(history.clone.glob("scratch-*"))


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
    mains = await asyncio.gather(first.sync(), second.sync())
    assert mains[0] == mains[1] is not None
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
    assert await history.sync() is not None
    assert len(packs_of(history)) == len(taken) == len(set(taken)) == 2


async def test_git_runs_as_few_children_at_once_to_bring_copies_in_and_as_few_to_ask_them_and_a_request_past_its_patience_is_told_so(
    tmp_path, storage, project, monkeypatch,
):
    landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    v2 = blob_of(b"PK\x03\x04 report v2")
    copies = [BucketHistory(storage, "agent", PREFIX, tmp_path / "api" / f"copy-{n}") for n in range(12)]
    running, most, gate = {"bring": 0, "ask": 0}, {"bring": 0, "ask": 0}, threading.Lock()
    run = subprocess.run

    def counted(command, **how):
        kind = "ask" if "cat-file" in command else "bring"
        with gate:
            running[kind] += 1
            most[kind] = max(most[kind], running[kind])
        try:
            time.sleep(0.02)
            return run(command, **how)
        finally:
            with gate:
                running[kind] -= 1

    monkeypatch.setattr(module.subprocess, "run", counted)
    # Every project's first read at once, then each asked again: all answered, and git ran as few at a time as each has turns.
    for _ in range(2):
        assert await asyncio.gather(*(copy.held([v2]) for copy in copies)) == [{v2}] * len(copies)
    assert most == {"bring": module._BRING_SLOTS, "ask": module._READ_SLOTS}
    # Together they are what the api's memory is spent on at most.
    assert (module._BRING_SLOTS + module._READ_SLOTS) * module._GIT_MEMORY == 2**30
    # While every turn to ask is taken for longer than a request waits, it is told so; it holds no turn after.
    release = threading.Event()

    def held_up(command, **how):
        release.wait(10)
        return run(command, **how)

    monkeypatch.setattr(module.subprocess, "run", held_up)
    monkeypatch.setattr(module, "_PATIENCE", 0.3)
    taken = [asyncio.create_task(copy.held([v2])) for copy in copies[: module._READ_SLOTS]]
    await asyncio.sleep(0.1)
    with pytest.raises(Busy, match="being read just now"):
        await copies[-1].held([v2])
    release.set()
    assert await asyncio.gather(*taken) == [{v2}] * module._READ_SLOTS
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
    assert await history.sync() is not None
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
    settings = HistorySettings(copies_path=str(tmp_path / "copies"), packs_bound=5, file_bound=3, copies_bound=7, copy_idle=9)
    first = BucketHistory.of(storage, master("agent", one), settings)
    assert first.clone.parent == tmp_path / "copies"
    assert first.bounds == Bounds(packs=5, file=3, copies=7, idle=9)
    assert first.prefix.endswith("/") and str(one) in first.prefix
    # The same project is the same copy, whoever of its sessions asks; another project, or another bucket, is another.
    assert BucketHistory.of(storage, master("agent", one), settings).clone == first.clone
    assert BucketHistory.of(storage, master("agent", two), settings).clone != first.clone
    assert BucketHistory.of(storage, master("other", one), settings).clone != first.clone
    # With no settings, the defaults, in the temp folder.
    plain = BucketHistory.of(storage, master("agent", one))
    assert (plain.clone.parent, plain.bounds) == (module.CLONES, Bounds())
    assert (HistorySettings().packs_bound, HistorySettings().copies_bound, HistorySettings().copy_idle) == (4 * 2**30, 8 * 2**30, 600)
    assert HistorySettings().file_bound == Bounds().file == 2**30


async def test_refs_at_their_bound_are_checked_off_the_loop_a_line_at_a_time_whatever_the_number_of_projects(
    tmp_path, storage, monkeypatch,
):
    # Six projects, each with as many refs and as many cut commits as a history's files may hold.
    refs, cut = refs_of(REFS_BOUND), "".join(f"{n:040x}\n" for n in range(REFS_BOUND // 41))
    copies = []
    for n in range(6):
        (by_hand(storage, f"p{n}", refs) / "shallow").write_text(cut)
        copies.append(copy_of(tmp_path, storage, f"p{n}"))
    del refs, cut
    # Each waits its turn for as long as it takes: this is what the six cost, not who is told to try again.
    monkeypatch.setattr(module, "_PATIENCE", 120.0)
    stalls, checking, most, checked_in, check = [], 0, 0, set(), module._checked

    def checked(scratch, clone):
        nonlocal checking, most
        checking += 1
        most = max(most, checking)
        checked_in.add(threading.current_thread().name.rsplit("_", 1)[0])
        try:
            return check(scratch, clone)
        finally:
            checking -= 1

    monkeypatch.setattr(module, "_checked", checked)

    async def tick() -> None:
        while True:
            at = time.perf_counter()
            await asyncio.sleep(0.005)
            stalls.append(time.perf_counter() - at - 0.005)

    ticking = asyncio.ensure_future(tick())
    await asyncio.sleep(0.05)
    stalls.clear()
    answers = await asyncio.gather(*(copy.held(["0" * 40]) for copy in copies))
    ticking.cancel()
    assert answers == [set()] * 6
    # Each was checked in the refs' own turn, one at a time, and the loop went on serving every other request meanwhile.
    assert (checked_in, most) == ({"history-refs"}, 1)
    assert max(stalls) < 0.25, max(stalls)
    # Each copy has its own refs and cut, as git reads them.
    for copy in copies:
        assert (copy.clone / "packed-refs").stat().st_size > REFS_BOUND - 128
        assert (copy.clone / "shallow").stat().st_size == (REFS_BOUND // 41) * 41
        assert git(copy.clone, "rev-parse", MAIN) == "1" * 40
    # Written anew, all six are read again at once: the api holds a piece of each file on its way to its disk, and
    # a line of the one it checks, never a file whole.
    for n in range(6):
        for name in ("packed-refs", "shallow"):
            os.utime(storage._resolve("agent", f"p{n}/") / "_history" / name, (time.time() + 5, time.time() + 5))
    read = read_from(storage, monkeypatch)
    tracemalloc.start()
    await asyncio.gather(*(copy.sync() for copy in copies))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert sorted(read) == ["packed-refs"] * 6 + ["shallow"] * 6
    assert peak < 4 * 2**20, peak


async def test_refs_the_bucket_says_are_unchanged_are_not_read_again_nor_are_refs_it_refused(tmp_path, storage, project, monkeypatch):
    landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    v2 = blob_of(b"PK\x03\x04 report v2")
    history, read = bucket(tmp_path, storage), read_from(storage, monkeypatch)

    def refs_read() -> list[str]:
        return [name for name in read if name in ("packed-refs", "shallow")]

    assert await history.held([v2]) == {v2}
    assert refs_read() == ["packed-refs"]
    for _ in range(3):
        assert await history.held([v2]) == {v2}
    assert refs_read() == ["packed-refs"]
    # A landing moves main: the bucket says the file is another, and it is read once more.
    two = landed(tmp_path, project, "saga:2", {"Report.docx": b"PK\x03\x04 report v3"})
    assert await history.sync() == two
    assert await history.sync() == two
    assert refs_read() == ["packed-refs"] * 2
    # Another file of the same size and the same date is told by what the bucket names its contents by.
    said_of, named = storage.stat, "one"

    async def stat(bucket_name, key):
        return {**await said_of(bucket_name, key), "modified": 1.0, "etag": named}

    monkeypatch.setattr(storage, "stat", stat)
    await history.sync()
    await history.sync()
    assert refs_read() == ["packed-refs"] * 3
    named = "another"
    await history.sync()
    assert refs_read() == ["packed-refs"] * 4
    monkeypatch.setattr(storage, "stat", said_of)
    # Refs a command wrote are refused, and refused again in the same words without being read again.
    refs = project / "_history" / "packed-refs"
    refs.write_text(refs.read_text() + f"{'z' * 40} refs/heads/other\n")
    for _ in range(3):
        with pytest.raises(HistoryError, match="refused the project's history: its packed-refs holds what is not a commit id"):
            await history.held([v2])
    assert refs_read() == ["packed-refs"] * 5
    # Put right, they are read again.
    refs.write_text(refs.read_text().replace(f"{'z' * 40} refs/heads/other\n", ""))
    assert await history.held([v2]) == {v2}


async def test_refs_the_bucket_is_slow_to_give_are_given_up_and_asked_for_again_at_the_next_request(tmp_path, storage, project, monkeypatch):
    one = landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    history, read = bucket(tmp_path, storage), storage.download

    async def stuck(bucket_name, key, target, **limit):
        await asyncio.sleep(30)

    monkeypatch.setattr(storage, "download", stuck)
    monkeypatch.setattr(module, "_READ_SECONDS", 0.3)
    began = time.monotonic()
    with pytest.raises(HistoryError, match="the project's refs took longer than 0s to read") as refused:
        await history.sync()
    assert said(refused.value) is None and time.monotonic() - began < 3
    # Nothing is noted against the refs: they are read once the bucket answers, and their turn was given back.
    monkeypatch.setattr(storage, "download", read)
    assert await history.sync() == one


async def test_refs_whose_turn_did_not_come_are_asked_for_again_at_the_next_request(tmp_path, storage, project, monkeypatch):
    one = landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    history = bucket(tmp_path, storage)
    release = threading.Event()
    taken = module._REFS.submit(release.wait, 20)
    monkeypatch.setattr(module, "_PATIENCE", 0.3)
    try:
        with pytest.raises(Busy, match="being read just now"):
            await history.sync()
    finally:
        release.set()
        taken.result()
    assert await history.sync() == one


@pytest.mark.parametrize("crafted, refusal", [
    ("out of order", "its packed-refs is not in order"),
    ("a ref twice", "its packed-refs is not in order"),
    ("a line no ref has", "its packed-refs holds a line longer than a ref's"),
    ("a cut that is one long line", "its shallow holds a line longer than a ref's"),
])
async def test_refs_no_writer_of_a_history_writes_are_refused(tmp_path, storage, crafted, refusal):
    one, two = f"{'1' * 40} refs/handoff/a\n", f"{'2' * 40} {MAIN}\n"
    durable = by_hand(storage, "p", {
        "out of order": two + one, "a ref twice": one + two + two,
        "a line no ref has": f"{'1' * 40} refs/handoff/{'a' * 5000}\n" + two,
    }.get(crafted, one + two))
    if crafted == "a cut that is one long line":
        (durable / "shallow").write_text(" ".join(["3" * 40] * 200))
    history = copy_of(tmp_path, storage, "p")
    with pytest.raises(HistoryError, match=f"refused the project's history: {refusal}"):
        await history.sync()
    # In order, with a cut of several commits a line, they are a history's own.
    (durable / "packed-refs").write_text(one + two)
    (durable / "shallow").write_text(f"{'3' * 40} {'4' * 40}\n\n{'5' * 40}\n")
    assert await history.sync() == "2" * 40
    assert (history.clone / "packed-refs").read_text().endswith(one + two)
    assert (history.clone / "shallow").read_text() == "".join(f"{n * 40}\n" for n in "345")
    # A history whose cut is gone has none in the copy.
    (durable / "shallow").unlink()
    (durable / "packed-refs").write_text(two)
    assert await history.sync() == "2" * 40
    assert not (history.clone / "shallow").exists()


async def test_a_copys_bound_counts_what_it_holds_on_the_apis_disk_with_the_indexes_the_api_makes(tmp_path, storage, monkeypatch):
    durable = by_hand(storage, "small")
    # Twenty thousand files of four bytes: an index holds more of each than the pack does.
    name = a_pack(durable / "objects" / "pack", (struct.pack(">I", n) for n in range(20_000)))
    in_bucket, index = (durable / "objects" / "pack" / f"{name}.pack").stat().st_size, 1072 + 28 * 20_000
    assert in_bucket < index
    # A bound the bucket's bytes are within, and the copy on the api's disk would not be.
    tight = copy_of(tmp_path, storage, "small", "tight", packs=in_bucket + index // 2)
    with pytest.raises(HistoryError) as refused:
        await tight.sync()
    assert said(refused.value) == TOO_LARGE
    # Nothing of the pack is left there, and what is left is counted.
    assert [left.suffix for left in (tight.clone / "objects" / "pack").iterdir()] == [".over"]
    assert module._size(tight.clone) == on_disk(tight.clone) < 16 * 2**10
    # It is refused again without the pack being read again.
    read = read_from(storage, monkeypatch)
    with pytest.raises(HistoryError) as refused:
        await tight.held([blob_of(struct.pack(">I", 7))])
    assert (said(refused.value), [key for key in read if key.endswith(".pack")]) == (TOO_LARGE, [])
    # With room for both it is read, and the copy is counted for all it holds.
    roomy = copy_of(tmp_path, storage, "small", "roomy", packs=in_bucket + index + 16 * 2**10)
    seven = blob_of(struct.pack(">I", 7))
    assert await roomy.held([seven]) == {seven}
    assert module._size(roomy.clone) == on_disk(roomy.clone) > in_bucket + index
    assert (roomy.clone / "objects" / "pack" / f"{name}.idx").stat().st_size == index
    # The copy that refused it reads it once it has the room, and notes nothing against it any more.
    assert await copy_of(tmp_path, storage, "small", "tight", packs=in_bucket + index + 16 * 2**10).held([seven]) == {seven}
    assert sorted(left.suffix for left in (tight.clone / "objects" / "pack").iterdir()) == [".bucket", ".idx", ".pack"]
    # A byte less than it holds is too few.
    exact = module._size(roomy.clone)
    with pytest.raises(HistoryError) as refused:
        await copy_of(tmp_path, storage, "small", "short", packs=exact - 1).sync()
    assert said(refused.value) == TOO_LARGE


async def test_a_pack_the_copy_has_no_room_left_for_is_refused_before_it_is_read(tmp_path, storage, monkeypatch):
    durable = by_hand(storage, "two")
    packs = durable / "objects" / "pack"
    def one_of(made, wanted) -> str:
        """A pack as *made* makes one, of a name *wanted* takes: the copy takes the bucket's packs in the order of their names."""
        for attempt in range(256):
            name = a_pack(packs, made(attempt))
            if wanted(name):
                return name
            for kind in ("pack", "idx"):
                (packs / f"{name}.{kind}").unlink()
        raise AssertionError("no pack of such a name in 256 tries")

    # Twenty thousand small files, read first, and one plain file, read after.
    many = one_of(lambda attempt: (struct.pack(">II", attempt, n) for n in range(20_000)), lambda name: name < "pack-8")
    plain = one_of(lambda attempt: [os.urandom(100_000)], lambda name: name > many)
    listed = sum(pack.stat().st_size for pack in packs.glob("*.pack"))
    index = 1072 + 28 * 20_000
    # Room for what the bucket lists, and for the first pack with its index, but not for the second after it.
    history = copy_of(tmp_path, storage, "two", packs=listed + index - 50_000)
    read = read_from(storage, monkeypatch)
    for asked in range(2):
        with pytest.raises(HistoryError) as refused:
            await history.sync()
        assert said(refused.value) == TOO_LARGE
        assert [key for key in read if key.endswith(".pack")] == [f"{many}.pack"], asked
    assert [pack.stem for pack in packs_of(history)] == [many]
    assert module._size(history.clone) == on_disk(history.clone) <= listed + index - 50_000


async def test_the_copies_together_are_counted_by_what_each_holds_on_disk(tmp_path, storage, monkeypatch):
    packs = {}
    for name in ("older", "newer", "newest"):
        durable = by_hand(storage, name)
        packs[name] = durable / "objects" / "pack" / f"{a_pack(durable / 'objects' / 'pack', (struct.pack('>I', n) for n in range(20_000)))}.pack"
    in_bucket = packs["older"].stat().st_size
    older = copy_of(tmp_path, storage, "older")
    await older.sync()
    held = module._size(older.clone)
    assert held > 3 * in_bucket

    def there() -> list[str]:
        return sorted(copy.name for copy in (tmp_path / "api").iterdir() if copy.is_dir())

    os.utime(older.clone / "HEAD", (time.time() - 300, time.time() - 300))
    # Room for two by the bucket's bytes, and for one by what a copy holds once its index is made: the older goes.
    newer = copy_of(tmp_path, storage, "newer", copies=held + held // 2)
    await newer.sync()
    assert there() == ["newer"]
    # No room for another pack even as the bucket lists it: the room is made before the pack is read.
    os.utime(newer.clone / "HEAD", (time.time() - 300, time.time() - 300))
    read, before = storage.download, []

    async def download(bucket_name, key, target, **limit):
        if key.endswith(".pack"):
            before.append(there())
        return await read(bucket_name, key, target, **limit)

    monkeypatch.setattr(storage, "download", download)
    await copy_of(tmp_path, storage, "newest", copies=held + in_bucket // 2).sync()
    assert (before, there()) == ([["newest"]], ["newest"])


async def test_a_copy_that_is_in_is_asked_without_the_copies_being_measured(tmp_path, storage, project, monkeypatch):
    landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    v2 = blob_of(b"PK\x03\x04 report v2")
    here, other = bucket(tmp_path, storage), BucketHistory(storage, "agent", PREFIX, tmp_path / "api" / "other")
    for history in (here, other, here):
        assert await history.held([v2]) == {v2}
    measured, size = [], module._size

    def counted(folder):
        measured.append(folder)
        return size(folder)

    monkeypatch.setattr(module, "_size", counted)
    assert await here.held([v2]) == {v2}
    assert measured == []
    # One not used for a while still goes, at any request.
    os.utime(other.clone / "HEAD", (time.time() - 700, time.time() - 700))
    assert await here.held([v2]) == {v2}
    assert (measured, other.clone.exists()) == ([], False)


async def test_what_a_request_that_was_killed_left_in_a_copy_goes_with_the_next_and_is_counted_until_then(tmp_path, storage, project):
    landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    history = bucket(tmp_path, storage)
    await history.sync()
    held = module._size(history.clone)
    # As a process killed while it brought a pack in leaves it.
    left = history.clone / "scratch-left"
    left.mkdir()
    (left / "pack-half.pack").write_bytes(os.urandom(2**20))
    assert module._size(history.clone) == held + 2**20
    await history.sync()
    assert not left.exists() and module._size(history.clone) == held


async def test_a_pruned_history_is_read_where_its_new_pack_fits_only_once_the_packs_it_replaced_are_gone(tmp_path, storage, project):
    one = landed(tmp_path, project, "saga:1", {"Report.docx": os.urandom(2**20)})
    landed(tmp_path, project, "saga:2", {"notes.txt": os.urandom(2**20)})
    kept = git(project / "_history", "rev-parse", f"{one}:Report.docx")
    history = bucket(tmp_path, storage, packs=3 * 2**20 + 2**19)
    assert await history.held([kept]) == {kept}
    before = {pack.name for pack in packs_of(history)}
    # The pruning's one pack holds what the two did: the copy cannot hold the old and the new together.
    cut_history(tmp_path, project / "_history", kept=2)
    assert module._size(history.clone) + sum(p.stat().st_size for p in (project / "_history" / "objects" / "pack").glob("*.pack")) > 3 * 2**20 + 2**19
    assert await history.held([kept]) == {kept}
    assert before.isdisjoint(pack.name for pack in packs_of(history)) and len(packs_of(history)) == 1


async def test_a_copy_already_in_is_read_while_every_turn_to_bring_a_copy_in_is_taken(tmp_path, storage, project, monkeypatch):
    landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    v2 = blob_of(b"PK\x03\x04 report v2")
    here = bucket(tmp_path, storage)
    assert await here.held([v2]) == {v2}
    # Other projects' first reads, each with a pack that takes its time to bring in.
    release, run = threading.Event(), subprocess.run

    def slow(command, **how):
        if "index-pack" in command:
            release.wait(20)
        return run(command, **how)

    monkeypatch.setattr(module.subprocess, "run", slow)
    monkeypatch.setattr(module, "_PATIENCE", 1.0)
    coming = [asyncio.create_task(BucketHistory(storage, "agent", PREFIX, tmp_path / "api" / f"other-{n}").sync()) for n in range(6)]
    await asyncio.sleep(0.5)
    began = time.monotonic()
    try:
        assert await here.held([v2]) == {v2}
        assert time.monotonic() - began < 0.9
    finally:
        release.set()
        await asyncio.gather(*coming, return_exceptions=True)


async def test_one_project_is_read_by_one_git_at_a_time_and_another_meanwhile(tmp_path, storage, project, monkeypatch):
    landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    v2 = blob_of(b"PK\x03\x04 report v2")
    one, other = bucket(tmp_path, storage), BucketHistory(storage, "agent", PREFIX, tmp_path / "api" / "other")
    for history in (one, other):
        await history.sync()
    reading, most, gate, run = {}, {}, threading.Lock(), subprocess.run

    def counted(command, **how):
        copy = how["env"]["GIT_DIR"]
        with gate:
            reading[copy] = reading.get(copy, 0) + 1
            most[copy] = max(most.get(copy, 0), reading[copy])
            most["together"] = max(most.get("together", 0), sum(reading.values()))
        try:
            time.sleep(0.05)
            return run(command, **how)
        finally:
            with gate:
                reading[copy] -= 1

    monkeypatch.setattr(module.subprocess, "run", counted)
    # Six readers of one project, as many tabs of one user, and one of another.
    answers = await asyncio.gather(*(one.held([v2]) for _ in range(6)), other.held([v2]))
    assert answers == [{v2}] * 7
    assert (most[str(one.clone)], most[str(other.clone)], most["together"]) == (1, 1, 2)


async def test_a_pack_that_takes_longer_to_bring_in_than_a_pack_may_is_stopped_refused_in_words_and_not_tried_at_every_request(
    tmp_path, storage, project, monkeypatch,
):
    landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    durable = by_hand(storage, "slow")
    # A megabyte in the bucket that is most of a gigabyte to git: one file of zeros.
    name = a_pack(durable / "objects" / "pack", zeros=768 * 2**20)
    for limit in ("_TAKE_LEAST", "_TAKE_MOST"):
        monkeypatch.setattr(module, limit, 0.4, raising=False)
    history = copy_of(tmp_path, storage, "slow")
    began = time.monotonic()
    with pytest.raises(HistoryError) as refused:
        await history.sync()
    assert said(refused.value) == TOO_LARGE
    assert time.monotonic() - began < 2.5
    # Git was stopped, and is gone: nothing of this pack still runs.
    running = [Path(f"/proc/{pid}/cmdline").read_bytes() for pid in os.listdir("/proc") if pid.isdigit() and Path(f"/proc/{pid}/cmdline").exists()]
    assert not [command for command in running if name.encode() in command]
    # The copy holds nothing of it but a mark, which is no mark of a pack git cannot read.
    assert sorted(left.suffix for left in (history.clone / "objects" / "pack").iterdir()) == [".slow"]
    # Its lock and its turn are given back: the copy is no one's, and another project is read at once.
    held = os.open(module._lock_of(history.clone), os.O_RDWR)
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.close(held)
    v2 = blob_of(b"PK\x03\x04 report v2")
    assert await bucket(tmp_path, storage).held([v2]) == {v2}
    # Asked again, it is refused at once: the pack is not read again, and git is not run on it.
    read, began = read_from(storage, monkeypatch), time.monotonic()
    with pytest.raises(HistoryError) as refused:
        await history.held([v2])
    assert (said(refused.value), [key for key in read if key.endswith(".pack")]) == (TOO_LARGE, [])
    assert time.monotonic() - began < 0.3
    # It is tried again once a while has passed, in case the api was only busy: stopped again, and marked anew.
    mark = history.clone / "objects" / "pack" / f"{name}.slow"
    os.utime(mark, (time.time() - 700, time.time() - 700))
    with pytest.raises(HistoryError) as refused:
        await history.sync()
    assert (said(refused.value), [key for key in read if key.endswith(".pack")]) == (TOO_LARGE, [f"{name}.pack"])
    assert time.time() - mark.stat().st_mtime < 30
    # And at once where a pack is given more time than it was stopped at: with the time it needs, it is read.
    monkeypatch.setattr(module, "_TAKE_LEAST", 120.0)
    monkeypatch.setattr(module, "_TAKE_MOST", 120.0)
    assert await history.sync() == "1" * 40
    assert sorted(left.suffix for left in (history.clone / "objects" / "pack").iterdir()) == [".bucket", ".idx", ".pack"]


def test_a_pack_has_seconds_to_be_brought_in_by_what_the_bucket_holds_of_it():
    # Twenty for nearly every pack; for one of gigabytes what a slow disk needs to read it; two minutes at most.
    assert [module._seconds(size) for size in (0, 2**20, 640 * 2**20, 2**30, 2 * 2**30, 4 * 2**30, 64 * 2**30)] == [
        20.0, 20.0, 20.0, 32.0, 64.0, 120.0, 120.0,
    ]
    # What an index holds of a pack's objects, each at its place; and a longer place in a pack past two gigabytes.
    for objects, size, index in ((0, 32, 1072), (1, 100, 1100), (1_000_000, 13_000_000, 28_001_072), (3, 2**31, 1072 + 3 * 36)):
        with tempfile.NamedTemporaryFile() as pack:
            pack.write(b"PACK" + struct.pack(">II", 2, objects))
            pack.flush()
            assert module._index_size(Path(pack.name), size) == index


async def test_a_pack_whose_turn_did_not_come_is_brought_in_later_never_left_out(tmp_path, storage, project, monkeypatch):
    landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    history = bucket(tmp_path, storage)
    await history.sync()
    landed(tmp_path, project, "saga:2", {"Report.docx": b"PK\x03\x04 report v3"})
    v3 = blob_of(b"PK\x03\x04 report v3")
    # Every turn to bring a pack in is another project's, for longer than a request waits.
    release = threading.Event()
    taken = [module._BRINGING.submit(release.wait, 20) for _ in range(module._BRING_SLOTS)]
    monkeypatch.setattr(module, "_PATIENCE", 0.3)
    try:
        with pytest.raises(Busy, match="being read just now"):
            await history.held([v3])
    finally:
        release.set()
        for turn in taken:
            turn.result()
    assert list((history.clone / "objects" / "pack").glob("*.bad")) == []
    assert await history.held([v3]) == {v3}


async def test_a_question_git_takes_too_long_over_is_stopped_and_said_to_have_failed(tmp_path, storage, project, monkeypatch):
    landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    v2 = blob_of(b"PK\x03\x04 report v2")
    history = bucket(tmp_path, storage)
    assert await history.held([v2]) == {v2}
    run = subprocess.run

    def stuck(command, **how):
        # As git on a disk that does not answer: it reads its question and never says a word.
        return run(["sleep", "30"], **{**how, "input": None}) if "cat-file" in command else run(command, **how)

    monkeypatch.setattr(module.subprocess, "run", stuck)
    monkeypatch.setattr(module, "_READ_SECONDS", 0.3, raising=False)
    began = time.monotonic()
    with pytest.raises(HistoryError, match="git cat-file took longer than") as refused:
        await history.held([v2])
    assert said(refused.value) is None and not isinstance(refused.value, Busy)
    assert time.monotonic() - began < 2
    monkeypatch.setattr(module.subprocess, "run", run)
    assert await history.held([v2]) == {v2}



async def test_a_version_is_written_to_a_file_of_the_copy_and_is_gone_once_it_is_sent(tmp_path, storage, project, monkeypatch):
    one = landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    landed(tmp_path, project, "saga:2", {"Report.docx": b"PK\x03\x04 report v3"})
    v1, v2 = (git(project / "_history", "rev-parse", f"{one}{side}:Report.docx") for side in ("^1", ""))
    history = bucket(tmp_path, storage)
    await history.sync()
    held = module._size(history.clone)
    on_the_loop = let_go_on_the_loop(monkeypatch)
    staged = await history.version(v2)
    # A file of the copy's own, counted with all else the copy holds until it is sent.
    [file] = staged_in(history)
    assert (staged.size, file.read_bytes(), file.stat().st_mode & 0o777) == (14, b"PK\x03\x04 report v2", 0o600)
    assert module._size(history.clone) == held + 14
    assert await sent(staged) == b"PK\x03\x04 report v2"
    assert (staged_in(history), module._size(history.clone)) == ([], held)
    # Let go off the loop: a large file takes its time to leave a disk.
    assert on_the_loop == [False]
    # The file as it was before its first landing is a version too; and one let go unsent is gone as well.
    assert await sent(await history.version(v1)) == b"PK\x03\x04 report v1"
    (await history.version(v2)).close()
    assert staged_in(history) == []
    # A version is a file's bytes: a commit's id names none, nor does an id the history never held.
    for other in (one, "0" * 40):
        with pytest.raises(NotKept) as gone:
            await history.version(other)
        assert str(gone.value) == "This version is no longer kept in the project's history."
    # An empty file is a version: nothing to send, and it is sent.
    empty = landed(tmp_path, project, "saga:3", {"notes.txt": b""})
    assert await sent(await history.version(git(project / "_history", "rev-parse", f"{empty}:notes.txt"))) == b""
    assert (staged_in(history), no_ones(history)) == ([], True)


async def test_a_pruned_version_is_kept_no_more(tmp_path, storage, project):
    one = landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    landed(tmp_path, project, "saga:2", {"Report.docx": b"PK\x03\x04 report v3"})
    v2 = git(project / "_history", "rev-parse", f"{one}:Report.docx")
    history = bucket(tmp_path, storage)
    assert await sent(await history.version(v2)) == b"PK\x03\x04 report v2"
    cut_history(tmp_path, project / "_history", kept=1)
    with pytest.raises(NotKept, match="This version is no longer kept in the project's history."):
        await history.version(v2)
    # Said as it is to the user by its own words, never as a bound's refusal; and nothing was written for it.
    assert said(NotKept("This version is no longer kept in the project's history.")) is None
    assert staged_in(history) == []
    assert await sent(await history.version(blob_of(b"PK\x03\x04 report v3"))) == b"PK\x03\x04 report v3"


async def test_a_version_that_is_no_id_never_reaches_git(tmp_path, storage, project, monkeypatch):
    landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    history = bucket(tmp_path, storage)
    await history.sync()
    ran = []
    monkeypatch.setattr(module.subprocess, "run", lambda command, **how: ran.append(command))
    for crafted in ("--batch-all-objects", "refs/heads/main", "main:Report.docx", "HEAD", "1" * 39, "A" * 40, f"{'1' * 40}\n"):
        with pytest.raises(HistoryError, match="refused the project's history: a version holds what is not a commit id"):
            await history.version(crafted)
    assert ran == []


async def test_a_version_never_passes_through_the_apis_memory_whole(tmp_path, storage, project, monkeypatch):
    data = os.urandom(64 * 2**20)  # 64 MiB that does not compress
    landed(tmp_path, project, "saga:1", {"Report.docx": data})
    report, digest = blob_of(data), hashlib.sha256(data).hexdigest()
    del data
    whole = []

    async def read(bucket_name, key):
        whole.append(key)  # the backend's read answers an object whole
        return await LocalBackend.read(storage, bucket_name, key)

    monkeypatch.setattr(storage, "read", read)
    history = bucket(tmp_path, storage)
    await history.sync()
    taken, largest = hashlib.sha256(), 0

    async def take(piece: bytes) -> None:
        nonlocal largest
        taken.update(piece)
        largest = max(largest, len(piece))

    tracemalloc.start()
    staged = await history.version(report)
    await staged.send(take)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    # 64 MiB written out and sent: a piece of it held at a time.
    assert (staged.size, taken.hexdigest()) == (64 * 2**20, digest)
    assert largest == 2**20 and peak < 8 * 2**20, peak
    assert (whole, staged_in(history)) == ([], [])


async def test_a_version_larger_than_one_may_be_or_than_the_copy_has_room_for_is_refused_in_words_and_none_of_it_is_written(
    tmp_path, storage, project,
):
    data = os.urandom(2**20)
    landed(tmp_path, project, "saga:1", {"Report.docx": data})
    report = blob_of(data)
    history = bucket(tmp_path, storage)
    await history.sync()
    held = module._size(history.clone)
    too_large = "This version is larger than Surogate can read here."
    # A version's own bound, and the copy's, with all it holds: each a byte short.
    for bounds in ({"file": 2**20 - 1}, {"packs": held + 2**20 - 1}):
        with pytest.raises(HistoryError) as refused:
            await bucket(tmp_path, storage, **bounds).version(report)
        assert (said(refused.value), staged_in(history)) == (too_large, [])
    # At its bound it is read; and a second, for which the copy has room once the first has gone, is told to try again.
    exact = bucket(tmp_path, storage, file=2**20, packs=held + 2**20)
    first = await exact.version(report)
    with pytest.raises(Busy, match="being read just now"):
        await exact.version(report)
    assert len(staged_in(history)) == 1
    assert await sent(first) == data
    assert await sent(await exact.version(report)) == data
    assert (staged_in(history), no_ones(history)) == ([], True)


async def test_a_version_the_copies_together_have_no_room_for_waits_for_room_where_no_other_copy_can_go(tmp_path, storage, project):
    data = os.urandom(2**20)
    landed(tmp_path, project, "saga:1", {"Report.docx": data})
    report = blob_of(data)

    def copy_of(name: str, **bounds: float) -> BucketHistory:
        return BucketHistory(storage, "agent", PREFIX, tmp_path / "api" / name, Bounds(**bounds))

    here, other = copy_of("here"), copy_of("other")
    for history in (here, other):
        await history.sync()
    held, ago = module._size(here.clone), time.time() - 120
    # Room for the two copies, a byte short of the version beside them.  The other was used just now: it is not
    # removed for this, and the disk is not used past the bound either.  The version waits.
    tight = copy_of("here", copies=2 * held + 2**20 - 1)
    with pytest.raises(Busy, match="being read just now"):
        await tight.version(report)
    assert (staged_in(here), other.clone.exists()) == ([], True)
    # Not used for a minute, the other goes, and the version is written out.
    os.utime(other.clone / "HEAD", (ago, ago))
    assert await sent(await tight.version(report)) == data
    assert not other.clone.exists()
    # A copy with a version of its own on its way is in use, however long ago it was asked: it is never removed.
    await other.sync()
    sending = await other.version(report)
    os.utime(other.clone / "HEAD", (ago, ago))
    with pytest.raises(Busy, match="being read just now"):
        await copy_of("here", copies=2 * held + 2 * 2**20 - 1).version(report)
    assert (staged_in(here), other.clone.exists(), len(staged_in(other))) == ([], True, 1)
    assert await sent(await copy_of("here", copies=2 * held + 2 * 2**20).version(report)) == data
    # And a third copy, one that can go, goes for it though the copies not in use would fit without that.
    third = copy_of("third")
    await third.sync()
    os.utime(third.clone / "HEAD", (ago, ago))
    assert await sent(await copy_of("here", copies=2 * held + 2 * 2**20).version(report)) == data
    assert (third.clone.exists(), other.clone.exists()) == (False, True)
    assert await sent(sending) == data
    # A version the copies could not hold with its own copy alone is not one to wait for.
    with pytest.raises(HistoryError) as refused:
        await copy_of("here", copies=held + 2**20 - 1).version(report)
    assert said(refused.value) == "This version is larger than Surogate can read here."


async def test_a_pack_the_copy_has_room_for_once_a_version_on_its_way_out_has_gone_is_told_to_try_again(tmp_path, storage, project):
    data = os.urandom(2**20)
    landed(tmp_path, project, "saga:1", {"Report.docx": data})
    history = bucket(tmp_path, storage)
    await history.sync()
    held = module._size(history.clone)
    staged = await history.version(blob_of(data))
    landed(tmp_path, project, "saga:2", {"notes.txt": os.urandom(2**19)})
    # Room for the version or for the new pack, not for both.
    tight = bucket(tmp_path, storage, packs=held + 2**20 + 2**18)
    with pytest.raises(Busy, match="being read just now"):
        await tight.sync()
    assert await sent(staged) == data
    assert await tight.sync() is not None and len(packs_of(history)) == 4


async def test_a_version_a_request_that_died_left_is_counted_until_the_next_request_removes_it_and_one_being_sent_is_left(
    tmp_path, storage, project,
):
    data = os.urandom(2**20)
    landed(tmp_path, project, "saga:1", {"Report.docx": data})
    report = blob_of(data)
    history = bucket(tmp_path, storage)
    await history.sync()
    held = module._size(history.clone)
    # The api, killed with a version on its way: another process of this code, on this copy.
    dies = (
        "import asyncio, os, signal, sys\n"
        "from pathlib import Path\n"
        "from surogates.storage.backend import LocalBackend\n"
        "from surogates.workstreams.bucket import BucketHistory\n"
        "root, prefix, clone, blob = sys.argv[1:]\n"
        "staged = asyncio.run(BucketHistory(LocalBackend(root), 'agent', prefix, Path(clone)).version(blob))\n"
        "os.kill(os.getpid(), signal.SIGKILL)\n"
    )
    died = await asyncio.to_thread(
        subprocess.run, [sys.executable, "-c", dies, str(tmp_path / "buckets"), PREFIX, str(history.clone), report], capture_output=True,
    )
    assert died.returncode == -signal.SIGKILL, died.stderr.decode()
    [left] = staged_in(history)
    assert module._size(history.clone) == held + 2**20
    # One this process is sending is no one's leftover: any request that has the copy removes the first and leaves the second.
    sending = await history.version(report)
    assert left not in staged_in(history) and len(staged_in(history)) == 1
    assert await history.held([report]) == {report}
    assert len(staged_in(history)) == 1 and module._size(history.clone) == held + 2**20
    assert await sent(sending) == data
    assert (staged_in(history), module._size(history.clone)) == ([], held)


async def test_a_version_whose_request_left_while_it_was_written_is_gone_once_git_ends_and_the_copy_is_git_s_until_then(
    tmp_path, storage, project, monkeypatch,
):
    landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    v2 = blob_of(b"PK\x03\x04 report v2")
    history = bucket(tmp_path, storage)
    await history.sync()
    writing, release, run = threading.Event(), threading.Event(), subprocess.run

    def slow(command, **how):
        if "blob" in command:
            writing.set()
            release.wait(20)
        return run(command, **how)

    monkeypatch.setattr(module.subprocess, "run", slow)
    on_the_loop = let_go_on_the_loop(monkeypatch)
    request = asyncio.create_task(history.version(v2))
    assert await asyncio.to_thread(writing.wait, 20)
    # Its client went away.
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    # Git, once begun, ends in its own time, the copy still its own: one project runs one git at a time.
    assert (len(staged_in(history)), no_ones(history)) == (1, False)
    release.set()
    for _ in range(200):
        if not staged_in(history) and no_ones(history) and not handles_of_versions():
            break
        await asyncio.sleep(0.05)
    # What it wrote was no one's to send: gone, and no handle of it is left open in the api.
    assert (staged_in(history), no_ones(history), handles_of_versions()) == ([], True, [])
    assert (history.clone in module._USING, on_the_loop) == (False, [False])
    monkeypatch.setattr(module.subprocess, "run", run)
    assert await sent(await history.version(v2)) == b"PK\x03\x04 report v2"


async def test_a_version_is_written_in_a_turn_that_brings_copies_in_one_git_a_project_and_a_copy_that_is_in_is_asked_meanwhile(
    tmp_path, storage, project, monkeypatch,
):
    landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    v2 = blob_of(b"PK\x03\x04 report v2")
    copies = [BucketHistory(storage, "agent", PREFIX, tmp_path / "api" / f"copy-{n}") for n in range(4)]
    for copy in copies:
        await copy.sync()
    reading, most, turns, gate = {}, {}, set(), threading.Lock()
    release, run = threading.Event(), subprocess.run

    def counted(command, **how):
        copy, writes = how["env"]["GIT_DIR"], "blob" in command
        with gate:
            reading[copy] = reading.get(copy, 0) + 1
            most[copy] = max(most.get(copy, 0), reading[copy])
            if writes:
                turns.add(threading.current_thread().name.rsplit("_", 1)[0])
        try:
            if writes:
                release.wait(20)
            return run(command, **how)
        finally:
            with gate:
                reading[copy] -= 1

    monkeypatch.setattr(module.subprocess, "run", counted)
    monkeypatch.setattr(module, "_PATIENCE", 1.0)
    # A version of each of two projects being written, for as long as a slow disk takes.
    writing = [asyncio.create_task(copy.version(v2)) for copy in copies[: module._BRING_SLOTS]]
    await asyncio.sleep(0.3)
    began = time.monotonic()
    try:
        # A copy that is in is asked at once: no version being written takes a turn of its.
        assert await copies[-1].held([v2]) == {v2}
        assert time.monotonic() - began < 0.9
        # Its own project waits for it: the copy is that git's alone.
        with pytest.raises(Busy, match="being read just now"):
            await copies[0].held([v2])
        # And a third version finds no turn within what a request waits, and is told so, with nothing left of it.
        with pytest.raises(Busy, match="being read just now"):
            await copies[-1].version(v2)
        assert staged_in(copies[-1]) == []
    finally:
        release.set()
    for staged in await asyncio.gather(*writing):
        assert await sent(staged) == b"PK\x03\x04 report v2"
    assert turns == {"history-bring"}
    assert max(most.values()) == 1
    assert (module._BRING_SLOTS + module._READ_SLOTS) * module._GIT_MEMORY == 2**30


async def test_a_version_git_takes_longer_to_write_than_it_may_is_stopped_and_refused_in_words(tmp_path, storage, project, monkeypatch):
    landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    v2 = blob_of(b"PK\x03\x04 report v2")
    history = bucket(tmp_path, storage)
    await history.sync()
    run = subprocess.run

    def stuck(command, **how):
        # As git on a disk that does not answer.
        return run(["sleep", "30"], **how) if "blob" in command else run(command, **how)

    monkeypatch.setattr(module.subprocess, "run", stuck)
    for limit in ("_TAKE_LEAST", "_TAKE_MOST"):
        monkeypatch.setattr(module, limit, 0.3)
    began = time.monotonic()
    with pytest.raises(HistoryError) as refused:
        await history.version(v2)
    assert said(refused.value) == "This version is larger than Surogate can read here."
    assert time.monotonic() - began < 2.5
    assert (staged_in(history), no_ones(history)) == ([], True)


async def test_a_version_git_wrote_out_short_is_not_sent_as_if_it_were_whole(tmp_path, storage, project, monkeypatch):
    landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    v2 = blob_of(b"PK\x03\x04 report v2")
    history = bucket(tmp_path, storage)
    await history.sync()
    run = subprocess.run

    def cut(command, **how):
        done = run(command, **how)
        if "blob" in command:
            os.ftruncate(how["stdout"], 5)  # as a disk that filled under it, with git none the wiser
        return done

    monkeypatch.setattr(module.subprocess, "run", cut)
    with pytest.raises(HistoryError, match="at another size than the history holds it at") as refused:
        await history.version(v2)
    assert (said(refused.value), isinstance(refused.value, NotKept), staged_in(history), no_ones(history)) == (None, False, [], True)


async def test_a_version_git_stored_as_a_change_to_another_is_written_whole_and_refused_in_words_past_gits_memory(
    tmp_path, storage, project, monkeypatch,
):
    first, second = a_table(24, 1), a_table(24, 2)
    landed(tmp_path, project, "saga:1", {"sales.csv": first})
    landed(tmp_path, project, "saga:2", {"sales.csv": second})
    # As a pruning leaves a history: one pack, a version of the table kept as what it changes of the other.
    cut_history(tmp_path, project / "_history", kept=2)
    [pack] = (project / "_history" / "objects" / "pack").glob("*.pack")
    assert pack.stat().st_size < len(first)
    history = bucket(tmp_path, storage)
    for data in (first, second):
        staged = await history.version(blob_of(data))
        assert (staged.size, hashlib.sha256(await sent(staged)).digest()) == (len(data), hashlib.sha256(data).digest())
    # Git holds such a version whole, with the one it changes: within less memory than that it stops, and it is said.
    monkeypatch.setattr(module, "_GIT_MEMORY", 56 * 2**20)
    refusals = []
    for data in (first, second):
        try:
            (await history.version(blob_of(data))).close()
        except HistoryError as refused:
            refusals.append(said(refused))
    assert refusals and set(refusals) == {"This version is larger than Surogate can read here."}
    assert (staged_in(history), no_ones(history)) == ([], True)


async def test_a_version_is_sent_within_its_seconds_and_gone_however_its_sending_ends(tmp_path, storage, project, monkeypatch):
    data = os.urandom(2**18)
    landed(tmp_path, project, "saga:1", {"Report.docx": data})
    report = blob_of(data)
    history = bucket(tmp_path, storage)
    # Two minutes for nearly every version; for one of gigabytes what a slow line needs.
    assert [module._send_seconds(size) for size in (0, 2**20, 120 * 2**20, 2**30)] == [120.0, 120.0, 120.0, 1024.0]

    async def never(piece: bytes) -> None:
        await asyncio.sleep(30)

    async def left(piece: bytes) -> None:
        raise OSError("the client closed its connection")

    # Who takes none of it in the seconds it has is taking none: it is stopped, and said to have been.
    with monkeypatch.context() as patched:
        patched.setattr(module, "_SEND_LEAST", 0.3)
        staged, began = await history.version(report), time.monotonic()
        with pytest.raises(Slow, match="took longer than"):
            await staged.send(never)
        assert time.monotonic() - began < 2
        assert staged_in(history) == []
        del staged
    # A client that left, by its connection or by its request being ended.
    with pytest.raises(OSError, match="closed its connection"):
        await (await history.version(report)).send(left)
    assert staged_in(history) == []
    sending = asyncio.create_task((await history.version(report)).send(never))
    await asyncio.sleep(0.1)
    sending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sending
    assert staged_in(history) == []
    # A version no one sent is gone with the last hold on it: the loop's own, of what it handed over, goes as it turns.
    staged = await history.version(report)
    assert len(staged_in(history)) == 1
    del staged
    await asyncio.sleep(0)
    assert staged_in(history) == []
    # Nothing of these keeps the copy in use, and no handle of them is left open.
    assert (history.clone in module._USING, handles_of_versions()) == (False, [])


# ----------------------------------------------------------------------
# The copy's write side: a landing's steps, run here as a pod runs them
# ----------------------------------------------------------------------

YOU = {"name": "u1", "email": "user:u1@surogate"}


def by_saga(saga: str, kind: str) -> list[list[str]]:
    return [["Surogate-Saga", saga], ["Surogate-Kind", kind]]


def left_in(history: BucketHistory) -> list[str]:
    """What the copy holds beside the bucket's own packs: an object of its own, or a file an act left on its way."""
    objects = history.clone / "objects"
    return sorted(str(p.relative_to(history.clone)) for p in (
        *(p for p in objects.rglob("*") if p.is_file() and p.parent != objects / "pack"),
        *(p for p in (objects / "pack").glob("*.pack") if not p.with_suffix(".bucket").exists()),
        *history.clone.glob("scratch-*"), *staged_in(history),
    ))


async def picked_up(history: BucketHistory, paths: list[str], saga: str = "saga:p") -> dict:
    """Your edits to *paths*, looked for and then pushed, as a Restore picks them up."""
    looked = await history.edits(paths)
    return await history.pickup(main=looked["main"], picked_up=looked["picked_up"], author=YOU, trailers=by_saga(saga, "pickup"))


async def test_a_look_tells_your_edits_to_the_files_asked_about_and_keeps_none_of_them(tmp_path, storage, project):
    one = landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    (project / "Report.docx").write_bytes(b"PK\x03\x04 report v2, edited by you")
    (project / "notes.txt").write_text("v2 notes, saved by you\n")
    (project / "new.md").write_text("made by you\n")
    history = bucket(tmp_path, storage)
    looked = await history.edits(["new.md", "Report.docx", "gone.md", "Report.docx"])
    # Only the files asked about, each once and in order, and only where they changed: the rest are the next landing's to pick up.
    assert looked == {"main": one, "picked_up": [
        {"path": "Report.docx", "before": blob_of(b"PK\x03\x04 report v2"), "after": blob_of(b"PK\x03\x04 report v2, edited by you")},
        {"path": "new.md", "before": None, "after": blob_of(b"made by you\n")},
    ]}
    # A file you deleted is an edit too.
    (project / "notes.txt").unlink()
    assert (await history.edits(["notes.txt"]))["picked_up"] == [{"path": "notes.txt", "before": blob_of(b"v1 notes\n"), "after": None}]
    # Nothing of them is kept: the history is as the bucket has it, and the copy holds nothing of its own.
    assert git(project / "_history", "rev-parse", MAIN) == one
    assert left_in(history) == []
    # Before a project's first landing there is no main to tell an edit from.
    assert await copy_of(tmp_path, storage, "empty").edits(["a.md"]) == {"main": None, "picked_up": []}


async def test_a_pickup_records_your_edits_on_main_before_a_file_of_them_is_written_over(tmp_path, storage, project):
    one = landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    (project / "Report.docx").write_bytes(b"PK\x03\x04 report v2, edited by you")
    (project / "notes.txt").unlink()
    history = bucket(tmp_path, storage)
    looked = await history.edits(["Report.docx", "notes.txt"])
    asked = {"main": looked["main"], "picked_up": looked["picked_up"], "author": YOU, "trailers": by_saga("saga:p", "pickup")}
    picked = await history.pickup(**asked)
    durable = project / "_history"
    # As a pod's pickup answers: main as it found it, its commit, and its files.
    assert (picked["main"], picked["picked_up"]) == (one, looked["picked_up"])
    assert git(durable, "rev-parse", MAIN) == picked["commit"]
    assert git(durable, "log", "-1", "--format=%an <%ae>|%P|%s|%(trailers:key=Surogate-Kind,valueonly)", picked["commit"]) == (
        f"u1 <user:u1@surogate>|{one}|Your changes|pickup"
    )
    assert git(durable, "show", f"{picked['commit']}:Report.docx") == "PK\x03\x04 report v2, edited by you"
    assert git(durable, "ls-tree", "--name-only", picked["commit"]).splitlines() == ["Report.docx"]
    assert git(durable, "fsck", "--no-dangling") == ""
    # Safe to repeat after a lost answer: the pickup is found by its saga, and nothing more goes up.
    pushed = sorted(p.name for p in (durable / "objects" / "pack").iterdir())
    assert await history.pickup(**asked) == picked
    assert sorted(p.name for p in (durable / "objects" / "pack").iterdir()) == pushed
    # The copy keeps the pack it pushed as one of the bucket's, and nothing else of the pickup.
    assert left_in(history) == []
    # A thread's next pod opens on it.
    copy = a_pod(tmp_path, project).copy
    assert (copy / "Report.docx").read_bytes() == b"PK\x03\x04 report v2, edited by you" and not (copy / "notes.txt").exists()


async def test_a_pickup_pushes_nothing_where_a_file_was_saved_again_since_it_was_looked_at_or_main_moved(tmp_path, storage, project):
    one = landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    (project / "Report.docx").write_bytes(b"PK\x03\x04 report v2, edited by you")
    history = bucket(tmp_path, storage)
    looked = await history.edits(["Report.docx", "notes.txt"])
    asked = {"main": looked["main"], "picked_up": looked["picked_up"], "author": YOU, "trailers": by_saga("saga:p", "pickup")}
    durable = project / "_history"
    pushed = sorted(p.name for p in (durable / "objects" / "pack").iterdir())
    # What the pickup pushes is what the look told of, which a row holds by then: a later save is the next look's.
    (project / "Report.docx").write_bytes(b"PK\x03\x04 report v2, saved again")
    with pytest.raises(HistoryConflict, match="Report.docx changed while it was picked up"):
        await history.pickup(**asked)
    (project / "Report.docx").unlink()
    with pytest.raises(HistoryConflict, match="Report.docx changed while it was picked up"):
        await history.pickup(**asked)
    assert git(durable, "rev-parse", MAIN) == one
    assert sorted(p.name for p in (durable / "objects" / "pack").iterdir()) == pushed and left_in(history) == []
    # And main as the look found it: another landing went first, with the lock lost unseen.
    (project / "Report.docx").write_bytes(b"PK\x03\x04 report v2, edited by you")
    two = landed(tmp_path, project, "saga:2", {"notes.txt": b"by another thread\n"})
    with pytest.raises(HistoryConflict, match="main moved"):
        await history.pickup(**asked)
    assert git(durable, "rev-parse", MAIN) == two


async def test_an_apply_and_its_record_land_on_main_and_the_next_pod_has_them(tmp_path, storage, project):
    one = landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    v1 = git(project / "_history", "rev-parse", f"{one}^1:Report.docx")
    history = bucket(tmp_path, storage)
    main = (await history.fetch())["main"]
    now = await history.real("Report.docx")
    assert (now, await history.recorded(main, "Report.docx"), await history.recorded(main, "no-such.md")) == (
        blob_of(b"PK\x03\x04 report v2"), now, None,
    )
    applied = await history.apply("Report.docx", now, v1)
    assert applied == {"path": "Report.docx", "before": now, "after": v1, "made": []}
    assert (project / "Report.docx").read_bytes() == b"PK\x03\x04 report v1"
    # Safe to repeat after a lost answer: a file that is already the version is left as it is.
    assert await history.apply("Report.docx", now, v1) == applied
    record = {"applied": [applied], "author": YOU, "trailers": by_saga("saga:r", "restore"), "main": main}
    restored = (await history.record(**record))["commit"]
    # And so is the record: the landing is found by its saga.
    assert (await history.record(**record))["commit"] == restored
    durable = project / "_history"
    assert git(durable, "log", "-1", "--format=%ae|%s|%P|%(trailers:key=Surogate-Kind,valueonly)", restored) == (
        f"user:u1@surogate|Restore|{main}|restore"
    )
    assert git(durable, "rev-parse", MAIN, f"{restored}:Report.docx").split() == [restored, v1]
    assert git(durable, "fsck", "--no-dangling") == ""
    assert left_in(history) == []
    # A thread's next pod opens on it.
    assert (a_pod(tmp_path, project).copy / "Report.docx").read_bytes() == b"PK\x03\x04 report v1"


async def test_a_version_the_history_already_holds_is_not_sent_to_the_bucket_again(tmp_path, storage, project):
    old = os.urandom(2**20)
    one = landed(tmp_path, project, "saga:1", {"Report.docx": old})
    landed(tmp_path, project, "saga:2", {"Report.docx": b"PK\x03\x04 report v3"})
    history = bucket(tmp_path, storage)
    main = (await history.fetch())["main"]
    packs = project / "_history" / "objects" / "pack"
    before = {p.name for p in packs.glob("*.pack")}
    applied = await history.apply("Report.docx", await history.real("Report.docx"), blob_of(old))
    await history.record(applied=[applied], author=YOU, trailers=by_saga("saga:r", "restore"), main=main)
    # The landing's pack holds its commit and its tree: the mebibyte it brought back is the history's already.
    [pushed] = {p.name for p in packs.glob("*.pack")} - before
    assert (packs / pushed).stat().st_size < 4096
    assert git(project / "_history", "cat-file", "-s", f"{MAIN}:Report.docx") == str(2**20) and one != main


async def test_an_apply_refuses_a_file_changed_since_and_a_put_back_leaves_a_newer_one(tmp_path, storage, project):
    one = landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    v1 = git(project / "_history", "rev-parse", f"{one}^1:Report.docx")
    v2 = blob_of(b"PK\x03\x04 report v2")
    history = bucket(tmp_path, storage)
    (project / "Report.docx").write_bytes(b"saved by you meanwhile")
    with pytest.raises(HistoryConflict, match="changed since"):
        await history.apply("Report.docx", v2, v1)
    assert (project / "Report.docx").read_bytes() == b"saved by you meanwhile"
    # A put-back writes only over the version the landing wrote; an apply that failed wrote nothing.
    assert await history.unapply("Report.docx", v2, v1, ran=False) == {"path": "Report.docx", "before": v2, "after": v1}
    with pytest.raises(HistoryConflict, match="changed after"):
        await history.unapply("Report.docx", v2, v1)
    assert (project / "Report.docx").read_bytes() == b"saved by you meanwhile"
    # Where the file is the version the landing wrote, it goes back; and a file the landing made goes.  Safe to repeat.
    (project / "Report.docx").write_bytes(b"PK\x03\x04 report v1")
    for _ in range(2):
        await history.unapply("Report.docx", v2, v1, made=["a folder a pod made"])
        assert (project / "Report.docx").read_bytes() == b"PK\x03\x04 report v2"
    (project / "docs").mkdir()
    (project / "docs" / "Report.docx").write_bytes(b"PK\x03\x04 report v1")
    for _ in range(2):
        await history.unapply("docs/Report.docx", None, v1)
        assert not (project / "docs" / "Report.docx").exists()
    assert left_in(history) == []


async def test_a_record_whose_main_moved_is_refused(tmp_path, storage, project):
    landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    history = bucket(tmp_path, storage)
    main = (await history.fetch())["main"]
    two = landed(tmp_path, project, "saga:2", {"notes.txt": b"by another thread\n"})
    with pytest.raises(HistoryConflict, match="main moved"):
        await history.record(applied=[], author=YOU, trailers=by_saga("saga:r", "restore"), main=main)
    assert git(project / "_history", "rev-parse", MAIN) == two and left_in(history) == []


async def test_a_push_that_finds_the_historys_refs_moved_while_its_pack_went_up_writes_none_of_its_own(
    tmp_path, storage, project, monkeypatch,
):
    one = landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    v1 = git(project / "_history", "rev-parse", f"{one}^1:Report.docx")
    history = bucket(tmp_path, storage)
    main = (await history.fetch())["main"]
    applied = await history.apply("Report.docx", await history.real("Report.docx"), v1)
    upload, went = storage.upload, []

    async def another_lands_meanwhile(bucket_name, key, source, **condition):
        await upload(bucket_name, key, source, **condition)
        if key.endswith(".idx") and not went:
            # The project's lock was lost unseen: another thread's landing went up while this one's pack did.
            went.append(landed(tmp_path, project, "saga:2", {"notes.txt": b"by another thread\n"}))

    monkeypatch.setattr(storage, "upload", another_lands_meanwhile)
    with pytest.raises(HistoryConflict, match="moved while it was pushed"):
        await history.record(applied=[applied], author=YOU, trailers=by_saga("saga:r", "restore"), main=main)
    assert git(project / "_history", "rev-parse", MAIN) == went[0]
    assert git(project / "_history", "fsck", "--no-dangling") == ""


async def test_it_answers_as_a_pod_does(tmp_path, storage, project):
    one = landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    history = bucket(tmp_path, storage)

    async def asked(**request) -> dict:
        return json.loads(await history.execute("api", "_history", json.dumps(request)))

    looked = await asked(action="fetch", saga="saga:1", commits=[one, "0" * 40])
    assert looked == {"main": one, "landing": one, "hidden": False, "packs": looked["packs"], "missing": ["0" * 40]}
    assert looked["packs"] == sum(pack.stat().st_size for pack in packs_of(history)) > 0
    v1, v2 = git(project / "_history", "rev-parse", f"{one}^1:Report.docx"), blob_of(b"PK\x03\x04 report v2")
    assert await asked(action="apply", path="Report.docx", before=v2, after=v1) == {"path": "Report.docx", "before": v2, "after": v1, "made": []}
    assert await asked(action="unapply", path="Report.docx", before=v2, after=v1, ran=True, made=[]) == {
        "path": "Report.docx", "before": v2, "after": v1,
    }
    # What it does not do, or is asked wrongly, is an error in words, as a pod's: a step's result it is not.
    for request, words in (
        ({"action": "prune", "keep": []}, "Unknown history action: prune"),
        ({"action": "keep", "base": True}, "Unknown history action: keep"),
        ({}, "Unknown history action: None"),
        ({"action": "apply", "path": "Report.docx"}, "missing 2 required positional arguments"),
        ({"action": "apply", "path": "_history/packed-refs", "before": None, "after": None}, "is not one of the project's files"),
        ({"action": "fetch", "commits": ["--upload-pack=touch /tmp/ran"]}, "refused the project's history"),
    ):
        assert words in (await asked(**request))["error"]
    assert "Unknown history action" in json.loads(await history.execute("api", "terminal", json.dumps({"action": "fetch"})))["error"]
    assert (project / "Report.docx").read_bytes() == b"PK\x03\x04 report v2"


async def test_a_look_finds_a_landing_by_its_saga_back_to_where_it_began_and_says_when_a_cut_hides_it(tmp_path, storage, project, monkeypatch):
    one = landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    two = landed(tmp_path, project, "saga:2", {"Report.docx": b"PK\x03\x04 report v3"})
    three = landed(tmp_path, project, "saga:3", {"Report.docx": b"PK\x03\x04 report v4"})
    history = bucket(tmp_path, storage)

    async def found(saga: str, since: str | None) -> tuple[str | None, bool]:
        looked = await history.fetch(saga=saga, since=since)
        return looked["landing"], looked["hidden"]

    # However many landings went over it since; and none that never pushed, once the look met where it began.
    assert await found("saga:2", one) == (two, False)
    assert await found("saga:1", None) == (one, False)
    assert await found("saga:never", two) == (None, False)
    assert await found("saga:never", None) == (None, False)
    # A saga's name whole, as its trailer has it: one that begins another's is not it.
    assert await found("saga:", None) == (None, False)
    # Only among main's own commits: a turn a landing merged carries the saga too, and is no landing.
    assert (await found("saga:3", two))[0] == three
    # Nor is it known where the look ends before it met where the landing began: it reads no further back than a look may.
    with monkeypatch.context() as patch:
        patch.setattr(module, "_LOOKED_MOST", 1)
        assert await found("saga:never", None) == (None, True)
        assert await found("saga:never", three) == (None, False)
        assert await found("saga:3", None) == (three, False)
    # Behind a pruning's cut, whether it pushed is not known; where the landing began is above the cut, it is.
    cut_history(tmp_path, project / "_history", kept=1)
    assert await found("saga:1", None) == (None, True)
    assert await found("saga:never", three) == (None, False)
    assert await found("saga:3", two) == (three, False)


async def test_a_file_and_a_version_never_pass_through_the_apis_memory_whole(tmp_path, storage, project, monkeypatch):
    video = os.urandom(48 * 2**20)  # 48 MiB that does not compress
    one = landed(tmp_path, project, "saga:1", {"Report.docx": os.urandom(64 * 2**20)})
    landed(tmp_path, project, "saga:2", {"Report.docx": video})
    report, saved = git(project / "_history", "rev-parse", f"{one}:Report.docx"), blob_of(video)
    del video
    whole, wrote = [], []

    async def read(bucket_name, key):
        whole.append(key)  # the backend's read answers an object whole
        return await LocalBackend.read(storage, bucket_name, key)

    async def write(bucket_name, key, data):
        wrote.append(key)  # and its write takes one whole
        return await LocalBackend.write(storage, bucket_name, key, data)

    monkeypatch.setattr(storage, "read", read)
    monkeypatch.setattr(storage, "write", write)
    history = bucket(tmp_path, storage)
    await history.sync()
    where = let_go_on_the_loop(monkeypatch)
    tracemalloc.start()
    main = (await history.fetch())["main"]
    assert await history.real("Report.docx") == saved
    applied = await history.apply("Report.docx", saved, report)
    await history.record(applied=[applied], author=YOU, trailers=by_saga("saga:r", "restore"), main=main)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    # A 48 MiB file checked twice and a 64 MiB version written: none of it held whole, and nothing through a whole object.
    assert peak < 8 * 2**20, peak
    assert whole == wrote == []
    assert await history.real("Report.docx") == report
    assert left_in(history) == []
    # Each file on its way was let go as soon as it was read or written, and off the loop: a large file's going would stall it.
    assert where and not any(where)


async def test_a_pickup_records_a_file_too_large_for_git_to_hold_whole(tmp_path, storage, project):
    landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    video = os.urandom(40 * 2**20)  # past the 32 MiB git holds whole: it writes the file straight into a pack
    (project / "video.mp4").write_bytes(video)
    history = bucket(tmp_path, storage)
    tracemalloc.start()
    picked = await picked_up(history, ["video.mp4"])
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert picked["picked_up"] == [{"path": "video.mp4", "before": None, "after": blob_of(video)}]
    assert peak < 8 * 2**20, peak
    # The file is pushed whole, once, and a pod reads it.
    durable = project / "_history"
    assert git(durable, "cat-file", "-s", f"{picked['commit']}:video.mp4") == str(len(video))
    assert git(durable, "fsck", "--no-dangling") == ""
    assert sum(p.stat().st_size for p in (durable / "objects" / "pack").glob("*.pack")) < 44 * 2**20
    # Once pushed, the copy keeps the pushed pack alone.
    assert left_in(history) == []


async def test_a_file_or_a_version_past_its_bound_is_refused_in_words_and_nothing_is_written(tmp_path, storage, project):
    one = landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2, the long one"})
    v1, v2 = (git(project / "_history", "rev-parse", f"{one}{side}:Report.docx") for side in ("^1", ""))
    history = bucket(tmp_path, storage, file=20)
    with pytest.raises(HistoryError, match="Report.docx is larger than Surogate can read here") as refused:
        await history.real("Report.docx")
    # A bound's refusal is said to the user as it is; any other failure of the history is not.
    assert said(refused.value) == "Report.docx is larger than Surogate can read here."
    assert said(HistoryError("git cat-file failed: fatal: bad object")) is None
    # Neither is it looked at as your edit, nor picked up, nor written over.
    with pytest.raises(HistoryError, match="Report.docx is larger than Surogate can read here"):
        await history.edits(["Report.docx"])
    with pytest.raises(HistoryError, match="Report.docx is larger than Surogate can read here"):
        await history.apply("Report.docx", v2, v1)
    (project / "Report.docx").write_bytes(b"PK\x03\x04 report v1")
    with pytest.raises(HistoryError, match="This version is larger than Surogate can read here"):
        await history.apply("Report.docx", v1, v2)
    assert (project / "Report.docx").read_bytes() == b"PK\x03\x04 report v1"
    assert left_in(history) == []


async def test_a_file_on_its_way_in_or_out_is_counted_with_the_copy_and_refused_where_the_copy_has_no_room_for_it(
    tmp_path, storage, project,
):
    old = os.urandom(300_000)
    landed(tmp_path, project, "saga:1", {"Report.docx": old})
    landed(tmp_path, project, "saga:2", {"Report.docx": b"PK\x03\x04 report v3"})
    roomy = bucket(tmp_path, storage)
    await roomy.sync()
    held = on_disk(roomy.clone)
    v3 = blob_of(b"PK\x03\x04 report v3")
    # The copy has room for all it holds, and for less than the version on its way to the real files.
    tight = bucket(tmp_path, storage, packs=held + 200_000)
    with pytest.raises(HistoryError, match="This version is larger than Surogate can read here"):
        await tight.apply("Report.docx", v3, blob_of(old))
    assert (project / "Report.docx").read_bytes() == b"PK\x03\x04 report v3"
    # Nor for a real file on its way in to be looked at: your edit, as large.
    (project / "Report.docx").write_bytes(os.urandom(300_000))
    with pytest.raises(HistoryError, match="Report.docx is larger than Surogate can read here"):
        await tight.real("Report.docx")
    # And a file that is picked up is kept, and pushed: twice its bytes on the way.
    kept = bucket(tmp_path, storage, packs=held + 500_000)
    assert await kept.real("Report.docx") is not None
    with pytest.raises(HistoryError, match="Report.docx is larger than Surogate can read here"):
        await picked_up(kept, ["Report.docx"])
    assert git(project / "_history", "rev-parse", f"{MAIN}:Report.docx") == v3
    assert left_in(roomy) == []


async def test_what_an_act_that_died_left_on_its_way_goes_with_the_next_request(tmp_path, storage, project):
    one = landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    history = bucket(tmp_path, storage)
    await history.sync()
    # As a killed api leaves them: a version half written out, and the objects of a commit it had not pushed.
    (history.clone / "out").mkdir()
    (history.clone / "out" / "dead").write_bytes(b"x" * 4096)
    (history.clone / "scratch-dead" / "objects" / "ab").mkdir(parents=True)
    (history.clone / "scratch-dead" / "objects" / "ab" / ("c" * 38)).write_bytes(b"y" * 4096)
    assert len(left_in(history)) == 2
    assert await history.real("Report.docx") == blob_of(b"PK\x03\x04 report v2")
    assert left_in(history) == []
    assert (await history.fetch())["main"] == one


async def test_a_deletion_the_storage_did_not_make_fails_its_apply(tmp_path, storage, project, monkeypatch):
    landed(tmp_path, project, "saga:1", {"a.md": b"a\n"})
    history = bucket(tmp_path, storage)
    # A deletion the storage makes is an apply as any other.
    (project / "b.md").write_bytes(b"b\n")
    assert (await history.apply("b.md", blob_of(b"b\n"), None))["after"] is None and not (project / "b.md").exists()

    async def swallowed(bucket_name, key):
        return None  # as S3Backend.delete does on any error

    monkeypatch.setattr(storage, "delete", swallowed)
    with pytest.raises(HistoryError, match="a.md could not be deleted"):
        await history.apply("a.md", blob_of(b"a\n"), None)
    assert (project / "a.md").exists()


@pytest.mark.parametrize("path", [
    "/etc/passwd", "../other/Report.docx", "docs/../../Report.docx", "", ".", "docs//Report.docx", "docs/./Report.docx", "docs/",
    "_history/packed-refs", "_history/objects/pack/pack-1.pack", "_artifacts/a.html", ".threads/t1/notes.md",
    "~$Report.docx", "docs/.~lock.Report.docx#", "Report.docx.tmp", ".git/config", "a\x00b",
])
async def test_a_path_that_is_none_of_the_projects_files_reaches_neither_the_storage_nor_git(tmp_path, storage, project, monkeypatch, path):
    one = landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    history = bucket(tmp_path, storage)
    await history.sync()
    reached = []
    for name in ("download", "upload", "delete", "stat", "exists", "write", "read", "list_entries"):
        monkeypatch.setattr(storage, name, lambda *args, _name=name, **more: reached.append((_name, args)))
    monkeypatch.setattr(module, "_child", lambda *args, **more: reached.append(("git", args)))
    v2 = blob_of(b"PK\x03\x04 report v2")
    for act in (
        lambda: history.real(path), lambda: history.edits(["Report.docx", path]), lambda: history.recorded(one, path),
        lambda: history.apply(path, None, v2), lambda: history.unapply(path, None, v2),
        lambda: history.pickup(main=one, picked_up=[{"path": path, "before": None, "after": v2}], author=YOU, trailers=by_saga("s", "pickup")),
        lambda: history.record(applied=[{"path": path, "before": None, "after": v2}], author=YOU, trailers=by_saga("s", "restore"), main=one),
    ):
        with pytest.raises(HistoryError, match="is not one of the project's files"):
            await act()
    assert reached == []


async def test_a_version_that_is_no_id_reaches_neither_the_storage_nor_git(tmp_path, storage, project, monkeypatch):
    one = landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    history = bucket(tmp_path, storage)
    await history.sync()
    reached = []
    for name in ("download", "upload", "delete", "stat", "exists", "write", "read"):
        monkeypatch.setattr(storage, name, lambda *args, _name=name, **more: reached.append((_name, args)))
    monkeypatch.setattr(module, "_child", lambda *args, **more: reached.append(("git", args)))
    v2 = blob_of(b"PK\x03\x04 report v2")
    for crafted in ("--output=/tmp/ran", "HEAD", f"{one}:Report.docx", v2.upper(), v2[:39], f"{v2}\n", ""):
        for act in (
            lambda: history.apply("Report.docx", v2, crafted), lambda: history.apply("Report.docx", crafted, v2),
            lambda: history.unapply("Report.docx", crafted, v2), lambda: history.recorded(crafted, "Report.docx"),
            lambda: history.pickup(main=crafted, picked_up=[], author=YOU, trailers=by_saga("s", "pickup")),
            lambda: history.pickup(main=one, picked_up=[{"path": "a.md", "before": None, "after": crafted}], author=YOU, trailers=by_saga("s", "pickup")),
            lambda: history.record(applied=[{"path": "a.md", "before": None, "after": crafted}], author=YOU, trailers=by_saga("s", "restore"), main=one),
            lambda: history.record(applied=[], author=YOU, trailers=by_saga("s", "restore"), main=crafted),
            lambda: history.fetch(since=crafted),
        ):
            with pytest.raises(HistoryError, match="refused the project's history"):
                await act()
    assert reached == []


async def test_a_file_is_not_written_where_a_folder_has_its_name_or_a_file_the_name_of_its_folder(tmp_path, storage, project):
    landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    v2 = blob_of(b"PK\x03\x04 report v2")
    history = bucket(tmp_path, storage)
    await history.sync()
    (project / "plans").mkdir()
    (project / "plans" / "q3.md").write_text("q3\n")
    # A folder is where the file would be, and a file where its folder would be: neither is written, as a landing leaves one out.
    assert [await history.takes(path) for path in ("plans", "notes.txt/old.md", "plans/q4.md", "new/deep/file.md", "Report.docx")] == [
        False, False, True, True, True,
    ]
    for path in ("plans", "notes.txt/old.md"):
        with pytest.raises(HistoryConflict, match="cannot be written"):
            await history.apply(path, None, v2)
    assert (project / "plans" / "q3.md").read_text() == "q3\n" and (project / "notes.txt").read_text() == "v1 notes\n"
    # Under a folder the files do not have yet, it is.
    await history.apply("new/deep/file.md", None, v2)
    assert (project / "new" / "deep" / "file.md").read_bytes() == b"PK\x03\x04 report v2"


async def test_git_writes_only_in_the_apis_copy_under_its_own_config_within_its_memory_and_its_turns(tmp_path, storage, project, monkeypatch):
    one = landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    durable, ran = project / "_history", tmp_path / "ran"
    # A config and a hook a command wrote into the bucket's history.
    (durable / "config").write_text(f"[core]\n\tfsmonitor = touch {ran}\n\thooksPath = hooks\n")
    (durable / "hooks").mkdir(exist_ok=True)
    for hook in ("reference-transaction", "post-index-change", "pre-commit", "post-commit"):
        (durable / "hooks" / hook).write_text(f"#!/bin/sh\ntouch {ran}\n")
        (durable / "hooks" / hook).chmod(0o755)
    child, ran_as = module._child, []

    async def watched(turns, seconds, command, **how):
        # Each git is the api's child in one of the copy's turns, within its seconds, with the copy this request's alone.
        ran_as.append((turns is module._BRINGING or turns is module._READING, 0 < seconds <= module._TAKE_MOST, no_ones(history)))
        assert command[:2] == ["git", "init"] or tuple(command[:1 + len(module._BOUNDED)]) == ("git", *module._BOUNDED)
        return await child(turns, seconds, command, **how)

    monkeypatch.setattr(module, "_child", watched)
    history = bucket(tmp_path, storage)
    (project / "Report.docx").write_bytes(b"PK\x03\x04 report v2, edited by you")
    picked = await picked_up(history, ["Report.docx"])
    v1 = git(durable, "rev-parse", f"{one}^1:Report.docx")
    applied = await history.apply("Report.docx", picked["picked_up"][0]["after"], v1)
    await history.record(applied=[applied], author=YOU, trailers=by_saga("saga:r", "restore"), main=picked["commit"])
    # The landing went up, and nothing of the bucket's ran.
    assert (project / "Report.docx").read_bytes() == b"PK\x03\x04 report v1"
    assert not ran.exists()
    assert "fsmonitor" not in (history.clone / "config").read_text() and not (history.clone / "hooks").exists()
    assert len(ran_as) > 8 and set(ran_as) == {(True, True, False)}


async def test_a_step_whose_request_left_goes_on_to_its_end_with_the_copy_its_own_and_the_next_waits_for_it(
    tmp_path, storage, project, monkeypatch,
):
    one = landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    v1, v2 = git(project / "_history", "rev-parse", f"{one}^1:Report.docx"), blob_of(b"PK\x03\x04 report v2")
    history = bucket(tmp_path, storage)
    await history.sync()
    upload, reached, go = storage.upload, asyncio.Event(), asyncio.Event()

    async def slow(bucket_name, key, source, **condition):
        reached.set()
        await go.wait()
        await upload(bucket_name, key, source, **condition)

    monkeypatch.setattr(storage, "upload", slow)
    applying = asyncio.ensure_future(history.apply("Report.docx", v2, v1))
    await reached.wait()
    # Its request leaves, as a step's does past its time: the write it began is not left half made, nor the copy let go under it.
    applying.cancel()
    with pytest.raises(asyncio.CancelledError):
        await applying
    assert not no_ones(history) and (project / "Report.docx").read_bytes() == b"PK\x03\x04 report v2"
    # Its put-back waits for it, and then finds what it wrote.
    putting_back = asyncio.ensure_future(history.unapply("Report.docx", v2, v1, ran=False))
    await asyncio.sleep(0.2)
    assert not putting_back.done()
    go.set()
    assert await putting_back == {"path": "Report.docx", "before": v2, "after": v1}
    assert (project / "Report.docx").read_bytes() == b"PK\x03\x04 report v2"
    assert no_ones(history) and left_in(history) == []


async def test_a_step_still_waiting_for_the_copy_when_its_request_left_never_runs(tmp_path, storage, project):
    one = landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    v1, v2 = git(project / "_history", "rev-parse", f"{one}^1:Report.docx"), blob_of(b"PK\x03\x04 report v2")
    history = bucket(tmp_path, storage)
    await history.sync()
    held = os.open(module._lock_of(history.clone), os.O_RDWR)
    fcntl.flock(held, fcntl.LOCK_EX)  # another request has the copy
    try:
        applying = asyncio.ensure_future(history.apply("Report.docx", v2, v1))
        await asyncio.sleep(0.2)
        applying.cancel()
        with pytest.raises(asyncio.CancelledError):
            await applying
    finally:
        os.close(held)
    await asyncio.sleep(0.3)
    # It had begun nothing, and begins nothing once the copy is free.
    assert (project / "Report.docx").read_bytes() == b"PK\x03\x04 report v2" and no_ones(history)


def test_a_real_file_kept_has_seconds_by_what_compressing_it_costs_git():
    # Keeping a file compresses it, which a file that does not compress makes slower than reading a pack:
    # twenty seconds at least, and a gibibyte in about a minute.
    assert [module._kept_seconds(size) for size in (0, 2**20, 300 * 2**20, 2**30, 4 * 2**30)] == [20.0, 20.0, 20.0, 64.0, 120.0]
    assert module._kept_seconds(2**30) > module._seconds(2**30)


async def test_an_edit_git_cannot_keep_within_its_seconds_is_refused_in_words_and_nothing_is_pushed(tmp_path, storage, project, monkeypatch):
    one = landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    (project / "Report.docx").write_bytes(os.urandom(4 * 2**20))
    history = bucket(tmp_path, storage)
    looked = await history.edits(["Report.docx"])
    monkeypatch.setattr(module, "_TAKE_LEAST", 0.001)
    monkeypatch.setattr(module, "_KEEP_RATE", 2**40)
    with pytest.raises(HistoryError) as refused:
        await history.pickup(main=looked["main"], picked_up=looked["picked_up"], author=YOU, trailers=by_saga("saga:p", "pickup"))
    # Stopped, it is a file larger than is kept here: said as the bounds are, never as a history that could not be read.
    assert said(refused.value) == "Report.docx is larger than Surogate can read here."
    assert git(project / "_history", "rev-parse", MAIN) == one and left_in(history) == []


async def test_a_save_while_the_version_is_written_out_is_seen_by_the_check_and_never_written_over(
    tmp_path, storage, project, monkeypatch,
):
    one = landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    v1, v2 = git(project / "_history", "rev-parse", f"{one}^1:Report.docx"), blob_of(b"PK\x03\x04 report v2")
    history = bucket(tmp_path, storage)
    written_out, saved = BucketHistory._written_out, b"saved by you while the version was written out"

    async def then_you_save(self, blob, size):
        staged = await written_out(self, blob, size)
        (project / "Report.docx").write_bytes(saved)
        return staged

    monkeypatch.setattr(BucketHistory, "_written_out", then_you_save)
    # The version is written out first, and the real file checked after: the save is seen, and kept.
    with pytest.raises(HistoryConflict, match="changed since"):
        await history.apply("Report.docx", v2, v1)
    assert (project / "Report.docx").read_bytes() == saved
    # So for a put-back: one that wrote the file is told someone changed it since; one that may not have leaves it.
    (project / "Report.docx").write_bytes(b"PK\x03\x04 report v1")
    with pytest.raises(HistoryConflict, match="changed after"):
        await history.unapply("Report.docx", v2, v1)
    assert (project / "Report.docx").read_bytes() == saved
    (project / "Report.docx").write_bytes(b"PK\x03\x04 report v1")
    assert (await history.unapply("Report.docx", v2, v1, ran=False))["path"] == "Report.docx"
    assert (project / "Report.docx").read_bytes() == saved
    assert left_in(history) == []


async def test_a_save_between_the_check_and_the_write_is_refused_by_the_store_where_it_can_say(tmp_path, storage, project, monkeypatch):
    one = landed(tmp_path, project, "saga:1", {"Report.docx": b"PK\x03\x04 report v2"})
    v1, v2 = git(project / "_history", "rev-parse", f"{one}^1:Report.docx"), blob_of(b"PK\x03\x04 report v2")
    history = bucket(tmp_path, storage)
    upload, saved = storage.upload, b"saved by you as the version went up"

    async def you_save_first(bucket_name, key, source, **condition):
        if key.endswith("/Report.docx"):
            (project / "Report.docx").write_bytes(saved)  # your save, after the check and before the version is in place
        return await upload(bucket_name, key, source, **condition)

    monkeypatch.setattr(storage, "upload", you_save_first)
    # The local store writes only over the file as the check saw it.
    with pytest.raises(HistoryConflict, match="changed as it was written"):
        await history.apply("Report.docx", v2, v1)
    assert (project / "Report.docx").read_bytes() == saved
    # And a file the check found missing is written only where none was made since.
    (project / "Report.docx").unlink()
    saved = b"made by you as the version went up"
    with pytest.raises(HistoryConflict, match="changed as it was written"):
        await history.apply("Report.docx", None, v1)
    assert (project / "Report.docx").read_bytes() == saved
    # A put-back over a save in that time: one that wrote the file is told someone changed it since; one that may not
    # have written it leaves it as it is.
    for ran in (True, False):
        (project / "Report.docx").write_bytes(b"PK\x03\x04 report v1")
        saved = b"saved by you as the put-back went up, " + str(ran).encode()
        if ran:
            with pytest.raises(HistoryConflict, match="changed after"):
                await history.unapply("Report.docx", v2, v1, ran=ran)
        else:
            assert (await history.unapply("Report.docx", v2, v1, ran=ran))["path"] == "Report.docx"
        assert (project / "Report.docx").read_bytes() == saved
    assert left_in(history) == []
