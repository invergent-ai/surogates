"""The names MCP tools are offered to the model under.

An MCP tool is offered as ``mcp__<server>__<tool>``.  OpenAI-compatible
engines refuse a whole request when any of its tool names does not match
``^[A-Za-z0-9_.-]{1,64}$`` (HTTP 400), so one long server and tool name
broke every model call of a session.  :func:`mcp_tool_name` keeps every
name that fits exactly as it was, and gives one that does not a
deterministic, readable shortening: the tool's own name and as much of the
server's as fits, then ``_`` and eight hex digits of a SHA-256 of the
original server and tool names.  The same tool gets the same name in every
wake, so the calls in a session's history (replayed or compacted) still
name it.

A call routes by the registry, whose handler holds the server and the
tool (in the MCP proxy, its ``tool_index``), never by parsing the name.
A check that needs a tool's server (the entitlement filter) reads it from
the tool's toolset, ``mcp-<server>`` (:func:`mcp_tool_server`): a
shortened name may hold only part of it.
"""

from __future__ import annotations

import hashlib
import re

#: What an OpenAI-compatible engine accepts as a function name.
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MAX_TOOL_NAME = 64

_PREFIX = "mcp__"
_SEPARATOR = "__"
_DIGEST_HEX = 8
#: Room for the server and tool parts in a shortened name:
#: ``mcp__<server>__<tool>_<digest>``.
_ROOM = MAX_TOOL_NAME - len(_PREFIX) - len(_SEPARATOR) - 1 - _DIGEST_HEX
#: A shortened name keeps at least this much of the server's name (less
#: only when the server's name is shorter).
_MIN_SERVER = 8

#: The toolset of an MCP server's tools: ``mcp-<server>``.
MCP_TOOLSET_PREFIX = "mcp-"


def sanitize_mcp_name_component(value: str) -> str:
    """Return an MCP name component safe for tool and prefix generation.

    Converts hyphens to underscores, and replaces any other character
    outside ``[A-Za-z0-9_]`` with ``_`` so generated tool names are
    compatible with provider validation rules.
    """
    return re.sub(r"[^A-Za-z0-9_]", "_", str(value or ""))


def mcp_tool_name(server: str, tool: str) -> str:
    """The name MCP tool *tool* of server *server* is offered under.

    ``mcp__<server>__<tool>``, both parts sanitized; shortened
    (:func:`fit_tool_name`) only when that is not a valid name.
    """
    s, t = sanitize_mcp_name_component(server), sanitize_mcp_name_component(tool)
    return fit_tool_name(f"{_PREFIX}{s}{_SEPARATOR}{t}", s, t, key=(str(server), str(tool)))


def fit_tool_name(name: str, server_part: str, tool_part: str, *, key: tuple[str, str]) -> str:
    """*name* if an engine accepts it; otherwise its shortening.

    *server_part* and *tool_part* are what *name* is made of, already in
    the name's alphabet; *key* (the original server and tool names) makes
    the digest, so two tools whose shortenings would read alike differ.
    """
    if TOOL_NAME_RE.match(name):
        return name
    tool_room = _ROOM - min(len(server_part), _MIN_SERVER)
    t = tool_part[:tool_room]
    s = server_part[: _ROOM - len(t)]
    digest = hashlib.sha256("\0".join(key).encode("utf-8")).hexdigest()[:_DIGEST_HEX]
    return f"{_PREFIX}{s}{_SEPARATOR}{t}_{digest}"


def mcp_toolset(server: str) -> str:
    """The toolset MCP server *server*'s tools are registered under."""
    return f"{MCP_TOOLSET_PREFIX}{server}"


def mcp_tool_server(name: str, toolset: str | None = None) -> str:
    """The sanitized server name of MCP tool *name* (``""`` when not one).

    From its *toolset* (``mcp-<server>``) when it has one, since a shortened
    name may hold only part of the server's name; from the name otherwise.
    """
    if toolset and toolset.startswith(MCP_TOOLSET_PREFIX) and len(toolset) > len(MCP_TOOLSET_PREFIX):
        return sanitize_mcp_name_component(toolset[len(MCP_TOOLSET_PREFIX):])
    return _parsed_server(name)


def _parsed_server(name: str) -> str:
    parts = name.split(_SEPARATOR)
    return parts[1] if len(parts) >= 3 and parts[0] == "mcp" else ""
