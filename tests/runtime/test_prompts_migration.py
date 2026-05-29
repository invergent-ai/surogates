"""Source-level regression: ``prompts.py`` must not read process-wide
``settings.{agent_id,org_id}``.

Plan 1b / Task 3.  Locks the migration in regression-style.  One read
of ``settings.agent_id`` lives in the ``_submit_one`` helper used by
``submit_prompt`` and ``submit_prompts_batch``; after migration the
helper takes an explicit ``agent_id`` parameter and each route resolves
it via ``agent_runtime_context_dep``.
"""

from __future__ import annotations

import re
from pathlib import Path


def test_prompts_routes_do_not_read_process_wide_settings():
    src = Path("surogates/api/routes/prompts.py").read_text()
    code = re.sub(r'""".*?"""', "", src, flags=re.S)
    code = re.sub(r"#.*", "", code)
    assert "settings.agent_id" not in code, (
        "prompts.py still reads settings.agent_id at runtime — "
        "migrate to agent_runtime_context_dep"
    )
    assert "settings.org_id" not in code, (
        "prompts.py still reads settings.org_id at runtime — "
        "migrate to agent_runtime_context_dep"
    )
