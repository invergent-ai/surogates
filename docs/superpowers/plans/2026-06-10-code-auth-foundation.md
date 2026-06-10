# `/code` Auth Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the "connect your coding plan" backend end-to-end — encrypted per-user storage of pasted Claude/Codex credentials, the REST routes the chat UI calls, and a working `/code` chat command (help/status/login/logout), with execution stubbed out.

**Architecture:** A new `surogates/coding_agents/` package holds a pure credential-bundle model + paste validator and a thin `CredentialVault` wrapper. Three tenant-authed REST routes (`/v1/coding-agents/...`) store/read/delete per-user credential bundles. A `CodeCommandMixin` on `AgentHarness` dispatches `/code` slash subcommands; the prompt-injection screen is exempted for `/code` text so coding prompts aren't 422'd.

**Tech Stack:** Python 3.12, FastAPI, async SQLAlchemy, Fernet (existing `CredentialVault`), pytest + pytest-asyncio (`asyncio_mode = "auto"`), testcontainers for integration tests.

**Spec:** `docs/superpowers/specs/2026-06-10-code-command-coding-agents-design.md` (this plan implements §5, §7.0–7.2, and the auth slice of §4).

## Progress

- [x] Task 1: Package + `CredentialBundle` model
- [x] Task 2: `validate_pasted()` paste validator
- [x] Task 3: `CodingAgentCredentials` vault wrapper
- [x] Task 4: `/code` command parser
- [x] Task 5: Rendered chat messages
- [ ] Task 6: `CodeCommandMixin` handler (in progress)
- [ ] Task 7: Credential REST routes + mount
- [ ] Task 8: Wire `/code` into the harness + injection-screen exemption
- [ ] Final verification (unit + integration + import smoke)

**Conventions in this repo (read before starting):**
- Run unit tests: `.venv/bin/python -m pytest tests/<file> -v` from `/work/surogates`.
- Integration tests spin up real Postgres/Redis via testcontainers and need Docker available: `.venv/bin/python -m pytest tests/integration/<file> -v`.
- Do **not** use `uv run` here — it reinstalls the pinned wheel over the local dev install.
- Commit messages: no `Co-Authored-By` trailer; no Plan/Task/Phase numbers in the message body.

---

### Task 1: Package + `CredentialBundle` model

**Files:**
- Create: `surogates/coding_agents/__init__.py`
- Create: `surogates/coding_agents/credentials.py`
- Test: `tests/test_coding_agents_credentials.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_coding_agents_credentials.py`:

```python
"""Unit tests for the coding-agent credential bundle (no DB)."""

from __future__ import annotations

from surogates.coding_agents.credentials import (
    PROVIDERS,
    CRED_NAME,
    CredentialBundle,
)


def test_providers_and_names():
    assert PROVIDERS == ("anthropic", "openai")
    assert CRED_NAME["anthropic"] == "code_cred:anthropic"
    assert CRED_NAME["openai"] == "code_cred:openai"


def test_bundle_round_trip():
    bundle = CredentialBundle(
        provider="anthropic",
        auth_mode="oauth",
        token_kind="setup_token",
        oauth_token="sk-ant-oat01-abc",
    )
    restored = CredentialBundle.from_json(bundle.to_json())
    assert restored == bundle


def test_bundle_status_hides_secret():
    bundle = CredentialBundle(
        provider="openai", auth_mode="api_key", api_key="sk-secret",
    )
    status = bundle.status()
    assert status == {
        "provider": "openai",
        "connected": True,
        "auth_mode": "api_key",
        "expires_at": None,
    }
    assert "sk-secret" not in str(status)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_coding_agents_credentials.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'surogates.coding_agents'`

- [ ] **Step 3: Create the package marker**

Create `surogates/coding_agents/__init__.py`:

```python
"""External coding-agent (Claude Code, Codex) orchestration for the /code command."""
```

- [ ] **Step 4: Write the minimal implementation**

Create `surogates/coding_agents/credentials.py`:

```python
"""Per-user credential storage for external coding agents.

Capture model "A": the user runs the vendor CLI's own login on their
machine and pastes the binary-minted credential.  We validate it and
store an opaque JSON bundle in the encrypted ``CredentialVault`` — we
never run an OAuth flow and never call provider APIs ourselves.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Final

PROVIDERS: Final[tuple[str, ...]] = ("anthropic", "openai")

CRED_NAME: Final[dict[str, str]] = {
    "anthropic": "code_cred:anthropic",
    "openai": "code_cred:openai",
}


class CredentialError(ValueError):
    """A pasted credential was malformed.  The message is user-facing."""


@dataclass
class CredentialBundle:
    """The opaque value stored (encrypted) in the credentials vault."""

    provider: str
    auth_mode: str  # "oauth" | "api_key"
    token_kind: str | None = None  # "setup_token" for anthropic oauth
    oauth_token: str | None = None  # anthropic setup-token
    api_key: str | None = None  # api_key mode
    auth_json: dict | None = None  # codex ~/.codex/auth.json (parsed)
    expires_at: int | None = None  # reserved; None in v1
    version: int = 1

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "CredentialBundle":
        data = json.loads(raw)
        return cls(
            provider=data["provider"],
            auth_mode=data["auth_mode"],
            token_kind=data.get("token_kind"),
            oauth_token=data.get("oauth_token"),
            api_key=data.get("api_key"),
            auth_json=data.get("auth_json"),
            expires_at=data.get("expires_at"),
            version=data.get("version", 1),
        )

    def status(self) -> dict:
        """Connection metadata for the UI — never includes the secret."""
        return {
            "provider": self.provider,
            "connected": True,
            "auth_mode": self.auth_mode,
            "expires_at": self.expires_at,
        }
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_coding_agents_credentials.py -v`
Expected: PASS (3 passed)

