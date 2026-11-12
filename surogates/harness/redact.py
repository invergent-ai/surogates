"""Secret redaction for logs and persisted harness payloads."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from typing import Any

_SENSITIVE_QUERY_PARAMS = frozenset(
    {
        "access_token",
        "refresh_token",
        "id_token",
        "token",
        "api_key",
        "apikey",
        "client_secret",
        "password",
        "auth",
        "jwt",
        "session",
        "secret",
        "key",
        "code",
        "signature",
        "x-amz-signature",
    }
)

_SECRET_ENV_NAMES = r"(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?|AUTH)"
# The secret word is a whole part of the name (``GITHUB_TOKEN``,
# ``OPENAI_API_KEY``, ``AUTH_SECRET``), not a piece of a longer word: code
# such as ``AUTHORITY_RANK = {...}`` or ``MAX_TOKENS = 4096`` is not a
# secret.  The spacing around ``=`` is kept as written.
_ENV_ASSIGN_RE = re.compile(
    rf"((?<![A-Za-z0-9_])(?:[A-Z0-9]+_)*{_SECRET_ENV_NAMES}(?:_[A-Z0-9]+)*)(?![A-Za-z0-9_])"
    r"(\s*=\s*)(['\"]?)(\S+)\3"
)
# A value that is code, not a literal: ``os.getenv("TOKEN")``,
# ``settings["api_key"]``, ``{...}`` / ``[...]`` / ``(...)``.
_CODE_VALUE_RE = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_.]*)?[(\[{]")
_JSON_KEY_NAMES = (
    r"(?:api_?[Kk]ey|token|secret|password|access_token|refresh_token|"
    r"auth_token|bearer|secret_value|raw_secret|secret_input|key_material)"
)
_JSON_FIELD_RE = re.compile(
    rf'("{_JSON_KEY_NAMES}")\s*:\s*"([^"]+)"',
    re.IGNORECASE,
)
# The token, then any closing punctuation after it ("... Bearer <jwt>)."),
# which is prose and stays as written.
_AUTH_HEADER_RE = re.compile(
    r"(Authorization:\s*Bearer\s+)(\S+?)([).,;:!?'\"]*)(?=\s|$)", re.IGNORECASE,
)
_TELEGRAM_RE = re.compile(r"(bot)?(\d{8,}):([-A-Za-z0-9_]{30,})")
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----"
)
_DB_CONNSTR_RE = re.compile(
    r"((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^:]+:)([^@]+)(@)",
    re.IGNORECASE,
)
_JWT_RE = re.compile(
    r"eyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_=-]{4,}){0,2}"
)
_URL_WITH_QUERY_RE = re.compile(
    r"(https?|wss?|ftp)://([^\s/?#]+)([^\s?#]*)\?([^\s#]+)(#\S*)?"
)
_URL_USERINFO_RE = re.compile(r"(https?|wss?|ftp)://([^/\s:@]+):([^/\s@]+)@")
_FORM_BODY_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_.-]*=[^&\s]*(?:&[A-Za-z_][A-Za-z0-9_.-]*=[^&\s]*)+$"
)
_DISCORD_MENTION_RE = re.compile(r"<@!?(\d{17,20})>")
_SIGNAL_PHONE_RE = re.compile(r"(\+[1-9]\d{6,14})(?![A-Za-z0-9])")

_PREFIX_PATTERNS = [
    r"sk-[A-Za-z0-9_-]{10,}",
    r"ghp_[A-Za-z0-9]{10,}",
    r"github_pat_[A-Za-z0-9_]{10,}",
    r"gho_[A-Za-z0-9]{10,}",
    r"ghu_[A-Za-z0-9]{10,}",
    r"ghs_[A-Za-z0-9]{10,}",
    r"ghr_[A-Za-z0-9]{10,}",
    r"xox[baprs]-[A-Za-z0-9-]{10,}",
    r"AIza[A-Za-z0-9_-]{30,}",
    r"pplx-[A-Za-z0-9]{10,}",
    r"fal_[A-Za-z0-9_-]{10,}",
    r"fc-[A-Za-z0-9]{10,}",
    r"bb_live_[A-Za-z0-9_-]{10,}",
    r"gAAAA[A-Za-z0-9_=-]{20,}",
    r"AKIA[A-Z0-9]{16}",
    r"sk_live_[A-Za-z0-9]{10,}",
    r"sk_test_[A-Za-z0-9]{10,}",
    r"rk_live_[A-Za-z0-9]{10,}",
    r"SG\.[A-Za-z0-9_-]{10,}",
    r"hf_[A-Za-z0-9]{10,}",
    r"r8_[A-Za-z0-9]{10,}",
    r"npm_[A-Za-z0-9]{10,}",
    r"pypi-[A-Za-z0-9_-]{10,}",
    r"dop_v1_[A-Za-z0-9]{10,}",
    r"doo_v1_[A-Za-z0-9]{10,}",
    r"am_[A-Za-z0-9_-]{10,}",
    r"sk_[A-Za-z0-9_]{10,}",
    r"tvly-[A-Za-z0-9]{10,}",
    r"exa_[A-Za-z0-9]{10,}",
    r"gsk_[A-Za-z0-9]{10,}",
    r"syt_[A-Za-z0-9]{10,}",
    r"retaindb_[A-Za-z0-9]{10,}",
    r"hsk-[A-Za-z0-9]{10,}",
    r"mem0_[A-Za-z0-9]{10,}",
    r"brv_[A-Za-z0-9]{10,}",
]
_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(" + "|".join(_PREFIX_PATTERNS) + r")(?![A-Za-z0-9_-])"
)


def mask_secret(
    value: str,
    *,
    head: int = 4,
    tail: int = 4,
    floor: int = 12,
    placeholder: str = "***",
    empty: str = "",
) -> str:
    """Mask a secret while preserving a small diagnostic prefix/suffix."""
    if not value:
        return empty
    if len(value) < floor:
        return placeholder
    return f"{value[:head]}...{value[-tail:]}"


# A template placeholder where a secret would go is not a secret: documents,
# code and answers name them ("Bearer <jwt>", "API_KEY=<your-key>",
# "Bearer $TOKEN", "TOKEN=${TOKEN}", "YOUR_API_KEY").  Masking one changes
# what the text says (a stored "Bearer ***" read as a markdown divider) and
# protects nothing.  The shapes are narrow on purpose: a bracketed or
# $-prefixed NAME of letters, underscores, hyphens or spaces with no digit
# (random tokens carry digits), a your- name, or pure filler.
_PLACEHOLDER_RE = re.compile(
    r"(?:"
    r"<[A-Za-z][A-Za-z _-]{0,39}>"
    r"|\{\{?\s*[A-Za-z][A-Za-z _.-]{0,39}\s*\}?\}"
    r"|\$\{[A-Za-z][A-Za-z_]{0,39}\}"
    r"|\$[A-Z][A-Z_]{0,39}"
    r"|\[[A-Za-z][A-Za-z _-]{0,39}\]"
    r"|your[_-][A-Za-z_-]{1,40}"
    r"|x{3,}|X{3,}|\*{3,}|\.{3}|\u2026"
    r")",
    re.IGNORECASE,
)
_PLACEHOLDER_CASED_RE = re.compile(r"\$[A-Z][A-Z_]{0,39}")


def _is_placeholder(value: str) -> bool:
    """*value* is a template placeholder, not a secret (see ``_PLACEHOLDER_RE``)."""
    if not _PLACEHOLDER_RE.fullmatch(value):
        return False
    # ``$NAME`` counts only in capitals (an environment variable), so a
    # "$ecret"-style password is still masked.
    if value.startswith("$") and not value.startswith("${"):
        return bool(_PLACEHOLDER_CASED_RE.fullmatch(value))
    return True


def _mask_token(token: str) -> str:
    if not token:
        return "***"
    if _is_placeholder(token):
        return token
    return mask_secret(token, head=6, tail=4, floor=18)


def _mask_env_assignment(m: re.Match[str]) -> str:
    name, eq, quote, value = m.group(1), m.group(2), m.group(3), m.group(4)
    if _CODE_VALUE_RE.match(value):
        return m.group(0)
    return f"{name}{eq}{quote}{_mask_token(value)}{quote}"


def _redact_query_string(query: str) -> str:
    parts: list[str] = []
    for pair in query.split("&"):
        if "=" not in pair:
            parts.append(pair)
            continue
        key, _, _value = pair.partition("=")
        parts.append(f"{key}=***" if key.lower() in _SENSITIVE_QUERY_PARAMS else pair)
    return "&".join(parts)


def _redact_url_query_params(text: str) -> str:
    def _sub(match: re.Match[str]) -> str:
        scheme = match.group(1)
        authority = match.group(2)
        path = match.group(3)
        query = _redact_query_string(match.group(4))
        fragment = match.group(5) or ""
        return f"{scheme}://{authority}{path}?{query}{fragment}"

    return _URL_WITH_QUERY_RE.sub(_sub, text)


def _redact_form_body(text: str) -> str:
    if not text or "\n" in text or "&" not in text:
        return text
    stripped = text.strip()
    if not _FORM_BODY_RE.match(stripped):
        return text
    return _redact_query_string(stripped)


def redact_sensitive_text(
    text: str | None,
    *,
    force: bool = True,
    code_file: bool = False,
    phones: bool = True,
) -> str | None:
    """Redact known secret shapes from arbitrary text.

    Redaction is force-on by default because this function is used at
    persistence and stderr boundaries where tenant secrets must not leak.

    *phones* masks phone numbers too.  A phone number is personal data,
    not a secret: logs mask it (the default), but the session's events --
    the conversation the user reads back and the model's context is rebuilt
    from -- keep it as written (``phones=False``), or a number the user or a
    tool gave would reach the model, its tool calls and its answer as
    ``+919****3210``.
    """
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return text
    if not force:
        return text

    text = _PREFIX_RE.sub(lambda m: _mask_token(m.group(1)), text)

    if not code_file:
        text = _ENV_ASSIGN_RE.sub(_mask_env_assignment, text)
        text = _JSON_FIELD_RE.sub(
            lambda m: f'{m.group(1)}: "{_mask_token(m.group(2))}"',
            text,
        )

    text = _AUTH_HEADER_RE.sub(lambda m: m.group(1) + _mask_token(m.group(2)) + m.group(3), text)
    text = _TELEGRAM_RE.sub(lambda m: f"{m.group(1) or ''}{m.group(2)}:***", text)
    text = _PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", text)
    text = _DB_CONNSTR_RE.sub(lambda m: f"{m.group(1)}***{m.group(3)}", text)
    text = _JWT_RE.sub(lambda m: _mask_token(m.group(0)), text)
    text = _URL_USERINFO_RE.sub(lambda m: f"{m.group(1)}://{m.group(2)}:***@", text)
    text = _redact_url_query_params(text)
    text = _redact_form_body(text)
    text = _DISCORD_MENTION_RE.sub(
        lambda m: f"<@{'!' if '!' in m.group(0) else ''}***>",
        text,
    )
    if phones:
        text = _SIGNAL_PHONE_RE.sub(
            lambda m: m.group(1)[:4] + "****" + m.group(1)[-4:],
            text,
        )
    return text


# Tools whose ``text`` argument is what the agent typed into a page.  Which
# field it went into is known only to the live browser, and a password typed
# into a plain text box is as secret as one typed into a password field, so
# every copy that is stored or shown carries the length, never the text.
TYPED_TEXT_TOOLS: frozenset[str] = frozenset({"browser_type"})
TYPED_TEXT_MASK = "\u2022"
# A truncated or otherwise unparseable arguments string still has its text
# value cut out, up to the end of the string when the closing quote is missing.
_TYPED_TEXT_FIELD_RE = re.compile(r'("text"\s*:\s*")((?:[^"\\]|\\.)*)')


def mask_typed_text(text: str) -> str:
    """*text* as the stored and shown copies carry it: its length, in bullets."""
    return TYPED_TEXT_MASK * len(text)


def mask_values(text: str, values: list[str] | set[str]) -> str:
    """*text* with each of *values* masked, as written and as JSON escapes it, longest first."""
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, mask_typed_text(value))
        escaped = json.dumps(value)[1:-1]
        if escaped != value:
            text = text.replace(escaped, mask_typed_text(value))
    return text


def mask_tool_arguments(name: Any, arguments: Any) -> Any:
    """*arguments* of tool *name* with typed text masked, in the same shape (dict or JSON string)."""
    if name not in TYPED_TEXT_TOOLS:
        return arguments
    if isinstance(arguments, Mapping):
        text = arguments.get("text")
        masked = dict(arguments)
        if isinstance(text, str):
            masked["text"] = mask_typed_text(text)
        return masked
    if not isinstance(arguments, str):
        return arguments
    try:
        parsed = json.loads(arguments)
    except ValueError:
        return _TYPED_TEXT_FIELD_RE.sub(
            lambda m: m.group(1) + mask_typed_text(m.group(2)), arguments,
        )
    if not isinstance(parsed, dict):
        return arguments
    return json.dumps(mask_tool_arguments(name, parsed), ensure_ascii=False)


def mask_typed_arguments(data: Any) -> Any:
    """*data* with typed text masked in every tool call at any depth, nothing else redacted.

    Events stored before typed text was masked on write still carry it; every
    read masks it again, so no reader is shown it.
    """
    if isinstance(data, Mapping):
        masked = {key: mask_typed_arguments(value) for key, value in data.items()}
        if "arguments" in masked:
            name = data.get("name") or data.get("tool_name")
            masked["arguments"] = mask_tool_arguments(name, masked["arguments"])
        return masked
    if isinstance(data, list):
        return [mask_typed_arguments(value) for value in data]
    return data


def typed_texts(tool_calls: list[dict[str, Any]]) -> list[str]:
    """The text each typing call in *tool_calls* (OpenAI shape) typed, longest first."""
    found: set[str] = set()
    for call in tool_calls:
        fn = call.get("function") or {}
        name = fn.get("name") or call.get("name")
        if name not in TYPED_TEXT_TOOLS:
            continue
        raw = fn.get("arguments") or call.get("arguments") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = {}
        text = raw.get("text") if isinstance(raw, dict) else None
        if isinstance(text, str) and text.strip():
            found.add(text)
    return sorted(found, key=len, reverse=True)


def redact_sensitive_data(data: Any, *, phones: bool = True) -> Any:
    """Return a redacted copy of nested event/log data.

    Typed text is masked in any tool call found at any depth: a tool.call's
    ``arguments``, an llm.response's ``tool_calls[].function`` and the
    messages a compaction keeps all name the tool beside its arguments.
    *phones* as in :func:`redact_sensitive_text`.
    """
    if isinstance(data, str):
        return redact_sensitive_text(data, phones=phones)
    if isinstance(data, Mapping):
        redacted = {key: redact_sensitive_data(value, phones=phones) for key, value in data.items()}
        if "arguments" in redacted:
            name = data.get("name") or data.get("tool_name")
            redacted["arguments"] = mask_tool_arguments(name, redacted["arguments"])
        return redacted
    if isinstance(data, list):
        return [redact_sensitive_data(value, phones=phones) for value in data]
    if isinstance(data, tuple):
        return tuple(redact_sensitive_data(value, phones=phones) for value in data)
    return copy.deepcopy(data)
