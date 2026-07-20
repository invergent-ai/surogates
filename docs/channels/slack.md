# Slack Channel

Connect an agent to Slack so users can interact with it via DMs, channel
@mentions, threads, slash commands, and Block Kit buttons. The integration
is **webhook-based** (Slack Events API): Slack POSTs events to the shared
channels service (`surogates channels`), which resolves the owning agent
per app, verifies the signature, and feeds the shared inbound pipeline.
Socket Mode is not used.

## How it works

```
Slack → POST https://<channels-host>/slack/{app_id}            (events)
        POST https://<channels-host>/slack/{app_id}/interact   (buttons/modals)
        POST https://<channels-host>/slack/{app_id}/commands   (slash commands)
             │  verify v0 HMAC signature (vault signing_secret, ±5min replay window)
             ▼
        channel_routing lookup (ops) → (org, agent, config)
             ▼
        shared inbound pipeline → session → worker
             ▼
        delivery_outbox → chat.postMessage / chat.update / files_upload_v2
```

- **Routing** — each Slack app maps to one agent via a `channel_routing`
  row (kind `slack`, identifier = App ID) managed by Studio's Channels
  form in `surogate-ops`; resolved with a 30s cache invalidated over
  Redis pub/sub.
- **Credentials** — `bot_token` (`xoxb-`) and `signing_secret` live in
  the per-tenant credential vault. There is no app-level `xapp-` token —
  that was the retired Socket Mode design.
- **Webhook registration is manual**: paste the three request URLs shown
  in Studio into the Slack app console (Event Subscriptions,
  Interactivity & Shortcuts, Slash Commands).

## Setup

1. Create an app at [api.slack.com/apps](https://api.slack.com/apps)
   (From scratch), in the target workspace.
2. **OAuth & Permissions → Bot Token Scopes**: `app_mentions:read`,
   `channels:history`, `channels:read`, `chat:write`, `files:read`,
   `files:write`, `groups:history`, `groups:read`, `im:history`,
   `im:read`, `im:write`, `mpim:history`, `reactions:read`,
   `reactions:write`, `users:read`.
3. **Event Subscriptions → bot events**: `message.channels`,
   `message.groups`, `message.im`, `message.mpim`, `app_mention`,
   `member_joined_channel` (enables history backfill on join).
4. Install the app, copy the **App ID**, **Bot Token**, and **Signing
   Secret** into Studio → agent → **Channels** → **Slack**, and save.
   The platform validates the bot token against Slack (`auth.test`) at
   save time — an invalid token deactivates the channel — and shows the
   request URLs to paste back into the Slack console.

## Identity

Same two policies as every channel (`identity_policy`): **shadow**
auto-provisions a user per Slack sender (channel membership is the
authorization boundary); **linked** requires a pairing code + account
link before any session is created.

## Sessions & behavior

Session keying: `agent:slack:{chat_type}:{channel_id}[:{thread_ts}]` —
DMs get one session per user, channels share a session, threads get their
own.

- **Mention gating**: `require_mention` gates channel messages on
  @mention; threads the bot participated in stay open;
  `free_response_channels` bypasses the gate; `allow_bots`
  (`none`/`mentions`/`all`) controls other bots.
- **Progress**: the bot posts a "_Thinking…_" placeholder and edits it in
  place with intermediate narration and coding-run heartbeats; the final
  answer replaces it.
- **Backfill**: on joining a channel (or first message there) the bot
  seeds the session with recent channel history (~7 days / 200 messages /
  8k tokens). DMs are not backfilled.
- **Long replies** are split at natural boundaries to stay under Slack's
  message-size limit and posted sequentially in the same thread.
- `/stop` (or `/cancel`) interrupts the running turn out-of-band.

## Files

- **Inbound** attachments are downloaded (bot-token auth, 20 MB / 10
  files caps) and ingested into the session workspace; the agent can also
  fetch older shared files on demand.
- **Outbound**: `MEDIA:<workspace-path>` markers in a reply upload the
  referenced workspace files via `files_upload_v2` into the thread.

## Interactive input (`ask_user_question`)

Mid-run questions render as a Block Kit message with an **Answer**
button that opens a modal (choices, free-text "Other"). Submitting the
modal resolves the durable pending record, replaces the button message
with the recorded answer, and wakes the waiting tool. While a question
is pending, plain replies get a nudge toward the Answer button instead
of piling onto the blocked worker.

## Ops notes

- Enable the platform on the channels deployment via the runtime config
  (`channels.slack.enabled: true`); routing rows control which apps are
  live.
- Delivery is a durable outbox: transient failures retry every 30s and
  dead-letter after 30 minutes or on permanent errors.
- Unknown app ids are fast-acked with 200 and no side effects; the URL
  verification challenge is answered automatically.