- [ ] **Step 6: Commit**

```bash
git add surogates/coding_agents/__init__.py surogates/coding_agents/credentials.py tests/test_coding_agents_credentials.py
git commit -m "feat(code): add coding-agent credential bundle model"
```

---

### Task 2: `validate_pasted()` paste validator

**Files:**
- Modify: `surogates/coding_agents/credentials.py`
- Test: `tests/test_coding_agents_credentials.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_coding_agents_credentials.py`:

```python
import pytest

from surogates.coding_agents.credentials import (
    CredentialError,
    validate_pasted,
)


def test_validate_anthropic_oauth_ok():
    bundle = validate_pasted("anthropic", "oauth", "  sk-ant-oat01-xyz  ")
    assert bundle.provider == "anthropic"
    assert bundle.auth_mode == "oauth"
    assert bundle.token_kind == "setup_token"
    assert bundle.oauth_token == "sk-ant-oat01-xyz"  # trimmed


def test_validate_anthropic_oauth_rejects_api_key():
    with pytest.raises(CredentialError, match="setup-token|setup token"):
        validate_pasted("anthropic", "oauth", "sk-ant-api03-nope")


def test_validate_anthropic_api_key_ok():
    bundle = validate_pasted("anthropic", "api_key", "sk-ant-api03-abc")
    assert bundle.auth_mode == "api_key"
    assert bundle.api_key == "sk-ant-api03-abc"


def test_validate_openai_oauth_ok():
    auth_json = '{"auth_mode":"chatgpt","tokens":{"access_token":"tok","refresh_token":"r","account_id":"a"}}'
    bundle = validate_pasted("openai", "oauth", auth_json)
    assert bundle.provider == "openai"
    assert bundle.auth_mode == "oauth"
    assert bundle.auth_json["tokens"]["access_token"] == "tok"


def test_validate_openai_oauth_rejects_non_json():
    with pytest.raises(CredentialError, match="auth.json"):
        validate_pasted("openai", "oauth", "not-json")


def test_validate_openai_oauth_rejects_missing_access_token():
    with pytest.raises(CredentialError, match="access_token"):
        validate_pasted("openai", "oauth", '{"tokens":{}}')


def test_validate_openai_api_key_ok():
    bundle = validate_pasted("openai", "api_key", "sk-proj-abc")
    assert bundle.api_key == "sk-proj-abc"


def test_validate_openai_api_key_rejects_anthropic_key():
    with pytest.raises(CredentialError):
        validate_pasted("openai", "api_key", "sk-ant-api03-abc")


def test_validate_rejects_unknown_provider_and_mode():
    with pytest.raises(CredentialError, match="provider"):
        validate_pasted("google", "oauth", "x")
    with pytest.raises(CredentialError, match="mode"):
        validate_pasted("openai", "magic", "x")


def test_validate_rejects_empty():
    with pytest.raises(CredentialError, match="empty"):
        validate_pasted("anthropic", "oauth", "   ")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_coding_agents_credentials.py -k validate -v`
Expected: FAIL with `ImportError: cannot import name 'validate_pasted'`

- [ ] **Step 3: Write the implementation**

Append to `surogates/coding_agents/credentials.py`:

```python
def validate_pasted(provider: str, mode: str, value: str) -> CredentialBundle:
    """Validate a pasted credential and build a bundle, or raise CredentialError."""
    if provider not in PROVIDERS:
        raise CredentialError(
            f"Unknown provider {provider!r}; expected one of {', '.join(PROVIDERS)}."
        )
    if mode not in ("oauth", "api_key"):
        raise CredentialError(f"Unknown mode {mode!r}; expected 'oauth' or 'api_key'.")

    value = value.strip()
    if not value:
        raise CredentialError("Credential value is empty.")

    if provider == "anthropic":
        if mode == "oauth":
            if not value.startswith("sk-ant-oat"):
                raise CredentialError(
                    "That does not look like a Claude setup-token. Run "
                    "`claude setup-token` and paste the value starting with "
                    "'sk-ant-oat'."
                )
            return CredentialBundle(
                provider="anthropic",
                auth_mode="oauth",
                token_kind="setup_token",
                oauth_token=value,
            )
        if not value.startswith("sk-ant-api"):
            raise CredentialError("Anthropic API keys start with 'sk-ant-api'.")
        return CredentialBundle(
            provider="anthropic", auth_mode="api_key", api_key=value,
        )

    # provider == "openai"
    if mode == "oauth":
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise CredentialError(
                "Paste the full contents of ~/.codex/auth.json (valid JSON)."
            ) from exc
        if not isinstance(parsed, dict):
            raise CredentialError("auth.json must be a JSON object.")
        token = (parsed.get("tokens") or {}).get("access_token")
        if not token or not isinstance(token, str):
            raise CredentialError(
                "auth.json is missing tokens.access_token. Run `codex login` "
                "first, then paste ~/.codex/auth.json."
            )
        return CredentialBundle(
            provider="openai", auth_mode="oauth", auth_json=parsed,
        )

    # openai api_key
    if not value.startswith("sk-") or value.startswith("sk-ant-"):
        raise CredentialError("OpenAI API keys start with 'sk-'.")
    return CredentialBundle(provider="openai", auth_mode="api_key", api_key=value)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_coding_agents_credentials.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add surogates/coding_agents/credentials.py tests/test_coding_agents_credentials.py
git commit -m "feat(code): validate pasted Claude/Codex credentials"
```

