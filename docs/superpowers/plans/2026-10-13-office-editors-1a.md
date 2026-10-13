# Office Editors 1a — Editing for Users in the Cloud — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A user opens a `.docx`, `.xlsx`, `.pptx` or `.pdf` from a cloud chat's or cloud project's workspace in the chat's file pane, edits it in the Euro-Office editor (alone or with colleagues), and the saves land back in the workspace.

**Architecture:** Euro-Office (document server, converters, RabbitMQ) runs in the DEV Kubernetes cluster. The surogates runtime API signs editor configs, serves the file to the document server and takes its save callbacks; a worker job lands the saves through the workspace layer with an ETag guard. The SDK's file drawer mounts the editor; ops proxies the editor route with the acting user.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, python-jose, httpx (surogates); React 19, vitest, happy-dom (SDK); FastAPI (surogate-ops); Helm templates rendered and applied with kubectl; Euro-Office v9.3.5 orchestrated images.

**Spec:** `docs/superpowers/specs/2026-10-13-office-editors-design.md` (this plan is its "1a").

## Global Constraints

- Branches: surogates work on `feat/office-editors-platform` in a new worktree `/work/surogates/.worktrees/office-editors-platform` from `origin/master`; **never checkout, pull, stash or commit in `/work/surogates` itself** (another session uses it). surogate-ops work continues on `feat/office-editors-platform` in the existing worktree `/work/surogate-ops/.worktrees/office-editors-platform` (it already holds `charts/office`).
- New cluster components get their own chart under surogate-ops `charts/`; never add templates to the stale `k8s/surogates-runtime` chart.
- Commits follow Conventional Commits (`type(scope): subject`), no `Co-Authored-By` trailer, and never mention plan, task or phase numbers (in code, comments or commits).
- surogates tests run from the worktree with `/work/surogates/.venv/bin/python -m pytest <path> -q`. Integration tests need Docker (testcontainers Postgres 16 + Redis 7).
- surogate-ops tests run with `/work/surogate-ops/.venv/bin/python -m pytest <path> -q` from the ops worktree. Never `uv run` in surogate-ops.
- SDK commands run in `sdk/agent-chat-react` after `npm ci`; web typecheck runs in `web` after `npm ci` (web compiles the SDK from source).
- Euro-Office image: `ghcr.io/euro-office/cluster-docs:v9.3.5@sha256:d00f3d257d845ceb7e6e678c9dfb27abb88c49ab1ee7f3ba64e4efb6ee997023`. RabbitMQ: `rabbitmq:4.1-alpine@sha256:33335c41151058d46086c04859e6b91bfdf97f9575efd3ca9709a20b5cd45aee`.
- `office.jwt_secret` and `office.token_secret` are at least 32 characters (Euro-Office's connector refuses shorter JWT secrets).
- In-cluster service names are written short (`<svc>.<ns>.svc`), never with a hardcoded `cluster.local` (PROD's cluster domain is `surogate.local`).
- No legacy fallbacks: the pdf.js viewer is deleted, not kept beside the editor.
- No secret values in the repo, in logs or in chat; secrets are created with `kubectl create secret` from values Flavius provides or generated on the spot.

## Review Focus

1. Two people open the same document within the same second: both must get the same document key (one editing session), never two.
2. A second save arrives while the first is still waiting to land: the newest content lands, nothing of it is lost, and the row closes only after the last save.
3. The file is deleted or replaced outside the editor while it is open: the save must not overwrite or resurrect silently; it lands as a sibling copy and the editors reload.
4. A host without the editor method (the example app, a third-party embedder) opens an office file or a PDF: it shows "Preview not available" with Download, never a broken image.
5. The editor script cannot load (document server down): the pane shows an error with the Download link instead of an empty pane.

---

### Task 1: Confirm a user's editor save survives a project thread's landing

Verify item 1 of the spec. Editing a cloud project's file from the editor writes the project's real files directly; the next landing must keep that edit (picked up as "you") when the landing thread did not change the file.

**Files:**
- Read: `surogates/harness/landing.py` (the `pickup` and `commit` steps around line 810-830), `surogates/sandbox/history.py` (how `commit` merges a thread's copy onto `main`), `surogates/workstreams/bucket.py` (the API-side landing).
- Create: `docs/superpowers/notes/2026-10-13-landing-keeps-editor-saves.md` (findings, kept on the plan's docs branch).

- [ ] **Step 1: Trace a landing for a file the thread did not touch**

Read the three files. Answer in the note, citing file:line:
- Does `pickup` commit every real-file change made since `main` (including one written by the API with no thread involved) authored "you"?
- Does the thread's `commit` step write only the files the thread's copy changed relative to its base (`refs/bases/<id>`), leaving other real files as they are?

- [ ] **Step 2: Decide**

If both answers are yes, write "Editing project files from the editor is safe" in the note. If either is no, write what breaks, and change Task 6's editor route to refuse sessions whose config has a `workspace_boundary` starting with `workstream:` with 409 "Project documents cannot be opened in the editor yet." (one extra `if` after the `device_of` check, plus a test in Task 6 asserting the 409).

- [ ] **Step 3: Commit the note**

```bash
cd /work/surogates/.worktrees/office-editors-spec
git add docs/superpowers/notes/2026-10-13-landing-keeps-editor-saves.md
git commit -m "docs(office): how a landing treats a project file saved from the editor"
```

---

### Task 2: Run the document server, converters and RabbitMQ in the DEV cluster

Done 2026-10-13. A standalone chart, `charts/office` in surogate-ops (commit `65f456b0` on `feat/office-editors-platform`; the runtime chart `k8s/surogates-runtime` is stale and is not used), installed on DEV as Helm release `office` in namespace `office`:

- `office-docs` (Euro-Office proxy + docservice, one replica), Service `office-docs` (port 80), Ingress `office.densemax.local`;
- `office-converter` (two replicas), `office-rabbitmq` (StatefulSet on an `emptyDir`);
- Secret `office` (keys `DB_PWD`, `JWT_SECRET`, `AMQP_PWD`, `local.json`) built from install-time values; `local.json` sets the R2 cache bucket `surogate-office-cache-dev` (`useDirectStorageUrls: false`) and `autoAssembly` every 5 minutes;
- database `docs` on `surogate-pg` with Euro-Office's v9.3.5 schema (`doc_changes`, `task_result`), taken from the Euro-Office server source because the `cluster-docs` image does not ship it;
- the chart sets `COMPANY_NAME=euro-office` (the image sets only `COMPANY_NAME_LOW`, and its entrypoints build paths from `COMPANY_NAME`).

Verified: `/healthcheck` answers `true` through the Ingress; a signed `/converter` call turned a two-page `.docx` into PDF (2.1 s) and into two PNGs (2.0 s), fetching it from the workstation and storing the result in the R2 bucket. The install and re-install steps are in `charts/office/README.md`.

**Interfaces produced:** the runtime reaches the document server at `http://office-docs.office.svc` in the cluster and at `https://office.densemax.local` from the workstation; `SUROGATES_OFFICE_JWT_SECRET` equals key `JWT_SECRET` of Secret `office` in namespace `office`.

---

### Task 3: `OfficeSettings`

**Files:**
- Modify: `surogates/config.py` (new class before `Settings`; new field on `Settings`)
- Test: `tests/test_office_settings.py`

**Interfaces:**
- Produces: `surogates.config.OfficeSettings` with `public_url`, `internal_url`, `callback_base_url`, `jwt_secret`, `token_secret` (all `str`, default `""`) and property `enabled -> bool`; `Settings.office: OfficeSettings`.

- [ ] **Step 1: Create the surogates worktree**

```bash
cd /work/surogates && git fetch -q origin master && git worktree add .worktrees/office-editors-platform -b feat/office-editors-platform origin/master
cd .worktrees/office-editors-platform
```

- [ ] **Step 2: Write the failing tests**

`tests/test_office_settings.py`:

```python
"""OfficeSettings: off unless configured, and whole when on."""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from surogates.config import OfficeSettings

SECRET = "s" * 32


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith("SUROGATES_OFFICE_"):
            monkeypatch.delenv(name)


def test_off_by_default():
    assert OfficeSettings().enabled is False


def test_on_needs_every_url_and_both_secrets(monkeypatch):
    monkeypatch.setenv("SUROGATES_OFFICE_PUBLIC_URL", "https://office.example")
    with pytest.raises(ValidationError):
        OfficeSettings()


def test_short_secrets_are_refused(monkeypatch):
    monkeypatch.setenv("SUROGATES_OFFICE_PUBLIC_URL", "https://office.example")
    monkeypatch.setenv("SUROGATES_OFFICE_INTERNAL_URL", "http://docs.internal")
    monkeypatch.setenv("SUROGATES_OFFICE_CALLBACK_BASE_URL", "https://api.example")
    monkeypatch.setenv("SUROGATES_OFFICE_JWT_SECRET", "short")
    monkeypatch.setenv("SUROGATES_OFFICE_TOKEN_SECRET", SECRET)
    with pytest.raises(ValidationError):
        OfficeSettings()


def test_on(monkeypatch):
    monkeypatch.setenv("SUROGATES_OFFICE_PUBLIC_URL", "https://office.example")
    monkeypatch.setenv("SUROGATES_OFFICE_INTERNAL_URL", "http://docs.internal")
    monkeypatch.setenv("SUROGATES_OFFICE_CALLBACK_BASE_URL", "https://api.example")
    monkeypatch.setenv("SUROGATES_OFFICE_JWT_SECRET", SECRET)
    monkeypatch.setenv("SUROGATES_OFFICE_TOKEN_SECRET", SECRET)
    settings = OfficeSettings()
    assert settings.enabled is True
    assert settings.internal_url == "http://docs.internal"
```

- [ ] **Step 3: Run to see it fail**

Run: `/work/surogates/.venv/bin/python -m pytest tests/test_office_settings.py -q`
Expected: FAIL, `ImportError: cannot import name 'OfficeSettings'`.

- [ ] **Step 4: Implement**

In `surogates/config.py`, before `class Settings`, add (and `from pydantic import model_validator` beside the existing pydantic imports if it is not imported yet):

```python
class OfficeSettings(BaseSettings):
    """Office documents opened in the Euro-Office document server.

    An empty ``public_url`` means there is no document server: the editor route
    answers 503 and nothing offers office editing.
    """

    model_config = {"env_prefix": "SUROGATES_OFFICE_"}

    # Browsers load the editor from here.
    public_url: str = ""
    # The runtime reaches the document server here (saved files, Command Service).
    internal_url: str = ""
    # The document server reaches the runtime API here: file fetches and save callbacks.
    callback_base_url: str = ""
    # Shared with the document server: signs editor configs, checks its callbacks.
    jwt_secret: str = ""
    # The runtime's own: signs the file and callback URLs it hands the document server.
    token_secret: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.public_url)

    @model_validator(mode="after")
    def _whole_when_on(self) -> "OfficeSettings":
        if not self.enabled:
            return self
        missing = [name for name in ("internal_url", "callback_base_url") if not getattr(self, name)]
        if missing:
            raise ValueError(f"office.{', office.'.join(missing)} must be set when office.public_url is")
        for name in ("jwt_secret", "token_secret"):
            if len(getattr(self, name)) < 32:
                raise ValueError(f"office.{name} must be at least 32 characters")
        return self
```

In `class Settings`, beside `voice: VoiceSettings = Field(default_factory=VoiceSettings)`, add:

```python
    office: OfficeSettings = Field(default_factory=OfficeSettings)
```

- [ ] **Step 5: Run to see it pass**

Run: `/work/surogates/.venv/bin/python -m pytest tests/test_office_settings.py -q`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add surogates/config.py tests/test_office_settings.py
git commit -m "feat(office): settings for the document server"
```

---

### Task 4: Office kinds, signed tokens and the editor config

**Files:**
- Create: `surogates/office/__init__.py`, `surogates/office/kinds.py`, `surogates/office/tokens.py`, `surogates/office/editor_config.py`
- Test: `tests/test_office_editor_config.py`

**Interfaces:**
- Produces:
  - `office_kind(path: str) -> OfficeKind | None`; `OfficeKind(file_type: str, document_type: str, editable: bool)`.
  - `url_token(secret: str, row_id: UUID, purpose: Literal["file", "callback"]) -> str`; `read_url_token(secret, token, purpose) -> UUID` raising `InvalidOfficeToken`.
  - `sign(secret: str, claims: dict) -> str`; `read_document_server_body(secret: str, body: dict, authorization: str | None) -> dict` raising `InvalidOfficeToken`.
  - `build_editor_config(*, settings: OfficeSettings, row_id: UUID, doc_key: str, path: str, kind: OfficeKind, user_id: str, user_name: str, edit: bool, theme: Literal["light", "dark"]) -> dict[str, Any]` (includes `token`).

- [ ] **Step 1: Write the failing tests**

`tests/test_office_editor_config.py`:

```python
"""Which files the editor opens, the signed URLs, and the editor config."""

from __future__ import annotations

import time
from uuid import uuid4

import pytest
from jose import jwt

from surogates.config import OfficeSettings
from surogates.office.editor_config import build_editor_config
from surogates.office.kinds import office_kind
from surogates.office.tokens import (
    InvalidOfficeToken,
    read_document_server_body,
    read_url_token,
    sign,
    url_token,
)

SECRET = "j" * 32
TOKEN_SECRET = "t" * 32


def _settings() -> OfficeSettings:
    return OfficeSettings(
        public_url="https://office.example",
        internal_url="http://docs.internal",
        callback_base_url="https://api.example/",
        jwt_secret=SECRET,
        token_secret=TOKEN_SECRET,
    )


@pytest.mark.parametrize(
    ("path", "file_type", "document_type", "editable"),
    [
        ("a/Report.DOCX", "docx", "word", True),
        ("sheet.xlsx", "xlsx", "cell", True),
        ("deck.pptx", "pptx", "slide", True),
        ("scan.pdf", "pdf", "pdf", True),
        ("old.doc", "doc", "word", False),
        ("old.ods", "ods", "cell", False),
    ],
)
def test_office_kind(path, file_type, document_type, editable):
    kind = office_kind(path)
    assert kind is not None
    assert (kind.file_type, kind.document_type, kind.editable) == (file_type, document_type, editable)


def test_not_office():
    assert office_kind("notes.md") is None
    assert office_kind("Makefile") is None


def test_url_token_round_trip():
    row = uuid4()
    assert read_url_token(TOKEN_SECRET, url_token(TOKEN_SECRET, row, "file"), "file") == row


def test_url_token_purpose_is_checked():
    token = url_token(TOKEN_SECRET, uuid4(), "file")
    with pytest.raises(InvalidOfficeToken):
        read_url_token(TOKEN_SECRET, token, "callback")


def test_file_token_expires():
    stale = jwt.encode({"sub": str(uuid4()), "pur": "file", "iat": 0, "exp": int(time.time()) - 1}, TOKEN_SECRET, algorithm="HS256")
    with pytest.raises(InvalidOfficeToken):
        read_url_token(TOKEN_SECRET, stale, "file")


def test_forged_token_is_refused():
    with pytest.raises(InvalidOfficeToken):
        read_url_token(TOKEN_SECRET, url_token("x" * 32, uuid4(), "callback"), "callback")


def test_document_server_body_in_body_and_in_header():
    body = {"key": "k", "status": 1}
    assert read_document_server_body(SECRET, {"token": sign(SECRET, body)}, None)["status"] == 1
    header = "Bearer " + sign(SECRET, {"payload": body})
    assert read_document_server_body(SECRET, {}, header)["key"] == "k"
    with pytest.raises(InvalidOfficeToken):
        read_document_server_body(SECRET, body, None)


def test_editor_config():
    row = uuid4()
    config = build_editor_config(
        settings=_settings(), row_id=row, doc_key="abc", path="docs/Plan.docx",
        kind=office_kind("docs/Plan.docx"), user_id="user:1", user_name="Flavius", edit=True, theme="dark",
    )
    assert config["document"]["key"] == "abc"
    assert config["document"]["title"] == "Plan.docx"
    assert config["document"]["fileType"] == "docx"
    assert config["documentType"] == "word"
    assert config["document"]["url"].startswith("https://api.example/v1/office/files/")
    assert config["editorConfig"]["callbackUrl"].startswith("https://api.example/v1/office/callback/")
    assert config["editorConfig"]["mode"] == "edit"
    assert config["editorConfig"]["user"] == {"id": "user:1", "name": "Flavius"}
    assert config["editorConfig"]["coEditing"] == {"mode": "fast", "change": False}
    assert config["editorConfig"]["customization"]["uiTheme"] == "theme-dark"
    signed = jwt.decode(config["token"], SECRET, algorithms=["HS256"])
    assert signed["document"] == config["document"]
    file_token = config["document"]["url"].rsplit("/", 1)[1]
    assert read_url_token(TOKEN_SECRET, file_token, "file") == row


def test_view_config_cannot_edit():
    config = build_editor_config(
        settings=_settings(), row_id=uuid4(), doc_key="abc", path="old.doc",
        kind=office_kind("old.doc"), user_id="user:1", user_name="F", edit=False, theme="light",
    )
    assert config["editorConfig"]["mode"] == "view"
    assert config["document"]["permissions"]["edit"] is False
    assert config["editorConfig"]["customization"]["uiTheme"] == "theme-light"
```

- [ ] **Step 2: Run to see it fail**

Run: `/work/surogates/.venv/bin/python -m pytest tests/test_office_editor_config.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'surogates.office'`.

- [ ] **Step 3: Implement**

`surogates/office/__init__.py`:

```python
"""Office documents opened in the Euro-Office document server."""
```

`surogates/office/kinds.py`:

```python
"""Which files the document server opens, and whether they can be edited."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True, slots=True)
class OfficeKind:
    file_type: str  # the extension, as the document server names it
    document_type: str  # word, cell, slide or pdf
    editable: bool


# Older formats open read-only: the editor converts them to view them.
_KINDS: dict[str, tuple[str, bool]] = {
    "docx": ("word", True),
    "xlsx": ("cell", True),
    "pptx": ("slide", True),
    "pdf": ("pdf", True),
    "doc": ("word", False),
    "odt": ("word", False),
    "rtf": ("word", False),
    "xls": ("cell", False),
    "ods": ("cell", False),
    "ppt": ("slide", False),
    "odp": ("slide", False),
}


def office_kind(path: str) -> OfficeKind | None:
    extension = PurePosixPath(path).suffix.lower().lstrip(".")
    found = _KINDS.get(extension)
    return OfficeKind(extension, found[0], found[1]) if found else None
```

`surogates/office/tokens.py`:

```python
"""Signed values the runtime exchanges with the document server."""

from __future__ import annotations

import time
from typing import Any, Literal
from uuid import UUID

from jose import JWTError, jwt

_ALGORITHM = "HS256"
FILE_TOKEN_SECONDS = 15 * 60

Purpose = Literal["file", "callback"]


class InvalidOfficeToken(Exception):
    """A token that is forged, expired, unsigned or for another purpose."""


def sign(secret: str, claims: dict[str, Any]) -> str:
    return jwt.encode(claims, secret, algorithm=_ALGORITHM)


def url_token(secret: str, row_id: UUID, purpose: Purpose) -> str:
    """The token in a file or callback URL: names the row and what the URL is for.

    File tokens expire; the document server fetches the file when the editing
    session starts.  Callback tokens do not: a save can come days later, and
    the row being closed is what retires them.
    """
    now = int(time.time())
    claims: dict[str, Any] = {"sub": str(row_id), "pur": purpose, "iat": now}
    if purpose == "file":
        claims["exp"] = now + FILE_TOKEN_SECONDS
    return sign(secret, claims)


def read_url_token(secret: str, token: str, purpose: Purpose) -> UUID:
    try:
        claims = jwt.decode(token, secret, algorithms=[_ALGORITHM])
    except JWTError as exc:
        raise InvalidOfficeToken(str(exc)) from None
    if claims.get("pur") != purpose:
        raise InvalidOfficeToken("the token is for another purpose")
    try:
        return UUID(str(claims["sub"]))
    except (KeyError, ValueError):
        raise InvalidOfficeToken("the token names no row") from None


def read_document_server_body(secret: str, body: dict[str, Any], authorization: str | None) -> dict[str, Any]:
    """The callback the document server signed.

    The token is in the body's ``token`` (JWT_IN_BODY) or in the Authorization
    header, where the body is wrapped in ``payload``.
    """
    token = body.get("token")
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ")
    if not token:
        raise InvalidOfficeToken("the callback is not signed")
    try:
        claims = jwt.decode(token, secret, algorithms=[_ALGORITHM])
    except JWTError as exc:
        raise InvalidOfficeToken(str(exc)) from None
    payload = claims.get("payload")
    return payload if isinstance(payload, dict) else claims
```

`surogates/office/editor_config.py`:

```python
"""The editor config the document server opens a document with."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Literal
from uuid import UUID

from surogates.config import OfficeSettings
from surogates.office.kinds import OfficeKind
from surogates.office.tokens import sign, url_token


def build_editor_config(
    *,
    settings: OfficeSettings,
    row_id: UUID,
    doc_key: str,
    path: str,
    kind: OfficeKind,
    user_id: str,
    user_name: str,
    edit: bool,
    theme: Literal["light", "dark"],
) -> dict[str, Any]:
    base = settings.callback_base_url.rstrip("/")
    config: dict[str, Any] = {
        "width": "100%",
        "height": "100%",
        "document": {
            "fileType": kind.file_type,
            "key": doc_key,
            "title": PurePosixPath(path).name,
            "url": f"{base}/v1/office/files/{url_token(settings.token_secret, row_id, 'file')}",
            "permissions": {"edit": edit, "review": edit, "comment": True, "download": True, "print": True},
        },
        "documentType": kind.document_type,
        "editorConfig": {
            "callbackUrl": f"{base}/v1/office/callback/{url_token(settings.token_secret, row_id, 'callback')}",
            "mode": "edit" if edit else "view",
            "lang": "en",
            "user": {"id": user_id, "name": user_name},
            # Strict mode would stop everyone's edits showing live.
            "coEditing": {"mode": "fast", "change": False},
            "customization": {
                "forcesave": True,
                "uiTheme": "theme-dark" if theme == "dark" else "theme-light",
            },
        },
    }
    config["token"] = sign(settings.jwt_secret, config)
    return config
```

- [ ] **Step 4: Run to see it pass**

Run: `/work/surogates/.venv/bin/python -m pytest tests/test_office_editor_config.py -q`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add surogates/office tests/test_office_editor_config.py
git commit -m "feat(office): editor configs and the signed URLs for the document server"
```

---

### Task 5: `office_sessions` and the module that keeps it

**Files:**
- Modify: `surogates/db/models.py` (new model `OfficeSession`)
- Create: `surogates/office/sessions.py`
- Test: `tests/integration/test_office_sessions.py`

**Interfaces:**
- Produces (`surogates.office.sessions`):
  - `OpenDocument(id: UUID, doc_key: str, path: str, session_ids: tuple[UUID, ...], base_version: str | None, editors: tuple[str, ...], pending_save: dict | None)` (frozen dataclass).
  - `async open_or_reuse(session_factory, *, location: str, path: str, session_id: UUID) -> OpenDocument`
  - `async get_open(session_factory, row_id: UUID) -> OpenDocument | None`
  - `async record_base_version(session_factory, row_id, version: str | None) -> None`
  - `async record_editors(session_factory, row_id, users: list[str]) -> None`
  - `async set_pending(session_factory, row_id, *, bucket: str, key: str, close: bool) -> str | None` (returns the key of a pending save it replaced)
  - `async close_when_saved(session_factory, row_id) -> None`
  - `async close(session_factory, row_id) -> None`
  - `claim_pending(session_factory, *, limit: int = 20)` async context manager yielding `list[OfficeSession]` locked `FOR UPDATE SKIP LOCKED`
  - `mark_landed(row: OfficeSession, version: str | None) -> None`, `mark_closed(row) -> None`, `mark_attempt(row) -> int`
  - `async close_never_opened(session_factory, *, older_than: timedelta, now: datetime | None = None) -> int`
  - `async open_documents(session_factory) -> list[OpenDocument]`

- [ ] **Step 1: Write the failing tests**

`tests/integration/test_office_sessions.py`:

```python
"""office_sessions: one open row per file, and what saves and closes do to it."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from surogates.office import sessions as office_sessions

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _location() -> str:
    return f"bucket:test/{uuid4()}/a.docx"


async def test_same_location_reuses_the_key(session_factory):
    location = _location()
    first, second = uuid4(), uuid4()
    one = await office_sessions.open_or_reuse(session_factory, location=location, path="a.docx", session_id=first)
    two = await office_sessions.open_or_reuse(session_factory, location=location, path="a.docx", session_id=second)
    assert one.doc_key == two.doc_key
    assert set(two.session_ids) == {first, second}


async def test_concurrent_opens_get_one_key(session_factory):
    location = _location()
    opened = await asyncio.gather(*[
        office_sessions.open_or_reuse(session_factory, location=location, path="a.docx", session_id=uuid4())
        for _ in range(8)
    ])
    assert len({document.doc_key for document in opened}) == 1


async def test_closed_row_gives_a_new_key(session_factory):
    location = _location()
    one = await office_sessions.open_or_reuse(session_factory, location=location, path="a.docx", session_id=uuid4())
    await office_sessions.close(session_factory, one.id)
    assert await office_sessions.get_open(session_factory, one.id) is None
    two = await office_sessions.open_or_reuse(session_factory, location=location, path="a.docx", session_id=uuid4())
    assert two.doc_key != one.doc_key


async def test_newer_save_replaces_a_pending_one_and_keeps_close(session_factory):
    document = await office_sessions.open_or_reuse(session_factory, location=_location(), path="a.docx", session_id=uuid4())
    assert await office_sessions.set_pending(session_factory, document.id, bucket="b", key="k1", close=True) is None
    replaced = await office_sessions.set_pending(session_factory, document.id, bucket="b", key="k2", close=False)
    assert replaced == "k1"
    pending = (await office_sessions.get_open(session_factory, document.id)).pending_save
    assert pending == {"bucket": "b", "key": "k2", "close": True, "attempts": 0}


async def test_close_waits_for_a_pending_save(session_factory):
    document = await office_sessions.open_or_reuse(session_factory, location=_location(), path="a.docx", session_id=uuid4())
    await office_sessions.set_pending(session_factory, document.id, bucket="b", key="k", close=False)
    await office_sessions.close_when_saved(session_factory, document.id)
    still = await office_sessions.get_open(session_factory, document.id)
    assert still is not None and still.pending_save["close"] is True
    async with office_sessions.claim_pending(session_factory) as rows:
        row = next(r for r in rows if r.id == document.id)
        office_sessions.mark_landed(row, "etag-2")
    assert await office_sessions.get_open(session_factory, document.id) is None


async def test_landing_records_the_new_version(session_factory):
    document = await office_sessions.open_or_reuse(session_factory, location=_location(), path="a.docx", session_id=uuid4())
    await office_sessions.set_pending(session_factory, document.id, bucket="b", key="k", close=False)
    async with office_sessions.claim_pending(session_factory) as rows:
        office_sessions.mark_landed(next(r for r in rows if r.id == document.id), "etag-9")
    after = await office_sessions.get_open(session_factory, document.id)
    assert after.base_version == "etag-9" and after.pending_save is None


async def test_never_opened_rows_are_closed(session_factory):
    stale = await office_sessions.open_or_reuse(session_factory, location=_location(), path="a.docx", session_id=uuid4())
    seen = await office_sessions.open_or_reuse(session_factory, location=_location(), path="b.docx", session_id=uuid4())
    await office_sessions.record_editors(session_factory, seen.id, ["user:1"])
    closed = await office_sessions.close_never_opened(
        session_factory, older_than=timedelta(minutes=10), now=datetime.now(timezone.utc) + timedelta(minutes=11),
    )
    assert closed >= 1
    assert await office_sessions.get_open(session_factory, stale.id) is None
    assert await office_sessions.get_open(session_factory, seen.id) is not None
```

- [ ] **Step 2: Run to see it fail**

Run: `/work/surogates/.venv/bin/python -m pytest tests/integration/test_office_sessions.py -q`
Expected: FAIL, `ImportError: cannot import name 'sessions' from 'surogates.office'`.

- [ ] **Step 3: Add the model**

In `surogates/db/models.py`, after `class ApprovalGrant`, add (the imports it uses — `Index`, `text`, `func`, `Text`, `DateTime`, `CheckConstraint`, `JSONB`, `GUID` — already exist in that module):

```python
class OfficeSession(Base):
    """A document open in the document server: one open row per file."""

    __tablename__ = "office_sessions"
    __table_args__ = (
        Index(
            "uq_office_sessions_open_location", "location", unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        Index("idx_office_sessions_pending", "updated_at", postgresql_where=text("pending_save IS NOT NULL")),
        CheckConstraint("status IN ('open', 'closed')", name="ck_office_sessions_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    # Where the file is, so one file has one row however it is reached.
    location: Mapped[str] = mapped_column(Text, nullable=False)
    # The chats whose panes opened the document (UUID strings); events go to all.
    session_ids: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    doc_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # The file's version when the document server fetched it or a save landed.
    base_version: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="open")
    editors: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    # A save waiting to land: {"bucket", "key", "close", "attempts"}.
    pending_save: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    connected_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now(),
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
```

- [ ] **Step 4: Write the module**

`surogates/office/sessions.py`:

```python
"""Documents open in the document server: one ``office_sessions`` row per open file."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from surogates.db.models import OfficeSession

OPEN = "open"
CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class OpenDocument:
    id: UUID
    doc_key: str
    path: str
    session_ids: tuple[UUID, ...]
    base_version: str | None
    editors: tuple[str, ...]
    pending_save: dict[str, Any] | None


def _snapshot(row: OfficeSession) -> OpenDocument:
    return OpenDocument(
        id=row.id,
        doc_key=row.doc_key,
        path=row.path,
        session_ids=tuple(UUID(str(value)) for value in row.session_ids),
        base_version=row.base_version,
        editors=tuple(str(value) for value in row.editors),
        pending_save=dict(row.pending_save) if row.pending_save is not None else None,
    )


async def open_or_reuse(session_factory: Any, *, location: str, path: str, session_id: UUID) -> OpenDocument:
    """The open row for *location*, created when there is none; *session_id* joins its chats."""
    async with session_factory() as db, db.begin():
        await db.execute(
            pg_insert(OfficeSession)
            .values(
                id=uuid4(), path=path, location=location, session_ids=[str(session_id)],
                doc_key=uuid4().hex, status=OPEN,
            )
            .on_conflict_do_nothing(index_elements=["location"], index_where=text("status = 'open'"))
        )
        row = await db.scalar(
            select(OfficeSession)
            .where(OfficeSession.location == location, OfficeSession.status == OPEN)
            .with_for_update()
        )
        if row is None:  # closed between the insert and the select: open a fresh one
            row = OfficeSession(
                id=uuid4(), path=path, location=location, session_ids=[str(session_id)],
                doc_key=uuid4().hex, status=OPEN,
            )
            db.add(row)
        elif str(session_id) not in row.session_ids:
            row.session_ids = [*row.session_ids, str(session_id)]
        await db.flush()
        return _snapshot(row)


async def get_open(session_factory: Any, row_id: UUID) -> OpenDocument | None:
    async with session_factory() as db:
        row = await db.scalar(select(OfficeSession).where(OfficeSession.id == row_id, OfficeSession.status == OPEN))
        return _snapshot(row) if row is not None else None


async def open_documents(session_factory: Any) -> list[OpenDocument]:
    async with session_factory() as db:
        rows = (await db.scalars(select(OfficeSession).where(OfficeSession.status == OPEN))).all()
        return [_snapshot(row) for row in rows]


async def record_base_version(session_factory: Any, row_id: UUID, version: str | None) -> None:
    async with session_factory() as db, db.begin():
        await db.execute(update(OfficeSession).where(OfficeSession.id == row_id).values(base_version=version))


async def record_editors(session_factory: Any, row_id: UUID, users: list[str]) -> None:
    async with session_factory() as db, db.begin():
        row = await db.get(OfficeSession, row_id, with_for_update=True)
        if row is None:
            return
        row.editors = list(users)
        if users and row.connected_at is None:
            row.connected_at = datetime.now(timezone.utc)


async def set_pending(session_factory: Any, row_id: UUID, *, bucket: str, key: str, close: bool) -> str | None:
    """Record a save waiting to land; returns the key of the pending save it replaced."""
    async with session_factory() as db, db.begin():
        row = await db.get(OfficeSession, row_id, with_for_update=True)
        if row is None:
            return None
        previous = dict(row.pending_save or {})
        row.pending_save = {
            "bucket": bucket,
            "key": key,
            "close": close or bool(previous.get("close")),
            "attempts": 0,
        }
        replaced = previous.get("key")
        return str(replaced) if replaced and replaced != key else None


async def close_when_saved(session_factory: Any, row_id: UUID) -> None:
    """Close the row now, or once its pending save lands."""
    async with session_factory() as db, db.begin():
        row = await db.get(OfficeSession, row_id, with_for_update=True)
        if row is None or row.status != OPEN:
            return
        if row.pending_save is None:
            _close(row)
        else:
            row.pending_save = {**row.pending_save, "close": True}


async def close(session_factory: Any, row_id: UUID) -> None:
    async with session_factory() as db, db.begin():
        row = await db.get(OfficeSession, row_id, with_for_update=True)
        if row is not None and row.status == OPEN:
            row.pending_save = None
            _close(row)


@asynccontextmanager
async def claim_pending(session_factory: Any, *, limit: int = 20) -> AsyncIterator[list[OfficeSession]]:
    """Rows with a save to land, locked so no other worker lands them meanwhile.

    The caller changes the rows (``mark_*``); the changes commit when the block ends.
    """
    async with session_factory() as db, db.begin():
        rows = list(
            (
                await db.scalars(
                    select(OfficeSession)
                    .where(OfficeSession.pending_save.is_not(None))
                    .order_by(OfficeSession.updated_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        yield rows


def mark_landed(row: OfficeSession, version: str | None) -> None:
    close_after = bool((row.pending_save or {}).get("close"))
    row.base_version = version
    row.pending_save = None
    if close_after:
        _close(row)


def mark_closed(row: OfficeSession) -> None:
    row.pending_save = None
    _close(row)


def mark_attempt(row: OfficeSession) -> int:
    pending = dict(row.pending_save or {})
    pending["attempts"] = int(pending.get("attempts", 0)) + 1
    row.pending_save = pending
    return pending["attempts"]


async def close_never_opened(session_factory: Any, *, older_than: timedelta, now: datetime | None = None) -> int:
    """Close rows whose editor never connected (a config fetched, never opened)."""
    cutoff = (now or datetime.now(timezone.utc)) - older_than
    async with session_factory() as db, db.begin():
        result = await db.execute(
            update(OfficeSession)
            .where(
                OfficeSession.status == OPEN,
                OfficeSession.connected_at.is_(None),
                OfficeSession.pending_save.is_(None),
                OfficeSession.created_at < cutoff,
            )
            .values(status=CLOSED, closed_at=datetime.now(timezone.utc))
        )
        return result.rowcount or 0


def _close(row: OfficeSession) -> None:
    row.status = CLOSED
    row.closed_at = datetime.now(timezone.utc)
```

- [ ] **Step 5: Run to see it pass**

Run: `/work/surogates/.venv/bin/python -m pytest tests/integration/test_office_sessions.py -q`
Expected: 7 passed.

- [ ] **Step 6: Commit**

```bash
git add surogates/db/models.py surogates/office/sessions.py tests/integration/test_office_sessions.py
git commit -m "feat(office): keep one open editing session per document"
```

---

### Task 6: Guarded writes and locations on the cloud workspace

**Files:**
- Modify: `surogates/tools/workspace_io/storage.py`
- Test: `tests/test_storage_workspace_write_if.py`

**Interfaces:**
- Produces on `StorageWorkspaceIO`:
  - `location(key: str) -> str` (`"bucket:<bucket>/<prefix><key>"`)
  - `async etag(key: str) -> str | None`
  - `async write_if(key: str, data: bytes, *, expected: str | None) -> None` — writes only over the version *expected*, or only where nothing is when `expected` is None; raises `RevisionConflict` otherwise. Every other method keeps today's behaviour.

- [ ] **Step 1: Write the failing tests**

`tests/test_storage_workspace_write_if.py`:

```python
"""StorageWorkspaceIO.write_if: a write that refuses to overwrite a changed file."""

from __future__ import annotations

import pytest

from surogates.storage.backend import LocalBackend
from surogates.tools.workspace_io import RevisionConflict, StorageWorkspaceIO


@pytest.fixture
def files(tmp_path):
    return StorageWorkspaceIO(LocalBackend(str(tmp_path)), bucket="b", prefix="s1/")


async def test_location_names_the_object(files):
    assert files.location("docs/a.docx") == "bucket:b/s1/docs/a.docx"


async def test_write_over_the_expected_version(files):
    await files.write("a.docx", b"one")
    tag = await files.etag("a.docx")
    await files.write_if("a.docx", b"two", expected=tag)
    assert await files.read("a.docx") == b"two"
    assert await files.etag("a.docx") != tag


async def test_changed_file_is_not_overwritten(files):
    await files.write("a.docx", b"one")
    tag = await files.etag("a.docx")
    await files.write("a.docx", b"someone else")
    with pytest.raises(RevisionConflict):
        await files.write_if("a.docx", b"mine", expected=tag)
    assert await files.read("a.docx") == b"someone else"


async def test_deleted_file_is_not_resurrected(files):
    await files.write("a.docx", b"one")
    tag = await files.etag("a.docx")
    await files.delete("a.docx")
    with pytest.raises(RevisionConflict):
        await files.write_if("a.docx", b"mine", expected=tag)
    assert await files.stat("a.docx") is None


async def test_new_file_only_where_nothing_is(files):
    await files.write_if("copy.docx", b"x", expected=None)
    with pytest.raises(RevisionConflict):
        await files.write_if("copy.docx", b"y", expected=None)
    assert await files.read("copy.docx") == b"x"


async def test_etag_of_a_missing_file(files):
    assert await files.etag("nope.docx") is None
```

If `LocalBackend`'s constructor takes a different argument (check `surogates/storage/backend.py`, the class whose `stat` returns `_tag(st)`), build it the way `create_backend` does for the local backend.

- [ ] **Step 2: Run to see it fail**

Run: `/work/surogates/.venv/bin/python -m pytest tests/test_storage_workspace_write_if.py -q`
Expected: FAIL, `AttributeError: 'StorageWorkspaceIO' object has no attribute 'location'`.

- [ ] **Step 3: Implement**

In `surogates/tools/workspace_io/storage.py`, add imports `import tempfile`, `from pathlib import Path` and `from surogates.storage.backend import Changed`, and these methods in `StorageWorkspaceIO`:

```python
    def location(self, key: str) -> str:
        """Where the object is, the same however the file is reached."""
        return f"bucket:{self._bucket}/{self._prefix}{key}"

    async def etag(self, key: str) -> str | None:
        try:
            info = await self._storage.stat(self._bucket, self._prefix + key)
        except KeyError:
            return None
        tag = info.get("etag")
        return str(tag) if tag else None

    async def write_if(self, key: str, data: bytes, *, expected: str | None) -> None:
        """Write only over the version *expected* (an etag), or only where nothing
        is when it is None; :class:`RevisionConflict` otherwise.

        Used by saves from the document editor; every other write keeps
        :meth:`write`'s unconditional behaviour.
        """
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(data)
            source = Path(handle.name)
        try:
            await self._storage.upload(
                self._bucket, self._prefix + key, source, if_tag=expected, if_absent=expected is None,
            )
        except Changed as exc:
            raise RevisionConflict(f"{key} changed since it was opened") from exc
        finally:
            source.unlink(missing_ok=True)
```

If `test_deleted_file_is_not_resurrected` fails because `upload(if_tag=…)` writes when the object is missing, make the backend's conditional upload raise `Changed` when `if_tag` is given and the object does not exist (both `LocalBackend._atomic_copy` and the S3 `_upload_if`), and add the same case to that backend's tests.

- [ ] **Step 4: Run to see it pass**

Run: `/work/surogates/.venv/bin/python -m pytest tests/test_storage_workspace_write_if.py -q`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add surogates/tools/workspace_io/storage.py tests/test_storage_workspace_write_if.py
git commit -m "feat(workspace): a write that refuses to overwrite a changed file"
```

---

### Task 7: The editor route

**Files:**
- Create: `surogates/api/routes/office.py`
- Modify: `surogates/api/app.py` (include the router)
- Modify: `surogates/session/events.py` (two event types, used by later tasks)
- Test: `tests/integration/test_office_routes.py`

**Interfaces:**
- Consumes: `office_kind`, `build_editor_config`, `office_sessions.open_or_reuse`, `StorageWorkspaceIO.location`.
- Produces: `GET /v1/sessions/{id}/workspace/editor?path=&theme=&view=` and `GET /v1/api/sessions/{id}/workspace/editor` (service accounts, with `X-Surogate-Acting-User-Id` / `X-Surogate-Acting-User-Name`) → `{"document_server_url": str, "config": dict}`; `EventType.OFFICE_CONFLICT = "office.conflict"`, `EventType.OFFICE_SAVE_FAILED = "office.save_failed"`; module helper `live_session(store, session_ids) -> Session | None` in `surogates/office/chats.py`.

- [ ] **Step 1: Write the failing tests**

`tests/integration/test_office_routes.py`:

```python
"""The editor route, the file the document server fetches, and its save callbacks."""

from __future__ import annotations

import os
import uuid
from urllib.parse import quote
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from jose import jwt

from surogates.office.tokens import read_url_token
from surogates.session.store import SessionStore
from surogates.storage.tenant import session_workspace_key
from surogates.tenant.auth.jwt import create_access_token

from .conftest import create_org, create_user, issue_service_account_token

pytestmark = pytest.mark.asyncio(loop_scope="session")

JWT_SECRET = "j" * 32
TOKEN_SECRET = "t" * 32


@pytest_asyncio.fixture(loop_scope="session")
async def app(session_factory, redis_client, pg_url, redis_url):
    os.environ["SUROGATES_DB_URL"] = pg_url
    os.environ["SUROGATES_REDIS_URL"] = redis_url

    from surogates.api.app import create_app
    from surogates.config import OfficeSettings, Settings
    from surogates.runtime import agent_runtime_context_dep, build_agent_runtime_context
    from surogates.storage.backend import create_backend

    application = create_app()
    application.state.session_factory = session_factory
    application.state.redis = redis_client
    application.state.session_store = SessionStore(session_factory)
    application.state.settings = Settings()
    application.state.settings.storage.bucket = f"test-agent-{uuid.uuid4()}"
    application.state.settings.office = OfficeSettings(
        public_url="https://office.example",
        internal_url="http://docs.internal",
        callback_base_url="https://api.example",
        jwt_secret=JWT_SECRET,
        token_secret=TOKEN_SECRET,
    )
    application.state.storage = create_backend(application.state.settings)
    application.dependency_overrides[agent_runtime_context_dep] = lambda: build_agent_runtime_context({
        "agent_id": "default", "org_id": "00000000-0000-0000-0000-000000000000",
        "project_id": "test-project", "enabled": True, "version": 1, "storage_key_prefix": "",
    })
    return application


@pytest_asyncio.fixture(loop_scope="session")
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _user_chat(client, app, session_factory, *, path="docs/Plan.docx", content=b"PK-docx"):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    token = create_access_token(org_id, user_id, {"sessions:read", "sessions:write", "tools:read"})
    headers = {"Authorization": f"Bearer {token}"}
    created = await client.post("/v1/sessions", json={"model": "gpt-4o"}, headers=headers)
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    session = await SessionStore(session_factory).get_session(UUID(session_id))
    await app.state.storage.write(session.config["storage_bucket"], session_workspace_key(session_id, path), content)
    return session_id, headers, user_id, session


async def test_user_gets_a_signed_config(client, app, session_factory):
    session_id, headers, user_id, _ = await _user_chat(client, app, session_factory)
    response = await client.get(f"/v1/sessions/{session_id}/workspace/editor",
                                params={"path": "docs/Plan.docx", "theme": "dark"}, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["document_server_url"] == "https://office.example"
    config = body["config"]
    assert config["editorConfig"]["user"]["id"] == f"user:{user_id}"
    assert config["editorConfig"]["user"]["name"] == f"Test User {user_id}"
    assert config["editorConfig"]["mode"] == "edit"
    assert config["editorConfig"]["customization"]["uiTheme"] == "theme-dark"
    assert jwt.decode(config["token"], JWT_SECRET, algorithms=["HS256"])["document"]["key"] == config["document"]["key"]


async def test_reopening_joins_the_same_session(client, app, session_factory):
    session_id, headers, _, _ = await _user_chat(client, app, session_factory)
    url = f"/v1/sessions/{session_id}/workspace/editor"
    one = (await client.get(url, params={"path": "docs/Plan.docx"}, headers=headers)).json()
    two = (await client.get(url, params={"path": "docs/Plan.docx"}, headers=headers)).json()
    assert one["config"]["document"]["key"] == two["config"]["document"]["key"]


async def test_view_param_opens_read_only(client, app, session_factory):
    session_id, headers, _, _ = await _user_chat(client, app, session_factory)
    response = await client.get(f"/v1/sessions/{session_id}/workspace/editor",
                                params={"path": "docs/Plan.docx", "view": "true"}, headers=headers)
    assert response.json()["config"]["editorConfig"]["mode"] == "view"


async def test_service_account_names_the_person(client, app, session_factory):
    org_id = await create_org(session_factory)
    issued = await issue_service_account_token(session_factory, org_id, name="ops")
    sa_headers = {"Authorization": f"Bearer {issued.token}"}
    created = await client.post("/v1/api/sessions", json={"config": {"source": "ops"}}, headers=sa_headers)
    session_id = created.json()["id"]
    session = await SessionStore(session_factory).get_session(UUID(session_id))
    await app.state.storage.write(session.config["storage_bucket"], session_workspace_key(session_id, "a.xlsx"), b"PK")
    url = f"/v1/api/sessions/{session_id}/workspace/editor"
    missing = await client.get(url, params={"path": "a.xlsx"}, headers=sa_headers)
    assert missing.status_code == 400
    named = await client.get(url, params={"path": "a.xlsx"}, headers={
        **sa_headers, "X-Surogate-Acting-User-Id": "42", "X-Surogate-Acting-User-Name": quote("Ana Popescu"),
    })
    assert named.status_code == 200, named.text
    user = named.json()["config"]["editorConfig"]["user"]
    assert user == {"id": f"sa:{issued.id}:42", "name": "Ana Popescu"}


async def test_acting_headers_are_ignored_on_user_tokens(client, app, session_factory):
    session_id, headers, user_id, _ = await _user_chat(client, app, session_factory)
    response = await client.get(f"/v1/sessions/{session_id}/workspace/editor", params={"path": "docs/Plan.docx"},
                                headers={**headers, "X-Surogate-Acting-User-Id": "someone-else"})
    assert response.json()["config"]["editorConfig"]["user"]["id"] == f"user:{user_id}"


async def test_not_office_missing_and_disabled(client, app, session_factory):
    session_id, headers, _, _ = await _user_chat(client, app, session_factory)
    url = f"/v1/sessions/{session_id}/workspace/editor"
    assert (await client.get(url, params={"path": "notes.md"}, headers=headers)).status_code == 415
    assert (await client.get(url, params={"path": "nope.docx"}, headers=headers)).status_code == 404
    assert (await client.get(url, params={"path": ".threads/x.docx"}, headers=headers)).status_code == 403
    saved = app.state.settings.office
    from surogates.config import OfficeSettings
    app.state.settings.office = OfficeSettings()
    try:
        assert (await client.get(url, params={"path": "docs/Plan.docx"}, headers=headers)).status_code == 503
    finally:
        app.state.settings.office = saved
```

- [ ] **Step 2: Run to see it fail**

Run: `/work/surogates/.venv/bin/python -m pytest tests/integration/test_office_routes.py -q`
Expected: FAIL with 404s (the route does not exist).

- [ ] **Step 3: Add the event types**

In `surogates/session/events.py`, after `ARTIFACT_UPDATED`, add (with a comment block in the file's style):

```python
    # A save from the document editor found the file changed outside it: the
    # save landed as a sibling copy (data: path, conflict_path) and the editors
    # were sent to reload.
    OFFICE_CONFLICT = "office.conflict"
    # A save from the document editor failed (data: path, and status or attempts).
    OFFICE_SAVE_FAILED = "office.save_failed"
```

- [ ] **Step 4: Write `surogates/office/chats.py`**

```python
"""The chats an open document belongs to."""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from surogates.session.store import SessionNotFoundError, SessionStore


async def live_session(store: SessionStore, session_ids: Iterable[UUID]):
    """The first of *session_ids* that still exists and is not archived, or None."""
    for session_id in session_ids:
        try:
            session = await store.get_session(UUID(str(session_id)))
        except SessionNotFoundError:
            continue
        if session.status != "archived":
            return session
    return None
```

- [ ] **Step 5: Write the route**

`surogates/api/routes/office.py`:

```python
"""Office documents in the document server: editor configs, the file it
fetches, and the saves it calls back with."""

from __future__ import annotations

import logging
from typing import Any, Literal
from urllib.parse import unquote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select

from surogates.api.routes.workspace import (
    _get_session_store,
    _get_workspace_session_bucket_and_root,
    _require_service_account_api_route,
    _validate_path,
    workspace_files,
)
from surogates.api.session_guards import require_user_writable_session
from surogates.db.models import User
from surogates.devices.binding import device_of
from surogates.office import sessions as office_sessions
from surogates.office.editor_config import build_editor_config
from surogates.office.kinds import office_kind
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext
from surogates.tools.workspace_io import StorageWorkspaceIO

logger = logging.getLogger(__name__)

router = APIRouter()


class EditorConfigResponse(BaseModel):
    document_server_url: str
    config: dict[str, Any]


@router.get("/api/sessions/{session_id}/workspace/editor", response_model=EditorConfigResponse)
@router.get("/sessions/{session_id}/workspace/editor", response_model=EditorConfigResponse)
async def get_workspace_editor(
    session_id: UUID,
    request: Request,
    path: str = Query(..., description="Relative path within the workspace"),
    theme: Literal["light", "dark"] = Query("light"),
    view: bool = Query(False, description="Open read-only"),
    tenant: TenantContext = Depends(get_current_tenant),
) -> EditorConfigResponse:
    _require_service_account_api_route(request, tenant)
    office = request.app.state.settings.office
    if not office.enabled:
        raise HTTPException(status_code=503, detail="No document server is configured to open office documents.")
    _validate_path(path)
    kind = office_kind(path)
    if kind is None:
        raise HTTPException(status_code=415, detail="This file is not an office document.")
    store = _get_session_store(request)
    session, _bucket, _root = await _get_workspace_session_bucket_and_root(
        request, store, session_id, tenant, path=path, change=True,
    )
    if device_of(session.config) is not None:
        raise HTTPException(status_code=409, detail="Documents in a folder on a computer cannot be opened in the editor yet.")
    user_id, user_name = await _acting_user(request, tenant)
    async with workspace_files(request, session) as files:
        if not isinstance(files, StorageWorkspaceIO):
            raise HTTPException(status_code=409, detail="This workspace cannot be opened in the editor yet.")
        key = await files.resolve(path)
        if await files.stat(key) is None:
            raise HTTPException(status_code=404, detail=f"File not found: {path}")
        location = files.location(key)
    document = await office_sessions.open_or_reuse(
        request.app.state.session_factory, location=location, path=path, session_id=session.id,
    )
    config = build_editor_config(
        settings=office, row_id=document.id, doc_key=document.doc_key, path=path, kind=kind,
        user_id=user_id, user_name=user_name, edit=kind.editable and not view and _writable(session), theme=theme,
    )
    return EditorConfigResponse(document_server_url=office.public_url.rstrip("/"), config=config)


async def _acting_user(request: Request, tenant: TenantContext) -> tuple[str, str]:
    """Who the editor shows: the token's user, or the person a service account acts for."""
    if tenant.service_account_id is not None:
        acting_id = request.headers.get("X-Surogate-Acting-User-Id", "").strip()
        if not acting_id:
            raise HTTPException(
                status_code=400,
                detail="A service account opening the editor names the person in X-Surogate-Acting-User-Id.",
            )
        name = unquote(request.headers.get("X-Surogate-Acting-User-Name", "")).strip() or acting_id
        return f"sa:{tenant.service_account_id}:{acting_id}", name
    if tenant.user_id is not None:
        async with request.app.state.session_factory() as db:
            user = await db.scalar(select(User).where(User.id == tenant.user_id, User.org_id == tenant.org_id))
        return f"user:{tenant.user_id}", user.display_name if user is not None else "You"
    raise HTTPException(status_code=403, detail="Only a person can open the editor.")


def _writable(session: Any) -> bool:
    try:
        require_user_writable_session(session)
    except HTTPException:
        return False
    return True
```

In `surogates/api/app.py`, import `office` beside `workspace` in the routes import and add, right after `app.include_router(workspace.router, prefix="/v1", tags=["workspace"])`:

```python
    app.include_router(office.router, prefix="/v1", tags=["office"])
```

- [ ] **Step 6: Run to see it pass**

Run: `/work/surogates/.venv/bin/python -m pytest tests/integration/test_office_routes.py -q`
Expected: 6 passed. If `test_not_office_missing_and_disabled` gets 403 instead of 404 for `nope.docx`, read `_get_workspace_session_bucket_and_root` and keep its order of checks; adjust only the assertion's expected code to what the existing `/workspace/file` route answers for a missing file.

- [ ] **Step 7: Commit**

```bash
git add surogates/api/routes/office.py surogates/api/app.py surogates/session/events.py surogates/office/chats.py tests/integration/test_office_routes.py
git commit -m "feat(office): the editor route signs a config for the person opening a document"
```

---

### Task 8: The file the document server fetches, and its save callbacks

**Files:**
- Modify: `surogates/api/routes/office.py`
- Test: `tests/integration/test_office_routes.py` (append)

**Interfaces:**
- Consumes: `read_url_token`, `read_document_server_body`, `office_sessions.get_open/record_base_version/record_editors/set_pending/close_when_saved`, `live_session`, `StorageWorkspaceIO.etag`.
- Produces: `GET /v1/office/files/{token}` (bytes), `POST /v1/office/callback/{token}` (`{"error": 0}`); module-level `async def _download(url: str) -> bytes` (tests monkeypatch it); pending saves at `_office/pending/<doc_key>/<uuid hex>` in the chat's bucket.

- [ ] **Step 1: Append the failing tests**

```python
from surogates.office import sessions as office_sessions
from surogates.office.tokens import sign


def _tokens(config):
    file_token = config["document"]["url"].rsplit("/", 1)[1]
    callback_token = config["editorConfig"]["callbackUrl"].rsplit("/", 1)[1]
    return file_token, callback_token


async def _opened(client, app, session_factory, **kwargs):
    session_id, headers, user_id, session = await _user_chat(client, app, session_factory, **kwargs)
    config = (await client.get(f"/v1/sessions/{session_id}/workspace/editor",
                               params={"path": kwargs.get("path", "docs/Plan.docx")}, headers=headers)).json()["config"]
    row_id = read_url_token(TOKEN_SECRET, _tokens(config)[0], "file")
    return session_id, headers, session, config, row_id


async def test_document_server_fetches_the_file(client, app, session_factory):
    _, _, _, config, row_id = await _opened(client, app, session_factory, content=b"PK-original")
    file_token, _ = _tokens(config)
    response = await client.get(f"/v1/office/files/{file_token}")
    assert response.status_code == 200
    assert response.content == b"PK-original"
    assert (await office_sessions.get_open(session_factory, row_id)).base_version


async def test_bad_file_token_is_refused(client):
    assert (await client.get("/v1/office/files/not-a-token")).status_code == 403


async def test_status_1_records_editors(client, app, session_factory):
    _, _, _, config, row_id = await _opened(client, app, session_factory)
    _, callback_token = _tokens(config)
    body = {"key": config["document"]["key"], "status": 1, "users": ["user:1"]}
    response = await client.post(f"/v1/office/callback/{callback_token}", json={"token": sign(JWT_SECRET, body)})
    assert response.json() == {"error": 0}
    assert (await office_sessions.get_open(session_factory, row_id)).editors == ("user:1",)


async def test_unsigned_callback_is_refused(client, app, session_factory):
    _, _, _, config, _ = await _opened(client, app, session_factory)
    _, callback_token = _tokens(config)
    response = await client.post(f"/v1/office/callback/{callback_token}", json={"status": 1, "users": []})
    assert response.status_code == 403


async def test_save_is_taken_and_kept_pending(client, app, session_factory, monkeypatch):
    from surogates.api.routes import office as office_route
    downloads: list[str] = []

    async def fake_download(url: str) -> bytes:
        downloads.append(url)
        return b"PK-edited"

    monkeypatch.setattr(office_route, "_download", fake_download)
    _, _, session, config, row_id = await _opened(client, app, session_factory)
    _, callback_token = _tokens(config)
    body = {"key": config["document"]["key"], "status": 2, "url": "https://office.example/cache/files/x/output.docx?md5=1"}
    response = await client.post(f"/v1/office/callback/{callback_token}", json={"token": sign(JWT_SECRET, body)})
    assert response.json() == {"error": 0}
    assert downloads == ["http://docs.internal/cache/files/x/output.docx?md5=1"]
    pending = (await office_sessions.get_open(session_factory, row_id)).pending_save
    assert pending["close"] is True
    assert pending["key"].startswith(f"_office/pending/{config['document']['key']}/")
    assert await app.state.storage.read(pending["bucket"], pending["key"]) == b"PK-edited"


async def test_newer_save_removes_the_older_pending_object(client, app, session_factory, monkeypatch):
    from surogates.api.routes import office as office_route
    contents = iter([b"first", b"second"])

    async def fake_download(url: str) -> bytes:
        return next(contents)

    monkeypatch.setattr(office_route, "_download", fake_download)
    _, _, _, config, row_id = await _opened(client, app, session_factory)
    _, callback_token = _tokens(config)
    url = "https://office.example/cache/files/x/output.docx"
    for status in (6, 6):
        body = {"key": config["document"]["key"], "status": status, "url": url}
        await client.post(f"/v1/office/callback/{callback_token}", json={"token": sign(JWT_SECRET, body)})
    pending = (await office_sessions.get_open(session_factory, row_id)).pending_save
    assert await app.state.storage.read(pending["bucket"], pending["key"]) == b"second"
    keys = [e["key"] for e in await app.state.storage.list_entries(pending["bucket"], prefix=f"_office/pending/{config['document']['key']}/")]
    assert keys == [pending["key"]]


async def test_closed_document_refuses_callbacks(client, app, session_factory):
    _, _, _, config, row_id = await _opened(client, app, session_factory)
    _, callback_token = _tokens(config)
    await office_sessions.close(session_factory, row_id)
    body = {"key": config["document"]["key"], "status": 1, "users": []}
    response = await client.post(f"/v1/office/callback/{callback_token}", json={"token": sign(JWT_SECRET, body)})
    assert response.status_code == 404
```

If `list_entries` returns entries whose key field is named differently (check `StorageBackend.list_entries` in `surogates/storage/backend.py`), read that name in the test.

- [ ] **Step 2: Run to see them fail**

Run: `/work/surogates/.venv/bin/python -m pytest tests/integration/test_office_routes.py -q -k "fetch or token or status_1 or unsigned or save or closed"`
Expected: FAIL with 404 (no such routes).

- [ ] **Step 3: Implement**

Add to `surogates/api/routes/office.py` (imports: `from uuid import uuid4`, `from urllib.parse import urlsplit, urlunsplit`, `import httpx`, `from fastapi.responses import JSONResponse, Response`, `from surogates.office.chats import live_session`, `from surogates.office.tokens import InvalidOfficeToken, read_document_server_body, read_url_token`, `from surogates.session.events import EventType`):

```python
PENDING_PREFIX = "_office/pending/"


@router.get("/office/files/{token}")
async def get_office_file(token: str, request: Request) -> Response:
    """The document the document server opens: read through the workspace layer."""
    document = await _document_for(request, token, "file")
    session = await _live_session(request, document)
    async with workspace_files(request, session) as files:
        if not isinstance(files, StorageWorkspaceIO):
            raise HTTPException(status_code=409, detail="This workspace cannot be opened in the editor yet.")
        key = await files.resolve(document.path)
        for _ in range(3):  # a version that matches the bytes served
            version = await files.etag(key)
            try:
                data = await files.read(key)
            except FileNotFoundError:
                raise HTTPException(status_code=404, detail="The document is gone.") from None
            if await files.etag(key) == version:
                break
        else:
            raise HTTPException(status_code=409, detail="The document keeps changing; try again.")
    await office_sessions.record_base_version(request.app.state.session_factory, document.id, version)
    return Response(content=data, media_type="application/octet-stream")


@router.post("/office/callback/{token}")
async def office_callback(token: str, request: Request) -> JSONResponse:
    """The document server's status reports and saves."""
    office = request.app.state.settings.office
    document = await _document_for(request, token, "callback")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="The callback is not JSON.") from None
    try:
        payload = read_document_server_body(office.jwt_secret, body, request.headers.get("Authorization"))
    except InvalidOfficeToken:
        raise HTTPException(status_code=403, detail="The callback is not signed by the document server.") from None
    factory = request.app.state.session_factory
    status_code = int(payload.get("status", 0))
    if status_code == 1:
        await office_sessions.record_editors(factory, document.id, [str(user) for user in payload.get("users") or []])
    elif status_code in (2, 6):
        await _take_save(request, document, payload, close=status_code == 2)
    elif status_code == 4:
        await office_sessions.close_when_saved(factory, document.id)
    elif status_code in (3, 7):
        logger.warning("document server could not save %s (status %d, key %s)", document.path, status_code, document.doc_key)
        await _announce(request, document, EventType.OFFICE_SAVE_FAILED, {"path": document.path, "status": status_code})
        if status_code == 3:
            await office_sessions.close_when_saved(factory, document.id)
    return JSONResponse({"error": 0})


async def _take_save(request: Request, document: office_sessions.OpenDocument, payload: dict[str, Any], *, close: bool) -> None:
    """Keep the edited file at once (the document server retries a callback only
    three times, seconds apart); a worker job lands it."""
    office = request.app.state.settings.office
    url = payload.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="The save names no file.")
    try:
        data = await _download(_internal(str(url), office.internal_url))
    except httpx.HTTPError:
        logger.warning("could not download the save of %s (key %s)", document.path, document.doc_key, exc_info=True)
        raise HTTPException(status_code=502, detail="Could not download the saved document.") from None
    session = await _live_session(request, document)
    bucket = session.config["storage_bucket"]
    key = f"{PENDING_PREFIX}{document.doc_key}/{uuid4().hex}"
    storage = request.app.state.storage
    await storage.write(bucket, key, data)
    replaced = await office_sessions.set_pending(
        request.app.state.session_factory, document.id, bucket=bucket, key=key, close=close,
    )
    if replaced:
        await storage.delete(bucket, replaced)


async def _download(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.content


def _internal(url: str, internal_url: str) -> str:
    """The document server's URL rewritten to how the runtime reaches it."""
    target, source = urlsplit(internal_url), urlsplit(url)
    return urlunsplit((target.scheme, target.netloc, source.path, source.query, source.fragment))


async def _document_for(request: Request, token: str, purpose: Literal["file", "callback"]) -> office_sessions.OpenDocument:
    office = request.app.state.settings.office
    if not office.enabled:
        raise HTTPException(status_code=503, detail="No document server is configured.")
    try:
        row_id = read_url_token(office.token_secret, token, purpose)
    except InvalidOfficeToken:
        raise HTTPException(status_code=403, detail="This link is not valid.") from None
    document = await office_sessions.get_open(request.app.state.session_factory, row_id)
    if document is None:
        raise HTTPException(status_code=404, detail="The document is no longer open.")
    return document


async def _live_session(request: Request, document: office_sessions.OpenDocument):
    session = await live_session(_get_session_store(request), document.session_ids)
    if session is None:
        raise HTTPException(status_code=404, detail="The document's chat is gone.")
    return session


async def _announce(request: Request, document: office_sessions.OpenDocument, event_type: EventType, data: dict[str, Any]) -> None:
    store = _get_session_store(request)
    for session_id in document.session_ids:
        try:
            await store.emit_event(session_id, event_type, data)
        except Exception:
            logger.warning("could not tell session %s about %s", session_id, event_type.value, exc_info=True)
```

- [ ] **Step 4: Run to see them pass**

Run: `/work/surogates/.venv/bin/python -m pytest tests/integration/test_office_routes.py -q`
Expected: all passed (13).

- [ ] **Step 5: Commit**

```bash
git add surogates/api/routes/office.py tests/integration/test_office_routes.py
git commit -m "feat(office): serve documents to the document server and keep its saves"
```

---

### Task 9: Land saves from a worker job

**Files:**
- Create: `surogates/office/commands.py`, `surogates/office/saves.py`
- Test: `tests/test_office_conflict_names.py`, `tests/integration/test_office_saves.py`

**Interfaces:**
- Consumes: `office_sessions.claim_pending/mark_landed/mark_closed/mark_attempt`, `live_session`, `StorageWorkspaceIO.write_if/etag`, `session_files` (`surogates/session/files.py`), `EventType.OFFICE_CONFLICT/OFFICE_SAVE_FAILED`.
- Produces:
  - `conflict_copy_paths(path: str, when: datetime) -> Iterator[str]`
  - `async drop_editors(office: OfficeSettings, doc_key: str, users: list[str]) -> None`
  - `async land_pending_saves(*, session_factory, session_store, storage, redis, office) -> int`

- [ ] **Step 1: Write the failing name tests**

`tests/test_office_conflict_names.py`:

```python
from datetime import datetime, timezone
from itertools import islice

from surogates.office.saves import conflict_copy_paths

WHEN = datetime(2026, 10, 13, 14, 32, tzinfo=timezone.utc)


def test_conflict_copy_names():
    names = list(islice(conflict_copy_paths("docs/Q3 plan.docx", WHEN), 3))
    assert names == [
        "docs/Q3 plan (edited 2026-10-13 14.32 UTC).docx",
        "docs/Q3 plan (edited 2026-10-13 14.32 UTC) 2.docx",
        "docs/Q3 plan (edited 2026-10-13 14.32 UTC) 3.docx",
    ]


def test_conflict_copy_at_the_root():
    assert next(conflict_copy_paths("Plan.xlsx", WHEN)) == "Plan (edited 2026-10-13 14.32 UTC).xlsx"
```

- [ ] **Step 2: Write the failing landing tests**

`tests/integration/test_office_saves.py`:

```python
"""Landing pending saves: guarded writes, conflict copies, closing."""

from __future__ import annotations

from uuid import uuid4

import pytest

from surogates.config import OfficeSettings
from surogates.office import saves as office_saves
from surogates.office import sessions as office_sessions
from surogates.session.store import SessionStore
from surogates.storage.backend import LocalBackend
from surogates.storage.tenant import session_workspace_key

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")

OFFICE = OfficeSettings(
    public_url="https://office.example", internal_url="http://docs.internal",
    callback_base_url="https://api.example", jwt_secret="j" * 32, token_secret="t" * 32,
)


@pytest.fixture
def storage(tmp_path):
    return LocalBackend(str(tmp_path))


async def _chat_with_document(session_factory, storage, content=b"v1"):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = SessionStore(session_factory)
    session = await store.create_session(user_id=user_id, org_id=org_id, agent_id="default", channel="web",
                                         config={"storage_bucket": "b"})
    key = session_workspace_key(session.id, "a.docx")
    await storage.write("b", key, content)
    document = await office_sessions.open_or_reuse(session_factory, location=f"bucket:b/{key}", path="a.docx", session_id=session.id)
    await office_sessions.record_base_version(session_factory, document.id, (await storage.stat("b", key))["etag"])
    return store, session, key, document


async def _pend(session_factory, storage, document, data, *, close=False):
    pending_key = f"_office/pending/{document.doc_key}/{uuid4().hex}"
    await storage.write("b", pending_key, data)
    await office_sessions.set_pending(session_factory, document.id, bucket="b", key=pending_key, close=close)
    return pending_key


async def test_save_lands_and_closes(session_factory, redis_client, storage):
    store, _, key, document = await _chat_with_document(session_factory, storage)
    pending_key = await _pend(session_factory, storage, document, b"v2", close=True)
    landed = await office_saves.land_pending_saves(session_factory=session_factory, session_store=store,
                                                   storage=storage, redis=redis_client, office=OFFICE)
    assert landed >= 1
    assert await storage.read("b", key) == b"v2"
    assert not await storage.exists("b", pending_key)
    assert await office_sessions.get_open(session_factory, document.id) is None


async def test_changed_file_lands_as_a_sibling_copy(session_factory, redis_client, storage, monkeypatch):
    dropped: list[tuple[str, list[str]]] = []

    async def fake_drop(office, doc_key, users):
        dropped.append((doc_key, users))

    monkeypatch.setattr(office_saves, "drop_editors", fake_drop)
    store, session, key, document = await _chat_with_document(session_factory, storage)
    await office_sessions.record_editors(session_factory, document.id, ["user:1"])
    await storage.write("b", key, b"someone else")
    await _pend(session_factory, storage, document, b"mine")
    await office_saves.land_pending_saves(session_factory=session_factory, session_store=store,
                                          storage=storage, redis=redis_client, office=OFFICE)
    assert await storage.read("b", key) == b"someone else"
    copies = [e for e in await storage.list_entries("b", prefix=session_workspace_key(session.id, "a (edited "))]
    assert len(copies) == 1
    assert dropped == [(document.doc_key, ["user:1"])]
    assert await office_sessions.get_open(session_factory, document.id) is None
    events = await store.get_events(session.id)
    assert any(e.type == "office.conflict" for e in events)


async def test_chat_gone_closes_without_writing(session_factory, redis_client, storage):
    store, session, key, document = await _chat_with_document(session_factory, storage)
    pending_key = await _pend(session_factory, storage, document, b"v2")
    await store.update_session_status(session.id, "archived")
    await office_saves.land_pending_saves(session_factory=session_factory, session_store=store,
                                          storage=storage, redis=redis_client, office=OFFICE)
    assert await storage.read("b", key) == b"v1"
    assert not await storage.exists("b", pending_key)
    assert await office_sessions.get_open(session_factory, document.id) is None
```

The calls `store.create_session(...)`, `store.update_session_status(...)` and `store.get_events(...)` must match `surogates/session/store.py`; open it and use the exact parameter names and the event attribute (`type` or `event_type`) it defines.

- [ ] **Step 3: Run to see them fail**

Run: `/work/surogates/.venv/bin/python -m pytest tests/test_office_conflict_names.py tests/integration/test_office_saves.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'surogates.office.saves'`.

- [ ] **Step 4: Implement the Command Service call**

`surogates/office/commands.py`:

```python
"""Commands to the document server (its Command Service)."""

from __future__ import annotations

import logging

import httpx

from surogates.config import OfficeSettings
from surogates.office.tokens import sign

logger = logging.getLogger(__name__)


async def drop_editors(office: OfficeSettings, doc_key: str, users: list[str]) -> None:
    """Disconnect *users* from the document, so their editors reload it."""
    if not users:
        return
    command = {"c": "drop", "key": doc_key, "users": users}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{office.internal_url.rstrip('/')}/command",
                json={**command, "token": sign(office.jwt_secret, command)},
            )
            response.raise_for_status()
    except httpx.HTTPError:
        logger.warning("could not drop the editors of %s", doc_key, exc_info=True)
```

- [ ] **Step 5: Implement landing**

`surogates/office/saves.py`:

```python
"""Landing saves from the document editor in the workspace."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

from surogates.config import OfficeSettings
from surogates.db.models import OfficeSession
from surogates.office import sessions as office_sessions
from surogates.office.chats import live_session
from surogates.office.commands import drop_editors
from surogates.session.events import EventType
from surogates.session.files import session_files
from surogates.tools.workspace_io import RevisionConflict, StorageWorkspaceIO

logger = logging.getLogger(__name__)

# After this many failed tries the chats hear about it; the save keeps retrying.
SAVE_FAILED_AFTER = 10


def conflict_copy_paths(path: str, when: datetime) -> Iterator[str]:
    """Names for a save that found its file changed: beside the original."""
    pure = PurePosixPath(path)
    label = when.astimezone(timezone.utc).strftime("%Y-%m-%d %H.%M")
    stem = f"{pure.stem} (edited {label} UTC)"
    parent = "" if str(pure.parent) == "." else f"{pure.parent}/"
    yield f"{parent}{stem}{pure.suffix}"
    for number in range(2, 100):
        yield f"{parent}{stem} {number}{pure.suffix}"


async def land_pending_saves(*, session_factory: Any, session_store: Any, storage: Any, redis: Any, office: OfficeSettings) -> int:
    landed = 0
    async with office_sessions.claim_pending(session_factory) as rows:
        for row in rows:
            try:
                await _land(row, session_factory=session_factory, session_store=session_store,
                            storage=storage, redis=redis, office=office)
                landed += 1
            except Exception:
                attempts = office_sessions.mark_attempt(row)
                logger.exception("could not land the save of %s (attempt %d)", row.path, attempts)
                if attempts == SAVE_FAILED_AFTER:
                    await _announce(session_store, row.session_ids, EventType.OFFICE_SAVE_FAILED,
                                    {"path": row.path, "attempts": attempts})
    return landed


async def _land(row: OfficeSession, *, session_factory: Any, session_store: Any, storage: Any, redis: Any, office: OfficeSettings) -> None:
    pending = dict(row.pending_save or {})
    session = await live_session(session_store, row.session_ids)
    if session is None:  # every chat is gone: nothing to save into
        office_sessions.mark_closed(row)
        await storage.delete(pending["bucket"], pending["key"])
        return
    data = await storage.read(pending["bucket"], pending["key"])
    async with session_files(session, storage=storage, session_factory=session_factory, redis=redis) as files:
        if not isinstance(files, StorageWorkspaceIO):
            raise RuntimeError(f"{row.path}: saving into a folder on a computer is not supported")
        key = await files.resolve(row.path)
        try:
            await files.write_if(key, data, expected=row.base_version)
        except RevisionConflict:
            copy_path = await _write_copy(files, row.path, data)
            await _announce(session_store, row.session_ids, EventType.OFFICE_CONFLICT,
                            {"path": row.path, "conflict_path": copy_path})
            await drop_editors(office, row.doc_key, [str(user) for user in row.editors])
            office_sessions.mark_closed(row)
        else:
            office_sessions.mark_landed(row, await files.etag(key))
    await storage.delete(pending["bucket"], pending["key"])


async def _write_copy(files: StorageWorkspaceIO, path: str, data: bytes) -> str:
    for candidate in conflict_copy_paths(path, datetime.now(timezone.utc)):
        try:
            await files.write_if(await files.resolve(candidate), data, expected=None)
        except RevisionConflict:
            continue
        return candidate
    raise RuntimeError(f"no free name for a copy of {path}")


async def _announce(session_store: Any, session_ids: list[Any], event_type: EventType, data: dict[str, Any]) -> None:
    for session_id in session_ids:
        try:
            await session_store.emit_event(UUID(str(session_id)), event_type, data)
        except Exception:
            logger.warning("could not tell session %s about %s", session_id, event_type.value, exc_info=True)
```

- [ ] **Step 6: Run to see them pass**

Run: `/work/surogates/.venv/bin/python -m pytest tests/test_office_conflict_names.py tests/integration/test_office_saves.py -q`
Expected: all passed.

- [ ] **Step 7: Commit**

```bash
git add surogates/office/commands.py surogates/office/saves.py tests/test_office_conflict_names.py tests/integration/test_office_saves.py
git commit -m "feat(office): land editor saves in the workspace from a worker job"
```

---

### Task 10: Close editing sessions nobody uses

**Files:**
- Create: `surogates/office/reaper.py`, `surogates/jobs/office_saves.py`
- Modify: `surogates/orchestrator/worker.py` (start and stop the job)
- Test: `tests/integration/test_office_reaper.py`

**Interfaces:**
- Consumes: `office_sessions.close_never_opened/open_documents/close`, `live_session`, `drop_editors`, `land_pending_saves`.
- Produces: `async reap(*, session_factory, session_store, office: OfficeSettings, now: datetime | None = None) -> int`; `async run_office_loop(*, session_factory, session_store, storage, redis, settings, interval_seconds: float = 2.0) -> None`.

- [ ] **Step 1: Write the failing test**

`tests/integration/test_office_reaper.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from surogates.config import OfficeSettings
from surogates.office import reaper
from surogates.office import sessions as office_sessions
from surogates.session.store import SessionStore

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")

OFFICE = OfficeSettings(public_url="https://o", internal_url="http://d", callback_base_url="https://a",
                        jwt_secret="j" * 32, token_secret="t" * 32)


async def test_documents_of_archived_chats_are_dropped_and_closed(session_factory, monkeypatch):
    dropped = []

    async def fake_drop(office, doc_key, users):
        dropped.append(doc_key)

    monkeypatch.setattr(reaper, "drop_editors", fake_drop)
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = SessionStore(session_factory)
    session = await store.create_session(user_id=user_id, org_id=org_id, agent_id="default", channel="web", config={})
    document = await office_sessions.open_or_reuse(session_factory, location=f"bucket:b/{uuid4()}", path="a.docx", session_id=session.id)
    await office_sessions.record_editors(session_factory, document.id, ["user:1"])
    await store.update_session_status(session.id, "archived")
    closed = await reaper.reap(session_factory=session_factory, session_store=store, office=OFFICE)
    assert closed >= 1
    assert document.doc_key in dropped
    assert await office_sessions.get_open(session_factory, document.id) is None


async def test_never_opened_documents_close_after_ten_minutes(session_factory):
    store = SessionStore(session_factory)
    document = await office_sessions.open_or_reuse(session_factory, location=f"bucket:b/{uuid4()}", path="a.docx", session_id=uuid4())
    await reaper.reap(session_factory=session_factory, session_store=store, office=OFFICE,
                      now=datetime.now(timezone.utc) + timedelta(minutes=11))
    assert await office_sessions.get_open(session_factory, document.id) is None
```

Use the exact `create_session` / `update_session_status` signatures from `surogates/session/store.py`, as in Task 9.

- [ ] **Step 2: Run to see it fail**

Run: `/work/surogates/.venv/bin/python -m pytest tests/integration/test_office_reaper.py -q`
Expected: FAIL, `ImportError: cannot import name 'reaper'`.

- [ ] **Step 3: Implement**

`surogates/office/reaper.py`:

```python
"""Closing editing sessions nobody uses: never opened, or whose chats are gone."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from surogates.config import OfficeSettings
from surogates.office import sessions as office_sessions
from surogates.office.chats import live_session
from surogates.office.commands import drop_editors

NEVER_OPENED_AFTER = timedelta(minutes=10)


async def reap(*, session_factory: Any, session_store: Any, office: OfficeSettings, now: datetime | None = None) -> int:
    closed = await office_sessions.close_never_opened(session_factory, older_than=NEVER_OPENED_AFTER, now=now)
    for document in await office_sessions.open_documents(session_factory):
        if await live_session(session_store, document.session_ids) is not None:
            continue
        # A deleted chat's document must not save back into its removed workspace.
        await drop_editors(office, document.doc_key, list(document.editors))
        await office_sessions.close(session_factory, document.id)
        closed += 1
    return closed
```

- [ ] **Step 4: Run the job from the worker**

`surogates/jobs/office_saves.py`:

```python
"""The worker job that lands saves from the document editor and closes stale sessions."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from surogates.config import Settings
from surogates.office.reaper import reap
from surogates.office.saves import land_pending_saves

logger = logging.getLogger(__name__)

REAP_EVERY = 30  # passes, about a minute apart at the default interval


async def run_office_loop(*, session_factory: Any, session_store: Any, storage: Any, redis: Any, settings: Settings,
                          interval_seconds: float = 2.0) -> None:
    office = settings.office
    if not office.enabled:
        return
    passes = 0
    while True:
        try:
            await land_pending_saves(session_factory=session_factory, session_store=session_store,
                                     storage=storage, redis=redis, office=office)
            if passes % REAP_EVERY == 0:
                await reap(session_factory=session_factory, session_store=session_store, office=office)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("office saves pass failed")
        passes += 1
        await asyncio.sleep(interval_seconds)
```


In `surogates/orchestrator/worker.py`, after the `browser_reaper_task = asyncio.create_task(...)` block, add:

```python
    # Saves from the document editor, landed in the workspace; stale editing sessions closed.
    from surogates.jobs.office_saves import run_office_loop
    office_task = asyncio.create_task(
        run_office_loop(
            session_factory=session_factory,
            session_store=session_store,
            storage=storage_backend,
            redis=redis_client,
            settings=settings,
        ),
        name="office-saves",
    )
```

and where `browser_reaper_task.cancel()` and `await browser_reaper_task` run at shutdown (around line 2303), add the same two lines for `office_task` in the same style (inside the same try/except shape used there).

- [ ] **Step 5: Run to see it pass, then the whole office suite**

Run: `/work/surogates/.venv/bin/python -m pytest tests/integration/test_office_reaper.py tests/integration/test_office_saves.py tests/integration/test_office_sessions.py tests/integration/test_office_routes.py tests/test_office_settings.py tests/test_office_editor_config.py tests/test_office_conflict_names.py tests/test_storage_workspace_write_if.py -q`
Expected: all passed.

- [ ] **Step 6: Commit**

```bash
git add surogates/office/reaper.py surogates/jobs/office_saves.py surogates/orchestrator/worker.py tests/integration/test_office_reaper.py
git commit -m "feat(office): land saves and close unused editing sessions from a worker job"
```

---

### Task 11: The editor in the SDK's file pane

**Files:**
- Modify: `sdk/agent-chat-react/src/types.ts` (new type, new optional adapter method)
- Modify: `sdk/agent-chat-react/src/index.ts` (export the type)
- Create: `sdk/agent-chat-react/src/lib/office.ts`
- Create: `sdk/agent-chat-react/src/components/workspace/office-editor-pane.tsx`
- Modify: `sdk/agent-chat-react/src/components/workspace/workspace-file-drawer.tsx`
- Modify: `sdk/agent-chat-react/src/components/workspace/file-viewer.tsx` (delete the pdf.js viewer)
- Modify: `sdk/agent-chat-react/src/agent-chat.tsx` (pass `view`)
- Modify: `sdk/agent-chat-react/package.json`, `web/package.json` (drop `pdfjs-dist`; regenerate both lockfiles)
- Modify: `sdk/agent-chat-react/tests/agent-chat.test.tsx` (delete the pdf.js viewer tests)
- Test: `sdk/agent-chat-react/tests/office-editor-pane.test.tsx`

**Interfaces:**
- Produces: `AgentChatWorkspaceEditor { document_server_url: string; config: Record<string, unknown> }`; `AgentChatAdapter.getWorkspaceEditorConfig?(input: { sessionId: string; path: string; theme: "light" | "dark"; view?: boolean }): Promise<AgentChatWorkspaceEditor>`; `isOfficeDocument(path: string): boolean`; `loadDocsApi(documentServerUrl: string): Promise<void>`.

- [ ] **Step 1: Install**

```bash
cd /work/surogates/.worktrees/office-editors-platform/sdk/agent-chat-react && npm ci
```

- [ ] **Step 2: Write the failing tests**

`sdk/agent-chat-react/tests/office-editor-pane.test.tsx`:

```tsx
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NO_BROWSER_ADAPTER } from "../src/adapter-context";
import { WorkspaceFileDrawer } from "../src/components/workspace/workspace-file-drawer";
import type { AgentChatAdapter } from "../src/types";

vi.mock("../src/lib/office", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/lib/office")>()),
  loadDocsApi: vi.fn().mockResolvedValue(undefined),
}));

import { loadDocsApi } from "../src/lib/office";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;
const destroyEditor = vi.fn();
const DocEditor = vi.fn(() => ({ destroyEditor }));

beforeEach(() => {
  DocEditor.mockClear();
  destroyEditor.mockClear();
  (window as unknown as { DocsAPI: unknown }).DocsAPI = { DocEditor };
});

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
});

function adapter(over: Partial<AgentChatAdapter> = {}): AgentChatAdapter {
  return {
    ...NO_BROWSER_ADAPTER,
    getWorkspaceDownloadUrl: ({ path }: { path: string }) => `/download?path=${path}`,
    getWorkspaceFile: vi.fn().mockResolvedValue({ path: "scan.pdf", content: "JVBERi0=", size: 6, mime_type: "application/pdf", encoding: "base64", truncated: false }),
    ...over,
  } as unknown as AgentChatAdapter;
}

async function mount(node: React.ReactElement) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(node);
  });
  await act(async () => {});
  return container;
}

describe("office documents in the file pane", () => {
  it("opens the editor with the signed config", async () => {
    const config = { document: { key: "k" }, token: "t" };
    const getWorkspaceEditorConfig = vi.fn().mockResolvedValue({ document_server_url: "https://office.example", config });
    const view = await mount(
      <WorkspaceFileDrawer adapter={adapter({ getWorkspaceEditorConfig })} sessionId="s-1" path="docs/Plan.docx" onClose={() => {}} />,
    );
    expect(getWorkspaceEditorConfig).toHaveBeenCalledWith({ sessionId: "s-1", path: "docs/Plan.docx", theme: "light", view: false });
    expect(loadDocsApi).toHaveBeenCalledWith("https://office.example");
    expect(DocEditor).toHaveBeenCalledTimes(1);
    const [placeholderId, passed] = DocEditor.mock.calls[0] as unknown as [string, unknown];
    expect(passed).toBe(config);
    expect(view.querySelector(`#${placeholderId}`)).not.toBeNull();
    expect(view.querySelector('a[aria-label="Download Plan.docx"]')?.getAttribute("href")).toBe("/download?path=docs/Plan.docx");
  });

  it("asks for view mode in a read-only chat", async () => {
    const getWorkspaceEditorConfig = vi.fn().mockResolvedValue({ document_server_url: "https://o", config: {} });
    await mount(<WorkspaceFileDrawer adapter={adapter({ getWorkspaceEditorConfig })} sessionId="s-1" path="a.xlsx" view onClose={() => {}} />);
    expect(getWorkspaceEditorConfig).toHaveBeenCalledWith(expect.objectContaining({ view: true }));
  });

  it("destroys the editor when the pane closes", async () => {
    const getWorkspaceEditorConfig = vi.fn().mockResolvedValue({ document_server_url: "https://o", config: {} });
    await mount(<WorkspaceFileDrawer adapter={adapter({ getWorkspaceEditorConfig })} sessionId="s-1" path="a.pptx" onClose={() => {}} />);
    act(() => root?.unmount());
    root = null;
    expect(destroyEditor).toHaveBeenCalledTimes(1);
  });

  it("shows why the editor cannot open, with the download link", async () => {
    const getWorkspaceEditorConfig = vi.fn().mockRejectedValue(new Error("No document server is configured."));
    const view = await mount(<WorkspaceFileDrawer adapter={adapter({ getWorkspaceEditorConfig })} sessionId="s-1" path="a.docx" onClose={() => {}} />);
    expect(view.querySelector('[role="alert"]')?.textContent).toContain("No document server is configured.");
    expect(view.querySelector('a[aria-label="Download a.docx"]')).not.toBeNull();
    expect(DocEditor).not.toHaveBeenCalled();
  });

  it("shows an error when the editor script cannot load", async () => {
    vi.mocked(loadDocsApi).mockRejectedValueOnce(new Error("The document editor could not be loaded."));
    const getWorkspaceEditorConfig = vi.fn().mockResolvedValue({ document_server_url: "https://down", config: {} });
    const view = await mount(<WorkspaceFileDrawer adapter={adapter({ getWorkspaceEditorConfig })} sessionId="s-1" path="a.docx" onClose={() => {}} />);
    expect(view.querySelector('[role="alert"]')?.textContent).toContain("could not be loaded");
  });

  it("without the editor method, a PDF says preview is not available", async () => {
    const view = await mount(<WorkspaceFileDrawer adapter={adapter()} sessionId="s-1" path="scan.pdf" onClose={() => {}} />);
    expect(view.textContent).toContain("Preview not available");
    expect(view.querySelector("img")).toBeNull();
  });
});
```

- [ ] **Step 3: Run to see it fail**

Run: `npx vitest run tests/office-editor-pane.test.tsx`
Expected: FAIL (`../src/lib/office` does not exist).

- [ ] **Step 4: Add the types**

In `src/types.ts`, after `AgentChatWorkspaceFile`:

```ts
/** A document opened in the document server's editor. */
export interface AgentChatWorkspaceEditor {
  /** Where the browser loads the editor (`<url>/web-apps/apps/api/documents/api.js`). */
  document_server_url: string;
  /** The signed editor config, passed to `DocsAPI.DocEditor` unchanged. */
  config: Record<string, unknown>;
}
```

In `AgentChatAdapter`, after `getWorkspaceDownloadUrl`:

```ts
  /**
   * Opens an office document (.docx, .xlsx, .pptx, .pdf; older formats read-only) in the
   * document server's editor. *theme* is the app's, signed into the config. Optional: a
   * host without a document server leaves it out, and office files show no preview.
   */
  getWorkspaceEditorConfig?(input: {
    sessionId: string;
    path: string;
    theme: "light" | "dark";
    view?: boolean;
  }): Promise<AgentChatWorkspaceEditor>;
```

In `src/index.ts`, export `AgentChatWorkspaceEditor` beside `AgentChatWorkspaceFile` (both export lists that name it).

- [ ] **Step 5: Write `src/lib/office.ts`**

```ts
const OFFICE_EXTENSIONS = new Set(["docx", "xlsx", "pptx", "pdf", "doc", "odt", "rtf", "xls", "ods", "ppt", "odp"]);

/** Whether the document server opens this file. */
export function isOfficeDocument(path: string): boolean {
  const dot = path.lastIndexOf(".");
  return dot >= 0 && OFFICE_EXTENSIONS.has(path.slice(dot + 1).toLowerCase());
}

export interface DocEditorInstance {
  destroyEditor(): void;
}

declare global {
  interface Window {
    DocsAPI?: {
      DocEditor: new (placeholderId: string, config: Record<string, unknown>) => DocEditorInstance;
    };
  }
}

const loads = new Map<string, Promise<void>>();

/** Loads the document server's `api.js` once per server. */
export function loadDocsApi(documentServerUrl: string): Promise<void> {
  const src = `${documentServerUrl.replace(/\/$/, "")}/web-apps/apps/api/documents/api.js`;
  let load = loads.get(src);
  if (!load) {
    load = new Promise<void>((resolve, reject) => {
      const script = document.createElement("script");
      script.src = src;
      script.async = true;
      script.onload = () => resolve();
      script.onerror = () => {
        loads.delete(src);
        script.remove();
        reject(new Error("The document editor could not be loaded."));
      };
      document.head.appendChild(script);
    });
    loads.set(src, load);
  }
  return load;
}
```

- [ ] **Step 6: Write the pane**

`src/components/workspace/office-editor-pane.tsx` (tabs, like the drawer):

```tsx
import { useEffect, useId, useState } from "react";
import { useTheme } from "next-themes";
import { AlertCircleIcon, DownloadIcon, FileTextIcon, XIcon } from "lucide-react";
import { type DocEditorInstance, loadDocsApi } from "../../lib/office";
import type { AgentChatAdapter } from "../../types";

interface OfficeEditorPaneProps {
	adapter: AgentChatAdapter;
	sessionId: string;
	path: string;
	view: boolean;
	onClose: () => void;
}

/** An office document open in the document server's editor, in the file pane. */
export function OfficeEditorPane({ adapter, sessionId, path, view, onClose }: OfficeEditorPaneProps) {
	const { resolvedTheme } = useTheme();
	const theme = resolvedTheme === "dark" ? "dark" : "light";
	const placeholderId = `office-editor-${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
	const [error, setError] = useState<string | null>(null);
	const [loading, setLoading] = useState(true);

	useEffect(() => {
		let cancelled = false;
		let editor: DocEditorInstance | null = null;
		setError(null);
		setLoading(true);
		void (async () => {
			try {
				if (!adapter.getWorkspaceEditorConfig) throw new Error("This app cannot open office documents.");
				const opened = await adapter.getWorkspaceEditorConfig({ sessionId, path, theme, view });
				await loadDocsApi(opened.document_server_url);
				if (cancelled) return;
				if (!window.DocsAPI) throw new Error("The document editor could not be loaded.");
				editor = new window.DocsAPI.DocEditor(placeholderId, opened.config);
			} catch (openError) {
				if (!cancelled) setError((openError as Error).message);
			} finally {
				if (!cancelled) setLoading(false);
			}
		})();
		return () => {
			cancelled = true;
			editor?.destroyEditor();
		};
	}, [adapter, sessionId, path, theme, view, placeholderId]);

	const fileName = path.split("/").pop() ?? path;
	return (
		<aside
			data-testid="file-preview-panel"
			className="flex h-full min-h-0 w-full flex-col overflow-hidden border-l border-line bg-card"
		>
			<div className="flex shrink-0 items-center gap-2 border-b border-line px-3 py-2">
				<FileTextIcon className="size-4 shrink-0 text-muted-foreground" />
				<div className="min-w-0 flex-1 truncate font-medium">{fileName}</div>
				<a
					href={adapter.getWorkspaceDownloadUrl({ sessionId, path })}
					download={fileName}
					aria-label={`Download ${fileName}`}
					title="Download"
					className="shrink-0 rounded p-0.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
				>
					<DownloadIcon className="size-4" />
				</a>
				<button
					type="button"
					onClick={onClose}
					aria-label="Close file"
					className="shrink-0 rounded p-0.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
				>
					<XIcon className="size-4" />
				</button>
			</div>
			{error && (
				<div role="alert" className="flex items-start gap-2 p-3 text-xs text-destructive">
					<AlertCircleIcon className="mt-0.5 size-3.5 shrink-0" />
					<span>{error}</span>
				</div>
			)}
			{loading && !error && <div className="p-3 text-sm text-muted-foreground">Opening {fileName}…</div>}
			<div className="min-h-0 flex-1">
				<div id={placeholderId} />
			</div>
		</aside>
	);
}
```

- [ ] **Step 7: Branch in the drawer before the fetch**

In `workspace-file-drawer.tsx`: rename the current component to `WorkspaceFilePreview` (unexported, same props and body), add `view?: boolean` to `WorkspaceFileDrawerProps`, import `isOfficeDocument` and `OfficeEditorPane`, and add:

```tsx
export function WorkspaceFileDrawer({ adapter, sessionId, path, view = false, onClose }: WorkspaceFileDrawerProps) {
	if (isOfficeDocument(path) && adapter.getWorkspaceEditorConfig) {
		return <OfficeEditorPane adapter={adapter} sessionId={sessionId} path={path} view={view} onClose={onClose} />;
	}
	return <WorkspaceFilePreview adapter={adapter} sessionId={sessionId} path={path} onClose={onClose} />;
}
```

In `src/agent-chat.tsx`, where `<WorkspaceFileDrawer adapter={adapter} sessionId={sessionId} path={previewPath} onClose={…} />` is rendered, add `view={effectiveDisabled}`.

- [ ] **Step 8: Delete the pdf.js viewer**

In `file-viewer.tsx`: delete the `pdfjs-dist` imports (the CSS import, the two `import type` blocks, `PDF_WORKER_SRC`), `PdfPreview`, `decodeBase64` and `formatPdfPreviewError`, and the now-unused icons. Replace the `isPdf`/`isImage` lines and the body branch with:

```tsx
  const isImage = file?.encoding === "base64" && (file.mime_type?.startsWith("image/") ?? false);
  const isBinary = file?.encoding === "base64" && !isImage;
```

and, inside the `ScrollArea`, after the error block:

```tsx
          {file && isBinary && (
            <FileViewerError message="Preview not available. Download the file to open it." />
          )}
```

keeping the image and text branches with their conditions changed to `file && isImage` and `file && !isImage && !isBinary`, and removing the `isPdf ? <PdfPreview …/> : (` wrapper so the `ScrollArea` is always the body. In `tests/agent-chat.test.tsx`, delete the tests that assert `div[aria-label="PDF viewer for report.pdf"]` or the message "PDF preview data is not valid base64." (around lines 845-1009). Remove `"pdfjs-dist"` from `sdk/agent-chat-react/package.json` and `web/package.json`, then run `npm install` in both folders to update their lockfiles.

- [ ] **Step 9: Run tests and typechecks**

```bash
cd /work/surogates/.worktrees/office-editors-platform/sdk/agent-chat-react
npx vitest run tests/office-editor-pane.test.tsx
npm test
npm run typecheck
cd ../../web && npm ci && npm run typecheck
```

Expected: the new tests pass, the whole SDK suite passes, both typechecks are clean.

- [ ] **Step 10: Commit**

```bash
cd /work/surogates/.worktrees/office-editors-platform
git add sdk/agent-chat-react web/package.json web/package-lock.json
git commit -m "feat(sdk): open office documents in the editor in the file pane"
```

---

### Task 12: The surogates web client opens the editor

**Files:**
- Modify: `web/src/api/workspace.ts`
- Modify: `web/src/features/chat/surogates-web-chat-adapter.ts`

**Interfaces:**
- Consumes: `AgentChatAdapter.getWorkspaceEditorConfig` (Task 11), `GET /v1/sessions/{id}/workspace/editor` (Task 7).

- [ ] **Step 1: Add the API call**

In `web/src/api/workspace.ts`, after `getWorkspaceFile`:

```ts
export interface WorkspaceEditorResponse {
	document_server_url: string;
	config: Record<string, unknown>;
}

export async function getWorkspaceEditor(
	sessionId: string,
	path: string,
	theme: "light" | "dark",
	view: boolean,
): Promise<WorkspaceEditorResponse> {
	const params = new URLSearchParams({ path, theme });
	if (view) params.set("view", "true");
	const response = await authFetch(`/api/v1/sessions/${sessionId}/workspace/editor?${params}`);
	if (!response.ok) {
		throw new Error(await refusal(sessionId, response, "Failed to open the document"));
	}
	return (await response.json()) as WorkspaceEditorResponse;
}
```

(Match the file's indentation; the excerpt above uses tabs.)

- [ ] **Step 2: Implement the adapter method**

In `surogates-web-chat-adapter.ts`, after `getWorkspaceFile`:

```ts
	async getWorkspaceEditorConfig(input) {
		return await workspaceApi.getWorkspaceEditor(input.sessionId, input.path, input.theme, input.view ?? false);
	},
```

- [ ] **Step 3: Typecheck**

Run: `cd web && npm run typecheck`
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add web/src/api/workspace.ts web/src/features/chat/surogates-web-chat-adapter.ts
git commit -m "feat(web): open office documents in the editor"
```

---

### Task 13: ops proxies the editor route with the acting user

**Files (surogate-ops worktree):**
- Modify: `surogate_ops/core/synthetic/surogates_api.py` (`_request` and `request_json` take `headers`)
- Modify: `surogate_ops/server/routes/sessions.py` (`_forward_json` passes `headers`; new route)
- Modify: `tests/test_sessions_route_authz.py` (`SESSION_ROUTES`)
- Test: `tests/test_sessions_live_proxy.py` (append)

**Interfaces:**
- Produces: `GET /api/sessions/{session_id}/workspace/editor?path=&theme=&view=` → forwards to `/v1/api/sessions/{id}/workspace/editor` with `X-Surogate-Acting-User-Id: <ops user id>` and `X-Surogate-Acting-User-Name: <percent-encoded name>`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_sessions_live_proxy.py`:

```python
async def test_workspace_editor_forwards_the_acting_user(monkeypatch):
    class FakeClient:
        instances: list["FakeClient"] = []

        def __init__(self, **kwargs):
            self.requests = []
            FakeClient.instances.append(self)

        async def request_json(self, method, path, *, json_body=None, params=None, headers=None):
            self.requests.append((method, path, params, headers))
            return 200, {"document_server_url": "https://office.example", "config": {"document": {"key": "k"}}}

        async def aclose(self):
            pass

    async def fake_get_agent(_session, agent_id):
        return _agent()

    class FakeUser:
        id = "ops-user-7"
        username = SUBJECT
        name = "Ana Popescu"

    async def fake_get_user_by_username(_session, username):
        assert username == SUBJECT
        return FakeUser()

    monkeypatch.setattr(sessions_route.agent_repo, "get_agent", fake_get_agent)
    monkeypatch.setattr(sessions_route.user_repo, "get_user_by_username", fake_get_user_by_username)
    monkeypatch.setattr(sessions_route, "SurogatesApiClient", FakeClient)

    class FakeSurogates:
        async def ensure_org(self, org_id, name):
            pass

    result = await sessions_route.get_live_workspace_editor(
        "22222222-2222-2222-2222-222222222222",
        request=_request(surogates=FakeSurogates()),
        path="docs/Plan.docx",
        theme="dark",
        view=False,
        scope=_session_scope(),
        ops_session=object(),
    )
    assert result["config"]["document"]["key"] == "k"
    assert FakeClient.instances[0].requests == [(
        "GET",
        "/v1/api/sessions/22222222-2222-2222-2222-222222222222/workspace/editor",
        {"path": "docs/Plan.docx", "theme": "dark"},
        {"X-Surogate-Acting-User-Id": "ops-user-7", "X-Surogate-Acting-User-Name": "Ana%20Popescu"},
    )]
```

(`SUBJECT`, `_agent`, `_request`, `_session_scope` are the module's existing helpers; `SUBJECT` is the scope's `current_subject`.)

- [ ] **Step 2: Run to see it fail**

Run: `/work/surogate-ops/.venv/bin/python -m pytest tests/test_sessions_live_proxy.py -q -k editor`
Expected: FAIL, `AttributeError: module 'routes.sessions' has no attribute 'get_live_workspace_editor'`.

- [ ] **Step 3: Let the client send extra headers**

In `surogate_ops/core/synthetic/surogates_api.py`, add `headers: dict[str, str] | None = None` to `_request(...)` and `request_json(...)`; in `_request`, after building `headers = {"Authorization": f"Bearer {token}"}` (rename that local to `request_headers` if `headers` is now the parameter), merge the extra ones: `request_headers.update(headers or {})`; `request_json` passes `headers=headers` through.

- [ ] **Step 4: Forward headers and add the route**

In `surogate_ops/server/routes/sessions.py`, add `headers: dict[str, str] | None = None` to `_forward_json`, and in its body add `if headers is not None: request_kwargs["headers"] = headers` beside the `files` line (existing fakes without a `headers` parameter keep working). Add `from urllib.parse import quote` to the imports, and after `get_live_workspace_file`:

```python
@router.get("/{session_id}/workspace/editor")
async def get_live_workspace_editor(
    session_id: str,
    request: Request,
    path: str = Query(..., description="Relative path within the workspace"),
    theme: str = Query("light", pattern="^(light|dark)$"),
    view: bool = Query(False),
    scope: SessionScope = Depends(resolve_session_scope),
    ops_session: AsyncSession = Depends(get_session),
):
    """An office document's editor config, for the person asking."""
    user = await user_repo.get_user_by_username(ops_session, scope.current_subject)
    if user is None:
        raise HTTPException(status_code=401, detail="Unknown user.")
    client = await _build_live_agent_client(
        scope.agent_id,
        request,
        ops_session,
        **_scope_service_account_kwargs(scope),
    )
    params: dict[str, Any] = {"path": path, "theme": theme}
    if view:
        params["view"] = "true"
    return await _forward_json(
        client,
        "GET",
        f"{_LIVE_API_PREFIX}/{session_id}/workspace/editor",
        params=params,
        headers={
            "X-Surogate-Acting-User-Id": str(user.id),
            "X-Surogate-Acting-User-Name": quote(user.name or user.username),
        },
    )
```

In `tests/test_sessions_route_authz.py`, add `("get", "/workspace/editor", None),` to `SESSION_ROUTES` after `("get", "/workspace/tree", None),`.

- [ ] **Step 5: Run to see it pass, plus the neighbouring suites**

Run: `/work/surogate-ops/.venv/bin/python -m pytest tests/test_sessions_live_proxy.py tests/test_sessions_route_authz.py -q`
Expected: all passed.

- [ ] **Step 6: Commit**

```bash
cd /work/surogate-ops/.worktrees/office-editors-platform
git add surogate_ops/core/synthetic/surogates_api.py surogate_ops/server/routes/sessions.py tests/test_sessions_live_proxy.py tests/test_sessions_route_authz.py
git commit -m "feat(sessions): proxy the office editor route with the acting user"
```

---

### Task 14: The ops frontend opens the editor (after the SDK release)

Runs once a surogates release has published `@invergent/agent-chat-react` with `getWorkspaceEditorConfig` (releasing is Flavius's call; this task waits for it).

**Files (surogate-ops, new branch `feat/office-editors-frontend` from `main` in a worktree):**
- Modify: `frontend/package.json`, `frontend/package-lock.json` (the new SDK version)
- Modify: `frontend/src/features/work/work-agent-chat-adapter.ts`

- [ ] **Step 1: Bump the SDK**

```bash
cd /work/surogate-ops && git worktree add .worktrees/office-editors-frontend -b feat/office-editors-frontend main
cd .worktrees/office-editors-frontend/frontend
npm install @invergent/agent-chat-react@<released version> --save-exact=false
```

(`<released version>` is the tag the release publishes; read it with `npm view @invergent/agent-chat-react version`.)

- [ ] **Step 2: Implement the adapter method**

In `work-agent-chat-adapter.ts`, after `getWorkspaceFile`:

```ts
    async getWorkspaceEditorConfig(input) {
      return await request<AgentChatWorkspaceEditor>(
        scopedSessionUrl(input.sessionId, "/workspace/editor", agentId, {
          path: input.path,
          theme: input.theme,
          view: input.view ? "true" : undefined,
        }),
        "Failed to open the document",
      );
    },
```

and add `AgentChatWorkspaceEditor` to the type imports from `@invergent/agent-chat-react`.

- [ ] **Step 3: Typecheck, lint, commit**

```bash
npm run typecheck && npm run lint
cd .. && git add frontend/package.json frontend/package-lock.json frontend/src/features/work/work-agent-chat-adapter.ts
git commit -m "feat(work): open office documents in the editor"
```

---

### Task 15: Try it end to end on DEV

- [ ] **Step 1: Run the runtime from the worktree with office on**

```bash
cd /work/surogates/.worktrees/office-editors-platform
export SUROGATES_CONFIG=/work/surogates/config.dev.yaml
export SUROGATES_OFFICE_PUBLIC_URL=https://office.densemax.local
export SUROGATES_OFFICE_INTERNAL_URL=https://office.densemax.local
export SUROGATES_OFFICE_CALLBACK_BASE_URL=http://$(ip route get 172.17.31.241 | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}'):8000
export SUROGATES_OFFICE_JWT_SECRET="$(KUBECONFIG=~/.kube/k3s.yaml kubectl -n office get secret office -o jsonpath='{.data.JWT_SECRET}' | base64 -d)"
export SUROGATES_OFFICE_TOKEN_SECRET="$(openssl rand -hex 32)"
/work/surogates/.venv/bin/python -m surogates.cli.main migrate
/work/surogates/.venv/bin/python -m surogates.cli.main api &      # port 8000, bound to 0.0.0.0 per config.dev.yaml
/work/surogates/.venv/bin/python -m surogates.cli.main worker &
```

Stop the VS Code "Surogates [shared]" API and worker first if they are running (same ports). If the API binds only to 127.0.0.1, set `SUROGATES_API_HOST=0.0.0.0` so the document server can reach it.

- [ ] **Step 2: Run the web client from the worktree**

```bash
cd /work/surogates/.worktrees/office-editors-platform/web && npm run dev
```

- [ ] **Step 3: Walk through, and record each result**

1. In a plain chat, upload a `.docx`, open it from the file tree: the editor opens in the pane, in the app's theme.
2. Type a paragraph, close the pane: within a few seconds the file in the workspace has the paragraph (download it).
3. Open the same document in a second browser profile as another user of the org: both see each other's typing live.
4. With the document open, upload a different file over it: within 5 minutes (or after closing), a sibling `(edited … UTC)` copy appears, the editors reload, and the chat shows the conflict notice.
5. In a project, open a project file, edit, close; then let a thread finish a turn: the edit is still there, and the project history shows it as yours.
6. Open a `.pdf`: it opens in the editor; open a `.ods`: it opens read-only.
7. Stop the document server (`kubectl -n office scale deploy/office-docs --replicas=0`), open a document: the pane shows the error and the Download link. Scale it back to 1.

- [ ] **Step 4: Report**

Write the results into the PR descriptions (surogates `feat/office-editors-platform`, surogate-ops `feat/office-editors-platform`), including anything that failed and what was fixed.
