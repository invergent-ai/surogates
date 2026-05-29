"""Source-level regression: ``auth.py`` must not read process-wide
``settings.{agent_id,org_id}``.

Plan 1b / Task 2.  Locks the migration in regression-style.  All four
reads were ``settings.org_id`` in the ``firebase_exchange`` and
``login`` handlers; after migration they go through
``agent_runtime_context_dep``.
"""

from __future__ import annotations

import re
from pathlib import Path


def test_auth_routes_do_not_read_process_wide_settings():
    src = Path("surogates/api/routes/auth.py").read_text()
    code = re.sub(r'""".*?"""', "", src, flags=re.S)
    code = re.sub(r"#.*", "", code)
    assert "settings.agent_id" not in code, (
        "auth.py still reads settings.agent_id at runtime — "
        "migrate to agent_runtime_context_dep"
    )
    assert "settings.org_id" not in code, (
        "auth.py still reads settings.org_id at runtime — "
        "migrate to agent_runtime_context_dep"
    )