---

### Task 3: `CodingAgentCredentials` vault wrapper

**Files:**
- Modify: `surogates/coding_agents/credentials.py`
- Test: `tests/integration/test_coding_agents_credentials.py`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_coding_agents_credentials.py`:

```python
"""Integration tests for CodingAgentCredentials against real PostgreSQL."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from surogates.coding_agents.credentials import (
    CodingAgentCredentials,
    CredentialBundle,
)
from surogates.tenant.credentials import CredentialVault

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture
def creds(session_factory) -> CodingAgentCredentials:
    vault = CredentialVault(session_factory, Fernet.generate_key())
    return CodingAgentCredentials(vault)


async def test_store_load_status(creds, session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)

    bundle = CredentialBundle(
        provider="anthropic", auth_mode="oauth",
        token_kind="setup_token", oauth_token="sk-ant-oat01-abc",
    )
    await creds.store(org_id=org_id, user_id=user_id, bundle=bundle)

    loaded = await creds.load(org_id=org_id, user_id=user_id, provider="anthropic")
    assert loaded == bundle

    statuses = await creds.statuses(org_id=org_id, user_id=user_id)
    by_provider = {s["provider"]: s for s in statuses}
    assert by_provider["anthropic"]["connected"] is True
    assert by_provider["openai"]["connected"] is False


async def test_no_org_fallback(creds, session_factory):
    """A user with no credential must NOT see an org-scoped one."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)

    # Store an org-scoped credential (user_id=None) directly via the vault.
    await creds._vault.store(
        org_id, "code_cred:anthropic",
        CredentialBundle(provider="anthropic", auth_mode="api_key",
                         api_key="sk-ant-api03-org").to_json(),
    )

    loaded = await creds.load(org_id=org_id, user_id=user_id, provider="anthropic")
    assert loaded is None  # never falls back to the org row


async def test_delete(creds, session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)

    await creds.store(
        org_id=org_id, user_id=user_id,
        bundle=CredentialBundle(provider="openai", auth_mode="api_key",
                                api_key="sk-proj-abc"),
    )
    assert await creds.delete(org_id=org_id, user_id=user_id, provider="openai") is True
    assert await creds.load(org_id=org_id, user_id=user_id, provider="openai") is None
    assert await creds.delete(org_id=org_id, user_id=user_id, provider="openai") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/integration/test_coding_agents_credentials.py -v`
Expected: FAIL with `ImportError: cannot import name 'CodingAgentCredentials'`

- [ ] **Step 3: Write the implementation**

Append to `surogates/coding_agents/credentials.py`:

```python
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from surogates.tenant.credentials import CredentialVault


class CodingAgentCredentials:
    """Per-user coding-agent credential storage over the encrypted vault.

    All reads pass the explicit ``user_id`` — there is deliberately **no**
    org fallback, so one user's missing credential never resolves to an
    org-scoped row (which would bill another principal's plan).
    """

    def __init__(self, vault: "CredentialVault") -> None:
        self._vault = vault

    async def store(
        self, *, org_id: UUID, user_id: UUID, bundle: CredentialBundle,
    ) -> None:
        await self._vault.store(
            org_id, CRED_NAME[bundle.provider], bundle.to_json(), user_id=user_id,
        )

    async def load(
        self, *, org_id: UUID, user_id: UUID, provider: str,
    ) -> CredentialBundle | None:
        raw = await self._vault.retrieve(
            org_id, CRED_NAME[provider], user_id=user_id,
        )
        return CredentialBundle.from_json(raw) if raw else None

    async def delete(
        self, *, org_id: UUID, user_id: UUID, provider: str,
    ) -> bool:
        return await self._vault.delete(
            org_id, CRED_NAME[provider], user_id=user_id,
        )

    async def statuses(self, *, org_id: UUID, user_id: UUID) -> list[dict]:
        out: list[dict] = []
        for provider in PROVIDERS:
            bundle = await self.load(
                org_id=org_id, user_id=user_id, provider=provider,
            )
            out.append(
                bundle.status()
                if bundle is not None
                else {
                    "provider": provider,
                    "connected": False,
                    "auth_mode": None,
                    "expires_at": None,
                }
            )
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/integration/test_coding_agents_credentials.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add surogates/coding_agents/credentials.py tests/integration/test_coding_agents_credentials.py
git commit -m "feat(code): per-user coding-agent credential vault wrapper"
```

---

### Task 4: `/code` command parser

**Files:**
- Create: `surogates/coding_agents/command.py`
- Test: `tests/test_coding_agents_command.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_coding_agents_command.py`:

```python
"""Unit tests for the /code command parser."""

from __future__ import annotations

from surogates.coding_agents.command import (
    is_code_command,
    parse_code_command,
)


def test_is_code_command():
    assert is_code_command("/code") is True
    assert is_code_command("  /code claude hi  ") is True
    assert is_code_command("/codex hi") is False  # not /code
    assert is_code_command("hello") is False


def test_bare_and_help():
    assert parse_code_command("/code").action == "help"
    assert parse_code_command("/code help").action == "help"
    assert parse_code_command("not a command") is None


def test_status():
    assert parse_code_command("/code status").action == "status"


def test_login_logout():
    login = parse_code_command("/code login claude")
    assert login.action == "login"
    assert login.provider == "anthropic"
    assert login.agent == "claude"

    logout = parse_code_command("/code logout codex")
    assert logout.action == "logout"
    assert logout.provider == "openai"

    bad = parse_code_command("/code login")
    assert bad.action == "login"
    assert bad.error is not None


