"""No code, skill or doc names a sub-agent tool that is gone, nor the setting that chose between them.

The agent family (``agent``, ``send_message``, ``list_agents``,
``stop_agent``, ``monitor``) is the one way to start and follow sub-agents.
A name left behind would tell a model or a reader of a tool that does not
exist.  Design history under ``docs/superpowers/`` and ``docs/plans/`` keeps
its words.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: The old tools, and the setting; ``message_thread_id`` is Telegram's own field.
OLD = re.compile(
    r"\b(delegate_task|spawn_worker|send_worker_message|stop_worker|spawn_task|unblock_task|cancel_task|worker_block"
    r"|worker_complete|worker_context|start_thread|message_thread|stop_thread|list_threads|read_thread|resolve_thread"
    r"|propose_threads|subagent_parity)\b(?!_id)"
)
SWEPT = ("surogates", "skills", "docs", "SUROGATES.md", "README.md")
HISTORY = ("docs/superpowers/", "docs/plans/")
TEXT = {".py", ".md", ".sql", ".yaml", ".yml", ".json", ".txt", ".toml", ".j2", ".html", ".sh"}


def _files():
    for top in SWEPT:
        path = ROOT / top
        for file in [path] if path.is_file() else sorted(path.rglob("*")):
            relative = file.relative_to(ROOT).as_posix()
            if file.is_file() and file.suffix in TEXT and "__pycache__" not in relative and not relative.startswith(HISTORY):
                yield relative, file


#: What follows a name that is code: a call or a definition.
_CALLED = re.compile(r"\s*\(")


def _names(relative: str, line: str):
    """The old names *line* gives as a tool's.  In code, a function of the same name (the threads' own
    ``start_thread``, a route's ``list_threads``) that is called, defined or imported is no tool's name."""
    for match in OLD.finditer(line):
        code = relative.endswith(".py") and (
            _CALLED.match(line, match.end()) or line.lstrip().startswith(("from ", "import "))
        )
        if not code:
            yield match.group(1)


def test_nothing_names_an_old_sub_agent_tool():
    named = {
        f"{relative}:{number}: {name}"
        for relative, file in _files()
        for number, line in enumerate(file.read_text(errors="replace").splitlines(), 1)
        for name in _names(relative, line)
    }
    assert sorted(named) == []
