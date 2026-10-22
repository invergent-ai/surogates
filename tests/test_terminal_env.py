"""Tests for ``surogates.tools.workspace_io.local`` child-env construction.

The terminal tool strips most environment variables from the subprocess
to prevent secrets in the worker's env from leaking into LLM-controlled
shell commands.  A small allowlist (``_ALWAYS_INHERIT``) covers the
variables required for basic shell + Python operation.
"""

from __future__ import annotations

import os
from unittest.mock import patch

from surogates.tools.workspace_io import local as term


class TestAlwaysInherit:
    """The allowlist must include the variables the agent's typical
    ``pip install X && python -c 'import X'`` flow depends on."""

    def test_node_module_paths_are_propagated(self):
        # The image's global npm packages (docx, pptxgenjs, ...) resolve
        # through NODE_PATH; stripped, the document skills' require('docx')
        # failed with "Cannot find module".  NPM_CONFIG_PREFIX sends
        # `npm install -g` to the user prefix the non-root sandbox can write.
        with patch.dict(os.environ, {
            "NODE_PATH": "/home/sandbox/.npm-global/lib/node_modules:/usr/lib/node_modules",
            "NPM_CONFIG_PREFIX": "/home/sandbox/.npm-global",
        }, clear=True):
            env = term._build_child_env()
        assert env["NODE_PATH"].endswith("/usr/lib/node_modules")
        assert env["NPM_CONFIG_PREFIX"] == "/home/sandbox/.npm-global"


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