def test_run_with_quoted_prompt_and_flags():
    cmd = parse_code_command('/code claude "fix the build" --model opus --effort high')
    assert cmd.action == "run"
    assert cmd.agent == "claude"
    assert cmd.provider == "anthropic"
    assert cmd.prompt == "fix the build"
    assert cmd.flags == {"model": "opus", "effort": "high"}


def test_run_requires_prompt():
    cmd = parse_code_command("/code codex")
    assert cmd.action == "run"
    assert cmd.error is not None


def test_unknown_subcommand_is_help_with_error():
    cmd = parse_code_command("/code wat")
    assert cmd.action == "help"
    assert cmd.error is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_coding_agents_command.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'surogates.coding_agents.command'`

- [ ] **Step 3: Write the implementation**

Create `surogates/coding_agents/command.py`:

```python
"""Parse ``/code ...`` chat commands.

This is the single source of truth for what counts as a /code command,
reused by the harness dispatcher AND the API-layer injection-screen
exemption so the two never disagree (spec §11 risk).
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Final

AGENT_TO_PROVIDER: Final[dict[str, str]] = {"claude": "anthropic", "codex": "openai"}
PROVIDER_TO_AGENT: Final[dict[str, str]] = {v: k for k, v in AGENT_TO_PROVIDER.items()}

# login/logout accept either the agent name or the provider name.
_PROVIDER_ALIASES: Final[dict[str, str]] = {
    "claude": "anthropic",
    "codex": "openai",
    "anthropic": "anthropic",
    "openai": "openai",
}

_VALUE_FLAGS: Final[frozenset[str]] = frozenset({"--model", "--effort", "--allow"})
_CODE_RE: Final = re.compile(r"^/code(?:\s+(.*))?$", re.DOTALL)


@dataclass
class CodeCommand:
    action: str  # "help" | "status" | "login" | "logout" | "run"
    agent: str | None = None  # "claude" | "codex"
    provider: str | None = None  # "anthropic" | "openai"
    prompt: str | None = None
    flags: dict[str, str] = field(default_factory=dict)
    error: str | None = None  # user-facing usage error, if any


def is_code_command(text: str) -> bool:
    return _CODE_RE.match(text.strip()) is not None


def _split_prompt_and_flags(rest: str) -> tuple[str, dict[str, str]]:
    try:
        tokens = shlex.split(rest)
    except ValueError:
        tokens = rest.split()
    flags: dict[str, str] = {}
    words: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in _VALUE_FLAGS and i + 1 < len(tokens):
            flags[token.lstrip("-")] = tokens[i + 1]
            i += 2
            continue
        words.append(token)
        i += 1
    return " ".join(words).strip(), flags


def parse_code_command(text: str) -> CodeCommand | None:
    """Return a CodeCommand, or None if *text* is not a /code command."""
    match = _CODE_RE.match(text.strip())
    if match is None:
        return None
    rest = (match.group(1) or "").strip()

    if not rest or rest == "help":
        return CodeCommand(action="help")

    parts = rest.split()
    head = parts[0]

    if head == "status":
        return CodeCommand(action="status")

    if head in ("login", "logout"):
        if len(parts) < 2:
            return CodeCommand(
                action=head, error=f"Usage: /code {head} <claude|codex>",
            )
        provider = _PROVIDER_ALIASES.get(parts[1])
        if provider is None:
            return CodeCommand(
                action=head,
                error=f"Unknown provider {parts[1]!r}; expected claude or codex.",
            )
        return CodeCommand(
            action=head, provider=provider, agent=PROVIDER_TO_AGENT[provider],
        )

    if head in AGENT_TO_PROVIDER:
        prompt, flags = _split_prompt_and_flags(rest[len(head):].strip())
        provider = AGENT_TO_PROVIDER[head]
        if not prompt:
            return CodeCommand(
                action="run", agent=head, provider=provider,
                error=f'Provide a prompt, e.g. /code {head} "fix the build".',
            )
        return CodeCommand(
            action="run", agent=head, provider=provider, prompt=prompt, flags=flags,
        )

    return CodeCommand(action="help", error=f"Unknown subcommand {head!r}.")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_coding_agents_command.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add surogates/coding_agents/command.py tests/test_coding_agents_command.py
git commit -m "feat(code): /code command parser"
```

---

### Task 5: Rendered chat messages

**Files:**
- Create: `surogates/coding_agents/messages.py`
- Test: `tests/test_coding_agents_messages.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_coding_agents_messages.py`:

```python
"""Unit tests for /code rendered chat messages."""

from __future__ import annotations

from surogates.coding_agents.messages import (
    render_connect_first,
    render_help,
    render_login_instructions,
    render_status,
)


def test_render_help_lists_subcommands():
    text = render_help()
    for token in ("/code claude", "/code codex", "/code login", "/code status"):
        assert token in text


def test_render_login_instructions_claude():
    text = render_login_instructions("claude")
    assert "claude setup-token" in text


def test_render_login_instructions_codex():
    text = render_login_instructions("codex")
    assert "codex login" in text


def test_render_status_marks_connected():
    statuses = [
        {"provider": "anthropic", "connected": True, "auth_mode": "oauth", "expires_at": None},
        {"provider": "openai", "connected": False, "auth_mode": None, "expires_at": None},
    ]
    text = render_status(statuses)
    assert "claude" in text and "codex" in text
    assert "connected" in text.lower()
    assert "not connected" in text.lower()


def test_render_connect_first():
    text = render_connect_first("claude")
    assert "/code login claude" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_coding_agents_messages.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

Create `surogates/coding_agents/messages.py`:

```python
"""Human-facing chat text for the /code command (pure string builders)."""

from __future__ import annotations

from surogates.coding_agents.command import PROVIDER_TO_AGENT

_SETUP = {
    "claude": (
        "On your machine, run `claude setup-token` (needs a Claude Pro/Max "
        "plan), then paste the token in **Settings → Coding Agents**."
    ),
    "codex": (
        "On your machine, run `codex login` (needs a ChatGPT plan), then paste "
        "the contents of `~/.codex/auth.json` in **Settings → Coding Agents**."
    ),
}


def render_help() -> str:
    return (
        "**Coding agents** — run Claude Code or Codex on your workspace using "
        "your own plan.\n\n"
        "- `/code claude \"<task>\"` — run Claude Code\n"
        "- `/code codex \"<task>\"` — run Codex\n"
        "- `/code login <claude|codex>` — connect your plan\n"
        "- `/code logout <claude|codex>` — disconnect\n"
        "- `/code status` — show what's connected\n\n"
        "Flags: `--model`, `--effort low|medium|high|xhigh`, `--allow read-only`."
    )


def render_login_instructions(agent: str) -> str:
    return _SETUP.get(agent, _SETUP["claude"])


def render_status(statuses: list[dict]) -> str:
    lines = ["**Coding agent connections**", ""]
    for status in statuses:
        agent = PROVIDER_TO_AGENT.get(status["provider"], status["provider"])
        if status.get("connected"):
            mode = status.get("auth_mode") or "?"
            lines.append(f"- **{agent}** — connected ({mode})")
        else:
            lines.append(f"- **{agent}** — not connected (`/code login {agent}`)")
    return "\n".join(lines)


def render_connect_first(agent: str) -> str:
    return (
        f"You haven't connected {agent} yet. Run `/code login {agent}` to "
        "connect your plan, then try again."
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_coding_agents_messages.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add surogates/coding_agents/messages.py tests/test_coding_agents_messages.py
git commit -m "feat(code): /code chat message rendering"
```

---

### Task 6: `CodeCommandMixin` handler

**Files:**
- Create: `surogates/harness/loop_code_commands.py`
- Test: `tests/test_code_command_mixin.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_code_command_mixin.py`:

```python
"""Unit tests for CodeCommandMixin via a fake harness (no real AgentHarness)."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.harness.loop_code_commands import CodeCommandMixin
from surogates.session.events import EventType

