"""The names MCP tools are offered to the model under.

An MCP tool is offered as ``mcp__<server>__<tool>``.  OpenAI-compatible
engines refuse a whole request when any of its tool names does not match
``^[A-Za-z0-9_.-]{1,64}$`` (HTTP 400), so one long server and tool name
broke every model call of a session.  :func:`mcp_tool_name` keeps every
name that fits exactly as it was, and gives one that does not a
deterministic, readable shortening: the tool's own name and as much of the
server's as fits (at least 12 characters of it), then ``-`` and twelve hex
digits of a SHA-256 of the full name.  Sanitizing never makes a ``-``, so
no tool's own name can equal a shortened one.  The same tool gets the same
name in every wake, so the calls in a session's history (replayed or
compacted) still name it; and a rule written with the full name finds it
(:func:`configured_tool_name`).

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
_DIGEST_HEX = 12
#: Before the digest: a character sanitizing never makes, so a shortened
#: name is never any tool's own.
_DIGEST_MARK = "-"
#: Room for the server and tool parts in a shortened name:
#: ``mcp__<server>__<tool>-<digest>``.
_ROOM = MAX_TOOL_NAME - len(_PREFIX) - len(_SEPARATOR) - len(_DIGEST_MARK) - _DIGEST_HEX
#: A shortened name keeps at least this much of the server's name (less
#: only when the server's name is shorter): all of ``tool_router``, so a
#: Composio tool's name keeps its ``mcp__tool_router__`` prefix.
_MIN_SERVER = 12

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
    return fit_tool_name(s, t)


def fit_tool_name(server_part: str, tool_part: str) -> str:
    """``mcp__<server_part>__<tool_part>`` if an engine accepts it;
    otherwise its shortening.

    The parts are already in the name's alphabet.  The digest is of the
    full name, so it follows from that name alone, and two tools whose
    shortenings would read alike differ.
    """
    name = f"{_PREFIX}{server_part}{_SEPARATOR}{tool_part}"
    if TOOL_NAME_RE.match(name):
        return name
    tool_room = _ROOM - min(len(server_part), _MIN_SERVER)
    t = tool_part[:tool_room]
    s = server_part[: _ROOM - len(t)]
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:_DIGEST_HEX]
    return f"{_PREFIX}{s}{_SEPARATOR}{t}{_DIGEST_MARK}{digest}"


def configured_tool_name(name: str) -> str:
    """The name a tool rule written with *name* applies to.

    A full MCP name too long for an engine (``mcp__<server>__<tool>``, as an
    admin reads it off the server) is the shortened name the tool is
    offered under; any other name is unchanged.
    """
    name = str(name)
    if TOOL_NAME_RE.match(name) or not name.startswith(_PREFIX):
        return name
    server_part, sep, tool_part = name[len(_PREFIX):].partition(_SEPARATOR)
    if not sep:
        return name
    return fit_tool_name(sanitize_mcp_name_component(server_part), sanitize_mcp_name_component(tool_part))


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
