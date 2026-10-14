"""Every sentence the platform writes in the business's name, with its default.

Each key can be overridden in ``followup_settings.texts`` (Studio, Settings →
Messages).  The tables live next to the code that uses them; this is the one
place that lists them all, so ops can show every key.
"""

from __future__ import annotations

from functools import cache
from typing import Any

__all__ = ["all_default_texts", "wording"]


def all_default_texts() -> dict[str, str]:
    """``{key: default wording}`` for every key the runtime reads from
    ``followup_settings.texts``."""
    from surogates.api.routes._commerce_turn import LIMIT_TEXTS
    from surogates.channels import person_link
    from surogates.followup import outreach, store
    from surogates.followup.missed import DEFAULT_MISSED_TEXT
    from surogates.tools.builtin import team

    return {
        **store.TEAM_TEXTS,
        **outreach.DEFAULT_TEXTS,
        **person_link.DEFAULT_TEXTS,
        **team.ESCALATION_TEXTS,
        **LIMIT_TEXTS,
        "welcome_text": team.WELCOME_TEXT,
        "missed_alert_text": DEFAULT_MISSED_TEXT,
    }


@cache
def _defaults() -> dict[str, str]:
    return all_default_texts()


def wording(texts: Any, key: str, **values: Any) -> str:
    """The business's wording for *key* with *values* filled in, else the
    platform's default.  *texts* is the follow-up settings row or its ``texts``."""
    from surogates.followup.store import render_text

    if texts is not None and not isinstance(texts, dict):
        texts = getattr(texts, "texts", None)
    return render_text((texts or {}).get(key), _defaults()[key], **values)
