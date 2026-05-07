"""Origin allow-list helpers for the website channel.

Browsers send the ``Origin`` header without a trailing slash and in
lowercase scheme; humans typing config files often diverge.  The
helpers here normalise both sides so a config of
``"HTTPS://Customer.com/"`` matches a browser-sent
``"https://customer.com"``.

Allow-list semantics are exact-match only — wildcards and subdomain
matching are deliberately out of scope.  Operators must enumerate
every embedding origin explicitly.  This is the same posture
``websecop``-style services adopt and matches the security-review
posture of the rest of the channel: the cookie binding, the CSRF
token, and the origin check are all conjunctive, not best-effort.
"""

from __future__ import annotations

__all__ = [
    "normalize_origin",
    "origin_allowed",
    "parse_allowed_origins",
]


def normalize_origin(origin: str) -> str:
    """Normalise *origin* to its canonical ``scheme://host[:port]`` form.

    Strips trailing slashes and lowercases the whole value.  A blank
    input returns blank — callers treat that as "no origin", which is
    rejected upstream.
    """
    return origin.strip().rstrip("/").lower()


def parse_allowed_origins(csv: str) -> tuple[str, ...]:
    """Split a comma-separated CSV of origins into a normalised tuple.

    The website settings carries ``allowed_origins`` as a CSV string
    (matches the Slack/Telegram env-var convention); this helper turns
    it into the tuple every comparison site expects.  Empty entries
    and surrounding whitespace are dropped so a trailing comma in
    config doesn't quietly admit an empty origin.
    """
    if not csv:
        return ()
    return tuple(
        normalize_origin(part)
        for part in csv.split(",")
        if part.strip()
    )


def origin_allowed(origin: str | None, allowed: tuple[str, ...]) -> bool:
    """Return True when *origin* is present in *allowed* after normalisation.

    Exact match only — wildcards and subdomain matching are
    deliberately out of scope for the public-website surface.  An
    empty *allowed* rejects every origin (a deployment that hasn't
    enumerated its embedders cannot authenticate any visitor).
    """
    if not origin:
        return False
    return normalize_origin(origin) in allowed
