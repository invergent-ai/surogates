"""Tests for ``surogates.tools.builtin.terminal`` child-env construction.

The terminal tool strips most environment variables from the subprocess
to prevent secrets in the worker's env from leaking into LLM-controlled
shell commands.  A small allowlist (``_ALWAYS_INHERIT``) covers the
variables required for basic shell + Python operation.
"""

from __future__ import annotations

import os
from unittest.mock import patch

from surogates.tools.builtin import terminal as term


class TestAlwaysInherit:
    """The allowlist must include the variables the agent's typical
    ``pip install X && python -c 'import X'`` flow depends on."""

    def test_pythonuserbase_is_propagated(self):
        # Pip-installed packages live under PYTHONUSERBASE (off s3fs in
        # the sandbox image) — without propagation, the child python's
        # site module looks at $HOME/.local and misses them.
        assert "PYTHONUSERBASE" in term._ALWAYS_INHERIT

    def test_path_is_propagated(self):
        # PATH carries .../home/sandbox/.local/bin entries for
        # console_scripts entry points installed by pip.
        assert "PATH" in term._ALWAYS_INHERIT


class TestBuildChildEnv:
    """``_build_child_env`` strips secrets but keeps allowlisted vars."""

    def test_pythonuserbase_propagated_when_set(self):
        with patch.dict(os.environ, {
            "PYTHONUSERBASE": "/home/sandbox/.local",
            "AWS_ACCESS_KEY_ID": "AKIA...",  # must be stripped
        }, clear=True):
            env = term._build_child_env()
        assert env.get("PYTHONUSERBASE") == "/home/sandbox/.local"
        assert "AWS_ACCESS_KEY_ID" not in env

    def test_pythonuserbase_absent_when_unset(self):
        # Defensive: when the worker doesn't set PYTHONUSERBASE we don't
        # synthesize one — the child resolves a Python default.
        clean = {k: v for k, v in os.environ.items() if k != "PYTHONUSERBASE"}
        with patch.dict(os.environ, clean, clear=True):
            env = term._build_child_env()
        assert "PYTHONUSERBASE" not in env
