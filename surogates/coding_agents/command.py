"""Parse ``/code ...`` chat commands.

This is the single source of truth for what counts as a /code command,
reused by the harness dispatcher AND the API-layer injection-screen
exemption so the two never disagree.
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
