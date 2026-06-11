"""Note-type constants and per-type content rules."""
from __future__ import annotations

NOTE_TYPES: tuple[str, ...] = ("FACT", "FAIL", "CLAIM", "RESULT")

# Content caps (characters). RESULT is larger because it carries the
# structured ``outcome=…|evidence=…|risk=…`` payload.
MAX_CONTENT_CHARS: dict[str, int] = {
    "FACT": 200,
    "FAIL": 200,
    "CLAIM": 200,
    "RESULT": 400,
}

STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_EXPIRED = "expired"

# Render priority: lower sorts first.  RESULT > FACT > FAIL > CLAIM.
RENDER_PRIORITY: dict[str, int] = {"RESULT": 0, "FACT": 1, "FAIL": 2, "CLAIM": 3}

# Share of the render budget reserved for FAIL notes so dead ends never
# scroll out of the window (DeLM's protected-reserve rule).
FAIL_RESERVE_FRACTION = 0.35

# Rough chars-per-token used to convert token budgets to char budgets.
CHARS_PER_TOKEN = 4
