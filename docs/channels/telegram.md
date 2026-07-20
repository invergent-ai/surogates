# Telegram Channel

Connect an agent to Telegram so users can talk to it in DMs, groups, and
forum topics. The integration is **webhook-based**: Telegram pushes updates
to the shared channels service (`surogates channels`), which resolves the
owning agent per bot, verifies the request, and feeds the shared inbound
pipeline. There is no long-polling process and no per-agent adapter.

## How it works

```
Telegram → POST https://<channels-host>/telegram/{bot_username}
             │  verify X-Telegram-Bot-Api-Secret-Token (vault webhook_secret)
             ▼
        channel_routing lookup (ops) → (org, agent, config)
             ▼
        shared inbound pipeline → session → worker
             ▼
        delivery_outbox → Telegram sendMessage (HTML, chunked)
```

- **Routing** — each bot maps to one agent via a `channel_routing` row
  (kind `telegram`, identifier `@botusername`) managed by Studio's
  Channels form in `surogate-ops`. The channels service resolves it over
  HTTP with a 30s cache invalidated by Redis pub/sub.
- **Credentials** — `bot_token` and `webhook_secret` live in the
  per-tenant credential vault, never in env vars or config files.
- **Webhook registration** — automatic. The reconciler calls
  `setWebhook` for every active routing with a minted secret token;
  verification compares the `X-Telegram-Bot-Api-Secret-Token` header in
  constant time.

## Setup

1. Message [@BotFather](https://t.me/BotFather), `/newbot`, copy the token.
   Optional: `/setprivacy` → **Disable** so the bot sees all group
   messages (not only commands/@mentions); `/setjoingroups` → **Enable**.
2. In Studio, open the agent → **Channels** → **Telegram**, paste the bot
   token, and save. The platform calls `getMe` to validate the token and
   derive the `@username` (an invalid token deactivates the channel), then
   registers the webhook automatically.

## Identity

Two policies, chosen per channel in Studio (`identity_policy`):

- **shadow** (default) — every Telegram sender is auto-provisioned a
  shadow user on first contact. Chat membership is the authorization
  boundary (the "Mate" model).
- **linked** — only linked Surogate accounts may talk. Unknown senders
  receive a private pairing code and a link prompt; no session is created
  until they link.

## Sessions & threading

Session keying: `agent:telegram:{chat_type}:{chat_id}[:{user_id}][:{thread_id}]` —
one session per DM user; one shared session per group (or per user with
**per-user groups**); forum topics get their own sessions.

- **Reply threading** (`reply_to_mode`): `first` replies to the message
  that opened the session; `all` replies to the sender's latest message;
  `off` posts plain messages. Groups only — DMs never use reply-to.
- **Reactions** (`reactions_enabled`): the bot reacts 👀 to a message it
  accepted for processing — the "seen, working on it" signal.
- **Mention gating**: in groups, `require_mention` gates on `@botusername`;
  `mention_patterns` (CSV) adds extra trigger words (e.g. a nickname);
  `free_response_channels` lists chat ids that bypass the gate.
- `/stop` (or `/cancel`) interrupts the running turn out-of-band.

## Media

- **Inbound**: photos, documents, voice, audio, video, and video notes
  are downloaded via `getFile` (20 MB cap, 10 files per message) and
  ingested into the session workspace; captions become the message text.
- **Outbound**: replies are rendered from markdown to Telegram HTML
  (bold/italic/code/pre/links; plain-text retry if Telegram rejects the
  parse) and split at natural boundaries to fit the 4096-char limit.

## Interactive input (`ask_user_question`)

When the agent asks a question mid-run:

- A single choice-question renders as an **inline keyboard** — tapping a
  button resolves the durable pending record, edits the prompt message to
  show the recorded answer, and wakes the waiting tool.
- Free-text (or multi-question) prompts are answered by simply replying;
  the pipeline matches choice labels case-insensitively and records
  anything else as a free-form answer, acking with "Got it".

Button callbacks are bound to the session's chat — a forged
`callback_data` from another chat is acked and ignored.

## Ops notes

- Enable the platform on the channels deployment via the runtime config
  (`channels.telegram.enabled: true`); routing rows control which bots are
  live.
- Delivery is a durable outbox: transient failures retry every 30s and
  dead-letter after 30 minutes or on permanent errors.
- Unknown bot usernames are fast-acked with 200 and no side effects, so
  the webhook endpoint is not an enumeration oracle.