pytestmark = pytest.mark.asyncio(loop_scope="session")


class _FakeStore:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    async def emit_event(self, session_id, event_type, data):
        self.events.append((event_type, data))
        return len(self.events)  # real store returns a BIGSERIAL int id

    async def advance_harness_cursor(self, session_id, *, through_event_id, lease_token):
        return None


class _Harness(CodeCommandMixin):
    def __init__(self, vault=None):
        self._store = _FakeStore()
        self._tenant = SimpleNamespace(org_id=uuid4(), user_id=uuid4())
        self._credential_vault = vault


def _session():
    return SimpleNamespace(id=uuid4(), config={})


def _lease():
    return SimpleNamespace(lease_token="lease-token")


def _last_message(harness) -> str:
    event_type, data = harness._store.events[-1]
    assert event_type == EventType.LLM_RESPONSE
    return data["message"]["content"]


async def test_help_emits_usage():
    h = _Harness()
    await h._handle_code_command(_session(), "/code", _lease())
    assert "/code claude" in _last_message(h)


async def test_login_emits_instructions():
    h = _Harness()
    await h._handle_code_command(_session(), "/code login claude", _lease())
    assert "claude setup-token" in _last_message(h)


async def test_status_without_vault_explains():
    h = _Harness(vault=None)
    await h._handle_code_command(_session(), "/code status", _lease())
    assert "vault" in _last_message(h).lower()


async def test_run_is_stubbed():
    h = _Harness()
    await h._handle_code_command(_session(), '/code claude "do it"', _lease())
    assert "isn't available yet" in _last_message(h)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_code_command_mixin.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'surogates.harness.loop_code_commands'`

- [ ] **Step 3: Write the implementation**

Create `surogates/harness/loop_code_commands.py`:

```python
"""``/code`` slash-command handler mixed into AgentHarness.

v1 (auth foundation) implements help/status/login/logout and stubs the
run path; execution lands in a later plan.
"""

from __future__ import annotations

from surogates.coding_agents.command import parse_code_command
from surogates.coding_agents.credentials import CodingAgentCredentials
from surogates.coding_agents.messages import (
    render_help,
    render_login_instructions,
    render_status,
)
from surogates.session.events import EventType

_NO_VAULT = "Credential vault is not configured on this deployment."


