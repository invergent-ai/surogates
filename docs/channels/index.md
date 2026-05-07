# 6. Channels

There is no CLI. All user interaction happens through channels. A channel is an adapter that normalizes platform-specific messages into the internal API and delivers responses back.

## Available Channels

| Channel | Description |
|---|---|
| **[Web](web.md)** | Browser-based chat UI with real-time streaming, session management, and workspace browsing |
| **[Slack](slack.md)** | Socket Mode integration with DMs, @mentions, threading, file attachments, and multi-workspace support |
| **[Telegram](telegram.md)** | Bot API integration with DMs, groups, forum topics, media handling, and fallback IP transport for restricted networks |
| **[Website](website.md)** | Public-website widget channel for anonymous visitors. Configured at deploy time via `website.*` settings — publishable-key auth, configured CORS allow-list, CSRF-protected cookie session, optional per-session message cap. |
| **[API](api.md)** | Fire-and-forget programmatic channel for synthetic data pipelines and batch jobs. Service-account auth, idempotent submission, results read from database tables. |

## Session Routing

Every inbound message is routed to a session based on where it came from:

- **DMs**: One session per user per platform.
- **Group channels**: Shared session per channel (configurable per-user isolation).
- **Threads**: Inherit parent session (configurable thread isolation).

For example, a Slack DM from user `U03ABCDEF` always routes to the same session. A thread in channel `C04XYZ` gets its own session that all thread participants share.

## Response Delivery

Responses are delivered reliably using a durable outbox pattern:

1. The agent emits response events (LLM text, tool results, etc.)
2. Events are written to a durable outbox in the database
3. A Redis notification wakes up the channel adapter immediately
4. The adapter claims pending messages, formats them for the platform, and sends
5. On success, the message is marked as delivered
6. On failure, the adapter retries with exponential backoff

This ensures:
- **No lost messages**: If an adapter crashes, undelivered messages remain in the outbox for the next instance.
- **No duplicates**: Deduplication keys prevent double delivery on retry.
- **Parallel delivery**: Multiple adapter replicas can deliver messages concurrently.
