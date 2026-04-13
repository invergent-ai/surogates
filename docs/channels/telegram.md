# Telegram Integration Guide

Connect your Surogates agent to Telegram so users can interact with it via DMs, groups, and forum topics.

## Prerequisites

- Surogates API server and worker running
- PostgreSQL and Redis accessible
- A Telegram account to create the bot

## 1. Create a Telegram Bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the prompts to choose a name and username
3. BotFather replies with a bot token — this is your `SUROGATES_TELEGRAM_BOT_TOKEN`

Optional BotFather settings:

- `/setprivacy` → **Disable** — allows the bot to see all group messages (not just commands and @mentions)
- `/setjoingroups` → **Enable** — allows the bot to be added to groups
- `/setcommands` → set a command list for the Telegram menu hint

## 2. Link Telegram Users

Telegram users must be linked to a Surogates user before they can interact with the agent. Currently this is done via the admin API:

```bash
curl -X POST http://localhost:8000/v1/admin/channel-identities \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "SUROGATES_USER_UUID",
    "platform": "telegram",
    "platform_user_id": "TELEGRAM_NUMERIC_USER_ID"
  }'
```

To find a user's numeric Telegram ID, have them message [@userinfobot](https://t.me/userinfobot) or check the logs when they send a message to the bot (the adapter logs unknown user IDs).

## 3. Configure and Run

### Option A: Environment Variables

```bash
export SUROGATES_TELEGRAM_BOT_TOKEN="123456789:ABCdefGHIjklMNOpqrsTUVwxyz"
SUROGATES_CONFIG=config.dev.yaml surogates channel telegram
```

### Option B: Config File

Add to your `config.yaml`:

```yaml
telegram:
  bot_token: "123456789:ABCdefGHIjklMNOpqrsTUVwxyz"
```

Then run:

```bash
SUROGATES_CONFIG=config.yaml surogates channel telegram
```

## 4. Test It

- **DM the bot** -- send a message directly. The bot responds with MarkdownV2 formatting.
- **Add to a group** -- add the bot to a group chat. By default it responds to all messages; set `require_mention: true` to require @mentions.
- **Send a photo** -- the bot downloads it to a local cache for vision tool access.
- **Send a document** -- supported types: PDF, Markdown, TXT, DOCX, XLSX, PPTX, ZIP (max 20 MB).
- **Send a voice message** -- the bot caches it for speech-to-text processing.
- **Share a location** -- the bot receives coordinates and venue details.

## Behavior Configuration

### Connection Mode

By default, the adapter uses **long polling** (outbound connection to Telegram). For cloud deployments where inbound HTTP can wake a suspended container, use **webhook mode**:

```yaml
telegram:
  bot_token: "..."
  webhook_url: "https://your-app.fly.dev/telegram"  # public HTTPS URL
  webhook_port: 8443                                  # local listen port
  webhook_secret: "a-random-secret"                   # update verification
```

When `webhook_url` is empty (the default), polling mode is used.

### Mention Gating

In group chats, control when the bot responds:

```yaml
telegram:
  require_mention: false        # respond to all group messages (default)
  free_response_chats: "123,456" # chat IDs where no mention is needed regardless
  mention_patterns: '["hey bot", "\\bsurogates\\b"]'  # regex wake-word patterns (JSON list or comma-separated)
```

When `require_mention` is `true`, the bot only responds in groups when:

- The bot is @mentioned
- The message is a reply to one of the bot's messages
- The message is a `/command`
- The message text matches a `mention_patterns` regex
- The chat is listed in `free_response_chats`

DMs are always unrestricted.

### Reply Threading

Control how the bot threads its replies to user messages:

```yaml
telegram:
  reply_to_mode: "first"  # "first" (default), "all", or "off"
```

- `first` -- only the first chunk of a multi-chunk response threads to the user's message
- `all` -- every chunk threads to the user's message
- `off` -- responses are never threaded

### Message Reactions

Enable emoji reactions to show processing status:

```yaml
telegram:
  reactions_enabled: false  # default: off
```

When enabled, the bot sets a reaction on the user's message when processing starts, and a thumbs-up when the response is delivered.

### Session Isolation

In group chats, each user gets their own session by default. To use a single shared session per group:

```yaml
telegram:
  per_user_groups: false  # default: true (separate session per user)
```

## Message Handling

### Text Message Aggregation

Telegram clients split long messages (>4096 characters) into multiple updates. The adapter buffers rapid successive messages from the same user and aggregates them into a single event before dispatching. The buffer uses an adaptive delay:

- **Normal messages**: 0.6 seconds quiet period (`text_batch_delay`)
- **Near-split-point chunks** (>4000 chars): 2.0 seconds (`text_batch_split_delay`)

### Photo and Album Batching

When a user sends multiple photos rapidly (or a Telegram album), the adapter merges them into a single event instead of interrupting the agent with each one. Album items sharing a `media_group_id` are always merged. Individual photos are batched with a 0.8 second delay (`media_batch_delay`).

### MarkdownV2 Formatting

Outbound messages are converted from standard Markdown to Telegram MarkdownV2. Code blocks, inline code, links, bold, italic, strikethrough, spoilers, and blockquotes are supported. If MarkdownV2 parsing fails (malformed markup), the adapter falls back to plain text automatically.

### Media Sending

The adapter supports sending rich media in responses:

