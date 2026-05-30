"""Source-level regression: ``website.py`` must not read process-wide
``settings.{agent_id,org_id}``.

Plan 1b / Task 1.  Locks the migration in regression-style so a later
edit cannot reintroduce the legacy pattern that breaks shared-runtime
routing.  References in docstrings or comments are documentation, not
live reads; the test strips both out before scanning.
"""

from __future__ import annotations

import re
from pathlib import Path


def test_website_routes_do_not_read_process_wide_settings():
    src = Path("surogates/api/routes/website.py").read_text()
    # Strip docstrings + comments out of the candidate text — references
    # in those are documentation, not live reads.
    code = re.sub(r'""".*?"""', "", src, flags=re.S)
    code = re.sub(r"#.*", "", code)
    assert "settings.agent_id" not in code, (
        "website.py still reads settings.agent_id at runtime — "
        "migrate to agent_runtime_context_dep"
    )
    assert "settings.org_id" not in code, (
        "website.py still reads settings.org_id at runtime — "
        "migrate to agent_runtime_context_dep"
    )
