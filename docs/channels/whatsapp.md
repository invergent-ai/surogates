# WhatsApp Channel

Connect an agent to WhatsApp so users can talk to it in one-to-one chats.
The integration uses Meta's **official WhatsApp Business Cloud API** — not
the unofficial WhatsApp Web bridge. It is **webhook-based**: Meta pushes
notifications to the shared channels service (`surogates channels`), which
resolves the owning agent per business phone number, verifies the request,
and feeds the shared inbound pipeline.

Each tenant brings their own Meta App and WhatsApp Business Account, so the
callback URL is per-number and the credentials are per-tenant.

## How it works

```
Meta → GET  https://<channels-host>/whatsapp/{phone_number_id}   (once)
             │  hub.challenge handshake (vault verify_token)
             ▼
Meta → POST https://<channels-host>/whatsapp/{phone_number_id}
             │  verify X-Hub-Signature-256 HMAC (vault app_secret)
             ▼
        channel_routing lookup (ops) → (org, agent, config)
             ▼
        shared inbound pipeline → session → worker
             ▼
        delivery_outbox → Graph POST /{phone_number_id}/messages
```

- **Routing** — each business phone number maps to one agent via a
  `channel_routing` row (kind `whatsapp`, identifier `phone_number_id`)
  managed by Studio's Channels form in `surogate-ops`. The channels service
  resolves it over HTTP with a 30s cache invalidated by Redis pub/sub.
- **Credentials** — `access_token`, `app_secret` and `verify_token` live in
  the per-tenant credential vault, never in env vars or config files. The
  tenant's `phone_number_id` and Graph `api_version` ride alongside them,
  because the outbound path receives only the outbox row and the resolved
  credentials.
- **Webhook registration** — manual. Meta has no `setWebhook` equivalent
  for the callback URL, so the operator pastes it into the App Dashboard.
  Studio renders the exact URL and verify token to copy.
- **The agent never initiates.** WhatsApp only permits free-form messages
  inside a 24-hour window that the *user* opens by messaging first, so this
  channel replies and never starts a conversation. It is excluded from
  ambient and scheduled routing.

## Setup

1. At [developers.facebook.com/apps](https://developers.facebook.com/apps),
   create an app with the **"Connect with customers through WhatsApp"** use
   case. A WhatsApp Business Account is created with it. Note the **Phone
   Number ID** in *WhatsApp → API Setup* — it sits just below the *From*
   dropdown and is 15–17 digits. It is **not** the phone number itself.
2. Create a permanent token at
   [business.facebook.com](https://business.facebook.com/latest/settings) →
   *System users* → add an Admin system user → *Assign Assets* (the app with
   *Manage app*, the WhatsApp account with *Manage WhatsApp Business
   Accounts*) → *Generate token* with `whatsapp_business_messaging`,
   `whatsapp_business_management` and `business_management`, expiration
   **Never**. The token shown on the API Setup page expires after 24 hours
   and the channel will stop working silently on day two — always use a
   System User token in production.
3. Copy the **App Secret** from *App Settings → Basic* (32 lowercase hex
   characters).
4. In Studio, open the agent → **Channels** → **WhatsApp**, paste the Phone
   Number ID, access token, app secret and WABA ID, and **save**. Saving
   writes the routing row and mints the verify token; the webhook handshake
   cannot succeed until this has happened.
5. Back in the Meta dashboard, *WhatsApp → Configuration → Edit webhook*:
   paste the **Callback URL** and **Verify Token** that Studio now shows,
   then *Verify and save*.
6. Still in *Configuration*, click *Manage* on webhook fields and subscribe
   to **`messages`**. Skipping this is the classic "verification succeeded
   but nothing arrives".
7. While the app is in development mode, Meta only delivers to numbers on
   its recipient list (*API Setup → To → Manage phone number list*, five
   maximum). This is Meta's list of who the business may message, and is a
   different thing from the agent's own allow-list of who may talk to it.

## Identity

A WhatsApp sender is identified by their `wa_id` — E.164 digits with no
leading `+`. Two policies, chosen per channel in Studio
(`identity_policy`):

- **shadow** (default) — every sender is auto-provisioned a shadow user on
  first contact.
- **linked** — only linked Surogate accounts may talk. Unknown senders
  receive a pairing code in the chat and no session is created until they
  link.

Meta is rolling out business-scoped user ids, for which `wa_id` may be
absent; identities written today are keyed on the phone number and will
need migrating when that lands.

## Sessions & threading

Cloud API conversations are one-to-one, so there are no groups, no threads
and no mention gating: one session per sender, and every message is a DM.
Deduplication keys on the `wamid`, Meta's globally unique message id, so a
redelivered notification is dropped.

`/stop` interrupts the running turn out-of-band.

## Media

- **Inbound**: images, video, audio, documents and stickers arrive as a
  media id and are fetched in two hops (metadata, then the signed
  lookaside URL, which still requires the bearer token), capped at 20 MB and
  10 files per message. Captions become the message text. There is no
  transcription, so a voice note reaches the agent as an `.ogg` file rather
  than text.
- **Outbound**: replies are transcoded from markdown to WhatsApp markup
  (`*bold*`, `_italic_`, `~strike~`, monospace) and split at natural
  boundaries to fit the 4096-character limit. A `MEDIA:<path>` marker
  uploads the workspace file and posts it as a native attachment; Meta caps
  images at 5 MB, video and audio at 16 MB, documents at 100 MB, and
  captions at 1024 characters.
- WhatsApp cannot edit or delete a sent message, so intermediate narration
  is not shown. The agent marks the user's message read and raises the
  typing indicator instead — that is the only progress signal available.

## Interactive input (`ask_user_question`)

When the agent asks a question mid-run, the question and its choices are
posted as a numbered plain-text list. Replying normally answers it: the
pipeline matches choice labels case-insensitively and records anything else
as a free-form answer. There are no tappable buttons on this channel.

## Ops notes

- Enable the platform on the channels deployment via the runtime config
  (`channels.whatsapp.enabled: true`); routing rows control which numbers
  are live.
- Delivery is a durable outbox: transient failures retry every 30s and
  dead-letter after 30 minutes or on permanent errors.
- Unknown phone number ids are fast-acked with 200 and no side effects, so
  the webhook endpoint is not an enumeration oracle. A handshake for an
  unknown id therefore returns an empty 200 and Meta reports verification
  failure — check that the Studio form was saved first.
- Because the agent never initiates, a reply that somehow outlives the
  user's 24-hour window is rejected by Meta with error `131047`, classified
  permanent, and dropped rather than retried.
