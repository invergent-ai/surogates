# Discord Channel

Connect an agent to the customer's own Discord bot so people can talk to it
in DMs and in server channels. Discord has no webhook for ordinary
messages, so this is the runtime's one **socket** channel: the shared
channels service (`surogates channels`) holds a Gateway websocket per bot,
and every message it receives runs the same inbound pipeline a webhook
body does. Answers go out over Discord's REST API.

## How it works

```
Discord Gateway (wss) ──► DiscordGatewayClient (one per bot, one replica per bot)
                              │  MESSAGE_CREATE
                              ▼
                     dispatcher.ingest_from_socket → channel_routing (ops) → (org, agent, config)
                              ▼
                     shared inbound pipeline → session → worker
                              ▼
                     delivery_outbox → POST /channels/{id}/messages (2 000-char chunks)
```

- **Routing**: each bot maps to one agent via a `channel_routing` row
  (kind `discord`, identifier = the bot's **application id**), written by
  Studio's Channels page in `surogate-ops`.
- **Credentials**: one, the `bot_token`, in the per-tenant vault
  (`vault://discord_bot_token_<application id>`).
- **Connection**: the Gateway supervisor lists the active routings on
  boot, every 10 s, and on `channel_routing_changed:discord:<app id>`. For
  each bot it takes the Redis lock `discord-gateway:<app id>`, so exactly
  one replica holds that bot's connection; the lock is renewed every pass
  and expires 30 s after a replica dies, when another takes the bot over.
  A changed routing restarts its connection, so a new token or intent
  setting takes effect at once.

## Setup

1. Open the [Discord Developer Portal](https://discord.com/developers/applications),
   **New Application**, name it (the name is what people see).
2. **Bot** → **Reset Token**, copy the token. Leave **Public Bot** on if
   people outside your team should be able to add it to their servers.
3. Optional: under **Privileged Gateway Intents**, turn on **Message
   Content Intent**. Without it the bot sees the text of DMs and of server
   messages that @mention it or reply to it, and nothing else, which is
   all a mention-gated agent needs. Turn it on (and set `message_content`
   on the channel) only if the agent should read every message in its
   free-response channels.
4. In Studio, open the agent → **Channels** → **Discord**, paste the token
   and save. Ops reads `GET /applications/@me` for the application id and
   `GET /users/@me` for the bot's name.
5. Add the bot to a server with this link, putting your application id in:

   ```
   https://discord.com/oauth2/authorize?client_id=<APP_ID>&scope=bot%20applications.commands&permissions=274878024768
   ```

   `274878024768` is View Channels (1 << 10) + Send Messages (1 << 11) +
   Send Messages in Threads (1 << 38) + Read Message History (1 << 16) +
   Attach Files (1 << 15) + Embed Links (1 << 14) + Add Reactions (1 << 6).

## Behaviour

- **DMs** always reach the agent. One session per person.
- **Server channels**: with `require_mention` (default on) the agent
  answers when @mentioned or when someone replies to one of its messages;
  `free_response_channels` lists channel ids it answers in without that.
  One shared session per channel. A thread is a channel of its own, so it
  gets its own session.
- The bot's own messages are dropped; other bots' only pass with
  `allow_bots` (`none` by default, `mentions` or `all`).
- **Answers** in a server quote the message they answer
  (`message_reference`); DMs are plain. Answers never ping anyone
  (`allowed_mentions: {"parse": []}`). Long answers are split at 2 000
  characters.
- **Progress**: text the agent writes beside a tool call is edited into
  one message in place; "is typing…" is kept up every 8 s while the turn
  runs.
- **Files**: attachments people send are downloaded into the session
  workspace; files the agent sends (`MEDIA:` markers) are uploaded as
  attachments.
- **Writing first** (programs, follow-ups): the bot opens a DM with
  `POST /users/@me/channels`. Discord only lets it DM people it shares a
  server with, so the platform writes first only to people who have
  written to the bot before.
- Questions (`ask_user_question`) are asked as text and answered by
  replying; there are no buttons yet.

## Channel config (`channel_routing.config`)

| key | default | meaning |
|---|---|---|
| `require_mention` | on | in servers, answer only when mentioned or replied to |
| `free_response_channels` | `[]` | channel ids answered without a mention |
| `allow_bots` | `none` | `none`, `mentions` or `all` other bots |
| `message_content` | off | identify with the privileged Message Content intent |
| `multi_session` | on | off folds every thread of a channel into one conversation |
| `identity_policy` | `shadow` | `linked` asks unknown people to link their account, in a DM |

## Ops notes

- Enable with `SUROGATES_CHANNELS_DISCORD_ENABLED=true` on the channels
  deployment. `SUROGATES_DISCORD_API_BASE` (default
  `https://discord.com/api/v10`) points it at `scripts/fake_discord.py` for
  local runs.
- Discord allows 1 000 IDENTIFYs a day per bot. Reconnects RESUME the
  session whenever it is alive and back off exponentially when they fail.
- Close codes 4004 (bad token) and 4010–4014 (shards, API version,
  intents: 4014 is the Message Content intent asked for but not enabled in
  the portal) stop that bot's connection for good, logged as
  `routing <app id> marked errored`. It is retried only after its routing
  changes (reconnect the channel in Studio).
- Permanent delivery errors (not retried): 10003 unknown channel, 50001
  missing access, 50007 cannot DM the user, 50013 missing permissions. A
  429 is waited out once for its `retry_after`, then retried by the
  outbox.
- Not yet: slash commands, buttons, sharding (a bot in more than 2 500
  servers).
