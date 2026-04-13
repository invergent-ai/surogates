# Slack Integration Guide

Connect your Surogates agent to Slack so users can interact with it via DMs and channel @mentions.

## Prerequisites

- Surogates API server and worker running
- PostgreSQL and Redis accessible
- A Slack workspace where you have admin permissions

## 1. Create a Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and click **Create New App** → **From scratch**
2. Name it (e.g., "Surogates Agent") and select your workspace
3. Under **Socket Mode**, click **Enable Socket Mode** and generate an app-level token with `connections:write` scope — this is your `SUROGATES_SLACK_APP_TOKEN` (starts with `xapp-`)

## 2. Configure Bot Permissions

Go to **OAuth & Permissions** → **Scopes** → **Bot Token Scopes** and add:

| Scope               | Purpose                       |
| ------------------- | ----------------------------- |
| `app_mentions:read` | Detect @mentions in channels  |
| `channels:history`  | Read channel messages         |
| `channels:read`     | List channels                 |
| `chat:write`        | Send messages                 |
| `files:read`        | Download file attachments     |
| `groups:history`    | Read private channel messages |
| `groups:read`       | List private channels         |
| `im:history`        | Read DM messages              |
| `im:read`           | List DMs                      |
| `im:write`          | Open DMs                      |
| `mpim:history`      | Read group DM messages        |
| `reactions:read`    | Read reactions                |
| `reactions:write`   | Add/remove reactions          |
| `users:read`        | Resolve user names            |

## 3. Subscribe to Events

Go to **Event Subscriptions** → **Enable Events**, then under **Subscribe to bot events** add:

- `message.channels`
- `message.groups`
- `message.im`
- `message.mpim`
- `app_mention`

If using the AI Assistant feature (optional):

- `assistant_thread_started`
- `assistant_thread_context_changed`

## 4. Install the App

Click **Install to Workspace** and authorize. Copy the **Bot User OAuth Token** — this is your `SUROGATES_SLACK_BOT_TOKEN` (starts with `xoxb-`).

## 5. Link Slack Users

Slack users must link their account to a Surogates user before they can interact with the agent. This happens automatically via **self-registration**:

1. An unlinked Slack user sends a message to the bot
2. The bot replies with an ephemeral message containing a pairing link and code
3. The user clicks the link, logs in to the Surogates web UI, and clicks "Link"
4. Their Slack ID is bound to their Surogates account — future messages work instantly

The pairing code expires after 10 minutes and is single-use. Users are rate-limited to one code per 10 minutes.

### Admin Registration (alternative)

Admins can also link users manually via the API:

```bash
curl -X POST http://localhost:8000/v1/admin/channel-identities \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "SUROGATES_USER_UUID",
    "platform": "slack",
    "platform_user_id": "U0123ABCDEF"
  }'
```

## 6. Configure and Run

### Option A: Environment Variables

```bash
export SUROGATES_SLACK_APP_TOKEN="xapp-1-..."
export SUROGATES_SLACK_BOT_TOKEN="xoxb-..."
SUROGATES_CONFIG=config.dev.yaml surogates channel slack
```

### Option B: Config File

Add to your `config.yaml`:

```yaml
slack:
  app_token: "xapp-1-..."
  bot_token: "xoxb-..."
  require_mention: true
  allow_bots: "none"
```

Then run:

```bash
SUROGATES_CONFIG=config.yaml surogates channel slack
```

### Option C: VSCode

Set `SUROGATES_SLACK_BOT_TOKEN` and `SUROGATES_SLACK_APP_TOKEN` in your shell environment, then use the **"Surogates: Channel (Slack)"** launch configuration, or **"Surogates: Full Stack + Slack"** to start everything together.

## 7. Test It

- **DM the bot** — send a message directly. The bot responds immediately.
- **@mention in a channel** — type `@Surogates Agent hello`. The bot responds in a thread.
- **Thread replies** — once mentioned in a thread, the bot responds to all follow-up messages in that thread without needing another @mention.

## Behavior Configuration

### Mention Gating

By default, the bot only responds in channels when @mentioned. Configure this:

```yaml
slack:
  require_mention: true # default: require @mention in channels
  free_response_channels: "C123,C456" # channels where no mention needed
```

### Bot Message Handling

Control whether the bot responds to other bots:

```yaml
slack:
  allow_bots: "none"       # ignore all bot messages (default)
  allow_bots: "mentions"   # respond to bots only if they @mention us
  allow_bots: "all"        # respond to all bot messages (except our own)
```

### Threading

```yaml
slack:
  reply_in_thread: true # respond in thread (default)
  reply_broadcast: false # also post to channel when replying in thread
```

## Multi-Workspace

For Slack apps installed in multiple workspaces (e.g., via OAuth distribution), provide comma-separated bot tokens:

```bash
export SUROGATES_SLACK_BOT_TOKEN="xoxb-workspace1-token,xoxb-workspace2-token"
```

Each workspace is authenticated independently. The adapter routes API calls to the correct workspace based on the channel's team ID.

## How It Works

```
Slack user sends message
  → Socket Mode WebSocket event
  → SlackAdapter._handle_slack_message() (12-step pipeline)
    1. Dedup check (prevent reprocessing on reconnects)
    2. Bot message filtering
    3. Ignore edits/deletions
    4. Extract text, channel, user, team
    5. Track channel → team mapping (multi-workspace)
    6. DM vs channel detection
    7. Thread timestamp resolution
    8. Channel mention gating
    9. Strip @mention, track mentioned threads
    10. Thread context fetch (prior messages for first mention)
    11. File/media download and caching
    12. Identity resolution → session creation → event emission
  → Redis work queue → Worker → LLM → response events
  → Delivery outbox → SlackAdapter._delivery_loop()
  → markdown → mrkdwn conversion → chat.postMessage
  → User sees response in Slack
```

## Slash Commands (Optional)

Register a slash command `/surogates` in your Slack app settings to allow users to invoke the agent via `/surogates <message>`. The command is treated as a regular message.

## Troubleshooting

| Problem                            | Solution                                                                                                              |
| ---------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| Bot doesn't respond in channels    | Check `require_mention` is `true` and you're @mentioning it                                                           |
| "Your Slack account is not linked" | Click the pairing link the bot sent, log in, and link your account. Or ask an admin to register via the API (step 5). |
| Bot responds twice                 | Check for duplicate Socket Mode connections (only one adapter process per app token)                                  |
| File attachments not working       | Ensure `files:read` scope is added and the bot is in the channel                                                      |
| Rate limit errors in logs          | Normal for thread context fetching — the adapter retries automatically                                                |
