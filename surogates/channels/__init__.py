"""Channel adapters — the only user interface.

The Surogates platform has no CLI or TUI.  All user interaction flows
through *channels*:

* **web** — the REST API + SSE stream.  This is the primary channel and
  is always available.  The browser SPA talks directly to the FastAPI
  routes; no adapter process is needed.

* **slack**, **telegram** — messaging-platform webhook dispatchers
  (``surogates.channels.platforms.slack``,
  ``surogates.channels.platforms.telegram``) that receive inbound
  platform events via HTTP webhooks and route them to the matching
  ``(org_id, agent_id)`` via the channel-routing cache.
"""
