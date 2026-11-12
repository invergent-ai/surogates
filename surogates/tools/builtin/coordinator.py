"""What a coordinator session is given: the tools a strict coordinator loses, and a project master's own.

A coordinator starts its sub-agents with the ``agent`` tool family
(``surogates.tools.builtin.agents``).  A strict coordinator (``/mission``,
and every project's master) is kept to coordinating: its implementation
tools are taken away (:func:`strict_coordinator_excluded`).
"""

from __future__ import annotations

import logging
from typing import Any

from surogates.devices.binding import device_of
from surogates.workstreams import is_project_master

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Tools a *strict coordinator* is NOT allowed to use.  ``/mission`` sets
# ``session.config['strict_coordinator'] = True`` so the harness strips
# this set on top of the user's ``excluded_tools``: implementation tools
# are filtered structurally, not just behaviourally, so the model cannot
# "just do the work itself" when it should start agents.
#
# Coordination tools (the ``agent`` family, ``consult_expert``,
# ``ask_user_question``, ``memory``, ``todo``, ``session_search``,
# ``skills_list``, ``skill_view``, ``skill_manage``, ``cron_*``,
# ``loop_wait``, ``loop_complete``) stay available.
COORDINATOR_IMPLEMENTATION_TOOLS: frozenset[str] = frozenset({
    # File I/O
    "read_file",
    "write_file",
    "patch",
    "list_files",
    "search_files",
    "office_read",
    "office_edit",
    # Code execution
    "terminal",
    "process",
    # Web
    "web_search",
    "web_extract",
    # Browser automation
    "browser_click",
    "browser_close",
    "browser_drag",
    "browser_evaluate",
    "browser_get_state",
    "browser_navigate",
    "browser_press_key",
    "browser_screenshot",
    "browser_scroll",
    "browser_type",
    "browser_upload_file",
    "browser_wait",
    # Image analysis
    "vision_analyze",
    # Knowledge-base access
    "kb_list_pages",
    "kb_read_page",
    "kb_search_pages",
    # Artifact materialisation (workers produce; coordinator orchestrates)
    "create_artifact",
})

# A project's master is a strict coordinator that does a simple thing in
# place: it reads the project's files and knowledge bases, makes a file or a
# small edit, and runs a quick command.  Anything more it gives out only
# through the thread tools: workers, tasks and delegation are taken away, and
# so is the coding tool, which like a waiting ``agent`` call holds the turn.  The web,
# the browser and images stay a thread's.
PROJECT_MASTER_OWN_TOOLS: frozenset[str] = frozenset({
    "read_file",
    "search_files",
    "list_files",
    "write_file",
    "patch",
    "terminal",
    "process",
    "kb_list_pages",
    "kb_read_page",
    "kb_search_pages",
})
PROJECT_MASTER_EXCLUDED_TOOLS: frozenset[str] = frozenset({
    "agent",
    "run_coding_agent",
})
# What the master of a project made on a computer is not sent besides: every
# thread of such a project works in a copy of the project's folder there, and
# only its user starts one, from its card.
PROJECT_ON_COMPUTER_EXCLUDED_TOOLS: frozenset[str] = frozenset({"start_thread"})


def strict_coordinator_excluded(config: dict[str, Any]) -> set[str] | None:
    """The tools a strict coordinator is not given, its own ``excluded_tools`` among them; None for any other session.

    ``strict_coordinator`` is the structural-enforcement flag: implementation
    tools (terminal, file I/O, web, browser, vision, KB) are stripped so the
    model can only start agents, not "fix it quickly" in-band.  ``/mission``
    and every project's master set it; other coordinators leave it off and
    keep their tools.
    """
    if not (config.get("coordinator") and config.get("strict_coordinator")):
        return None
    user_excluded = set(config.get("excluded_tools") or [])
    excluded = user_excluded | COORDINATOR_IMPLEMENTATION_TOOLS
    if is_project_master(config):
        excluded -= PROJECT_MASTER_OWN_TOOLS - user_excluded
        excluded |= PROJECT_MASTER_EXCLUDED_TOOLS
        # Where the project works is the server's stamp, never the caller's.
        if device_of(config) is not None:
            excluded |= PROJECT_ON_COMPUTER_EXCLUDED_TOOLS
    return excluded
