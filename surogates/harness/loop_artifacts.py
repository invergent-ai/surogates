"""Artifact promotion and workspace-scan helpers for the harness loop."""

from __future__ import annotations

import json
import posixpath
import re
import shlex
from datetime import datetime, timezone
from typing import Any


_TERMINAL_TOKEN_RE = re.compile(
    r'''(?:[^\s;&|<>()'"\\]+|'[^']*'|"(?:\\.|[^"\\])*"|\\[^\n])+'''
    r"|[;&|<>]+|[()\n]"
)
_PYTHON_COMMAND_RE = re.compile(r"(?:python|pypy)(?:\d+(?:\.\d+)*)?")
_SHELL_COMMANDS = {"sh", "bash", "dash", "zsh", "ksh"}
_SHELL_CONTROL_COMMANDS = {
    "cd", "pushd", "popd", "if", "then", "else", "elif", "fi", "for",
    "while", "until", "do", "done", "case", "esac", "select", "function",
    "{", "}", "source", ".", "eval", "command", "builtin", "exec",
    "exit", "return", "break", "continue",
}


def _terminal_executes_file(command: str, path: str) -> bool:
    """Recognize a workspace file in an executable or interpreter-script slot.

    This is deliberately a small, conservative shell recognizer: references in
    inspection commands, output arguments, redirections, or inline code do not
    make a file a generator script. Unsupported syntax returns False so a
    possible deliverable stays visible. No shell command is evaluated.
    """
    if not command or not path or len(command) > 65536 or "$" in command or "`" in command:
        return False

    # Keep quotes until after splitting commands: a quoted ";" or "&&" is an
    # argument, not a shell operator. shlex then decodes each individual word.
    groups: list[list[str]] = [[]]
    offset = 0
    token_count = 0
    while offset < len(command):
        if command[offset] in " \t\r":
            offset += 1
            continue
        if command[offset] == "#":
            newline = command.find("\n", offset)
            offset = newline if newline >= 0 else len(command)
            continue
        match = _TERMINAL_TOKEN_RE.match(command, offset)
        if match is None:
            return False
        raw = match.group()
        offset = match.end()
        token_count += 1
        if token_count > 4096:
            return False
        if raw in {";", "&&", "||", "|", "&", "\n"}:
            groups.append([])
        elif raw in {"(", ")"} or raw.startswith("<<"):
            return False
        else:
            groups[-1].append(raw)

    commands: list[list[str]] = []
    for group in groups:
        words: list[str] = []
        index = 0
        while index < len(group):
            raw = group[index]
            if raw in {">", ">>", "<", "<>", ">|", ">&", "<&", "&>"}:
                if index + 1 >= len(group):
                    return False
                # A numeric word before a redirect may be a file descriptor.
                # Discard it conservatively even when separated by whitespace.
                if words and words[-1].isdigit():
                    words.pop()
                index += 2
                continue
            if raw[0] in ";&|<>":
                return False
            try:
                decoded = shlex.split(raw)
            except ValueError:
                return False
            if len(decoded) != 1:
                return False
            words.append(decoded[0])
            index += 1
        while words and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*=.*", words[0], re.DOTALL):
            words.pop(0)
        # Directory changes and compound shell statements need execution state
        # that this recognizer intentionally does not attempt to reconstruct.
        if words and words[0] in _SHELL_CONTROL_COMMANDS:
            return False
        if words:
            commands.append(words)

    expected = posixpath.normpath(path)
    for words in commands:
        executable = words[0]
        if "/" in executable and posixpath.normpath(executable) == expected:
            return True
        interpreter = posixpath.basename(executable)
        is_python = _PYTHON_COMMAND_RE.fullmatch(interpreter) is not None
        is_shell = interpreter in _SHELL_COMMANDS
        if not is_python and not is_shell and interpreter not in {"node", "nodejs", "ruby", "perl"}:
            continue
        index = 1
        while index < len(words) and words[index].startswith("-"):
            option = words[index]
            if option == "--":
                index += 1
                break
            if is_python and option in {"-W", "-X"}:
                index += 2
            elif is_python and re.fullmatch(r"-[bBdEiIOPqRsSuv]+", option):
                index += 1
            elif is_shell and re.fullmatch(r"-[aefuvxC]+", option):
                index += 1
            elif is_shell and option == "-o" and index + 1 < len(words) and words[index + 1] == "pipefail":
                index += 2
            else:
                # Includes Python -c/-m and shell -c/-s: subsequent words are
                # inline code, module names, or arguments, not a script path.
                break
        if index < len(words) and not words[index].startswith("-"):
            if posixpath.normpath(words[index]) == expected:
                return True
    return False


def _coerce_modified_to_datetime(raw: Any) -> "datetime | None":
    """Normalize a storage backend's ``modified`` field to ``datetime``.

    LocalBackend returns a POSIX float (``st_mtime``); S3Backend
    returns the boto3 ``LastModified`` ``datetime`` directly. Anything
    else is treated as unparseable and yields ``None`` so the caller
    skips the entry rather than crashing.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=timezone.utc)
        return raw
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(raw, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _coerce_tool_args(raw: Any) -> dict[str, Any]:
    """Best-effort coercion of a TOOL_CALL ``arguments`` field to a dict.

    Different tool emitters store ``arguments`` either as a JSON string
    (OpenAI convention) or as a pre-parsed dict.  Anything else is
    treated as opaque and yields an empty dict so candidate-artifact
    collection can keep going.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}
_PROMOTABLE_FENCES: dict[str, tuple[str, str]] = {
    "svg": ("svg", "svg"),
    "html": ("html", "html"),
}

# Precompiled regex that matches ``` + language-tag + body + ``` .  The
# (?s) flag lets ``.`` match newlines inside the body.  Only matches
# fences starting at line-begin to avoid misfires on inline backticks.
_FENCE_RE = re.compile(
    r"(?ms)^```([a-zA-Z0-9_-]+)\s*\n(.*?)^```\s*$"
)
def _derive_artifact_name(kind: str, messages: list[dict]) -> str:
    """Pick a human-readable name for an auto-promoted artifact.

    Uses the most recent user message's first line (trimmed to a
    reasonable length) so the artifact header says something like
    "Draw a minimal SVG logo…" instead of a generic "SVG artifact".
    Falls back to a kind-based default when no user message is
    available or the extract is empty.
    """
    fallback = {
        "svg": "SVG artifact",
        "html": "HTML preview",
    }.get(kind, "Artifact")

    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content") or ""
        if not isinstance(content, str):
            continue
        first_line = content.strip().splitlines()[0] if content.strip() else ""
        # Strip surrounding quotes the frontend sometimes inherits from
        # copy-pasted prompts.
        first_line = first_line.strip(' "\'')
        if first_line:
            return first_line[:80]
        break
    return fallback
