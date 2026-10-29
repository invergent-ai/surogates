"""Identifiers a rehearsal world hands out.

Every id starts with ``999``: E.164 leaves country code 999 unassigned and
Meta's object ids never start with it, so a rehearsal id can never be a real
phone number or phone-number id, and the runtime tells one apart without a
lookup.
"""

from __future__ import annotations

import secrets

__all__ = ["REHEARSAL_ID_PREFIX", "is_rehearsal_id", "new_phone_number_id", "new_wa_id"]

REHEARSAL_ID_PREFIX = "999"


def _digits(count: int) -> str:
    return "".join(secrets.choice("0123456789") for _ in range(count))


def new_phone_number_id() -> str:
    return REHEARSAL_ID_PREFIX + _digits(13)


def new_wa_id() -> str:
    return REHEARSAL_ID_PREFIX + _digits(9)


def is_rehearsal_id(value: str) -> bool:
    return str(value or "").startswith(REHEARSAL_ID_PREFIX)
