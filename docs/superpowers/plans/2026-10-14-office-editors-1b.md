# Office Editors 1b: Documents in Folders on the Computer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In Surogate Desktop, an office document in a chat's folder on the user's computer, or in a project on the computer, opens in the file pane's editor and saves back to the computer. The agent edits it live there too, as it already does in the cloud.

**Architecture:**
- **Guarded writes on the computer.** `DeviceWorkspaceIO` gains the two methods the office code already calls on cloud files: `etag` (the file's revision) and `write_if` (a write guarded by that revision, or create-only). The office code then works on both kinds of workspace.
- **One place says where a document is and how the editor writes to it.** A new module, `surogates/office/files.py`:
  - **`document_session`** maps a project thread on a computer to its project's master. The master works in the real folder; a thread works in a copy.
  - **`editor_files`** opens the files for one editor write. On a computer, that is a change request whose id starts with `editor.`. No route can produce such an id, because route request ids may not contain a dot. So the desktop recognises an editor save, while the server's existing rules for user requests (limits, retention, cancelling) still apply unchanged.
- **The save job never blocks on a computer.** It waits a few seconds, then leaves a still-pending write for its next pass, which rejoins it under the same request id.
- **The desktop asks once per document.** In an "Ask every time" folder it asks before an editor save, then lets that document's later saves through.
- **The pane says when a save is waiting** for the computer.

**Tech Stack:** Python 3.12 (FastAPI, SQLAlchemy async, pytest with testcontainers), TypeScript (Electron desktop with vitest, React SDK with vitest).

**Spec:** `docs/superpowers/specs/2026-10-13-office-editors-design.md`. This plan is its plan 1b, "Computer folders". It also brings plan 3's live agent editing to computers, which plan 3 left off with `has_file_pane` false for device sessions.

## Verified before planning

Verify item 3 asks: "The project coordinator's folder binding on a computer can serve the editor's reads and writes while the project's threads run." Confirmed by reading the code:

- **The master's tool host works on the real folder, and threads don't touch it.** The desktop runs one tool host per root and locks its folder by dev:ino (`desktop/src/hosts/folder-record.ts:27-37`). The master's host is on the real folder (`desktop/src/hosts/tool-hosts.ts:626-649`). A thread's host is on its copy (`:711`), so running threads never lock the folder.
- **A landing does hold the folder.** It holds it from its first `land` step until `forget` (`tool-hosts.ts:363-372`). A request on the master's root during that time waits 10 s (`folder-record.ts:13`), then fails with "unavailable". The editor's reads and saves then fail and are retried, and that is the behaviour this plan specifies.
- **The server lets a user request through on the master's root.** It checks only the binding and the session rows (`surogates/devices/operations.py:272-347`).

The DEV walkthrough (Task 8) confirms it on a real computer.

## Global Constraints

- Python 3.12; tests run with `/work/surogates/.venv/bin/python -m pytest` (never `uv run`); integration tests use the testcontainers fixtures in `tests/integration/conftest.py`.
- Desktop tests: `cd desktop && npx vitest run <file>`. SDK tests: `cd sdk/agent-chat-react && npx vitest run <file>`, typecheck `npm run typecheck`.
- **The editor is the only writer of an open document.** A conflicting save never overwrites: it lands as a sibling copy, `<stem> (edited <YYYY-MM-DD HH.MM> UTC)<ext>`.
- **Device writes from the editor carry request ids that start with `editor.`.** The full invocation is `request:editor.<…>`. Route request ids match `^[A-Za-z0-9_-]{16,64}$`, so no route can produce an editor id.
- **Size cap:** a file over 50 MiB on a computer cannot be opened or saved. The device layer refuses it with `OSError(EFBIG)`, which the route answers with 413.
- **What history records:** an editor save to a project on a computer is a plain guarded write through the project's master. The computer records it as "you" at its next landing (spec, Known gaps).
- **Commits:**
  - Conventional Commits.
  - Commit named files only (`git add <paths>`, never `-a`).
  - No `Co-Authored-By` trailer.
  - Never mention plans, tasks or reviews in code, comments or commit messages.
- **Comments:** match the surrounding style, which is sparse, plain sentences saying why.

## Review Focus

1. **The computer goes offline while a save is pending.** The save waits without counting as a failure, and the pane says "Saving to <computer> once it is back online". It lands when the computer returns, and the notice clears. Pinned in Task 4.
2. **An "Ask every time" folder.**
   - The first save of a document prompts once, and later saves of that document go through unasked.
   - A declined save is not retried silently: the pane says the computer declined it, and the next save asks again.

   Pinned in Tasks 4 and 6.
3. **A project landing holds the real folder while the editor opens or saves.**
   - The open fails with the computer's own words, not a generic error.
   - The save is retried. Because a replayed request would only repeat its recorded failure, the retry uses a new request id.

   Pinned in Task 4: a failed device save bumps its request id.
4. **A thread on a computer opens or creates a document.** It reaches the project's real folder through the master, never the thread's copy, and its location matches the one the pane uses. Pinned in Tasks 2 and 5.
5. **A file over 50 MiB on a computer.** Opening it answers 413 with the device layer's words. Pinned in Task 3.

## Not in this plan

- **Terminal writes in a computer thread's copy.** A thread on a computer that writes an open document from its terminal (python-docx, say) is not held back when it lands. In the cloud, `hold_open_documents` holds such a write back; the local landing (`surogates/harness/local_landing.py`) has no hook for it. The landed write changes the real file, so the editor's next save lands as a conflict copy beside it. `write_file` and `patch` are refused on computers too (Task 5), and the office skill tells the agent to edit open documents only with the office tools.
- **Who history names on a computer.** History records editor saves there as "you", as the spec's Known gaps say.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `surogates/devices/workspace.py` | modify | `DeviceWorkspaceIO.write(create=)`, `etag`, `write_if`. |
| `surogates/office/files.py` | create | `DOCUMENT_FILES`, `EDITOR_REQUEST`, `FolderNotReady`, `document_session`, `editor_files`. |
| `surogates/office/agent.py` | modify | `location_of` for computers; `open_for_agent` on computers. |
| `surogates/api/routes/office.py` | modify | The editor and file-fetch routes on computers. |
| `surogates/office/sessions.py` | modify | `mark_waiting`, `next_try`, `drop_pending`. |
| `surogates/office/saves.py` | modify | Device landing, with waiting, rejoining, declining and the new events. |
| `surogates/session/events.py` | modify | `OFFICE_SAVE_WAITING`, `OFFICE_SAVED`. |
| `surogates/session/files.py` | modify | Docstring: the pending area of the bucket is the editor's. |
| `surogates/harness/tool_schemas.py` | modify | `has_file_pane` true on computers. |
| `surogates/office/guard.py` | modify | Applies on computers. |
| `surogates/office/turn_end.py` | modify | Locations without opening files; works on computers. |
| `surogates/harness/loop_artifact_completion.py` | modify | The turn's save runs on computers. |
| `surogates/tools/builtin/office.py` | modify | `_picture` failures on computers. |
| `desktop/src/binding/approvals.ts`, `desktop/src/shell/prompt-content.ts`, `desktop/src/files/operations.ts` | modify | Editor saves: asked once per document; their prompt; the `create` comment. |
| `sdk/agent-chat-react/src/types.ts`, `src/runtime/events.ts`, `src/runtime/reducer.ts`, `src/components/workspace/office-editor-pane.tsx` | modify | The waiting and saved notices. |
| `tests/device_computer.py` | create | A fake computer: a runner answering as the desktop does, over a dict. |
| Tests | create/modify | Listed per task. |

---

### Task 1: Guarded writes on the computer

**Files:**
- Create: `tests/device_computer.py`
- Modify: `surogates/devices/workspace.py:344-357` (`write`), add `etag` and `write_if` after it
- Test: `tests/test_device_workspace_guarded.py`

**Interfaces:**
- Produces:
  - `DeviceWorkspaceIO.write(key, data, *, expected_revision: str | None = None, create: bool = False) -> None`
  - `DeviceWorkspaceIO.etag(key) -> str | None`
  - `DeviceWorkspaceIO.write_if(key, data, *, expected: str | None) -> None`. It raises `RevisionConflict` when the file is not at `expected`, or, when `expected` is None, when a file is already there.
  - `tests.device_computer.Computer`, a runner with `.files: dict[str, tuple[bytes, str]]` mapping each key to its bytes and revision, and `.calls: list[tuple[str, dict]]`.
  - `tests.device_computer.computer_io(folder, files=None, device_id=None) -> DeviceWorkspaceIO`.

- [ ] **Step 1: Write the fake computer**

`tests/device_computer.py`:

```python
"""A folder on a computer, answered as the desktop's file helper answers (desktop/src/files/operations.ts)."""

from __future__ import annotations

import base64
import posixpath
from uuid import uuid4

from surogates.devices.workspace import DeviceWorkspaceIO


class Computer:
    """An operation runner over a dict of files: key -> (bytes, revision). Revisions change on every write."""

    def __init__(self, folder: str, files: dict[str, bytes] | None = None) -> None:
        self.folder = folder
        self.files: dict[str, tuple[bytes, str]] = {}
        self.calls: list[tuple[str, dict]] = []
        self._writes = 0
        for path, data in (files or {}).items():
            self._put(posixpath.join(folder, path), data)

    def _put(self, key: str, data: bytes) -> None:
        self._writes += 1
        self.files[key] = (data, f"1:{self._writes}:{len(data)}:{self._writes}:{self._writes}")

    async def run(self, kind: str, args: dict, payload: bytes | None = None) -> dict:
        self.calls.append((kind, args))
        if kind == "resolve":
            return {"ok": posixpath.normpath(posixpath.join(self.folder, args["path"]))}
        key = args.get("key")
        if kind == "stat":
            if key not in self.files:
                return {"ok": None}
            data, revision = self.files[key]
            return {"ok": {"is_dir": False, "size": len(data), "mtime": 1.0, "revision": revision}}
        if kind == "read":
            if key not in self.files:
                return {"error": {"type": "os", "code": "ENOENT", "message": f"No such file: {key}"}}
            return {"ok": base64.b64encode(self.files[key][0]).decode("ascii")}
        if kind == "write":
            data = payload if payload is not None else base64.b64decode(args["data"])
            if args.get("create") is True:
                if key in self.files:
                    return {"error": {"type": "os", "code": "EEXIST", "message": f"File exists: {key}"}}
            elif "expected_revision" in args and (key not in self.files or self.files[key][1] != args["expected_revision"]):
                return {"error": {"type": "conflict", "message": f"{key} changed on the computer"}}
            self._put(key, data)
            return {"ok": None}
        raise AssertionError(f"the fake computer does not do {kind}")


def computer_io(folder: str, files: dict[str, bytes] | None = None, device_id: str | None = None) -> DeviceWorkspaceIO:
    """A DeviceWorkspaceIO on a fake computer; its runner is ``io.runner``."""
    return DeviceWorkspaceIO(Computer(folder, files), root=folder, identity=f"device:{device_id or uuid4()}")
```

- [ ] **Step 2: Write the failing tests**

`tests/test_device_workspace_guarded.py`:

```python
"""Writes on the computer guarded by the file's revision, as the office editor's saves need them."""

from __future__ import annotations

import pytest

from surogates.tools.workspace_io import RevisionConflict
from tests.device_computer import computer_io

FOLDER = "/home/me/notes"
KEY = f"{FOLDER}/a.docx"


async def test_etag_is_the_files_revision_and_none_where_nothing_is():
    io = computer_io(FOLDER, {"a.docx": b"one"})
    assert await io.etag(KEY) == io.runner.files[KEY][1]
    assert await io.etag(f"{FOLDER}/missing.docx") is None


async def test_write_if_writes_over_the_revision_it_expects():
    io = computer_io(FOLDER, {"a.docx": b"one"})
    revision = await io.etag(KEY)
    await io.write_if(KEY, b"two", expected=revision)
    assert io.runner.files[KEY][0] == b"two"
    assert ("write", {"key": KEY, "data": "dHdv", "expected_revision": revision}) in io.runner.calls


async def test_write_if_refuses_a_file_changed_since():
    io = computer_io(FOLDER, {"a.docx": b"one"})
    with pytest.raises(RevisionConflict):
        await io.write_if(KEY, b"two", expected="1:0:0:0:0")
    assert io.runner.files[KEY][0] == b"one"


async def test_write_if_without_an_expectation_only_makes_a_new_file():
    io = computer_io(FOLDER, {"a.docx": b"one"})
    await io.write_if(f"{FOLDER}/b.docx", b"new", expected=None)
    assert io.runner.files[f"{FOLDER}/b.docx"][0] == b"new"
    assert io.runner.calls[-1][1]["create"] is True
    with pytest.raises(RevisionConflict):
        await io.write_if(KEY, b"two", expected=None)
    assert io.runner.files[KEY][0] == b"one"


async def test_a_plain_write_asks_for_neither_a_revision_nor_a_new_file():
    io = computer_io(FOLDER)
    await io.write(KEY, b"x")
    assert io.runner.calls[-1] == ("write", {"key": KEY, "data": "eA=="})
```

- [ ] **Step 3: Run them to see them fail**

Run: `/work/surogates/.venv/bin/python -m pytest tests/test_device_workspace_guarded.py -q`
Expected: FAIL — `AttributeError: 'DeviceWorkspaceIO' object has no attribute 'etag'` (and `write() got an unexpected keyword argument 'create'`).

- [ ] **Step 4: Implement**

In `surogates/devices/workspace.py`, replace `write` and add the two methods after it (import `RevisionConflict` is already there; it is raised by `_raise`):

```python
    async def write(self, key: str, data: bytes, *, expected_revision: str | None = None, create: bool = False) -> None:
        refuse_nul(key)
        # Only when there is one: a write that expects nothing asks for what it always did.
        expected = {} if expected_revision is None else {"expected_revision": expected_revision}
        if create:
            # Makes the file only where none is: the computer answers EEXIST otherwise.
            expected["create"] = True
        if len(data) <= MAX_PAYLOAD_BYTES:
            await self._call("write", key=key, data=base64.b64encode(data).decode("ascii"), **expected)
            return
        if len(data) > MAX_WRITE_BYTES:
            raise OSError(errno.EFBIG, WRITE_TOO_LARGE)
        # Named by its content, so a resumed call that makes the same bytes asks
        # for the same operation.  Up to 50 MiB: hashed off the event loop the
        # worker's other sessions share.
        transfer = await asyncio.to_thread(_transfer_for, data)
        await self._call("write", payload=data, key=key, transfer=transfer, **expected)

    async def etag(self, key: str) -> str | None:
        """The file's revision, None where no file is: what :meth:`write_if` expects."""
        stat = await self.stat(key)
        return None if stat is None or stat.is_dir else stat.revision

    async def write_if(self, key: str, data: bytes, *, expected: str | None) -> None:
        """Write over the revision *expected*, or, with None, only where no file is; RevisionConflict otherwise."""
        if expected is not None:
            await self.write(key, data, expected_revision=expected)
            return
        try:
            await self.write(key, data, create=True)
        except FileExistsError as exc:
            raise RevisionConflict(str(exc)) from None
```

(`OSError(errno.EEXIST, …)` is constructed as `FileExistsError` by Python, so the computer's EEXIST arrives as one.)

- [ ] **Step 5: Run the tests to see them pass**

Run: `/work/surogates/.venv/bin/python -m pytest tests/test_device_workspace_guarded.py tests/test_device_read_tracking.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/device_computer.py tests/test_device_workspace_guarded.py surogates/devices/workspace.py
git commit -m "feat(devices): revision-guarded and create-only writes on the computer"
```

---

### Task 2: Where a document is, and the files the editor writes through

**Files:**
- Create: `surogates/office/files.py`
- Modify: `surogates/office/agent.py:55-58` (`location_of`)
- Test: `tests/test_office_files.py`

**Interfaces:**
- Consumes: `DeviceWorkspaceIO.etag/write_if` (Task 1).
- Produces:
  - **`location_of(session, path) -> str`** (in `surogates/office/agent.py`).
    - On a computer it returns `f"device:{device_id}:{posixpath.join(workspace_path, path)}"`, where `path` is workspace-relative.
    - In the cloud it returns the same as before.
    - A thread's copy sits at the folder's own path, so a project thread and its master name a document the same way.
  - **`surogates/office/files.py`:**
    - **`DOCUMENT_FILES`:** `(StorageWorkspaceIO, DeviceWorkspaceIO)`, the workspaces the editor reads and saves.
    - **`EDITOR_REQUEST = "editor."`**
    - **`class FolderNotReady(Exception)`:** the folder is not set up on the computer yet.
    - **`async def document_session(session, *, session_factory) -> session`:** a project thread on a computer, or a helper under one, maps to its project's master; any other session maps to itself.
    - **`editor_files(session, *, storage, session_factory, redis, request_id: str, data: bytes)`:** an async context manager yielding the session's files for one editor write.
      - On a computer it is a change request `request:editor.<request_id>`, named by the sha256 of `data`, with the session owner's access.
      - It raises `FolderNotReady` when the folder's binding is not `bound`.

- [ ] **Step 1: Write the failing tests**

`tests/test_office_files.py`:

```python
"""Where a document is, and the files the document editor writes it through."""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.devices.binding import Binding
from surogates.office import files as office_files
from surogates.office.agent import location_of
from surogates.office.files import EDITOR_REQUEST, FolderNotReady, document_session, editor_files

DEVICE = str(uuid4())
FOLDER = "/home/me/project"


def on_computer(**config):
    return SimpleNamespace(id=uuid4(), config={
        "execution": {"kind": "device", "device_id": DEVICE, "device_name": "Mac"}, "workspace_path": FOLDER,
        "storage_bucket": "b", **config,
    })


def test_a_document_on_a_computer_is_named_by_its_path_there():
    assert location_of(on_computer(), "docs/a.docx") == f"device:{DEVICE}:{FOLDER}/docs/a.docx"


def test_a_thread_and_its_master_name_a_document_alike():
    master = on_computer(workspace_boundary="project:p1")
    thread = on_computer(workspace_boundary="project:p1", execution={
        "kind": "device", "device_id": DEVICE, "history": {"thread": str(uuid4())},
    })
    assert location_of(thread, "a.docx") == location_of(master, "a.docx")


async def test_a_cloud_chat_and_a_master_are_their_own_document_sessions():
    cloud = SimpleNamespace(id=uuid4(), config={"storage_bucket": "b"})
    assert await document_session(cloud, session_factory=None) is cloud
    master = on_computer(workspace_boundary=f"project:{uuid4()}")
    assert await document_session(master, session_factory=None) is master


async def test_a_thread_on_a_computer_reaches_its_documents_through_its_projects_master(monkeypatch):
    project_id, master = uuid4(), on_computer()
    thread = on_computer(workspace_boundary=f"project:{project_id}", execution={
        "kind": "device", "device_id": DEVICE, "history": {"thread": str(uuid4())},
    })

    async def master_of(_factory, pid):
        assert str(pid) == str(project_id)
        return master

    monkeypatch.setattr(office_files, "_master_of", master_of)
    assert await document_session(thread, session_factory=object()) is master


def _recording(monkeypatch, *, state="bound"):
    given: list[dict] = []

    @asynccontextmanager
    async def fake_session_files(session, **kwargs):
        given.append(kwargs)
        yield "files"

    async def binding(_db, _root):
        return Binding(state, "not set up" if state != "bound" else None)

    @asynccontextmanager
    async def factory():
        yield None

    monkeypatch.setattr(office_files, "session_files", fake_session_files)
    monkeypatch.setattr(office_files, "binding_of", binding)
    return given, factory


async def test_an_editor_write_on_a_computer_is_a_change_request_the_desktop_knows(monkeypatch):
    given, factory = _recording(monkeypatch)
    session = on_computer()
    async with editor_files(session, storage=None, session_factory=factory, redis=None, request_id="k1.s1.0", data=b"doc") as files:
        assert files == "files"
    [kwargs] = given
    assert kwargs["request_id"] == f"{EDITOR_REQUEST}k1.s1.0"
    assert kwargs["change"] == hashlib.sha256(b"doc").hexdigest()
    assert (kwargs["access"].session_id, kwargs["access"].bound) == (session.id, True)


async def test_an_editor_write_waits_for_the_folder_to_be_set_up(monkeypatch):
    _, factory = _recording(monkeypatch, state="pending")
    with pytest.raises(FolderNotReady):
        async with editor_files(on_computer(), storage=None, session_factory=factory, redis=None, request_id="k", data=b""):
            pass


async def test_an_editor_write_in_the_cloud_is_the_chats_files(monkeypatch):
    given, factory = _recording(monkeypatch)
    cloud = SimpleNamespace(id=uuid4(), config={"storage_bucket": "b"})
    async with editor_files(cloud, storage=None, session_factory=factory, redis=None, request_id="k", data=b"doc"):
        pass
    assert given[0]["change"] is None and given[0]["access"] is None
```

- [ ] **Step 2: Run them to see them fail**

Run: `/work/surogates/.venv/bin/python -m pytest tests/test_office_files.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'surogates.office.files'`.

- [ ] **Step 3: Implement `location_of`**

In `surogates/office/agent.py` replace `location_of`:

```python
def location_of(session: Any, path: str) -> str:
    """Where *path*, relative to the workspace, is: one name however the file is reached.

    A cloud session names it as the workspace layer does (``StorageWorkspaceIO.location``); a session on a computer
    by its path there.  A project thread's copy sits at the folder's own path, so it names the real file, as its
    master does: the editor only ever opens that one.
    """
    device_id = device_of(session.config)
    if device_id is not None:
        return f"device:{device_id}:{posixpath.join(str(session.config['workspace_path']).rstrip('/'), path)}"
    prefix = boundary_workspace_prefix(session.config, session, workspace_root_id(session))
    return StorageWorkspaceIO(None, bucket=session.config["storage_bucket"], prefix=prefix).location(path)
```

- [ ] **Step 4: Write `surogates/office/files.py`**

```python
"""The files a document open in the editor is read from and saved to."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from surogates.db.models import Workstream
from surogates.devices.binding import binding_of, copy_of, device_of
from surogates.devices.workspace import DeviceWorkspaceIO
from surogates.sandbox.pool import sandbox_session_key
from surogates.session.files import DeviceAccess, session_files
from surogates.session.store import SessionStore
from surogates.tools.workspace_io import StorageWorkspaceIO
from surogates.workstreams.stream import project_of

# The workspaces the editor opens documents in and saves them to.
DOCUMENT_FILES = (StorageWorkspaceIO, DeviceWorkspaceIO)
# The request ids of the editor's writes to a computer. No route's request id holds a dot, so none is taken for one,
# and the desktop knows them (desktop/src/binding/approvals.ts).
EDITOR_REQUEST = "editor."


class FolderNotReady(Exception):
    """The folder is not set up on the computer yet, so the editor has nowhere to save."""


async def document_session(session: Any, *, session_factory: Any) -> Any:
    """The session whose files hold *session*'s documents.

    A project thread on a computer, and a helper under one, work in a copy of the folder; the document is the real
    file, which the project's master works in.  Any other session holds its own.
    """
    config = session.config or {}
    if device_of(config) is None or copy_of(config) is None:
        return session
    project_id = project_of(config.get("workspace_boundary"))
    if project_id is None:
        raise FolderNotReady("This thread's project is gone.")
    return await _master_of(session_factory, UUID(str(project_id)))


async def _master_of(session_factory: Any, project_id: UUID) -> Any:
    async with session_factory() as db:
        project = await db.get(Workstream, project_id)
    if project is None or project.master_session_id is None:
        raise FolderNotReady("This thread's project is gone.")
    return await SessionStore(session_factory).get_session(project.master_session_id)


@asynccontextmanager
async def editor_files(
    session: Any, *, storage: Any, session_factory: Any, redis: Any, request_id: str, data: bytes,
) -> AsyncIterator[Any]:
    """*session*'s files for one write of the editor's.

    On a computer the write is a change request, ``request:editor.<request_id>``, named by *data*'s digest: one the
    caller stops waiting for stays open there, for the same id to join again.  It carries the session owner's access,
    given here once the folder is set up on the computer: the editor acts for that person, and no route is in front
    of it.  A cloud session's files are its own, as before.
    """
    access = change = None
    if device_of(session.config) is not None:
        async with session_factory() as db:
            binding = await binding_of(db, UUID(sandbox_session_key(session)))
        if binding.state != "bound":
            raise FolderNotReady(binding.message or "The folder is not set up on the computer yet.")
        access = DeviceAccess(session_id=session.id, bound=True)
        change = hashlib.sha256(data).hexdigest()
    async with session_files(
        session, storage=storage, session_factory=session_factory, redis=redis,
        request_id=f"{EDITOR_REQUEST}{request_id}", change=change, access=access,
    ) as files:
        yield files
```

(If `surogates.session.files` importing `surogates.office` creates a cycle at import time, import `session_files`/`DeviceAccess` inside the functions instead. `surogates/session/files.py` does not import `surogates.office`, so no cycle is expected.)

- [ ] **Step 5: Run the tests to see them pass**

Run: `/work/surogates/.venv/bin/python -m pytest tests/test_office_files.py tests/test_office_agent_paths.py tests/integration/test_office_agent.py -q`
Expected: PASS, apart from `test_other_files_and_computer_folders_are_refused` in `tests/integration/test_office_agent.py`. That test still expects computers to be refused; Task 5 changes it.

- [ ] **Step 6: Commit**

```bash
git add surogates/office/files.py surogates/office/agent.py tests/test_office_files.py
git commit -m "feat(office): name documents on a computer, and write them through the editor's own requests"
```

### Task 3: Opening a document that is on the computer

**Files:**
- Modify: `surogates/api/routes/office.py` (`get_workspace_editor`, `get_office_file`, imports)
- Test: `tests/integration/test_office_routes.py` (replace `test_computer_folder_chats`, add three tests)

**Interfaces:**
- Consumes: `document_session`, `DOCUMENT_FILES`, `FolderNotReady` (Task 2); `location_of` (Task 2); `DeviceWorkspaceIO.etag` (Task 1).
- Produces:
  - The editor route opens documents in computer-folder chats and computer projects.
  - The file-fetch route serves them from the computer.
  - The row's `location` is `location_of(document_session(...), path)`.

- [ ] **Step 1: Write the failing tests**

In `tests/integration/test_office_routes.py`, add the imports:

```python
from contextlib import asynccontextmanager

from tests.device_computer import computer_io
```

Then replace `test_computer_folder_chats` with:

```python
FOLDER = "/home/me/notes"


async def _computer_chat(client, app, session_factory, monkeypatch, files, *, config=None):
    """A chat on a folder of the user's computer, whose files a fake computer answers; the sessions asked for, kept."""
    _, headers, user_id, chat = await _user_chat(client, app, session_factory)
    device_id = str(uuid.uuid4())
    store = SessionStore(session_factory)
    device_chat = await store.create_session(
        user_id=user_id, org_id=chat.org_id, agent_id=chat.agent_id,
        config={"storage_bucket": chat.config["storage_bucket"], "workspace_path": FOLDER,
                "execution": {"kind": "device", "device_id": device_id, "device_name": "Mac"}, **(config or {})},
    )
    computer = computer_io(FOLDER, files, device_id=device_id)
    asked: list = []

    @asynccontextmanager
    async def fake_session_files(session, **kwargs):
        asked.append(session)
        yield computer

    async def bound(_db, _root):
        return Binding("bound")

    monkeypatch.setattr("surogates.api.routes.workspace.session_files", fake_session_files)
    monkeypatch.setattr("surogates.api.session_guards.binding_of", bound)
    return headers, user_id, device_chat, computer, asked


async def test_computer_folder_chats_are_hidden_from_other_members(client, app, session_factory, monkeypatch):
    _, user_id, device_chat, _, _ = await _computer_chat(client, app, session_factory, monkeypatch, {})
    other = await create_user(session_factory, device_chat.org_id)
    other_token = create_access_token(device_chat.org_id, other, {"sessions:read", "sessions:write", "tools:read"})
    hidden = await client.get(f"/v1/sessions/{device_chat.id}/workspace/editor", params={"path": "a.docx"},
                              headers={"Authorization": f"Bearer {other_token}"})
    assert hidden.status_code == 404


async def test_a_document_on_the_computer_opens_and_is_fetched_from_it(client, app, session_factory, monkeypatch):
    headers, _, device_chat, computer, _ = await _computer_chat(
        client, app, session_factory, monkeypatch, {"docs/Plan.docx": b"PK-on-the-mac"},
    )
    response = await client.get(f"/v1/sessions/{device_chat.id}/workspace/editor",
                                params={"path": "docs/Plan.docx"}, headers=headers)
    assert response.status_code == 200, response.text
    config = response.json()["config"]
    document = await office_sessions.get_open(session_factory, _row_id(config))
    device_id = device_chat.config["execution"]["device_id"]
    assert document.location == f"device:{device_id}:{FOLDER}/docs/Plan.docx"
    fetched = await client.get(f"/v1/office/files/{config['document']['url'].rsplit('/', 1)[1]}")
    assert fetched.status_code == 200 and fetched.content == b"PK-on-the-mac"
    assert (await office_sessions.get_open(session_factory, document.id)).base_version == computer.runner.files[f"{FOLDER}/docs/Plan.docx"][1]


async def test_a_document_over_the_computers_cap_is_refused(client, app, session_factory, monkeypatch):
    headers, _, device_chat, _, _ = await _computer_chat(client, app, session_factory, monkeypatch, {"big.docx": b"12345"})
    monkeypatch.setattr("surogates.api.routes.office.MAX_READ_BYTES", 4)
    response = await client.get(f"/v1/sessions/{device_chat.id}/workspace/editor",
                                params={"path": "big.docx"}, headers=headers)
    assert response.status_code == 413
    assert "50 MiB" in response.json()["detail"]


async def test_a_project_thread_on_the_computer_opens_the_real_file(client, app, session_factory, monkeypatch):
    project_id = uuid.uuid4()
    headers, user_id, master, computer, asked = await _computer_chat(
        client, app, session_factory, monkeypatch, {"a.docx": b"real"},
        config={"workspace_boundary": f"project:{project_id}"},
    )
    thread = await SessionStore(session_factory).create_session(
        user_id=user_id, org_id=master.org_id, agent_id=master.agent_id,
        config={**master.config, "execution": {**master.config["execution"], "history": {"thread": str(uuid.uuid4())}}},
    )

    async def master_of(_factory, pid):
        assert pid == project_id
        return master

    monkeypatch.setattr("surogates.office.files._master_of", master_of)
    response = await client.get(f"/v1/sessions/{thread.id}/workspace/editor", params={"path": "a.docx"}, headers=headers)
    assert response.status_code == 200, response.text
    assert [session.id for session in asked] == [master.id]
    document = await office_sessions.get_open(session_factory, _row_id(response.json()["config"]))
    assert document.location == f"device:{master.config['execution']['device_id']}:{FOLDER}/a.docx"
```

- [ ] **Step 2: Run them to see them fail**

Run: `/work/surogates/.venv/bin/python -m pytest tests/integration/test_office_routes.py -q -k "computer or project_thread"`
Expected: FAIL. The editor route answers 409 "Documents in a folder on a computer cannot be opened in the editor yet."

- [ ] **Step 3: Implement**

In `surogates/api/routes/office.py`:

- Remove `from surogates.devices.binding import device_of` and `from surogates.tools.workspace_io import StorageWorkspaceIO`.
- Add the imports:

```python
from surogates.devices.workspace import MAX_READ_BYTES, DeviceWorkspaceIO
from surogates.office.agent import location_of
from surogates.office.files import DOCUMENT_FILES, FolderNotReady, document_session
```

In `get_workspace_editor`, replace everything from `await require_device_access(request, session, tenant)` down to the line before `document = await _open_document(...)` with:

```python
    await require_device_access(request, session, tenant)
    user_id, user_name = await _acting_user(request, tenant)
    target = await _document_session(request, session)
    async with workspace_files(request, target) as files:
        if not isinstance(files, DOCUMENT_FILES):
            raise HTTPException(status_code=409, detail="This workspace cannot be opened in the editor yet.")
        stat = await files.stat(await files.resolve(path))
        if stat is None:
            raise HTTPException(status_code=404, detail=f"File not found: {path}")
        if isinstance(files, DeviceWorkspaceIO) and stat.size > MAX_READ_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"{path} is over 50 MiB, the most a document on a computer can be to open in the editor.",
            )
    location = location_of(target, path)
```

In `get_office_file`, replace the two lines `session = await _live_session(request, document)` and `async with workspace_files(request, session) as files:`, together with the `isinstance(files, StorageWorkspaceIO)` check under them, with:

```python
    target = await _document_session(request, await _live_session(request, document))
    async with workspace_files(request, target) as files:
        if not isinstance(files, DOCUMENT_FILES):
            raise HTTPException(status_code=409, detail="This workspace cannot be opened in the editor yet.")
```

Add beside `_live_session`:

```python
async def _document_session(request: Request, session: Any) -> Any:
    """The session whose files hold *session*'s documents: a project thread on a computer reaches the real folder
    through its master."""
    try:
        return await document_session(session, session_factory=request.app.state.session_factory)
    except FolderNotReady as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
```

In `_take_save`, above `bucket = session.config["storage_bucket"]`, add the comment:

```python
    # A chat on a computer keeps its bucket stamp: the pending area is the editor's, outside every chat's files.
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `/work/surogates/.venv/bin/python -m pytest tests/integration/test_office_routes.py -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add surogates/api/routes/office.py tests/integration/test_office_routes.py
git commit -m "feat(office): open documents from a folder on the computer, and serve them to the editor from it"
```

---

### Task 4: Saving to the computer

**Files:**
- Modify: `surogates/office/sessions.py` (add `mark_waiting`, `next_try`, `drop_pending`)
- Modify: `surogates/office/files.py` (add `editor_request_id`, `editor_invocation`)
- Modify: `surogates/office/saves.py` (`land_pending_saves`, `_land`)
- Modify: `surogates/session/events.py` (add `OFFICE_SAVE_WAITING`, `OFFICE_SAVED`)
- Modify: `surogates/api/routes/office.py` (`_take_save` cancels a replaced waiting write)
- Modify: `surogates/session/files.py` (module docstring)
- Test: `tests/integration/test_office_saves_computer.py`

**Interfaces:**
- Consumes: `editor_files`, `document_session`, `DOCUMENT_FILES` (Task 2); `DeviceWorkspaceIO.write_if/etag` (Task 1).
- Produces:
  - **Events:**
    - `EventType.OFFICE_SAVE_WAITING = "office.save_waiting"`, with data `{path, computer, reason}`. `reason` is `"offline"` or `"waiting"`.
    - `EventType.OFFICE_SAVED = "office.saved"`, with data `{path}`. It is sent only for a save that had waited.
    - `office.save_failed` gains a variant, `{path, reason: "declined", computer}`.
  - **Row helpers** in `office_sessions`:
    - `mark_waiting(row, reason) -> bool`
    - `next_try(row) -> None`
    - `drop_pending(row) -> tuple[str, str]`
  - **Request ids** in `office/files.py`:
    - `editor_request_id(doc_key, pending) -> str`
    - `editor_invocation(doc_key, pending) -> str`

**The rules this task implements:**
- **Computer offline** (`ComputerAway`, not revoked):
  - The save waits with reason `"offline"`.
  - It is not a failed try.
  - Its request id moves to the next try: `session_files` cancelled the change it left open.
- **No answer within `COMPUTER_WAIT_S` = 5 s** (`TimeoutError`), or too many open requests (`TooManyRequests`):
  - The save waits with reason `"waiting"`.
  - It keeps its request id, so the next pass joins the same operation.
- **Declined on the computer** (`PermissionError`: the user said no, or nobody answered in 10 minutes):
  - The pending save is dropped, its blob deleted, and a closing one closes its row.
  - The chats hear `office.save_failed` with reason `"declined"`.
  - The edits stay in the editor, and its next save asks again.
- **Any other failure** counts as a try, as before, and moves the request id to the next try: a replayed request would only repeat its recorded failure.
- **A project on a computer** saves with a plain guarded write through the master. There is no `land_editor_save`; the computer records the save as "you" at its next landing.
- **A newer save replacing one that was waiting** on the computer cancels the older save's open write. That keeps the older write from landing afterwards and turning the newer one into a conflict copy.

- [ ] **Step 1: Write the failing tests**

`tests/integration/test_office_saves_computer.py`:

```python
"""Landing the editor's saves in a folder on the user's computer."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete

from surogates.config import OfficeSettings
from surogates.db.models import OfficeSession
from surogates.devices.binding import Binding
from surogates.office import files as office_files
from surogates.office import saves as office_saves
from surogates.office import sessions as office_sessions
from surogates.office.files import EDITOR_REQUEST
from surogates.session.events import EventType
from surogates.session.files import ComputerAway
from surogates.session.store import SessionStore
from surogates.storage.backend import LocalBackend
from tests.device_computer import computer_io

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")

OFFICE = OfficeSettings(
    public_url="https://office.example", internal_url="http://docs.internal",
    callback_base_url="https://api.example", jwt_secret="j" * 32, token_secret="t" * 32,
)
FOLDER = "/home/me/notes"


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _no_rows(session_factory):
    async with session_factory() as db, db.begin():
        await db.execute(delete(OfficeSession))


@pytest.fixture
def storage(tmp_path):
    return LocalBackend(str(tmp_path))


class Desk:
    """The computer as the save job reaches it: its files, and what each request asked."""

    def __init__(self, monkeypatch, files):
        self.io = computer_io(FOLDER, files, device_id=str(uuid4()))
        self.requests: list[dict] = []
        self.away: ComputerAway | None = None
        self.hold = False  # the computer answers nothing: the user has not answered its prompt
        self.deny = False

        @asynccontextmanager
        async def fake_session_files(session, **kwargs):
            self.requests.append(kwargs)
            if self.away is not None:
                raise self.away
            yield self.io

        async def bound(_db, _root):
            return Binding("bound")

        original_run = self.io.runner.run

        async def run(kind, args, payload=None):
            if kind == "write" and self.hold:
                await asyncio.sleep(3600)
            if kind == "write" and self.deny:
                return {"error": {"type": "os", "code": "EACCES", "message": "The user denied this change on this computer"}}
            return await original_run(kind, args, payload)

        self.io.runner.run = run
        monkeypatch.setattr(office_files, "session_files", fake_session_files)
        monkeypatch.setattr(office_files, "binding_of", bound)
        monkeypatch.setattr(office_saves, "COMPUTER_WAIT_S", 0.2)


async def _computer_document(session_factory, storage, desk, path="a.docx"):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = SessionStore(session_factory)
    device_id = desk.io.identity.split(":", 1)[1]
    session = await store.create_session(user_id=user_id, org_id=org_id, agent_id="default", channel="web", config={
        "storage_bucket": "b", "workspace_path": FOLDER,
        "execution": {"kind": "device", "device_id": device_id, "device_name": "Mac"},
    })
    key = f"{FOLDER}/{path}"
    document = await office_sessions.open_or_reuse(
        session_factory, location=f"device:{device_id}:{key}", path=path, session_id=session.id,
    )
    await office_sessions.record_base_version(session_factory, document.id, desk.io.runner.files[key][1])
    return store, session, key, document


async def _pend(session_factory, storage, document, data, *, close=False):
    pending_key = f"_office/pending/{document.doc_key}/{uuid4().hex}"
    await storage.write("b", pending_key, data)
    await office_sessions.set_pending(session_factory, document.id, bucket="b", key=pending_key, close=close)
    return pending_key


async def _land(session_factory, redis_client, storage, store):
    return await office_saves.land_pending_saves(session_factory=session_factory, session_store=store,
                                                 storage=storage, redis=redis_client, office=OFFICE)


async def _types(store, session_id):
    return [event.type for event in await store.get_events(session_id)]


async def test_a_save_lands_on_the_computer_over_the_revision_it_opened(session_factory, redis_client, storage, monkeypatch):
    desk = Desk(monkeypatch, {"a.docx": b"v1"})
    store, session, key, document = await _computer_document(session_factory, storage, desk)
    pending_key = await _pend(session_factory, storage, document, b"v2")
    assert await _land(session_factory, redis_client, storage, store) == 1
    assert desk.io.runner.files[key][0] == b"v2"
    [request] = desk.requests
    assert request["request_id"].startswith(f"{EDITOR_REQUEST}{document.doc_key}.")
    assert request["access"].session_id == session.id
    still_open = await office_sessions.get_open(session_factory, document.id)
    assert still_open.pending_save is None and still_open.base_version == desk.io.runner.files[key][1]
    assert not await storage.exists("b", pending_key)


async def test_a_save_waits_for_an_offline_computer_without_failing(session_factory, redis_client, storage, monkeypatch):
    desk = Desk(monkeypatch, {"a.docx": b"v1"})
    store, session, key, document = await _computer_document(session_factory, storage, desk)
    await _pend(session_factory, storage, document, b"v2")
    desk.away = ComputerAway("Mac", revoked=False)
    for _ in range(office_saves.SAVE_FAILED_AFTER + 2):
        assert await _land(session_factory, redis_client, storage, store) == 0
    pending = (await office_sessions.get_open(session_factory, document.id)).pending_save
    assert pending["attempts"] == 0 and pending["waiting"] == "offline"
    types = await _types(store, session.id)
    assert types.count(EventType.OFFICE_SAVE_WAITING.value) == 1
    assert EventType.OFFICE_SAVE_FAILED.value not in types
    desk.away = None
    assert await _land(session_factory, redis_client, storage, store) == 1
    assert desk.io.runner.files[key][0] == b"v2"
    assert EventType.OFFICE_SAVED.value in await _types(store, session.id)


async def test_a_save_waiting_for_its_users_ok_joins_the_same_request_again(session_factory, redis_client, storage, monkeypatch):
    desk = Desk(monkeypatch, {"a.docx": b"v1"})
    store, session, _, document = await _computer_document(session_factory, storage, desk)
    await _pend(session_factory, storage, document, b"v2")
    desk.hold = True
    assert await _land(session_factory, redis_client, storage, store) == 0
    assert await _land(session_factory, redis_client, storage, store) == 0
    first, second = desk.requests
    assert first["request_id"] == second["request_id"] and first["change"] == second["change"]
    pending = (await office_sessions.get_open(session_factory, document.id)).pending_save
    assert pending["waiting"] == "waiting" and pending["attempts"] == 0


async def test_a_declined_save_is_dropped_and_said(session_factory, redis_client, storage, monkeypatch):
    desk = Desk(monkeypatch, {"a.docx": b"v1"})
    store, session, key, document = await _computer_document(session_factory, storage, desk)
    pending_key = await _pend(session_factory, storage, document, b"v2")
    desk.deny = True
    assert await _land(session_factory, redis_client, storage, store) == 0
    assert desk.io.runner.files[key][0] == b"v1"
    still_open = await office_sessions.get_open(session_factory, document.id)
    assert still_open is not None and still_open.pending_save is None
    assert not await storage.exists("b", pending_key)
    [failed] = [e for e in await store.get_events(session.id) if e.type == EventType.OFFICE_SAVE_FAILED.value]
    assert failed.data == {"path": "a.docx", "reason": "declined", "computer": "Mac"}


async def test_a_failed_save_asks_the_computer_anew_next_time(session_factory, redis_client, storage, monkeypatch):
    desk = Desk(monkeypatch, {"a.docx": b"v1"})
    store, _, _, document = await _computer_document(session_factory, storage, desk)
    await _pend(session_factory, storage, document, b"v2")
    original = desk.io.runner.run

    async def unavailable(kind, args, payload=None):
        if kind == "write":
            return {"error": {"type": "unavailable", "message": "another chat on this computer is working in this folder"}}
        return await original(kind, args, payload)

    desk.io.runner.run = unavailable
    assert await _land(session_factory, redis_client, storage, store) == 0
    desk.io.runner.run = original
    assert await _land(session_factory, redis_client, storage, store) == 1
    first, second = desk.requests
    assert first["request_id"] != second["request_id"]


async def test_a_changed_file_on_the_computer_lands_as_a_copy_beside_it(session_factory, redis_client, storage, monkeypatch):
    async def no_drop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(office_saves, "drop_editors", no_drop)
    monkeypatch.setattr(office_saves, "close_pages", no_drop)
    desk = Desk(monkeypatch, {"a.docx": b"v1"})
    store, _, key, document = await _computer_document(session_factory, storage, desk)
    await desk.io.write(key, b"someone else")
    await _pend(session_factory, storage, document, b"mine")
    await _land(session_factory, redis_client, storage, store)
    assert desk.io.runner.files[key][0] == b"someone else"
    copies = [k for k in desk.io.runner.files if k.startswith(f"{FOLDER}/a (edited ")]
    assert len(copies) == 1 and desk.io.runner.files[copies[0]][0] == b"mine"
```

- [ ] **Step 2: Run them to see them fail**

Run: `/work/surogates/.venv/bin/python -m pytest tests/integration/test_office_saves_computer.py -q`
Expected: FAIL. The save raises `RuntimeError: a.docx: saving into a folder on a computer is not supported`, and `EventType` has no `OFFICE_SAVE_WAITING`.

- [ ] **Step 3: Add the events**

In `surogates/session/events.py`, after `OFFICE_SAVE_FAILED`:

```python
    # A save from the document editor waits for the computer its file is on (data: path, computer, and reason:
    # "offline", or "waiting" for the computer's user to allow it). Said once per pending save.
    OFFICE_SAVE_WAITING = "office.save_waiting"
    # A save that had waited for its computer landed (data: path).
    OFFICE_SAVED = "office.saved"
```

and extend the `OFFICE_SAVE_FAILED` comment: `(data: path, and status or attempts; or reason "declined" and computer)`.

- [ ] **Step 4: Add the row helpers**

In `surogates/office/sessions.py`, after `mark_attempt`:

```python
def mark_waiting(row: OfficeSession, reason: str) -> bool:
    """Record that the pending save waits on its computer for *reason*; whether that is news to its chats."""
    pending = dict(row.pending_save or {})
    if pending.get("waiting") == reason:
        return False
    pending["waiting"] = reason
    row.pending_save = pending
    return True


def next_try(row: OfficeSession) -> None:
    """Let the next try ask the computer anew: a request joined again only repeats what it got."""
    pending = dict(row.pending_save or {})
    pending["try"] = int(pending.get("try", 0)) + 1
    row.pending_save = pending


def drop_pending(row: OfficeSession) -> tuple[str, str]:
    """Give up the pending save; the (bucket, key) of its blob, to delete. A closing save's row closes."""
    pending = row.pending_save or {}
    blob = (str(pending["bucket"]), str(pending["key"]))
    if pending.get("close"):
        _close(row)
    row.pending_save = None
    return blob
```

- [ ] **Step 5: Add the request ids**

In `surogates/office/files.py`, add the import `from pathlib import PurePosixPath` and `from surogates.devices.operations import REQUEST_PREFIX`, then:

```python
def editor_request_id(doc_key: str, pending: dict[str, Any]) -> str:
    """The request id of a pending save's write to a computer: its blob, and the try that asks anew."""
    return f"{doc_key}.{PurePosixPath(str(pending['key'])).name}.{int(pending.get('try', 0))}"


def editor_invocation(doc_key: str, pending: dict[str, Any]) -> str:
    """The device invocation of that write, as ``session_files`` names it."""
    return f"{REQUEST_PREFIX}{EDITOR_REQUEST}{editor_request_id(doc_key, pending)}"
```

- [ ] **Step 6: Land on the computer**

In `surogates/office/saves.py`:

- Remove the import `from surogates.session.files import session_files`; it is replaced by `editor_files`.
- Add the imports:

```python
import asyncio
from contextlib import nullcontext

from surogates.devices.binding import device_of
from surogates.devices.operations import TooManyRequests
from surogates.office.files import DOCUMENT_FILES, document_session, editor_files, editor_request_id
from surogates.session.files import ComputerAway
from surogates.tools.workspace_io import RevisionConflict
```

and remove `StorageWorkspaceIO` from the `surogates.tools.workspace_io` import (keep `RevisionConflict`). Then add:

```python
# How long one pass waits on a computer for a save: one still waiting is the next pass's to join again.
COMPUTER_WAIT_S = 5.0


class SaveWaiting(Exception):
    """The save waits on its computer, to come online or for its user to allow it: no failed try."""

    def __init__(self, reason: str, computer: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.computer = computer


class SaveDeclined(Exception):
    """The computer's user declined the save, or let its prompt go unanswered."""

    def __init__(self, computer: str) -> None:
        super().__init__(computer)
        self.computer = computer
```

In `land_pending_saves`, replace the `try:` / `except Exception as exc:` block inside `async with office_sessions.claim_pending(...) as row:` with:

```python
            waited = bool((row.pending_save or {}).get("waiting"))
            try:
                blob = await _land(row, session_factory=session_factory, session_store=session_store,
                                   storage=storage, redis=redis, office=office, settings=settings)
                landed += 1
                if waited:
                    await _announce(session_store, row.session_ids, EventType.OFFICE_SAVED, {"path": row.path})
            except SaveWaiting as waiting:
                # Not a failed try. Its new updated_at moves the row behind the others.
                row.updated_at = datetime.now(timezone.utc)
                if waiting.reason == "offline":
                    office_sessions.next_try(row)  # the change it left open was cancelled
                if office_sessions.mark_waiting(row, waiting.reason):
                    await _announce(session_store, row.session_ids, EventType.OFFICE_SAVE_WAITING,
                                    {"path": row.path, "computer": waiting.computer, "reason": waiting.reason})
                logger.info("the save of %s waits on %s (%s)", row.path, waiting.computer, waiting.reason)
            except SaveDeclined as declined:
                # The edits stay in the editor: its next save asks again.
                blob = office_sessions.drop_pending(row)
                logger.info("%s declined the save of %s", declined.computer, row.path)
                await _announce(session_store, row.session_ids, EventType.OFFICE_SAVE_FAILED,
                                {"path": row.path, "reason": "declined", "computer": declined.computer})
            except Exception as exc:
                if isinstance(exc, Refused) and str(exc) == BUSY:
                    # The project is busy: not a failed try. Its new updated_at moves the row behind the others.
                    row.updated_at = datetime.now(timezone.utc)
                    logger.info("the project of %s is busy: its save waits", row.path)
                    continue
                # A computer's request joined again would only repeat this failure: the next try asks anew.
                office_sessions.next_try(row)
                # Commits with the row; its new updated_at moves it behind the others.
                attempts = office_sessions.mark_attempt(row)
                logger.exception("could not land the save of %s (attempt %d)", row.path, attempts)
                if attempts == SAVE_FAILED_AFTER:
                    await _announce(session_store, row.session_ids, EventType.OFFICE_SAVE_FAILED,
                                    {"path": row.path, "attempts": attempts})
```

Replace `_land` from `data = await storage.read(*blob)` to its end with:

```python
    data = await storage.read(*blob)
    target = await document_session(session, session_factory=session_factory)
    computer = _computer_of(target)
    try:
        async with editor_files(target, storage=storage, session_factory=session_factory, redis=redis,
                                request_id=editor_request_id(row.doc_key, pending), data=data) as files:
            if not isinstance(files, DOCUMENT_FILES):
                raise RuntimeError(f"{row.path}: this workspace cannot take the editor's saves")
            async with asyncio.timeout(COMPUTER_WAIT_S) if computer is not None else nullcontext():
                await _write(row, files, data, session=session, session_factory=session_factory,
                             session_store=session_store, storage=storage, redis=redis, office=office,
                             settings=settings, on_computer=computer is not None)
    except ComputerAway as away:
        if away.revoked:
            raise
        raise SaveWaiting("offline", away.name) from None
    except (TimeoutError, TooManyRequests):
        if computer is None:
            raise
        raise SaveWaiting("waiting", computer) from None
    except PermissionError:
        if computer is None:
            raise
        raise SaveDeclined(computer) from None
    return blob


def _computer_of(session: Any) -> str | None:
    """The name of the computer *session*'s files are on, or None in the cloud."""
    if device_of(session.config) is None:
        return None
    return str((session.config.get("execution") or {}).get("device_name") or "your computer")


async def _write(row: OfficeSession, files: Any, data: bytes, *, session: Any, session_factory: Any, session_store: Any,
                 storage: Any, redis: Any, office: OfficeSettings, settings: Any, on_computer: bool) -> None:
    """Write the save over the version the editor opened, or, when the file changed since, beside it as a copy."""
    key = await files.resolve(row.path)
    # On a computer the project's own landing records it, as yours: the save is the folder's, through its master.
    project = None if on_computer else await _project_of(session_factory, session)
    try:
        if project is None:
            await files.write_if(key, data, expected=row.base_version)
        else:
            if settings is None:
                raise RuntimeError("landing a project's save needs the runtime's settings")
            state = SimpleNamespace(session_factory=session_factory, session_store=session_store, storage=storage,
                                    settings=settings, redis=redis)
            pending = dict(row.pending_save or {})
            await land_editor_save(
                state, project, path=row.path,
                author=await editor_author(session_store, project, list(pending.get("authors") or [])),
                write=lambda: files.write_if(key, data, expected=row.base_version),
            )
    except RevisionConflict:
        # An earlier landing may have written these bytes and stopped before its row
        # change committed. The etag comes first: a write between the two then shows
        # as a conflict at the next save, never as landed.
        version = await files.etag(key)
        try:
            current = await files.read(key)
        except FileNotFoundError:
            current = None
        if current == data:
            office_sessions.mark_landed(row, version)
            return
        copy_path = await _write_copy(files, row.path, data)
        await _announce(session_store, row.session_ids, EventType.OFFICE_CONFLICT,
                        {"path": row.path, "conflict_path": copy_path})
        await drop_editors(office, row.doc_key, [str(user) for user in row.editors])
        await close_pages(office, row.doc_key, row.session_ids)
        office_sessions.mark_closed(row)
    else:
        office_sessions.mark_landed(row, await files.etag(key))
```

(`_land` keeps its first lines: `pending = dict(row.pending_save or {})`, the blob, `live_session`, the closed-chat branch. The cloud behaviour is unchanged: the same calls in the same order, now inside `_write`.)

Change `_write_copy`'s first parameter annotation from `StorageWorkspaceIO` to `Any`.

- [ ] **Step 7: Cancel a replaced waiting write**

In `surogates/api/routes/office.py`, `_take_save`, read the previous pending save before replacing it. Replace `discarded = await office_sessions.set_pending(` with:

```python
    previous = dict(document.pending_save or {})
    discarded = await office_sessions.set_pending(
```

(the call's arguments unchanged), and after the `if discarded:` block add:

```python
    if discarded and discarded == previous.get("key") and previous.get("waiting") == "waiting":
        # Its write may still be open on the computer: landing later, it would make this newer save a conflict copy.
        await _cancel_editor_write(request, session, document.doc_key, previous)
```

and the helper (imports `from surogates.devices.operations import DeviceOperations` and `editor_invocation` from `surogates.office.files`):

```python
async def _cancel_editor_write(request: Request, session: Any, doc_key: str, pending: dict[str, Any]) -> None:
    try:
        target = await document_session(session, session_factory=request.app.state.session_factory)
        await DeviceOperations(request.app.state.session_factory, request.app.state.redis).cancel_invocation(
            target.id, editor_invocation(doc_key, pending),
        )
    except Exception:  # the newer save is kept either way; a write that still lands shows as a conflict copy
        logger.warning("could not cancel the replaced save of %s on its computer", doc_key, exc_info=True)
```

- [ ] **Step 8: Say where the pending saves are**

In `surogates/session/files.py`, change the module docstring's last sentence to:

```
Nothing reads or writes the cloud workspace of a session on a computer; its storage
fields are kept for ``create_child_session``, and its bucket for the document
editor's pending saves, under ``_office/pending/``, outside every chat's files.
```

- [ ] **Step 9: Run the tests to see them pass**

Run: `/work/surogates/.venv/bin/python -m pytest tests/integration/test_office_saves_computer.py tests/integration/test_office_saves.py tests/integration/test_office_routes.py tests/integration/test_office_sessions.py -q`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add surogates/office/sessions.py surogates/office/files.py surogates/office/saves.py surogates/session/events.py surogates/api/routes/office.py surogates/session/files.py tests/integration/test_office_saves_computer.py
git commit -m "feat(office): land the editor's saves on the computer, waiting for it without failing"
```

### Task 5: The agent edits documents on the computer

**Files:**
- Modify: `surogates/harness/tool_schemas.py:84-90` (`has_file_pane`)
- Modify: `surogates/office/agent.py` (`open_for_agent`, imports)
- Modify: `surogates/office/guard.py:18-20` (`_applies`, imports)
- Modify: `surogates/office/turn_end.py` (`save_turn`)
- Modify: `surogates/harness/loop_artifact_completion.py:1528-1540` (`_save_office_documents`)
- Modify: `surogates/tools/builtin/office.py` (`_picture`)
- Test: `tests/test_office_tool_offering.py`, `tests/integration/test_office_agent.py`, `tests/integration/test_office_guard.py`, `tests/integration/test_office_turn_end.py`

**Interfaces:**
- Consumes: `document_session`, `editor_files`, `DOCUMENT_FILES`, `FolderNotReady` (Task 2); `location_of` (Task 2); `DeviceWorkspaceIO.etag/write_if` (Task 1).
- Produces:
  - **Offering:** `has_file_pane(session)` is true for computer chats as for cloud ones. It stays false for channels, voice, the API and websites.
  - **Opening:** `open_for_agent` opens documents on a computer. A project thread opens the real file through its master. A new document is made through an editor request whose id starts `editor.new.`.
  - **Saving at the end of a turn:** `save_turn(*, store, session, after, office, session_factory)`; the `storage` and `redis` parameters are removed. It names documents with `location_of` and opens no files.
  - **Guard:** `refused_open_document` and `open_document_note` apply on computers.

- [ ] **Step 1: Write the failing tests**

In `tests/test_office_tool_offering.py`, rename `test_a_file_pane_is_a_web_chat_or_a_projects_thread_in_the_cloud` to `test_a_file_pane_is_a_web_chat_or_a_projects_thread` and change its last assertion to:

```python
    # Surogate Desktop shows the pane beside a chat on a folder of the computer too.
    assert has_file_pane(SimpleNamespace(channel="web", config=device)) is True
```

In `tests/integration/test_office_agent.py`, add the imports:

```python
from contextlib import asynccontextmanager

from surogates.devices.binding import Binding
from surogates.office import agent as office_agent
from surogates.office import files as office_files
from surogates.office.files import EDITOR_REQUEST
from tests.device_computer import computer_io
```

Replace `test_other_files_and_computer_folders_are_refused` with the first test below, keeping its non-`.docx` assertion, then add the other three:

```python
async def test_other_files_are_refused(session_factory, redis_client, storage):
    session = await _chat(session_factory)
    with pytest.raises(AgentDocumentError, match="Word documents"):
        await _open(session, storage, session_factory, redis_client, FakeRuntime(), "a.xlsx", create=True)


FOLDER = "/home/me/notes"


def _computer(monkeypatch, files, device_id):
    """A fake computer answering every request of the office code; the sessions and requests asked for, kept."""
    io = computer_io(FOLDER, files, device_id=device_id)
    asked: list = []

    @asynccontextmanager
    async def fake_session_files(session, **kwargs):
        asked.append((session, kwargs))
        yield io

    async def bound(_db, _root):
        return Binding("bound")

    monkeypatch.setattr(office_agent, "session_files", fake_session_files)
    monkeypatch.setattr(office_files, "session_files", fake_session_files)
    monkeypatch.setattr(office_files, "binding_of", bound)
    return io, asked


def _on_computer(device_id, **config):
    return {"workspace_path": FOLDER, "execution": {"kind": "device", "device_id": device_id, "device_name": "Mac"}, **config}


async def test_a_document_on_the_computer_opens_in_the_agents_editor(session_factory, redis_client, storage, monkeypatch):
    device_id = str(uuid4())
    io, _ = _computer(monkeypatch, {"a.docx": b"PK"}, device_id)
    session = await _chat(session_factory, config=_on_computer(device_id))
    runtime = FakeRuntime()
    opened = await _open(session, storage, session_factory, redis_client, runtime, "a.docx", create=False)
    assert opened.document.location == f"device:{device_id}:{FOLDER}/a.docx"
    assert opened.created is False and len(runtime.opened) == 1


async def test_a_new_document_on_the_computer_is_made_through_the_editors_own_request(session_factory, redis_client, storage, monkeypatch):
    device_id = str(uuid4())
    io, asked = _computer(monkeypatch, {}, device_id)
    session = await _chat(session_factory, config=_on_computer(device_id))
    opened = await _open(session, storage, session_factory, redis_client, FakeRuntime(), "notes/Plan.docx", create=True)
    assert opened.created is True
    assert io.runner.files[f"{FOLDER}/notes/Plan.docx"][0] == BLANK_DOCX.read_bytes()
    made = [kwargs for _, kwargs in asked if kwargs.get("change")]
    assert len(made) == 1 and made[0]["request_id"].startswith(f"{EDITOR_REQUEST}new.")


async def test_a_thread_on_the_computer_opens_its_projects_real_file(session_factory, redis_client, storage, monkeypatch):
    device_id, project_id = str(uuid4()), uuid4()
    io, asked = _computer(monkeypatch, {"a.docx": b"real"}, device_id)
    master = await _chat(session_factory, config=_on_computer(device_id, workspace_boundary=f"project:{project_id}"))
    thread = await _chat(session_factory, config=_on_computer(
        device_id, workspace_boundary=f"project:{project_id}", workstream_role=THREAD,
        execution={"kind": "device", "device_id": device_id, "history": {"thread": str(uuid4())}},
    ))

    async def master_of(_factory, pid):
        assert pid == project_id
        return master

    monkeypatch.setattr(office_files, "_master_of", master_of)
    opened = await _open(thread, storage, session_factory, redis_client, FakeRuntime(), "a.docx", create=False)
    assert {session.id for session, _ in asked} == {master.id}
    assert opened.document.location == f"device:{device_id}:{FOLDER}/a.docx"
```

(`_open(session, storage, session_factory, redis_client, runtime, path, *, create=False)` and `_chat(session_factory, *, config=None)` are the file's existing helpers.)

In `tests/integration/test_office_guard.py`, add:

```python
async def test_writing_an_open_document_on_the_computer_is_refused(session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await SessionStore(session_factory).create_session(
        user_id=user_id, org_id=org_id, agent_id="writer", channel="web", config={
            "storage_bucket": "b", "workspace_path": "/home/me/notes",
            "execution": {"kind": "device", "device_id": "00000000-0000-0000-0000-000000000001"},
        },
    )
    await office_sessions.open_or_reuse(session_factory, location=location_of(session, "a.docx"), path="a.docx", session_id=session.id)
    said = await guard.refused_open_document("write_file", {"path": "/home/me/notes/a.docx", "content": "x"}, session,
                                             session_factory=session_factory)
    assert said and "office_edit" in said
```

In `tests/integration/test_office_turn_end.py`, remove `storage=storage,` and `redis=None` from every `turn_end.save_turn(...)` call, then add:

```python
async def test_a_turn_on_the_computer_saves_the_document_it_edited(session_factory, monkeypatch):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = SessionStore(session_factory)
    session = await store.create_session(user_id=user_id, org_id=org_id, agent_id="writer", channel="web", config={
        "storage_bucket": "b", "workspace_path": "/home/me/notes",
        "execution": {"kind": "device", "device_id": "00000000-0000-0000-0000-000000000001"},
    })
    document = await office_sessions.open_or_reuse(
        session_factory, location=location_of(session, "a.docx"), path="a.docx", session_id=session.id,
    )
    after = (await store.get_events(session.id))[-1].id if await store.get_events(session.id) else 0
    await store.emit_event(session.id, EventType.TOOL_CALL, {"name": "office_edit", "arguments": {"path": "a.docx", "ops": []}})
    flushed, sent = [], []

    class Runtime:
        def __init__(self, office):
            pass

        async def flush(self, page):
            flushed.append(page)
            return True

    async def force_save(office, key, userdata):
        sent.append(userdata)
        return 4  # nothing unsaved: every edit is saved

    monkeypatch.setattr(turn_end, "RuntimeOffice", Runtime)
    monkeypatch.setattr(turn_end, "force_save", force_save)
    await turn_end.save_turn(store=store, session=session, after=after, office=OFFICE, session_factory=session_factory)
    assert flushed == [f"{document.doc_key}.{session.id}"] and len(sent) == 1
```

(`EventType`, `location_of`, `create_org`, `create_user` and `OFFICE` are already imported in that file.)

- [ ] **Step 2: Run them to see them fail**

Run: `/work/surogates/.venv/bin/python -m pytest tests/test_office_tool_offering.py tests/integration/test_office_agent.py tests/integration/test_office_guard.py tests/integration/test_office_turn_end.py -q`
Expected: FAIL.
- `has_file_pane` is False on computers.
- `open_for_agent` raises "Documents in a folder on a computer cannot be edited live yet."
- The guard returns None.
- `save_turn()` gets an unexpected keyword argument or misses `storage`.

- [ ] **Step 3: Offer the tools on computers**

In `surogates/harness/tool_schemas.py`:

```python
def has_file_pane(session: Any) -> bool:
    """Whether the session's chat shows a document beside it: the web and Surogate Desktop (on a folder of the
    computer too), Surogate Studio's Work chat and a project's threads; never a phone call, a channel, a website
    visitor or an API caller."""
    from surogates.workstreams import is_project_thread

    config = getattr(session, "config", None) or {}
    return getattr(session, "channel", None) in ("web", STUDIO_CHANNEL) or is_project_thread(config)
```

- [ ] **Step 4: Open documents on computers**

In `surogates/office/agent.py`, replace `open_for_agent` with the version below.
- Add the imports `from uuid import uuid4`, `from surogates.devices.workspace import DeviceOperationError, said`, `from surogates.office.files import DOCUMENT_FILES, FolderNotReady, document_session, editor_files` and `from surogates.session.files import ComputerAway`.
- `device_of` stays, because `location_of` uses it.

```python
async def open_for_agent(
    *, session: Any, agent_id: str, agent_name: str, path: str, create: bool, office: OfficeSettings,
    runtime: RuntimeOffice, storage: Any, session_factory: Any, redis: Any,
) -> AgentDocument:
    """The agent's editor page on the document at *path*, made blank first when *create* and it does not exist.

    A project thread on a computer opens the real file, through its project's master: its own copy is not the
    document."""
    kind = office_kind(path)
    if kind is None or kind.file_type != "docx":
        raise AgentDocumentError("office_read and office_edit work on Word documents (.docx) for now.")
    created = False
    try:
        target = await document_session(session, session_factory=session_factory)
        async with session_files(target, storage=storage, session_factory=session_factory, redis=redis) as files:
            if not isinstance(files, DOCUMENT_FILES):
                raise AgentDocumentError("This workspace cannot be edited live yet.")
            try:
                key = await files.resolve(path)
            except (WorkspaceSandboxError, ValueError) as exc:  # a traversal, or a NUL in the path
                raise AgentDocumentError(str(exc)) from None
            missing = await files.etag(key) is None
        if missing:
            if not create:
                raise AgentDocumentError(
                    f"No document at {path} in the project's files: one this thread made this turn reaches them when the turn ends."
                    if is_project_thread(session.config) else f"No document at {path}."
                )
            created = await _make_blank(target, path, storage=storage, session_factory=session_factory, redis=redis)
    except FolderNotReady as exc:
        raise AgentDocumentError(str(exc)) from None
    except ComputerAway as exc:
        raise AgentDocumentError(f"{exc}: the document opens once it is back.") from None
    except PermissionError:
        raise AgentDocumentError(f"{path} was not made: the computer's user declined it.") from None
    except (DeviceOperationError, OSError) as exc:
        raise AgentDocumentError(said(exc)) from None
    location = location_of(target, path)
    user_id = agent_user_id(agent_id, session.id)
    try:
        document = await office_sessions.open_waiting(
            session_factory, location=location, path=path, session_id=session.id, user=(user_id, agent_name),
        )
    except office_sessions.DocumentClosing:
        raise AgentDocumentError("The document is still being saved; try again in a moment.") from None
    config = build_editor_config(
        settings=office, row_id=document.id, doc_key=document.doc_key, path=path, kind=kind,
        user_id=user_id, user_name=agent_name, edit=True, theme="light", plugins=False,
    )
    page = await runtime.open(doc_key=document.doc_key, session_id=str(session.id), config=config,
                              document_server_url=office.internal_url)
    return AgentDocument(page_id=page, document=document, path=path, user_id=user_id, created=created)


async def _make_blank(target: Any, path: str, *, storage: Any, session_factory: Any, redis: Any) -> bool:
    """Make *path* a blank document; whether this call made it. On a computer, through the editor's own request,
    which its app asks about once in a folder that asks every time."""
    blank = BLANK_DOCX.read_bytes()
    async with editor_files(target, storage=storage, session_factory=session_factory, redis=redis,
                            request_id=f"new.{uuid4().hex}", data=blank) as files:
        try:
            await files.write_if(await files.resolve(path), blank, expected=None)
        except RevisionConflict:
            return False  # someone made it meanwhile: open theirs
    return True
```

Keep the `StorageWorkspaceIO` import: `location_of` uses it.

- [ ] **Step 5: Guard documents on computers**

In `surogates/office/guard.py`, remove `from surogates.devices.binding import device_of` and replace `_applies`:

```python
def _applies(session: Any, session_factory: Any) -> bool:
    return session_factory is not None and load_settings().office.agent_editing
```

- [ ] **Step 6: The turn's save names documents without opening files**

In `surogates/office/turn_end.py`:
- Remove the imports of `session_files`, `WorkspaceSandboxError` and `StorageWorkspaceIO`.
- Change `from surogates.office.agent import relative_path` to `from surogates.office.agent import location_of, relative_path`.
- Replace `save_turn` with:

```python
async def save_turn(*, store: Any, session: Any, after: int, office: OfficeSettings, session_factory: Any) -> None:
    """Save every document this turn edited: flush the agent's editor, force a save, wait for it to come."""
    paths = await edited_paths(store, session.id, after)
    if not paths:
        return
    # Waiting on the document server holds none of the worker's turn slots; a turn that edited nothing gives none back.
    async with turn_waiting():
        # As office_edit named them: stripped, relative to the workspace, one name however the file is reached.
        locations = list(dict.fromkeys(location_of(session, relative_path(path.strip(), session)) for path in paths))
        runtime = RuntimeOffice(office)
        for location in locations:
            document = await office_sessions.open_at(session_factory, location)
            if document is not None:
                await _save(runtime, office, session_factory, document, str(session.id))
```

(A path the call refused, a traversal or a NUL, names no open row, so nothing is saved for it.)

In `surogates/harness/loop_artifact_completion.py`, `_save_office_documents`:

```python
            office = load_settings().office
            if not office.agent_editing:
                return
            # Local: the office package reaches back into the session and sandbox modules this one is imported by.
            from surogates.office.turn_end import save_turn

            await save_turn(store=self._store, session=session, after=self._turn_after_event_id, office=office,
                            session_factory=self._session_factory)
```

If `device_of` is then unused in that module, remove its import.

- [ ] **Step 7: Pictures on a computer**

In `surogates/tools/builtin/office.py`, add the imports `from surogates.devices.workspace import DeviceOperationError, said` and `from surogates.session.files import ComputerAway`, and change `_picture`'s read to:

```python
    try:
        async with session_files(session, storage=kwargs["storage"], session_factory=kwargs["session_factory"],
                                 redis=kwargs["redis"]) as files:
            data = await files.read(await files.resolve(path), max_bytes=MAX_IMAGE_BYTES + 1)
    except (FileNotFoundError, WorkspaceSandboxError, ValueError):  # ValueError: a NUL in the path
        raise AgentDocumentError(f"No picture at {path}.") from None
    except (ComputerAway, DeviceOperationError, OSError) as exc:
        raise AgentDocumentError(f"Could not read {path}: {said(exc)}") from None
```

- [ ] **Step 8: Run the tests to see them pass**

Run: `/work/surogates/.venv/bin/python -m pytest tests/test_office_tool_offering.py tests/test_office_loop.py tests/integration/test_office_agent.py tests/integration/test_office_guard.py tests/integration/test_office_turn_end.py tests/integration/test_office_tools.py -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add surogates/harness/tool_schemas.py surogates/office/agent.py surogates/office/guard.py surogates/office/turn_end.py surogates/harness/loop_artifact_completion.py surogates/tools/builtin/office.py tests/test_office_tool_offering.py tests/integration/test_office_agent.py tests/integration/test_office_guard.py tests/integration/test_office_turn_end.py
git commit -m "feat(office): the agent edits documents in a folder on the computer live"
```

---

### Task 6: The desktop asks once per document

**Files:**
- Modify: `desktop/src/binding/approvals.ts` (the `EDITOR` constant, `ApprovalRequest`, `requestFor`, `Approvals.admit`, `Approvals.asking`)
- Modify: `desktop/src/shell/prompt-content.ts` (the editor's save prompt)
- Modify: `desktop/src/files/operations.ts:356` (the `create` comment)
- Test: `desktop/test/approvals.test.ts`, `desktop/test/prompt-content.test.ts`

**Interfaces:**
- Consumes: the server's editor writes, invocation `request:editor.<…>` (Task 2). A new document or a conflict copy carries `create: true` (Task 1).
- Produces:
  - In "Ask every time", an editor write to a document asks once.
  - After "Allow", that document's later editor writes in the same chat go through unasked until the app quits.
  - A deny is not remembered.
  - The prompt says it is the document editor saving.

- [ ] **Step 1: Write the failing tests**

In `desktop/test/approvals.test.ts`, inside the `describe` that holds the canvas test, add:

```ts
  it("asks once about the document editor's saves of a document, then lets that document's saves through", async () => {
    bind(ROOT, "ask");
    user.auto = "allow";
    const doc = `${FOLDER}/docs/Plan.docx`;
    const save = (key: string, n: number) => ({ ...op("write", { key, data: "UEs=" }), invocationId: `request:editor.k1.s${n}.0` });
    expect(await approvals.admit(save(doc, 1), never())).toBeNull();
    expect(await approvals.admit(save(doc, 2), never())).toBeNull();
    expect(user.asked).toHaveLength(1);
    expect(user.asked[0]).toMatchObject({ kind: "change", action: "write", path: doc, editor: true });
    // Another document asks for itself; the page's own request and an agent's tool call writing this one still ask.
    expect(await approvals.admit(save(`${FOLDER}/other.docx`, 3), never())).toBeNull();
    expect(await approvals.admit({ ...op("write", { key: doc, data: "" }), invocationId: "request:0f3a9c2e7b1d4a6f" }, never())).toBeNull();
    expect(await approvals.admit(op("write", { key: doc, data: "" }), never())).toBeNull();
    expect(user.asked).toHaveLength(4);
  });

  it("asks again about a document whose editor save was denied", async () => {
    bind(ROOT, "ask");
    user.auto = "deny";
    const save = { ...op("write", { key: `${FOLDER}/a.docx`, data: "" }), invocationId: "request:editor.k1.s1.0" };
    expect(await approvals.admit(save, never())).toEqual(CHANGE_DENIED);
    expect(await approvals.admit({ ...save, id: "again" }, never())).toEqual(CHANGE_DENIED);
    expect(user.asked).toHaveLength(2);
  });
```

In `desktop/test/prompt-content.test.ts`, add:

```ts
  it("says the document editor saves a document, and that later saves of it go through", () => {
    const content = approval({
      kind: "change", chat: CHAT, action: "write", path: "/home/me/notes/docs/Plan.docx", bytes: 2048, preview: null, editor: true,
    });
    expect(content.title).toBe("Save Plan.docx?");
    expect(content.lead).toBe(
      `The document editor wants to save this document in notes, ${sizeOf(2048)}. Allowed, its later saves of it go through until you quit Surogate.`,
    );
    expect(content.details).toEqual([{ label: "File", value: "docs/Plan.docx", code: true, keep: "" }]);
  });
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd desktop && npx vitest run test/approvals.test.ts test/prompt-content.test.ts`
Expected: FAIL. The second editor save asks again, and the editor prompt is still "Write Plan.docx?".

- [ ] **Step 3: Implement the approvals**

In `desktop/src/binding/approvals.ts`, below `const REQUEST = "request:";`:

```ts
// The document editor's saves (surogates/office/files.py): its request ids start "editor.", which no request of the
// chat's page can name. In a chat that asks every time, a document's first is asked about; once allowed, that
// document's later saves in the chat go through until the app quits. A denial is not remembered.
const EDITOR = `${REQUEST}editor.`;
const isEditorSave = (operation: Operation): boolean => operation.kind === "write" && operation.invocationId.startsWith(EDITOR);
```

In the `ApprovalRequest` union's `change` member, add `editor?: true;` after `download?: DownloadBy;`.

In `requestFor`, the `write` branch:

```ts
  if (kind === "write") {
    const bytes = Buffer.byteLength(text("data"), "base64");
    return {
      kind: "change", chat, action: kind, path: text("key"), bytes, preview: previewOf(text("data"), bytes),
      ...(download ? { download } : {}), ...(isEditorSave(operation) ? { editor: true as const } : {}),
    };
  }
```

In `class Approvals`, beside the other private fields:

```ts
  // The documents whose editor saves its user allowed: a chat's root and the file's key.
  private readonly editorSaves = new Set<string>();
```

In `admit`, in the callback passed to `inLine`, just before `return answer === "allow" || answer === "stop_asking" ? null : denied(operation.kind);`:

```ts
      if (answer === "allow" && isEditorSave(operation)) this.editorSaves.add(`${root}\0${String(operation.args.key)}`);
```

In `asking`, after the `unasked` computation and before the download check, add:

```ts
    const allowedSave = isEditorSave(operation) && typeof key === "string" && posix.normalize(key) === key
      && this.editorSaves.has(`${operation.sessionId}\0${key}`);
```

and change the condition below it to `if (operation.kind === "write" && (unasked || allowedSave) && download === undefined) {`.

- [ ] **Step 4: The prompt**

In `desktop/src/shell/prompt-content.ts`, in the `change` branch, after the `if (request.download) { … }` block and before the generic write `return`:

```ts
    if (request.editor) {
      return {
        ...OPERATION,
        title: `Save ${named(request.path)}?`,
        lead: `The document editor wants to save this document${where}, ${sizeOf(bytes)}. Allowed, its later saves of it go through until you quit Surogate.`,
        details: [file],
        height: 320,
      };
    }
```

- [ ] **Step 5: The create comment**

In `desktop/src/files/operations.ts`, replace the comment above `if (args.create === true) return create(key, data);` with:

```ts
  // Make-only: the desktop's own downloads, and the server's document editor making a new document or a copy
  // beside one that changed (surogates/office). Never over a file that is there.
```

- [ ] **Step 6: Run the tests to see them pass**

Run: `cd desktop && npx vitest run test/approvals.test.ts test/prompt-content.test.ts && npx tsc --noEmit -p .`
Expected: PASS, and no type errors.

- [ ] **Step 7: Commit**

```bash
git add desktop/src/binding/approvals.ts desktop/src/shell/prompt-content.ts desktop/src/files/operations.ts desktop/test/approvals.test.ts desktop/test/prompt-content.test.ts
git commit -m "feat(desktop): ask once before the document editor saves a document"
```

---

### Task 7: The pane says when a save waits on the computer

**Files:**
- Modify: `sdk/agent-chat-react/src/types.ts` (`AgentChatEventType`, `AgentChatOfficeEvent`)
- Modify: `sdk/agent-chat-react/src/runtime/events.ts` (`AGENT_CHAT_LISTENED_EVENTS`)
- Modify: `sdk/agent-chat-react/src/runtime/reducer.ts` (the office cases)
- Modify: `sdk/agent-chat-react/src/components/workspace/office-editor-pane.tsx` (the notice)
- Test: `sdk/agent-chat-react/tests/reducer-office.test.ts`, `sdk/agent-chat-react/tests/office-editor-pane.test.tsx`

**Interfaces:**
- Consumes: `office.save_waiting {path, computer, reason}`, `office.saved {path}`, and `office.save_failed {path, reason: "declined", computer}` (Task 4).
- Produces:
  - `AgentChatOfficeEvent` gains `type` values `"office.save_waiting" | "office.saved"`, and the fields `computer?: string` and `reason?: string`.
  - The pane's notice per event:

  | Event | Notice |
  |---|---|
  | waiting, offline | "Saving to Mac once it is back online." |
  | waiting, for approval | "Saving to Mac: waiting for it to allow the save." |
  | declined | "Mac declined the save. Your changes are still here; the next save asks again." |
  | `office.saved` | none |

- [ ] **Step 1: Write the failing tests**

In `sdk/agent-chat-react/tests/reducer-office.test.ts`, in `describe("office save events", …)`, add:

```ts
  it("records a save waiting on the computer, and the save that landed after it", () => {
    expect(AGENT_CHAT_LISTENED_EVENTS).toEqual(expect.arrayContaining(["office.save_waiting", "office.saved"]));
    const state = createInitialAgentChatState();
    const waiting = applyAgentChatEvent(state, {
      type: "office.save_waiting", eventId: 3, data: { path: "a.docx", computer: "Mac", reason: "offline" },
    });
    expect(waiting.officeEvents["a.docx"]).toEqual({ type: "office.save_waiting", eventId: 3, computer: "Mac", reason: "offline" });
    const saved = applyAgentChatEvent(waiting, { type: "office.saved", eventId: 4, data: { path: "a.docx" } });
    expect(saved.officeEvents["a.docx"]).toEqual({ type: "office.saved", eventId: 4 });
    expect(saved.workspaceRefreshKey).toBe(state.workspaceRefreshKey);
  });
```

In `sdk/agent-chat-react/tests/office-editor-pane.test.tsx`, add (with `import type { AgentChatOfficeEvent } from "../src/types";` if it is not imported yet):

```tsx
  it.each([
    [{ type: "office.save_waiting", eventId: 13, computer: "Mac", reason: "offline" }, "Saving to Mac once it is back online."],
    [{ type: "office.save_waiting", eventId: 13, computer: "Mac", reason: "waiting" }, "Saving to Mac: waiting for it to allow the save."],
    [{ type: "office.save_failed", eventId: 13, computer: "Mac", reason: "declined" },
      "Mac declined the save. Your changes are still here; the next save asks again."],
  ] as const)("says where a save on the computer is: %o", async (officeEvent, said) => {
    const getWorkspaceEditorConfig = vi.fn().mockResolvedValue({ document_server_url: "https://o", config: {} });
    const host = adapter({ getWorkspaceEditorConfig });
    const pane = (event?: AgentChatOfficeEvent) => (
      <WorkspaceFileDrawer adapter={host} sessionId="s-1" path="a.docx" officeEvent={event} onClose={() => {}} />
    );
    const view = await mount(pane());
    await rerender(pane(officeEvent as AgentChatOfficeEvent));
    expect(view.querySelector('[role="status"]')?.textContent).toBe(said);
    await rerender(pane({ type: "office.saved", eventId: 14 }));
    expect(view.querySelector('[role="status"]')).toBeNull();
    expect(getWorkspaceEditorConfig).toHaveBeenCalledTimes(1);
  });
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd sdk/agent-chat-react && npx vitest run tests/reducer-office.test.ts tests/office-editor-pane.test.tsx`
Expected: FAIL. The events are not listened to, and the pane shows "Your latest changes could not be saved yet."

- [ ] **Step 3: Types and the listened events**

In `src/types.ts`, add `| "office.save_waiting"` and `| "office.saved"` after `| "office.save_failed"` in `AgentChatEventType`. Replace `AgentChatOfficeEvent` with:

```ts
/** A save from the document editor that met trouble or waited: the file changed outside it, it could not land yet,
 * it waits on the computer its file is on, or it landed after waiting. */
export interface AgentChatOfficeEvent {
  type: "office.conflict" | "office.save_failed" | "office.save_waiting" | "office.saved";
  eventId: number;
  /** office.conflict: where the editor's edits were saved instead. */
  conflictPath?: string;
  /** The computer a save on it waits on, or that declined it. */
  computer?: string;
  /** office.save_waiting: "offline", or "waiting" for the computer to allow it; office.save_failed: "declined". */
  reason?: string;
}
```

In `src/runtime/events.ts`, add `"office.save_waiting",` and `"office.saved",` after `"office.save_failed",`.

- [ ] **Step 4: The reducer**

In `src/runtime/reducer.ts`, replace the `case "office.conflict": case "office.save_failed": { … }` block with:

```ts
    case "office.conflict":
    case "office.save_failed":
    case "office.save_waiting":
    case "office.saved": {
      const path = stringValue(event.data.path);
      if (!path) return nextState;
      const conflict = event.type === "office.conflict";
      const computer = stringValue(event.data.computer);
      const reason = stringValue(event.data.reason);
      return {
        ...nextState,
        officeEvents: {
          ...nextState.officeEvents,
          [path]: {
            type: event.type,
            eventId: event.eventId,
            ...(conflict ? { conflictPath: stringValue(event.data.conflict_path) } : {}),
            ...(computer ? { computer } : {}),
            ...(reason ? { reason } : {}),
          },
        },
        // The copy the conflicting save landed as shows in the tree.
        workspaceRefreshKey: nextState.workspaceRefreshKey + (conflict ? 1 : 0),
      };
    }
```

(Check the existing conflict test still expects `conflictPath` exactly as `stringValue` returns it. The shape for a conflict and for a plain `save_failed` is unchanged.)

- [ ] **Step 5: The pane**

In `src/components/workspace/office-editor-pane.tsx`, import `AgentChatOfficeEvent` with the types, add above the component:

```tsx
/** What the pane says about a save, or null when there is nothing to say. */
function saveNotice(notice: AgentChatOfficeEvent): string | null {
	const computer = notice.computer ?? "your computer";
	if (notice.type === "office.conflict") {
		return `This document changed outside the editor. Your edits were saved to ${notice.conflictPath}.`;
	}
	if (notice.type === "office.saved") return null;
	if (notice.type === "office.save_waiting") {
		return notice.reason === "offline"
			? `Saving to ${computer} once it is back online.`
			: `Saving to ${computer}: waiting for it to allow the save.`;
	}
	if (notice.reason === "declined") {
		return `${notice.computer ?? "Your computer"} declined the save. Your changes are still here; the next save asks again.`;
	}
	return "Your latest changes could not be saved yet.";
}
```

and replace the `{notice && ( <div role="status" …> … </div> )}` block with:

```tsx
			{notice && saveNotice(notice) && (
				<div role="status" className="border-b border-line px-3 py-2 text-xs text-muted-foreground">
					{saveNotice(notice)}
				</div>
			)}
```

- [ ] **Step 6: Run the tests to see them pass**

Run: `cd sdk/agent-chat-react && npx vitest run tests/reducer-office.test.ts tests/office-editor-pane.test.tsx && npm run typecheck`
Expected: PASS, and "TypeScript: No errors found".

- [ ] **Step 7: Commit**

```bash
git add sdk/agent-chat-react/src/types.ts sdk/agent-chat-react/src/runtime/events.ts sdk/agent-chat-react/src/runtime/reducer.ts sdk/agent-chat-react/src/components/workspace/office-editor-pane.tsx sdk/agent-chat-react/tests/reducer-office.test.ts sdk/agent-chat-react/tests/office-editor-pane.test.tsx
git commit -m "feat(sdk): say when a document's save waits on the computer"
```

---

### Task 9: A landing takes the folder from chat hosts that sit idle on it

On a computer, every chat's host on a project's real folder keeps the folder for `IDLE_MS` (2 minutes) after its last operation. That includes the master's host, which serves the document editor's reads and saves. A thread's landing then waits that long for the folder. With this task, a landing asks every chat host on its folder to let the folder go as soon as nothing of that host runs:
- **Idle host:** lets go now.
- **Busy host:** lets go when its current operation ends.
- **Host with a live background process:** keeps the folder until the process ends, as before.

**Files:**
- Modify: `desktop/src/hosts/tool-hosts.ts`. Add `Host.yieldFolder()` and the `yielding` field, and a check in `Host.idle()`. In `ToolHosts`, yield the folder's chat hosts when a landing's host is made, and in `hostFor` when a landing is already waiting for that folder.
- Modify: `docs/superpowers/specs/2026-10-13-office-editors-design.md`. Remove the Known-gaps bullet that begins "**On a computer, the editor's reads and saves keep the desktop's hold**".
- Test: `desktop/test/tool-hosts.test.ts`

**Interfaces:**
- Produces: `Host.yieldFolder(): void`.

- [ ] **Step 1: Write the failing test**

In `desktop/test/tool-hosts.test.ts`, inside the `describe` that holds `"lands a thread's file in the folder through the landing's host, …"` (it uses `threads(...)`, `changed()`, `op(...)`, `executor.land(step(...))`, `CHAT`, `signal()`), add:

```ts
  it("takes the folder from a chat's host that sits idle on it, without waiting out its idle time", { timeout: 90_000 }, async () => {
    const folder = changed();
    // The chat's host keeps the folder a minute after its last operation; the landing waits five seconds for it.
    const executor = threads({ idleMs: 60_000, landWaitMs: 5_000 });
    // The chat on the folder itself read it a moment ago: its host holds the folder, idle.
    expect(await executor.run(op("resolve", { path: "" }, CHAT), signal())).toEqual({ ok: folder });
    // The landing has the folder within its wait, not after the chat's minute.
    expect(await executor.land(step("revisions", { paths: [] }), signal())).toEqual({ ok: { revisions: [] } });
    expect(await executor.land(step("forget", { saga: "s1", applied: [] }), signal())).toEqual({ ok: {} });
    // The chat's next operation starts a host again once the landing is forgotten.
    await until(async () => "ok" in (await executor.run(op("resolve", { path: "" }, CHAT), signal())), 15_000);
  });
```

Use the file's own helper names: `step` is the local factory the landing tests use. If that `describe` names it differently, use its name, with the default root `ROOT_A`. `until` is the file's polling helper; if it takes a sync predicate, poll the resolve result through a variable.

- [ ] **Step 2: Run it to see it fail**

Run: `cd desktop && npm run build && npx vitest run test/tool-hosts.test.ts -t "takes the folder from a chat's host"`
Expected: FAIL. The landing answers `folderBusy(folder)` after its 5 s wait, because the chat's host holds the folder for 60 s.

- [ ] **Step 3: Implement**

In `class Host` (`desktop/src/hosts/tool-hosts.ts`), add beside the other private fields:

```ts
  // A landing waits for the folder this host holds: it lets it go as soon as nothing of it runs, not idleMs after.
  private yielding = false;
```

and a method beside `finish`:

```ts
  /** A landing wants this host's folder: it lets it go once no operation of it runs and no process of its root lives. */
  yieldFolder(): void {
    this.yielding = true;
    this.idle();
  }
```

In `idle()`, after the two lines that abort and renew `this.prompts`, before the `idleTimer` is set:

```ts
    if (this.yielding) {
      this.finish();
      return;
    }
```

In `ToolHosts`, add a private helper:

```ts
  // The chats' hosts on *folder* itself let it go once nothing of theirs runs: a landing there would otherwise wait
  // out a host that only read or wrote a file a moment before. One still running something keeps it until that ends.
  private yieldTo(folder: string, lander: Host): void {
    for (const host of this.live) {
      if (host === lander || host.works.at !== undefined || host.kept !== undefined || host.recovers) continue;
      if (host.works.folder.path === folder) host.yieldFolder();
    }
  }
```

Call `this.yieldTo(binding.folder, host)` right after `host = this.lander(root, binding, copy, kept);` succeeds, inside the existing `try`, after the assignment and before `void host.exited…`.

In `hostFor`, after the `new Host(...)` for a root on the folder itself (`works.at === undefined`), yield it at once when a landing on that folder still waits for it:

```ts
    if (works.at === undefined && [...this.landers.values()].some((lander) => !lander.holds && lander.works.folder.path === works.folder.path)) {
      host.yieldFolder();
    }
```

Place it after the host is made and registered, before `return host`. Read `hostFor` first: if it returns the host in one expression, keep that shape, binding `host` first.

- [ ] **Step 4: Remove the Known gap**

In the spec's Known gaps, delete the bullet "**On a computer, the editor's reads and saves keep the desktop's hold on a project's real folder for its 2-minute idle**, so a thread landing right after one waits up to that long."

- [ ] **Step 5: Run the tests to see them pass**

Run: `cd desktop && npx vitest run test/tool-hosts.test.ts test/host.test.ts && npx tsc --noEmit -p .`
Expected: PASS, apart from failures this machine already has in other files. `host.test.ts` exercises `Host` directly. Never leave a test process running.

- [ ] **Step 6: Commit**

```bash
git add desktop/src/hosts/tool-hosts.ts desktop/test/tool-hosts.test.ts docs/superpowers/specs/2026-10-13-office-editors-design.md
git commit -m "fix(desktop): a landing takes the folder from chat hosts that sit idle on it"
```

---

### Task 10: One document however its name is spelled on the computer

On a computer whose folder ignores case or Unicode form (macOS, Windows), `plan.docx` and `Plan.docx` are one file. Today they are two locations, so they open two editing sessions, and a sandbox write under the other spelling passes the guard. With this task:
- **Opening on a computer** (the pane's route, and the agent when no open row matches as written) takes the spelling the computer's folder lists.
- **The guard, the read note and the turn's save** match open documents on a computer without case.
- **Locations on a computer** are NFC-normalised.

**Files:**
- Modify: `surogates/office/files.py`. Add `on_disk_spelling` and `_folded`.
- Modify: `surogates/office/agent.py`. NFC in `location_of`'s computer branch; `open_for_agent` takes the on-disk spelling.
- Modify: `surogates/api/routes/office.py`. `get_workspace_editor` takes the on-disk spelling.
- Modify: `surogates/office/sessions.py`. Add `open_like`.
- Modify: `surogates/office/guard.py`. On a computer, the refusal and the read note match open rows with `open_like`.
- Modify: `surogates/office/turn_end.py`. On a computer, save every open row `open_like` finds.
- Modify: `surogates/db/models.py` and `surogates/db/observability.sql`. A partial index on `lower(location)` where open.
- Modify: `docs/superpowers/specs/2026-10-13-office-editors-design.md`. Remove the Known-gaps bullet that begins "**On a computer whose folder ignores case**".
- Test: `tests/test_office_files.py`, `tests/integration/test_office_routes.py`, `tests/integration/test_office_agent.py`, `tests/integration/test_office_guard.py`, `tests/integration/test_office_turn_end.py`, `tests/integration/test_office_sessions.py`

**Interfaces:**
- Produces:
  - `async def on_disk_spelling(files: DeviceWorkspaceIO, path: str) -> str`, in `surogates/office/files.py`.
  - `async def open_like(session_factory, locations: list[str]) -> list[OpenDocument]`, in `surogates/office/sessions.py`.

- [ ] **Step 1: Write the failing tests**

**`tests/test_office_files.py`.** Add these. The fake computer's `list_dir` is needed: add a `list_dir` branch to `tests/device_computer.py` `Computer.run`, answering the names of the keys directly under `args["key"]` (`{"ok": sorted(names)}`). Also add a case-insensitive mode: a `Computer(..., ignores_case=True)` whose `stat`, `read` and `resolve` of a key find a stored key equal under `casefold()`.

```python
from tests.device_computer import Computer
from surogates.devices.workspace import DeviceWorkspaceIO
from surogates.office.files import on_disk_spelling


def _io(files, *, ignores_case):
    return DeviceWorkspaceIO(Computer(FOLDER, files, ignores_case=ignores_case), root=FOLDER, identity=f"device:{DEVICE}")


async def test_a_name_the_computer_finds_in_another_case_takes_its_listed_spelling():
    io = _io({"Docs/Plan.docx": b"x"}, ignores_case=True)
    assert await on_disk_spelling(io, "docs/plan.docx") == "Docs/Plan.docx"
    assert await on_disk_spelling(io, "Docs/Plan.docx") == "Docs/Plan.docx"


async def test_a_folder_that_tells_case_apart_keeps_the_name_as_written():
    io = _io({"plan.docx": b"x"}, ignores_case=False)
    # Not found as written: a new file, not plan.docx.
    assert await on_disk_spelling(io, "Plan.docx") == "Plan.docx"


async def test_a_name_not_there_stays_as_written_with_the_rest_of_its_path():
    io = _io({"docs/a.docx": b"x"}, ignores_case=True)
    assert await on_disk_spelling(io, "Docs/new/b.docx") == "docs/new/b.docx"


def test_a_computer_location_is_one_unicode_form():
    nfd = "Re\u0301sume\u0301.docx"
    nfc = "R\u00e9sum\u00e9.docx"
    assert location_of(on_computer(), nfd) == location_of(on_computer(), nfc)
```

(In the third test, `Docs` is found as `docs`. `new` is not there, so it and `b.docx` stay as written.)

**`tests/integration/test_office_sessions.py`:**

```python
async def test_open_rows_are_found_without_case(session_factory):
    document = await office_sessions.open_or_reuse(session_factory, location="device:d:/f/Plan.docx", path="Plan.docx", session_id=uuid4())
    [found] = await office_sessions.open_like(session_factory, ["device:d:/f/plan.DOCX"])
    assert found.id == document.id
    assert await office_sessions.open_like(session_factory, ["device:d:/f/other.docx"]) == []
```

(Use the file's helpers for a session id, if `open_or_reuse` needs a real session.)

**`tests/integration/test_office_routes.py`.** In the computer tests, which use `_computer_chat` and the fake `session_files`, build the fake with `Computer(..., ignores_case=True)`. A new test opens `docs/plan.docx` when the computer holds `Docs/Plan.docx`:
- the row's location ends `/Docs/Plan.docx` and its `path` is `Docs/Plan.docx`;
- opening `Docs/Plan.docx` again joins the same row (same doc key).

Let `_computer_chat` take `ignores_case=False` and pass it through.

**`tests/integration/test_office_agent.py`:** on a case-insensitive fake computer holding `Plan.docx`, the agent's `office_edit` path `plan.docx` with create=True:
- joins the row the pane opened for `Plan.docx` (same doc key);
- does not make a new file (no `write` in `io.runner.calls`).

**`tests/integration/test_office_guard.py`:** on a computer session with `Plan.docx` open:
- `write_file` to `plan.docx` is refused;
- in a cloud session, the guard stays exact: `plan.docx` is not refused when `Plan.docx` is open there.

**`tests/integration/test_office_turn_end.py`:** on a computer, an `office_edit` naming `plan.docx` saves the open row of `Plan.docx` (flushed once, forcesave sent).

- [ ] **Step 2: Run them to see them fail**

Run: `/work/surogates/.venv/bin/python -m pytest tests/test_office_files.py tests/integration/test_office_sessions.py tests/integration/test_office_routes.py tests/integration/test_office_agent.py tests/integration/test_office_guard.py tests/integration/test_office_turn_end.py -q`
Expected: FAIL. `on_disk_spelling` and `open_like` don't exist, and the spellings open two rows.

- [ ] **Step 3: `on_disk_spelling`**

In `surogates/office/files.py` (imports `unicodedata`):

```python
def _folded(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


async def on_disk_spelling(files: DeviceWorkspaceIO, path: str) -> str:
    """*path*, relative to the folder, spelled as the computer's folder lists it.

    A folder that ignores case or Unicode form (macOS, Windows) finds ``plan.docx`` for ``Plan.docx``: each name not
    listed as written, but found all the same, takes the one listed spelling, so one file is one document however it
    is asked for. A name the computer does not find stays as written, with the rest of the path: a new file, or a
    folder that tells case apart.
    """
    parts = [part for part in path.split("/") if part]
    done: list[str] = []
    for index, part in enumerate(parts):
        try:
            names = await files.list_dir(await files.resolve("/".join(done)))
        except (FileNotFoundError, NotADirectoryError):
            return "/".join([*done, *parts[index:]])
        if part not in names:
            matches = [name for name in names if _folded(name) == _folded(part)]
            if len(matches) != 1 or await files.stat(await files.resolve("/".join([*done, part]))) is None:
                return "/".join([*done, *parts[index:]])
            part = matches[0]
        done.append(part)
    return "/".join(done)
```

- [ ] **Step 4: Locations in one Unicode form**

In `location_of`'s computer branch (`surogates/office/agent.py`, imports `unicodedata`), normalise the joined path:

```python
        return f"device:{device_id}:{unicodedata.normalize('NFC', posixpath.join(folder, posixpath.normpath(path)))}"
```

- [ ] **Step 5: The pane's route takes the on-disk spelling**

In `get_workspace_editor`, inside `async with workspace_files(request, target) as files:`, right after the `DOCUMENT_FILES` check and before the stat:

```python
        if isinstance(files, DeviceWorkspaceIO):
            path = await on_disk_spelling(files, path)
```

The `stat`, the 413 check, `location_of(target, path)`, `_open_document(…, path=path, …)` and `build_editor_config(…, path=path, …)` then all use that spelling. Import `on_disk_spelling` from `surogates.office.files`.

- [ ] **Step 6: The agent takes it too**

In `open_for_agent`, keep the exact `open_at` fast path. When it finds nothing, take the on-disk spelling inside the files context, then look for an open row again before checking existence. Replace the `async with session_files(...)` block's body with:

```python
                if not isinstance(files, DOCUMENT_FILES):
                    raise AgentDocumentError("This workspace cannot be edited live yet.")
                try:
                    if isinstance(files, DeviceWorkspaceIO):
                        path = await on_disk_spelling(files, path)
                    key = await files.resolve(path)
                except (WorkspaceSandboxError, ValueError) as exc:  # a traversal, or a NUL in the path
                    raise AgentDocumentError(str(exc)) from None
                location = location_of(target, path)
                # Asked for in another spelling of a document the editor has open: that one.
                missing = await office_sessions.open_at(session_factory, location) is None and await files.etag(key) is None
```

Import `DeviceWorkspaceIO` from `surogates.devices.workspace` and `on_disk_spelling` from `surogates.office.files`. The returned `AgentDocument.path` is then the on-disk spelling.

- [ ] **Step 7: `open_like`**

In `surogates/office/sessions.py` (imports `func` from sqlalchemy):

```python
async def open_like(session_factory: Any, locations: list[str]) -> list[OpenDocument]:
    """The open rows at *locations*, compared without case: a folder on a computer may ignore it. Each row once."""
    if not locations:
        return []
    keys = sorted({location.lower() for location in locations})
    async with session_factory() as db:
        rows = await db.scalars(
            select(OfficeSession).where(func.lower(OfficeSession.location).in_(keys), OfficeSession.status == OPEN)
        )
        return [_snapshot(row) for row in rows.all()]
```

In `surogates/db/models.py`, add to `OfficeSession.__table_args__`:

```python
        Index("idx_office_sessions_open_location_folded", func.lower(text("location")), postgresql_where=text("status = 'open'")),
```

Use the file's existing imports and spelling for `func`/`text`. If the column expression is written differently there, use `func.lower(location_column)` as the file does elsewhere.

In `surogates/db/observability.sql`, beside the other `office_sessions` statements:

```sql
CREATE INDEX IF NOT EXISTS idx_office_sessions_open_location_folded ON office_sessions (lower(location)) WHERE status = 'open';
```

- [ ] **Step 8: The guard, the read note and the turn's save match without case on a computer**

**`surogates/office/guard.py`.** `refused_open_document` and `open_document_note` currently call `office_sessions.open_locations(...)`. On a computer (`device_of(session.config)` not None), look the locations up with `open_like` instead. A requested location is open when its `.lower()` equals the `.lower()` of a found row's location; refuse it, or add the note, with the requested path in the message. In the cloud, keep `open_locations` (exact). `hold_open_documents` stays as it is, since it runs only in the cloud landing.

**`surogates/office/turn_end.py`.** On a computer, replace the per-location `office_sessions.open_at(session_factory, location)` with `office_sessions.open_like(session_factory, [location])`. Save each row found, once per row id across the turn. In the cloud, keep `open_at`.

- [ ] **Step 9: Remove the Known gap**

In the spec's Known gaps, delete the bullet that begins "**On a computer whose folder ignores case (macOS, Windows), a document opened under two spellings**".

- [ ] **Step 10: Run the tests to see them pass**

Run: `/work/surogates/.venv/bin/python -m pytest tests/test_office_files.py tests/test_device_workspace_guarded.py tests/integration/test_office_*.py tests/test_office_*.py -q`
Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add surogates/office/files.py surogates/office/agent.py surogates/api/routes/office.py surogates/office/sessions.py surogates/office/guard.py surogates/office/turn_end.py surogates/db/models.py surogates/db/observability.sql docs/superpowers/specs/2026-10-13-office-editors-design.md tests/device_computer.py tests/test_office_files.py tests/integration/test_office_sessions.py tests/integration/test_office_routes.py tests/integration/test_office_agent.py tests/integration/test_office_guard.py tests/integration/test_office_turn_end.py
git commit -m "feat(office): one document however its name is spelled on the computer"
```

---

### Task 8: End to end on DEV, with Surogate Desktop

This step replaces the DEV runtime's processes with this branch's code against the shared DEV database and Redis, and needs Surogate Desktop signed in to DEV on Flavius's computer. **Ask Flavius before Step 2**, and tell him when DEV is back on master.

- [ ] **Step 1: Run every suite on the branch head**

```bash
cd "$(git rev-parse --show-toplevel)"   # the implementation worktree
/work/surogates/.venv/bin/python -m pytest tests -n 8 -q -p no:cacheprovider
cd runtime-office && npm test && npx tsc --noEmit && cd ..
cd sdk/agent-chat-react && npm test && npm run typecheck && cd ../..
cd desktop && npx vitest run && npx tsc --noEmit -p . && cd ..
```

Expected: all pass. On the workstation, about 81 `tests/test_local_history.py` failures with `assert (129 == 0)` are environmental: git 2.43 lacks `fsck --no-references`.

- [ ] **Step 2: Start the branch's processes (after Flavius's OK)**

Stop the VS Code "Surogates [shared]" processes first (same ports). Then, each in its own terminal, from the implementation worktree (`W=$(git rev-parse --show-toplevel)`):

```bash
# runtime-office (config.dev.yaml's office block names it at http://127.0.0.1:8080)
cd "$W/runtime-office" && npm ci && npm run build
RUNTIME_OFFICE_TOKEN=$(/work/surogates/.venv/bin/python -c 'import yaml;print(yaml.safe_load(open("/work/surogates/config.dev.yaml"))["office"]["runtime_office_token"])') \
  RUNTIME_OFFICE_IGNORE_TLS=1 PORT=8080 node dist/main.js

# the runtime: python -m puts the worktree's surogates first
cd "$W"
export SUROGATES_CONFIG=/work/surogates/config.dev.yaml SUROGATES_RUNTIME_MODE=shared SUROGATES_PLATFORM_API_URL=http://localhost:8888
export SUROGATES_PLATFORM_API_TOKEN=<the value in /work/surogates/.vscode/launch.json's API Gateway config>
/work/surogates/.venv/bin/python -m surogates.cli.main migrate
/work/surogates/.venv/bin/python -m surogates.cli.main api                  # one terminal
/work/surogates/.venv/bin/python -m surogates.cli.main worker               # another
SUROGATES_PORT=8002 /work/surogates/.venv/bin/python -m surogates.cli.main mcp-proxy
SUROGATES_SCHEDULED_SESSIONS_ENABLED=true SUROGATES_WORKER_ID=platform-ticker-dev /work/surogates/.venv/bin/python -m surogates.scheduled.platform_ticker

# ops, from /work/surogate-ops (main), once the runtime is up
cd /work/surogate-ops && .venv/bin/python -c 'import sys; from surogate_ops.cli.main import cli_main; sys.exit(cli_main())' server

# the web client, building the SDK from source (copy /work/surogates/web/.env.local into the worktree's web/ first)
cd "$W/web" && npm ci && npm run dev
```

Start the ops server only after the worker is listening for ops; started before it, the worker fails queued sessions. Surogate Desktop loads the web client from the agent's origin. Flavius points his desktop at the DEV web client, or runs the desktop's dev build (`cd "$W/desktop" && npm run dev`) against it, so the desktop runs this branch's approvals.

- [ ] **Step 3: Walk through, and record each result**

1. **Open and save.** In a chat on a computer folder in "Work freely", open a `.docx` from the folder in the pane, edit it, and press Save. The file on disk changes within seconds.
2. **Ask every time.** Switch the chat to "Ask every time" and edit again. The first save prompts "Save <name>?" from the document editor once. Later saves go through without a prompt. Denying a fresh document's first save shows "Mac declined the save…" in the pane.
3. **Offline.** With the document open, quit Surogate Desktop, edit, and press Save. The pane shows "Saving to <computer> once it is back online." Start the desktop again: the save lands and the notice goes.
4. **The agent edits.** Ask the agent to add a section. It arrives live under the agent's name, and the turn's save lands on disk.
5. **The agent creates a document.** Ask the agent for a new `notes/Plan.docx`. In "Ask every time" it prompts once. The file appears in the folder and opens in the pane.
6. **A project on the computer.** Ask the master for a document. The thread edits the project's real file live, never its copy. The real file changes on disk. At the thread's next landing, History records the change as yours.
7. **Too large.** A `.docx` over 50 MiB in the folder answers with the 50 MiB message in the pane.
8. **Two spellings.** On a Mac, with `Plan.docx` open in the pane, ask the agent to edit `plan.docx`: the edit arrives in the same pane, and no second file or session appears.
9. **A landing right after an edit.** In the project, a thread that edits a document live and changes another file in the same turn finishes its turn without a two-minute wait for the folder (time its `session.complete`).

- [ ] **Step 4: Put DEV back**

Stop the branch's processes and tell Flavius. Write the walkthrough results into the PR description, including anything that failed and what was fixed.
