# Microsoft Teams Channel

Connect an agent to Microsoft Teams so people can talk to it in a personal
chat, in group chats and in team channels. Each customer brings their own
**Azure Bot**, so the bot carries their name and lives in their tenant. The
integration is **webhook-based**: the Bot Framework posts every activity to
the shared channels service (`surogates channels`), which resolves the
owning agent per bot, verifies the request and feeds the shared inbound
pipeline.

## How it works

```
Teams → Bot Framework → POST https://<channels-host>/teams/{app_id}
             │  verify the Bot Framework JWT (JWKS, aud = App ID, serviceUrl)
             ▼
        channel_routing lookup (ops) → (org, agent, config)
             ▼
        shared inbound pipeline → session → worker
             ▼
        delivery_outbox → POST {serviceUrl}/v3/conversations/{id}/activities
                          (client-credentials token from the customer's tenant)
```

- **Routing** — each bot maps to one agent via a `channel_routing` row
  (kind `teams`, identifier = the bot's **Microsoft App ID**) managed by
  Studio's Channels form in `surogate-ops`.
- **Credentials** — the client secret (`app_password`) and the Entra
  tenant id (`tenant_id`) live in the per-tenant credential vault.
- **Verification** — the Bot Framework signs no body; it sends a bearer
  token. The channels service checks it against Microsoft's published keys:
  RS256, issuer `https://api.botframework.com`, audience equal to *this*
  bot's App ID, five minutes of clock skew, and a `serviceUrl` claim equal
  to the activity's — replies go to that URL, so it must be the one
  Microsoft signed.
- **Webhook registration** — manual. Studio shows the messaging endpoint to
  paste into the Azure Bot.

## Setup

1. In the [Azure portal](https://portal.azure.com), *Create a resource* →
   **Azure Bot**. Choose **Single Tenant** as the type of app and let it
   create a new Microsoft App ID.
2. Open the bot's *Configuration*. Copy the **Microsoft App ID** and the
   **App Tenant ID**. Click *Manage Password* → *New client secret* and copy
   the secret's **Value** (shown once). Secrets expire — at most 24 months —
   so note the date: the channel stops answering the day it does.
3. In Studio, open the agent → **Channels** → **Microsoft Teams**, paste the
   App ID, tenant id and secret, and connect. Studio checks them by fetching
   a token from your tenant.
4. Back in the bot's *Configuration*, paste the **Messaging endpoint** Studio
   shows (`https://<channels-host>/teams/<app id>`) and *Apply*.
5. In the bot's *Channels*, add **Microsoft Teams** and accept the terms.
6. Download the **Teams app package** (a `.zip` manifest) from Studio and
   upload it: in Teams, *Apps* → *Manage your apps* → *Upload an app* →
   *Upload a custom app*; or have an administrator publish it to the
   organisation from the [Teams admin center](https://admin.teams.microsoft.com)
   (*Teams apps* → *Manage apps* → *Upload new app*).

**Admin approval.** Many tenants block custom apps. If *Upload a custom
app* is missing or the upload is refused, a Teams administrator must either
allow custom app uploads in the app setup policy or publish the package to
the organisation's app catalog — and, where app permission policies restrict
third-party or custom apps, allow this one. Nothing reaches the agent until
the app is installed somewhere.

## Identity

A Teams sender is identified by their Entra **`aadObjectId`** (falling back
to the Teams user id for guests), which is the same for every bot in the
tenant. Two policies, chosen per channel in Studio (`identity_policy`):

- **shadow** (default) — every sender is auto-provisioned a shadow user on
  first contact.
- **linked** — only linked Surogate accounts may talk. Teams has no private
  side channel for the pairing code yet, so unknown senders are not sent one;
  use **shadow** on Teams for now.

## Sessions & threading

- A **personal chat** is one session per person.
- A **group chat** is one shared session.
- A **channel** keys a session per thread (the root post's id); with
  **multi session** off, every thread of the channel continues one
  conversation and replies follow the latest thread.
- **Mention gating** (`require_mention`, default on): in group chats and
  channels the agent answers only when @mentioned. Teams itself delivers
  channel messages to a bot only when it is mentioned. The bot's own
  `<at>…</at>` mention is removed from the text; other people's become their
  plain names.
- Deduplication keys on the activity id (`teams:<id>`), so a redelivered
  activity is dropped. `/stop` interrupts the running turn.

## Media

- **Inbound**: files shared in a chat arrive with a pre-authorised
  `downloadUrl` and are ingested into the session workspace (20 MB cap, 10
  files per message).
- **Outbound**: replies are sent as Teams markdown and split at natural
  boundaries under 24 000 characters. A `MEDIA:<path>` image up to 1 MB is
  attached inline; other files cannot be attached by a bot without
  SharePoint, so they are not sent.
- Progress: text the agent writes beside a tool call is edited in place
  into one message, and "typing…" shows while a turn runs.

## Interactive input (`ask_user_question`)

There are no buttons on Teams yet (no Adaptive Cards). A question is asked
in the agent's text, and the person's next message answers it.

## Writing first

The agent can write to someone (an opener, an outreach message, a team
alert into a channel) only in a conversation the bot already knows: a
person who installed the app or wrote to it, or a channel or chat it was
added to. Each install and each message records where that conversation
lives for 90 days; past that, or for someone who never installed the app,
the write fails with a readable reason.

## Ops notes

- Enable the platform on the channels deployment with
  `SUROGATES_CHANNELS_TEAMS_ENABLED=true` (`channels.teams.enabled`);
  routing rows control which bots are live.
- Delivery is a durable outbox: transient failures (429 throttling, 5xx)
  retry, and a blocked bot (`MessageWritesBlocked`,
  `ConversationBlockedByUser`), a bot removed from the conversation
  (`BotNotInConversationRoster`), a missing conversation (404), an
  oversized message (`MessageSizeTooBig`) or a refused client secret
  (`invalid_client`) is permanent and dropped.
- Unknown App IDs are fast-acked with 200 and no side effects.
- Local development: `scripts/fake_teams.py` stands in for the Bot
  Framework and Entra ID; point `SUROGATES_TEAMS_LOGIN_BASE` and
  `SUROGATES_TEAMS_OPENID_URL` at it (its docstring has the dev
  credentials).