| Type | Bot API method | Notes |
|---|---|---|
| Text | `sendMessage` | MarkdownV2 with plain-text fallback, chunked at 4096 chars |
| Photo (URL) | `sendPhoto` | Falls back to download+upload for >5 MB images |
| Photo (file) | `sendPhoto` | Local file upload |
| Voice/Audio | `sendVoice` / `sendAudio` | `.ogg`/`.opus` as voice bubble, others as audio file |
| Video | `sendVideo` | Local file upload |
| Document | `sendDocument` | Any file with display name |
| Animation | `sendAnimation` | GIF auto-play, falls back to photo |
| Edit | `editMessageText` | For streaming response updates |

## Network Resilience

### Fallback IP Transport

In networks where `api.telegram.org` is unreachable (DNS poisoning, geo-blocking), the adapter auto-discovers alternative IPs via DNS-over-HTTPS (Google and Cloudflare) and routes requests through them while preserving TLS/SNI. This is transparent and requires no configuration.

To provide explicit fallback IPs:

```yaml
telegram:
  fallback_ips: "149.154.167.220,149.154.167.221"
```

To disable fallback transport (e.g., when using a proxy):

```bash
export SUROGATES_TELEGRAM_DISABLE_FALLBACK_IPS=true
```

The adapter also respects standard proxy environment variables (`HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY`).

### Polling Error Recovery

The adapter handles transient failures automatically:

- **Network errors** (connection reset, timeout): exponential backoff (5s, 10s, 20s... up to 60s), up to 10 retries
- **409 Conflict** (another instance polling the same token): 3 retries at 10-second intervals, then stops
- **Send failures**: 3 retries with exponential backoff; flood control (`429 Too Many Requests`) waits the `retry_after` duration

### Custom Bot API Server

For self-hosted [Telegram Bot API servers](https://core.telegram.org/bots/api#using-a-local-bot-api-server):

```yaml
telegram:
  base_url: "http://localhost:8081/bot"
```

## Advanced Configuration

All settings use the `SUROGATES_TELEGRAM_` environment variable prefix:

| Setting | Env var | Default | Description |
|---|---|---|---|
| `bot_token` | `SUROGATES_TELEGRAM_BOT_TOKEN` | | Bot token from BotFather |
| `webhook_url` | `SUROGATES_TELEGRAM_WEBHOOK_URL` | | Webhook URL (empty = polling) |
| `webhook_port` | `SUROGATES_TELEGRAM_WEBHOOK_PORT` | `8443` | Webhook listen port |
| `webhook_secret` | `SUROGATES_TELEGRAM_WEBHOOK_SECRET` | | Webhook verification secret |
| `require_mention` | `SUROGATES_TELEGRAM_REQUIRE_MENTION` | `false` | Require @mention in groups |
| `free_response_chats` | `SUROGATES_TELEGRAM_FREE_RESPONSE_CHATS` | | Comma-separated chat IDs |
| `mention_patterns` | `SUROGATES_TELEGRAM_MENTION_PATTERNS` | | JSON list or comma-separated regexes |
| `reply_to_mode` | `SUROGATES_TELEGRAM_REPLY_TO_MODE` | `first` | Reply threading mode |
| `reactions_enabled` | `SUROGATES_TELEGRAM_REACTIONS_ENABLED` | `false` | Processing lifecycle reactions |
| `per_user_groups` | `SUROGATES_TELEGRAM_PER_USER_GROUPS` | `true` | Per-user sessions in groups |
| `fallback_ips` | `SUROGATES_TELEGRAM_FALLBACK_IPS` | | Comma-separated fallback IPs |
| `base_url` | `SUROGATES_TELEGRAM_BASE_URL` | | Custom Bot API server URL |
| `media_batch_delay` | `SUROGATES_TELEGRAM_MEDIA_BATCH_DELAY` | `0.8` | Photo/album batch delay (seconds) |
| `text_batch_delay` | `SUROGATES_TELEGRAM_TEXT_BATCH_DELAY` | `0.6` | Text batch delay (seconds) |
| `text_batch_split_delay` | `SUROGATES_TELEGRAM_TEXT_BATCH_SPLIT_DELAY` | `2.0` | Delay for near-split-point chunks |

## Troubleshooting

| Problem | Solution |
|---|---|
| Bot doesn't respond | Check `SUROGATES_TELEGRAM_BOT_TOKEN` is set correctly. Check logs for "Connected (polling mode)". |
| "Unknown user ... ignoring message" in logs | The Telegram user is not linked. Register them via the admin API (step 2). |
| Bot doesn't respond in groups | If `require_mention` is `true`, @mention the bot. Also check BotFather `/setprivacy` is disabled. |
| "Another process is already polling" | Only one adapter instance can poll per bot token. Stop the other process. |
| Messages are delayed | Check `text_batch_delay` and `media_batch_delay` settings. The adapter buffers rapid messages to handle Telegram's client-side splitting. |
| MarkdownV2 parse errors in logs | Normal -- the adapter falls back to plain text automatically. Check for malformed markdown in agent responses. |
| "Telegram network error, scheduling reconnect" | Transient network issue. The adapter retries automatically with exponential backoff. |
| Photos not processed | Ensure the bot has permission to download files (default for Bot API). Check `/tmp/surogates/cache/images` for cached files. |