class CodeCommandMixin:
    """Provides ``_handle_code_command``.  Expects the host to define
    ``self._store`` (SessionStore), ``self._tenant`` (TenantContext), and
    ``self._credential_vault`` (CredentialVault | None)."""

    async def _handle_code_command(self, session, content, lease) -> None:
        cmd = parse_code_command(content)
        if cmd is None:  # defensive — dispatch only calls us for /code
            return

        if cmd.action == "help":
            message = (f"{cmd.error}\n\n" if cmd.error else "") + render_help()
        elif cmd.action == "status":
            message = await self._render_code_status()
        elif cmd.action == "login":
            message = cmd.error or render_login_instructions(cmd.agent)
        elif cmd.action == "logout":
            message = cmd.error or await self._logout_code_provider(
                cmd.provider, cmd.agent,
            )
        elif cmd.action == "run":
            message = cmd.error or (
                f"Running coding agents isn't available yet. Connect now with "
                f"`/code login {cmd.agent}` — execution ships in a later release."
            )
        else:
            message = render_help()

        response_event_id = await self._store.emit_event(
            session.id,
            EventType.LLM_RESPONSE,
            {"message": {"role": "assistant", "content": message}},
        )
        await self._store.advance_harness_cursor(
            session.id,
            through_event_id=response_event_id,
            lease_token=lease.lease_token,
        )

    def _code_credentials(self) -> CodingAgentCredentials | None:
        if getattr(self, "_credential_vault", None) is None:
            return None
        return CodingAgentCredentials(self._credential_vault)

    async def _render_code_status(self) -> str:
        creds = self._code_credentials()
        if creds is None:
            return _NO_VAULT
        statuses = await creds.statuses(
            org_id=self._tenant.org_id, user_id=self._tenant.user_id,
        )
        return render_status(statuses)

    async def _logout_code_provider(self, provider: str, agent: str) -> str:
        creds = self._code_credentials()
        if creds is None:
            return _NO_VAULT
        removed = await creds.delete(
            org_id=self._tenant.org_id,
            user_id=self._tenant.user_id,
            provider=provider,
        )
        return f"Disconnected {agent}." if removed else f"{agent} was not connected."
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_code_command_mixin.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add surogates/harness/loop_code_commands.py tests/test_code_command_mixin.py
git commit -m "feat(code): /code command handler (help/status/login/logout)"
```

---

### Task 7: Credential REST routes + mount

**Files:**
- Create: `surogates/api/routes/coding_agents.py`
- Modify: `surogates/api/app.py` (import block `640-665`; include block after `687`)
- Test: `tests/integration/test_coding_agents_routes.py`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_coding_agents_routes.py`:

```python
"""Integration tests for the /v1/coding-agents routes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from surogates.api.routes import coding_agents
from surogates.runtime import agent_runtime_context_dep
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext
from surogates.tenant.credentials import CredentialVault

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture
async def client(session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)

    app = FastAPI()
    app.include_router(coding_agents.router, prefix="/v1")
    app.state.credential_vault = CredentialVault(session_factory, Fernet.generate_key())

    tenant = TenantContext(
        org_id=org_id, user_id=user_id, org_config={}, user_preferences={},
        permissions=frozenset({"read", "write"}), asset_root="/tmp",
    )
    app.dependency_overrides[get_current_tenant] = lambda: tenant
    app.dependency_overrides[agent_runtime_context_dep] = lambda: SimpleNamespace(
        org_id=str(org_id), agent_id="agent-test",
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as c:
        yield c


async def test_connections_starts_empty(client):
    resp = await client.get("/v1/coding-agents/connections")
    assert resp.status_code == 200
    by_provider = {c["provider"]: c for c in resp.json()["connections"]}
    assert by_provider["anthropic"]["connected"] is False
    assert by_provider["openai"]["connected"] is False


async def test_submit_then_connected(client):
    resp = await client.post(
        "/v1/coding-agents/anthropic/credential",
        json={"mode": "oauth", "value": "sk-ant-oat01-abc"},
    )
    assert resp.status_code == 200
    assert resp.json()["connected"] is True

    listed = await client.get("/v1/coding-agents/connections")
    by_provider = {c["provider"]: c for c in listed.json()["connections"]}
    assert by_provider["anthropic"]["connected"] is True


async def test_submit_invalid_returns_422(client):
    resp = await client.post(
        "/v1/coding-agents/anthropic/credential",
        json={"mode": "oauth", "value": "not-a-token"},
    )
    assert resp.status_code == 422


async def test_delete(client):
    await client.post(
        "/v1/coding-agents/openai/credential",
        json={"mode": "api_key", "value": "sk-proj-abc"},
    )
    resp = await client.delete("/v1/coding-agents/openai")
    assert resp.status_code == 204

    resp2 = await client.delete("/v1/coding-agents/openai")
    assert resp2.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/integration/test_coding_agents_routes.py -v`
Expected: FAIL with `ImportError: cannot import name 'coding_agents'`

- [ ] **Step 3: Write the route module**

Create `surogates/api/routes/coding_agents.py`:

