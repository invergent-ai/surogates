"""A command's full output is spilled under a name its output decides, so a resumed call writes the same file."""

from __future__ import annotations

import pytest

from surogates.tools.builtin.terminal import _spill_full_output
from surogates.tools.workspace_io import LocalWorkspaceIO

pytestmark = pytest.mark.asyncio


async def test_the_same_output_is_spilled_under_the_same_name(tmp_path):
    wio = LocalWorkspaceIO(str(tmp_path))
    first = await _spill_full_output("line\n" * 10, wio)
    again = await _spill_full_output("line\n" * 10, wio)
    other = await _spill_full_output("other\n", wio)
    assert first is not None and first == again and first != other
