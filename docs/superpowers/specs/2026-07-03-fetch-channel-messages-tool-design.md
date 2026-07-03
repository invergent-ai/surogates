# fetch_channel_messages — live Slack channel history tool

## Problem

A Slack-channel agent has no way to read channel messages on demand. The only
channel-facing runtime tool is `fetch_channel_file` (`toolset="channels"`), which
pulls *files*, not messages. Channel message history reaches the agent only as a
one-time backfill snapshot (`channel_backfill.py`), seeded as a synthetic user
message on the session's first turn. That snapshot:

- is labelled `[channel context - history before the agent joined]`, which frames
  it as stale/pre-join and irrelevant;
- is bounded (`max_messages=200`, `max_tokens=8000`, `max_age_days=7`,
  `max_pages=1`) and never refreshes;
- covers only messages before the agent joined — nothing posted afterward.

Observed failure (PROD agent `308504f3-74e2-41b3-8078-c17da42fd818`, session
`8c62ce25`): asked to "look at the latest messages from `<@U063C2DB7GW>` in the
surogate channel", the agent replied it had no way to browse channel history —
even though the backfill block was present in its context. Two root causes: (a)
there is genuinely no live message-reading tool, and (b) the backfill framing led
the model to ignore the history it did have.

## Goal

Give channel agents an on-demand tool to fetch recent channel messages, and
reframe the backfill snapshot so the model uses it and knows the live tool exists.

## Non-goals (YAGNI)

- DMs / MPDMs — already excluded by `fetch_channel_context` (channel-only, v1).
- Thread replies (`conversations.replies`) — single flat channel history only.
- Cursor pagination exposed to the model — one bounded page, same as backfill.
- Server-side full-text search — the model filters from the returned block.

## Design

The existing primitive `SlackPlatform.fetch_channel_context(creds, channel_id,
limits)` (`channels/platforms/slack.py`) already fetches channel meta + a bounded,
newest-first `list[RawMessage]` via `conversations.history` using the bot token.
The tool is a thin, session-scoped wrapper over it; the bot token never leaves the
server.

### Components

1. **Builtin tool** — `surogates/tools/builtin/channel_messages.py`, registered
   with `toolset="channels"`, mirroring `channel_files.py`. Parameters:
   - `limit` (int, optional, default 50, capped 200) — how many recent messages.
   - `since` (str, optional) — ISO date (`2026-07-01`) or relative (`7d`, `24h`).
   - `user` (str, optional) — Slack user id (`U063…`) or mention (`<@U063…>`);
     the mention wrapper is stripped to the bare id.

   Handler validates it has a session-scoped `api_client` (else structured error),
   then delegates to `api_client.fetch_channel_messages(limit, since, user)`.

2. **Harness API client** — `fetch_channel_messages(...)` in
   `surogates/harness/api_client.py`, alongside `fetch_channel_file`. Requires
   `self._session_id`; `POST /v1/sessions/{sid}/channel-messages` with a JSON
   body `{limit, since, user}`. Returns the same `{"success": True, **data}` /
   error-envelope shape as `fetch_channel_file`.

3. **Server route** — `POST /sessions/{session_id}/channel-messages` in
   `surogates/api/routes/channel_files.py`. Reuses the session-store lookup and
   tenant-ownership guard, and the `effective_channel_platform(session) == "slack"`
   check. Resolves `bot_token` from the credential vault via
   `resolve_channel_credentials`, derives the channel id from the session source,
   builds a `BackfillLimits` (defaults, `max_pages=1`), calls
   `platform.fetch_channel_context(...)`, then runs the pure filter/format core and
   returns `{"messages_block": <str|None>, "count": <int>, "channel": <name>}`.

4. **Filter + format core** — a pure function in `channel_backfill.py`
   (no I/O, unit-testable): given `meta`, `messages`, and `{since, user, limit}`,
   filter by `since` (drop older) and `user` (match `RawMessage.author`/id), take
   the newest `limit`, and render via the shared formatter. `RawMessage` currently
   carries a resolved display-name `author`; to filter by id the core needs the
   raw user id — extend `RawMessage`/`fetch_channel_context` to also carry the
   Slack user id (`author_id`), or filter on the id inside the platform before
   name resolution. Chosen: add `author_id` to `RawMessage` so the core stays pure
   and the block can still show display names. (Confirm during implementation that
   no other `RawMessage` construction site breaks.)

5. **Backfill reframe** — parameterize `format_context_block`'s header (currently
   the hardcoded `[channel context - history before the agent joined]` at
   `channel_backfill.py:106`) with a `header:` argument. Backfill passes a neutral
   header such as `[recent channel history — snapshot; call fetch_channel_messages
   for more or newer messages]`; the tool passes its own (e.g. `[channel messages]`
   with the applied filters noted). One formatter, two callers.

### Data flow

```
agent → fetch_channel_messages(limit, since, user)          [tool, sandbox side]
      → api_client.fetch_channel_messages → POST .../channel-messages
      → route: resolve session + creds + channel_id          [server side]
      → SlackPlatform.fetch_channel_context (conversations.history, bot token)
      → filter_and_format(meta, messages, since, user, limit) [pure core]
      → {messages_block, count, channel} → JSON to the tool → model
```

### Error handling

- No session-scoped client / no `session_id` → structured `{"success": False,
  error}` from the tool/client (no HTTP call).
- Non-Slack session → 400 (mirrors file route).
- Slack error / bot not a member / DM → `fetch_channel_context` returns `None` →
  route returns `{"messages_block": None, "count": 0}` with a clear note, not a 500.
- Empty result after filtering → `messages_block: None`, `count: 0`, note that no
  messages matched.

## Testing

- **Pure core** (`test_channel_backfill_*` sibling): since filter, user filter
  (by id and by mention form), limit cap, empty input, header parameterization.
- **Tool handler** (like `test_fetch_channel_file_tool.py`): missing api_client
  error; delegates with parsed args; `<@U…>` → bare id normalization.
- **Route** (like `test_channel_files_route.py`): happy path, non-Slack 400,
  tenant-ownership 404, `fetch_channel_context` returns None → empty block.
- **Regression**: existing backfill tests still pass after header parameterization.

## Rollout

surogates change only (base `master`). No ops-server change required — the tool
ships in the harness/runtime image. Existing channel sessions gain the tool on
next runtime deploy; the reframed backfill header applies to newly seeded blocks.