```python
"""End-user routes to connect coding-agent plans (capture model A).

The chat UI submits the credential the user pasted (a `claude setup-token`,
a Codex `auth.json`, or an API key); we validate and store it user-scoped
in the encrypted vault.  Plaintext is never returned by any route.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from surogates.coding_agents.credentials import (
    PROVIDERS,
    CodingAgentCredentials,
    CredentialError,
    validate_pasted,
)
from surogates.runtime import AgentRuntimeContext, agent_runtime_context_dep
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext
from surogates.tenant.credentials import CredentialVault

router = APIRouter()


class CredentialSubmit(BaseModel):
    mode: str = Field(..., description="'oauth' or 'api_key'")
    value: str = Field(..., repr=False)


def _require_end_user(tenant: TenantContext, ctx: AgentRuntimeContext) -> UUID:
    if tenant.user_id is None:
        raise HTTPException(status_code=401, detail="end-user identity required")
    if str(tenant.org_id) != ctx.org_id:
        raise HTTPException(
            status_code=403, detail="agent does not belong to this tenant",
        )
    return tenant.user_id


def _vault(request: Request) -> CredentialVault:
    vault = getattr(request.app.state, "credential_vault", None)
    if vault is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Credential vault is not configured. Set SUROGATES_ENCRYPTION_KEY.",
        )
    return vault


@router.get("/coding-agents/connections")
async def list_connections(
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    ctx: AgentRuntimeContext = Depends(agent_runtime_context_dep),
) -> dict:
    user_id = _require_end_user(tenant, ctx)
    creds = CodingAgentCredentials(_vault(request))
    connections = await creds.statuses(org_id=tenant.org_id, user_id=user_id)
    return {"connections": connections}


@router.post("/coding-agents/{provider}/credential")
async def submit_credential(
    provider: str,
    body: CredentialSubmit,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    ctx: AgentRuntimeContext = Depends(agent_runtime_context_dep),
) -> dict:
    user_id = _require_end_user(tenant, ctx)
    try:
        bundle = validate_pasted(provider, body.mode, body.value)
    except CredentialError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    creds = CodingAgentCredentials(_vault(request))
    await creds.store(org_id=tenant.org_id, user_id=user_id, bundle=bundle)
    return {"provider": provider, "connected": True, "auth_mode": bundle.auth_mode}


@router.delete(
    "/coding-agents/{provider}", status_code=status.HTTP_204_NO_CONTENT,
)
async def disconnect(
    provider: str,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    ctx: AgentRuntimeContext = Depends(agent_runtime_context_dep),
) -> Response:
    user_id = _require_end_user(tenant, ctx)
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail=f"Unknown provider {provider!r}.")
    creds = CodingAgentCredentials(_vault(request))
    removed = await creds.delete(
        org_id=tenant.org_id, user_id=user_id, provider=provider,
    )
    if not removed:
        raise HTTPException(status_code=404, detail=f"{provider} is not connected.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
```

- [ ] **Step 4: Mount the router in `surogates/api/app.py`**

In the import block (currently lines `640-665`), add `coding_agents,` alphabetically between `browser,` and `composio,`:

```python
    from surogates.api.routes import (
        admin,
        admin_credentials,
        admin_mcp,
        admin_service_accounts,
        agents,
        artifacts,
        ask_user_question,
        auth,
        browser,
        coding_agents,
        composio,
        events,
        feedback,
        health,
        inbox,
        memory,
        missions,
        prompts,
        scheduled_work,
        sessions,
        skills,
        tools,
        transparency,
        website,
        workspace,
    )
```

Then immediately after the composio include (currently line `687`), add:

```python
    app.include_router(composio.router, prefix="/v1", tags=["composio"])
    app.include_router(coding_agents.router, prefix="/v1", tags=["coding-agents"])
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/integration/test_coding_agents_routes.py -v`
Expected: PASS (4 passed)

- [ ] **Step 6: Verify the app still imports with the router mounted**

Run: `.venv/bin/python -c "import surogates.api.app as a; print('ok')"`
Expected: prints `ok` with no ImportError.

- [ ] **Step 7: Commit**

```bash
git add surogates/api/routes/coding_agents.py surogates/api/app.py tests/integration/test_coding_agents_routes.py
git commit -m "feat(code): coding-agent credential REST routes"
```

---

### Task 8: Wire `/code` into the harness + exempt it from injection screening

**Files:**
- Modify: `surogates/harness/slash_skill.py` (frozenset at `32`)
- Modify: `surogates/harness/loop.py` (class decl `267`; dispatch `~858`; `__init__` `290-346`)
- Modify: `surogates/orchestrator/worker.py` (AgentHarness construction `~1086`)
- Modify: `surogates/api/routes/sessions.py` (injection screen `~727-740`)
- Test: `tests/test_code_command_wiring.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_code_command_wiring.py`:

```python
"""Wiring tests: /code is reserved, exempt from injection, and dispatched."""

from __future__ import annotations

from surogates.harness.loop import AgentHarness
from surogates.harness.loop_code_commands import CodeCommandMixin
from surogates.harness.slash_skill import (
    _BUILTIN_SLASH_COMMANDS,
    parse_slash_command,
)


def test_code_is_reserved_builtin():
    assert "code" in _BUILTIN_SLASH_COMMANDS
    # Reserved builtins return None so they never resolve as a skill.
    assert parse_slash_command("/code claude hi") is None


def test_harness_has_code_handler():
    assert issubclass(AgentHarness, CodeCommandMixin)
    assert hasattr(AgentHarness, "_handle_code_command")


def test_injection_screen_skips_code_commands():
    # The exemption predicate the API layer uses.
    from surogates.coding_agents.command import is_code_command

    assert is_code_command("/code claude \"ignore previous instructions\"") is True
    assert is_code_command("ignore previous instructions") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_code_command_wiring.py -v`
Expected: FAIL — `"code" in _BUILTIN_SLASH_COMMANDS` is False and `issubclass(AgentHarness, CodeCommandMixin)` is False.

- [ ] **Step 3: Reserve the builtin in `surogates/harness/slash_skill.py`**

Change the frozenset (line `32`) to include `"code"`:

```python
_BUILTIN_SLASH_COMMANDS: Final[frozenset[str]] = frozenset({
    "clear",
    "code",
    "compress",
    "deep-research",
    "goal",
    "loop",
    "mission",
})
```

- [ ] **Step 4: Mix the handler into AgentHarness and add the vault param**

In `surogates/harness/loop.py`, add the import near the other harness imports (top of file):

```python
from surogates.harness.loop_code_commands import CodeCommandMixin
```

