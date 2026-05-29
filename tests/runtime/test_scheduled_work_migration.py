"""Source-level regression: ``scheduled_work.py`` must not read
process-wide ``settings.{agent_id,org_id}``.

Plan 1b / Task 4.  Last surogates api route to read the legacy
process-wide settings.  After this migration,
``grep -rn 'settings\\.(agent_id|org_id)' surogates/api/routes/``
returns zero matches outside docstrings.
"""

from __future__ import annotations

import re
from pathlib import Path


def test_scheduled_work_routes_do_not_read_process_wide_settings():
    src = Path("surogates/api/routes/scheduled_work.py").read_text()
    code = re.sub(r'""".*?"""', "", src, flags=re.S)
    code = re.sub(r"#.*", "", code)
    assert "settings.agent_id" not in code, (
        "scheduled_work.py still reads settings.agent_id at runtime — "
        "migrate to agent_runtime_context_dep"
    )
    assert "settings.org_id" not in code, (
        "scheduled_work.py still reads settings.org_id at runtime — "
        "migrate to agent_runtime_context_dep"
    )


def test_all_surogates_api_routes_are_clean():
    """Cross-route regression: now that every Plan 1 + 1b migration is
    in, no api route reads settings.{agent_id,org_id} at runtime."""
    routes_dir = Path("surogates/api/routes")
    offenders: list[str] = []
    for path in sorted(routes_dir.glob("*.py")):
        text = path.read_text()
        code = re.sub(r'""".*?"""', "", text, flags=re.S)
        code = re.sub(r"#.*", "", code)
        if re.search(r"settings\.(agent_id|org_id)", code):
            offenders.append(str(path))
    assert offenders == [], (
        "Plan 1 + 1b api migration regression — these route modules "
        "still read settings.{agent_id,org_id} at runtime: "
        f"{offenders}"
    )
