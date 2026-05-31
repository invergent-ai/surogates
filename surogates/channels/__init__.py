"""Channel adapters — the only user interface.

The Surogates platform has no CLI or TUI.  All user interaction flows
through *channels*:

* **web** — the REST API + SSE stream.  This is the primary channel and
  is always available.  The browser SPA talks directly to the FastAPI
  routes; no adapter process is needed.

* **slack**, **telegram** — messaging-platform inbound resolvers
  (:class:`surogates.channels.slack.SharedSlackInbound`,
  :class:`surogates.channels.telegram.SharedTelegramInbound`) that map
  inbound platform events to ``(org_id, agent_id)`` via the channel-
  routing cache.
"""
