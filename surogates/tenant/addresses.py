"""The mail addresses of an organisation's users, and which of them mail can reach.

``users.email`` is a key as much as an address: the runtime stores stand-in addresses for users that have no mailbox, so
that the unique index on (org, lower(email)) holds. A channel user is
``<platform>-<id>@<org>.channels.surogate.local`` and a followed person is ``person-<id>@<org>.people.surogate.local``.
Both sit under :data:`STAND_IN_DOMAIN`. The runtime's agent addresses sit under ``.invalid``, which no mail reaches
(RFC 2606). :func:`deliverable` is the one place that knows this: a caller that mails a user asks it first.
"""

from __future__ import annotations

__all__ = ["STAND_IN_DOMAIN", "deliverable"]

#: The domain every stand-in address of the runtime is under.
STAND_IN_DOMAIN = "surogate.local"


def deliverable(email: str) -> bool:
    """Whether mail can reach ``email``: it is ``local@domain``, and it is no stand-in and no ``.invalid`` address.

    ``.local`` in general stays deliverable: an on-premises mail domain can be ``.local``.
    """
    local, at, domain = email.partition("@")
    if not at or not local or "@" in domain or any(c.isspace() for c in email):
        return False
    domain = domain.lower().rstrip(".")
    if not domain:
        return False
    if domain == STAND_IN_DOMAIN or domain.endswith(f".{STAND_IN_DOMAIN}"):
        return False
    return domain.rsplit(".", 1)[-1] != "invalid"