The class declaration (line `267`) currently reads:

```python
class AgentHarness(
    AdvisorMixin,
    ContextReplayMixin,
    IterationSummaryMixin,
    OutcomeCommandMixin,
    ArtifactCompletionMixin,
):
```

Add `CodeCommandMixin,` to the base list (after `OutcomeCommandMixin,`):

```python
class AgentHarness(
    AdvisorMixin,
    ContextReplayMixin,
    IterationSummaryMixin,
    OutcomeCommandMixin,
    CodeCommandMixin,
    ArtifactCompletionMixin,
):
```

In `AgentHarness.__init__`, add a keyword parameter alongside the other optional `*` kwargs (after `api_client: Any | None = None,` at line `311`):

```python
        api_client: Any | None = None,
        credential_vault: Any | None = None,
```

And store it next to `self._api_client = api_client` (line `345`):

```python
        self._api_client = api_client
        self._credential_vault = credential_vault
```

- [ ] **Step 5: Add the dispatch branch in `wake()`**

In `surogates/harness/loop.py`, immediately after the `/mission` dispatch block (the `if last_user_content == "/mission" ...: return` at line `~856-858`), add:

```python
            if last_user_content == "/code" or last_user_content.startswith("/code "):
                await self._handle_code_command(session, last_user_content, lease)
                return
```

- [ ] **Step 6: Pass the vault from the worker**

In `surogates/orchestrator/worker.py`, in the `AgentHarness(...)` construction (line `~1086`), add the argument next to `api_client=harness_api_client,` (line `1101`). The local `credential_vault` is already in scope (constructed at line `707`):

```python
            api_client=harness_api_client,
            credential_vault=credential_vault,
```

- [ ] **Step 7: Exempt `/code` from the injection screen in `surogates/api/routes/sessions.py`**

Replace the injection-screen block (lines `~727-740`) so it skips genuine `/code` commands:

```python
    # Screen user message for prompt injection (AGT PromptInjectionDetector).
    # /code command text is exempt: it is a command to the user's own coding
    # agent (parsed by the harness, never fed to the platform LLM), and coding
    # prompts routinely trip the detector.  Attachments/filenames stay screened.
    from surogates.coding_agents.command import is_code_command

    injection_source = (
        "api_channel" if session.channel == API_CHANNEL else "web_channel"
    )
    detector = _get_injection_detector()
    if not is_code_command(body.content):
        injection_result = detector.detect(
            body.content,
            source=injection_source,
        )
        if injection_result.is_injection:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(f"Message blocked: {injection_result.explanation}"),
            )
```

(Leave the attachment-filename screening block below it unchanged — it uses both `detector` and `injection_source`, which is why `detector = _get_injection_detector()` must stay **outside** the `is_code_command` conditional. Only the body-content detection is guarded.)

- [ ] **Step 8: Run the wiring tests + the full new-module suite**

Run: `.venv/bin/python -m pytest tests/test_code_command_wiring.py -v`
Expected: PASS (3 passed)

Run the whole feature's unit suite together:

Run: `.venv/bin/python -m pytest tests/test_coding_agents_credentials.py tests/test_coding_agents_command.py tests/test_coding_agents_messages.py tests/test_code_command_mixin.py tests/test_code_command_wiring.py -v`
Expected: PASS (all)

- [ ] **Step 9: Verify imports are clean (no circulars from the new cross-imports)**

Run: `.venv/bin/python -c "import surogates.harness.loop; import surogates.api.routes.sessions; import surogates.orchestrator.worker; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 10: Commit**

```bash
git add surogates/harness/slash_skill.py surogates/harness/loop.py surogates/orchestrator/worker.py surogates/api/routes/sessions.py tests/test_code_command_wiring.py
git commit -m "feat(code): dispatch /code in the harness and exempt it from injection screening"
```

---

## Final Verification

- [ ] **Run the full new unit suite:**

Run: `.venv/bin/python -m pytest tests/test_coding_agents_credentials.py tests/test_coding_agents_command.py tests/test_coding_agents_messages.py tests/test_code_command_mixin.py tests/test_code_command_wiring.py -v`
Expected: all PASS.

- [ ] **Run the integration suite (needs Docker):**

Run: `.venv/bin/python -m pytest tests/integration/test_coding_agents_credentials.py tests/integration/test_coding_agents_routes.py -v`
Expected: all PASS.

- [ ] **Smoke the API import:**

Run: `.venv/bin/python -c "import surogates.api.app; print('ok')"`
Expected: `ok`.

## What ships after this plan

- Users can connect/disconnect Claude and Codex plans via `POST/DELETE /v1/coding-agents/...` and see status via `GET /v1/coding-agents/connections`.
- In chat, `/code`, `/code help`, `/code status`, `/code login <agent>`, `/code logout <agent>` all work; `/code claude|codex "<task>"` parses and returns a "not available yet" stub.
- Coding prompts are no longer 422'd by the injection screen.

## Deferred to later plans (out of scope here)

- **Plan 2 — Connect UI (SDK):** `CodingAgentsPanel` paste form, adapter methods (`listCodingAgentConnections`, `submitCodingAgentCredential`, `disconnectCodingAgentProvider`), composer `/code` entries gated by `codeAgentsEnabled`.
- **Plan 3 — Execution:** gated by the isolation preflight spike (spec §6.2/§11) — `agents.py`, `runner.py`, the `/code claude|codex` run path, `CODE_RUN_*` events, `CodeRunBlock`, sandbox image, deny-all-except-providers NetworkPolicy.
