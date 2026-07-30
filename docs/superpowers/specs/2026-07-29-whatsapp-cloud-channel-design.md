# WhatsApp Business Cloud API channel — design

**Date:** 2026-07-29
**Status:** approved, ready for planning
**Repos touched:** `surogates` (channel adapter), `surogate-ops` (provisioning, commerce, Studio UI)

Adds `whatsapp` as a fourth managed channel alongside `slack`, `telegram` and
`website`, using Meta's **official WhatsApp Business Cloud API**. The Baileys /
WhatsApp-Web bridge approach is explicitly out of scope.

---

## 1. Product decisions

These four choices constrain everything below. A change to any of them
invalidates large parts of the design.

| # | Decision | Consequence |
|---|---|---|
| 1 | **Per-tenant BYO Meta App.** Each agent owner creates their own Meta App, WhatsApp Business Account and phone number, then pastes credentials into Studio. | The tenant identifier is `phone_number_id` and lives in the URL path. The existing dispatcher ordering holds unchanged. No Meta Tech Provider status, no Embedded Signup. |
| 2 | **Reactive only — the agent never initiates.** | No message templates, no 24-hour-window tracker, no hold-and-flush queue. WhatsApp is excluded from ambient and scheduled routing. |
| 3 | **v1 capabilities:** text both ways, HMAC webhook security, identity, outbound media, inbound media, read receipts + typing indicator. | No *native* interactive buttons. See §3.3 — `ask_user_question` still requires a text-mode renderer, because omitting it breaks the tool entirely rather than degrading it. |
| 4 | **Studio UI mirrors Telegram.** A form with paste-time shape validation, a generated verify token, and the callback URL rendered for copying. | No live Graph API probes (number picker, token-expiry alarms) in v1. |

### 1.1 Why "the agent never initiates" is the load-bearing decision

WhatsApp opens a **24-hour customer service window** when an end user messages
the business number. Inside it, free-form messages are unlimited. Outside it,
free-form sends are rejected with error `131047`, and only a **pre-approved
template** is deliverable.

Under decision 1 (BYO), templates would be a per-tenant burden: each customer
would have to author templates in their own WABA and wait for Meta approval
before any proactive message worked at all. Decision 2 removes that entirely.

What remains is defensive only: a reactive reply is inside the window by
construction, but a retry chain that starts near the boundary could cross it. So
`131047` must be classified as a permanent delivery error with a distinct reason
rather than retried for 30 minutes and dropped generically.

---

## 2. Reference implementation

The integration was designed against hermes-agent's Cloud API adapter, which
lives on the **unmerged branch `origin/feat/whatsapp-cloud-api`**, commit
`984e6cb5b`.

> The files at `study/hermes-agent/gateway/platforms/whatsapp.py` and
> `study/hermes-agent/gateway/whatsapp_identity.py` on the checked-out commit are
> the **Baileys bridge**, not Cloud API. `whatsapp_identity.py` is JID↔LID
> collapse machinery that exists only because Baileys flips identity forms
> mid-conversation. Neither is relevant here.

The Cloud API bundle is `gateway/platforms/whatsapp_cloud.py` (1869 lines),
`gateway/platforms/whatsapp_common.py` (351), `hermes_cli/setup_whatsapp_cloud.py`
(530), `tests/gateway/test_whatsapp_cloud.py` (2250),
`tests/hermes_cli/test_whatsapp_cloud_setup.py` (406) and
`website/docs/user-guide/messaging/whatsapp-cloud.md` (418).

It is single-tenant: one adapter instance, one phone number, one token, a
module-global media cache, and five in-memory dicts with no tenant key. Its
webhook-security half (signature check, handshake ladder, 3 MB pre-HMAC cap)
and its formatting logic are excellent and portable; its allow-list gating is
only half-wired (the Cloud adapter's `allow_from` is never populated, so the
mixin policy falls back to the Baileys-shared `WHATSAPP_DM_POLICY` default
`"open"`), and its state management is not portable at all. §9 records what
must not be copied.

---

## 3. Architecture

WhatsApp is architecturally **closer to Telegram than Slack** — no message
editing, no `send_files` precedent, and a path-derived identifier. Telegram is
the template to follow.

```
surogates/channels/platforms/
  whatsapp.py         WhatsAppPlatform — the ChannelPlatform implementation
  whatsapp_api.py     Graph client: URL building, sends, media up/down, error formatting
  whatsapp_format.py  markdown → WhatsApp transcoder (mirrors telegram_format.py)
```

Three modules rather than one: the Graph client is the only part that performs
HTTP, so isolating it lets the platform logic be tested without mocking network
calls, and lets the client be tested without constructing a platform.

### 3.1 The adapter contract

`ChannelPlatform` (`surogates/channels/registry.py:104`) is a structural
`Protocol`. `SlackPlatform` and `TelegramPlatform` do not inherit from it, and
`ChannelRegistry.register` (`registry.py:272`) does not `isinstance`-check its
argument — a platform missing a required method registers cleanly and fails at
the first webhook. `WhatsAppPlatform` follows the same convention: a plain class,
no inheritance.

`surogates/channels/base.py:85` `ChannelAdapter` is the older, vestigial
protocol used only by the dead `channels/teams.py` and `channels/webhook.py`
stubs. **Do not implement it.** `SendResult` from `base.py:71` *is* live and is
what `send` returns.

Required members:

| Member | Value for WhatsApp |
|---|---|
| `kind` | `"whatsapp"` |
| `topology` | `"webhook"` |
| `descriptor` | see §7 |
| `route_path(identifier=None)` | see §5.1 — must return the concrete path when given an identifier, the template when not |
| `identifier_of(request, body)` | `request.path_params["phone_number_id"]`; `body` is always `None` |
| `verify(request, body, *, creds)` | branches on `request.method`; see §5.2 |
| `parse(body, *, creds, identifier)` | see §5.3 |
| `send(item, *, creds)` | see §6.1 |

### 3.2 Optional members implemented in v1

| Member | Why it is required |
|---|---|
| `send_files` | outbound media (§6.3) |
| `download_file` | inbound media (§6.5) |
| `ack_received` | read receipts + typing (§6.2) |
| `send_private` | trivially `send` — every WhatsApp conversation is a DM. Required for the `linked` identity policy: `runner.py:180-188` withholds the pairing code from platforms that cannot deliver it privately. |
| `post_input_nudge` | **not interactive-only.** `runner.py:310-312` getattr-guards it, so omitting it fails silently. It is the delivery path for the `/stop` acknowledgement (`inbound.py:668-672`) and — critically — the allowance/subscription block notice carrying `commerce_buy_url` (`inbound.py:791-800`). Without it, a user who exhausts their allowance is met with total silence and no buy link, while §11 makes commerce metering a security-shaped requirement. Implement as a plain text send to `channel` (the sender's `wa_id`), ignoring `thread_ts`. |

Deliberately **not** implemented: `supports_edit` (WhatsApp cannot edit a sent
message), `interactive_paths`, `handle_interactive`,
`handle_non_message_update`, `post_thinking_placeholder`, `enrich`,
`fetch_channel_context`, `list_channel_files`, `delete_message`.

### 3.3 `ask_user_question` needs a text-mode renderer

Decision 3 declines *native buttons*. It must not be read as declining
`ask_user_question`, because leaving WhatsApp out of
`INTERACTIVE_PROMPT_CHANNELS` does not degrade the tool — it breaks it silently.

`_build_channel_payload` (`store.py:133-144`, gate at `:135`) only populates an
`INBOX_INPUT_REQUIRED` payload when `channel in INTERACTIVE_PROMPT_CHANNELS`.
Otherwise the payload stays `{}`, and `store.py:1170-1171` `if not payload:
return` means **no outbox row is written at all**. The user sees nothing, and the
session parks waiting for an answer that can never arrive.

Symmetrically, `inbound.py:679` gates pending-input interception to
`("slack", "telegram")`. That gate is not "interactive only": the non-Slack
fallthrough at `inbound.py:896-917` is precisely the **plain-text answer** path —
*"Telegram has no modal; a plain reply IS the answer"* (docstring, `:864-865`) —
resolving via `resolve_text_answer`. The fallthrough is the implicit else after
the Slack branch at `:890`, not Telegram-gated, so any kind added to the `:679`
tuple opts into it automatically. That is exactly what a button-less platform
needs most.

So v1 adds `whatsapp` to both, with a text renderer: the question and its
choices rendered as a numbered list in the message body, and a plain typed reply
resolving the pending record through the existing `resolve_text_answer`. This is
a small amount of work that reuses Telegram's machinery; the alternative is to
strip `ask_user_question` from the WhatsApp toolset entirely so the agent never
calls a tool that cannot work.

### 3.4 Graph API version

**Pin v23.0 or later, configurable per tenant** via `config.api_version`.

Hermes pins `DEFAULT_API_VERSION = "v20.0"`. Meta removes v20.0 on
**2026-09-24**. Inheriting that constant ships a channel with a two-month fuse.

---

## 4. The one framework change — mounting the GET handshake

Meta verifies a callback URL with
`GET ?hub.mode=subscribe&hub.verify_token=…&hub.challenge=…`, expecting the
challenge echoed as a plain-text body. `_mount_platform`
(`dispatcher.py:146-163`) registers `methods=["POST"]` only. This is the sole
framework blocker in the design.

Slack's challenge arrives as a *signed POST* and so reuses `verify`; WhatsApp's
arrives **unsigned on GET** and cannot.

### 4.1 Chosen approach

One optional member, one small handler, **zero change to the POST path**:

```python
# _mount_platform, after the existing POST route
if getattr(platform, "handshake_get", False):
    app.add_api_route(
        platform.route_path(), self._make_handshake_handler(platform), methods=["GET"]
    )

def _make_handshake_handler(self, platform):
    async def _handler(request: Request) -> Response:
        *_, err = await self._resolve_and_verify(platform, request, b"")
        return err if err is not None else Response(status_code=400)
    return _handler
```

`WhatsAppPlatform` sets `handshake_get = True`.

This works because `_resolve_and_verify` (`dispatcher.py:169`) already performs
the whole hardened front-half — path identifier → `resolve_tenant` → vault
credentials → `verify` — and already renders a `str` response body as
`PlainTextResponse` (`dispatcher.py:255-258`), which is exactly the un-quoted
challenge echo Meta requires. `verify` branches on `request.method` and returns
`VerificationResult(accepted=True, response_body=challenge, status_code=200)`.

The trailing `Response(status_code=400)` is unreachable in practice: it fires
only if `verify` returns a bare truthy value instead of a `VerificationResult`,
which would be an implementation bug.

### 4.2 Consequences accepted deliberately

- **Unknown identifier fast-acks 200 with an empty body** (`dispatcher.py:213`;
  an exception inside `identifier_of` fast-acks the same way at `:199-204`).
  Meta reads an empty body as a failed challenge and shows "verification
  failed", which is the correct outcome, and we keep the no-liveness-oracle
  property that the fast-ack exists to provide.
- **Every rejection renders 401.** `_resolve_and_verify` discards
  `VerificationResult.status_code` whenever `accepted` is `False`
  (`dispatcher.py:240-246`), and an exception raised inside `verify` is caught
  into the same 401 (`dispatcher.py:233-238`). Bad mode, token mismatch, missing
  challenge and unconfigured verify token are therefore indistinguishable by
  status code. **The distinguishing signal must be the log level, not the
  response** — see §5.2. Meta only distinguishes 200 from non-200, so nothing is
  lost operationally.

### 4.3 Ordering requirement this creates

The handshake needs the `channel_routing` row and the vault entries to exist
already. Therefore **the Studio form must be saved before the operator pastes
the callback URL into Meta's dashboard.** If the order is reversed, the
handshake fast-acks 200 empty and Meta rejects the URL with no diagnostic.

The Studio UI must state this explicitly, and the docs must repeat it — it is
the most likely setup failure and it produces a misleading error.

### 4.4 Alternative considered and rejected

Adding a generic `route_methods` member and teaching `_make_handler` to branch on
`request.method`. Rejected because `_make_handler` would have to skip the body
read, the JSON parse, `handle_non_message_update`, `parse`, `enrich` and the
pipeline for GET requests — six conditionals threaded through the
security-critical handler to serve one platform. A separate four-line handler
that reuses the shared front-half is smaller and leaves the POST path untouched.

---

## 5. Inbound

### 5.1 Route and identifier

Mirror Telegram (`telegram.py:487-498`) and Slack (`slack.py:539`): return the
**concrete** path when given an identifier and the template only when not.

```python
def route_path(self, identifier: str | None = None) -> str:
    if identifier is None:
        return "/whatsapp/{phone_number_id}"
    return f"/whatsapp/{identifier}"

def identifier_of(self, request: Any, body: Any) -> str:
    return request.path_params["phone_number_id"]
```

Returning the template unconditionally would hand callers a literal
`{phone_number_id}`. `dispatcher.py:1115` builds
`self._public_url + platform.route_path(identifier)`; that is benign today only
because the reconciler skips `manual` platforms. §12's Studio callback URL must
be built from `route_path(identifier)` so the copy string and the mounted route
cannot drift.

Meta configures **one callback URL per Meta App**. Under decision 1 each tenant
owns their app, so a per-tenant path is exactly what their dashboard produces.

`channel_routing` enforces `UNIQUE(channel_kind, channel_identifier)`, so one
`phone_number_id` maps to exactly one agent. A WABA owning several numbers is
fine because the identifier is the number, not the WABA.

### 5.2 `verify`

**GET branch** — the handshake ladder. Every rejection renders 401 (§4.2), so the
diagnostic signal is the log:

| Condition | Result |
|---|---|
| `verify_token` missing from creds | reject (`accepted=False`) + **log at ERROR** — this is a misconfiguration, not an attack, and the log is the only way to tell it apart. Never silent-accept. |
| `hub.mode != "subscribe"` | reject, log at INFO |
| token mismatch | reject, log at WARNING |
| `hub.challenge` empty | reject, log at INFO |
| otherwise | `VerificationResult(accepted=True, response_body=challenge)` |

Compare the token as **bytes**:
`hmac.compare_digest(token.encode("utf-8", "surrogatepass"), expected.encode("utf-8"))`.
`compare_digest` on `str` raises `TypeError` on non-ASCII input, and the token is
attacker-controlled — the str form yields an unhandled exception that
`dispatcher.py:233-238` converts to a 401, losing the real cause.

Refusing when unconfigured is load-bearing: an unset secret makes
`compare_digest("", "")` true, so an attacker who guesses the misconfiguration
can subscribe their own webhook.

**POST branch** — signature validation:

```python
def _verify_signature(app_secret: str, raw_body: bytes, header: str) -> bool:
    if not app_secret or not header:      return False
    if not header.startswith("sha256="):  return False
    expected_hex = header[len("sha256="):].strip()
    if not expected_hex:                  return False
    computed = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed.lower(), expected_hex.lower())
```

Port verbatim: HMAC over the **raw bytes** never a re-serialisation; the
`sha256=` prefix requirement; lower-casing both hex sides (case-insensitive
without losing constant time); no SHA-1 `X-Hub-Signature` fallback.

Add a **3 MB body cap checked before any crypto** — Meta's documented maximum.
The dispatcher already hands `verify` the raw bytes (`dispatcher.py:298`), so the
raw-body requirement is satisfied for free.

A missing `app_secret` for a *known* tenant must hard-reject. Meta's multi-day
retry window then replays the backlog once the operator fixes the secret.

### 5.3 `parse`

```python
if body.get("object") != "whatsapp_business_account": return None
for entry in body.get("entry") or []:
    for change in entry.get("changes") or []:
        if change.get("field") != "messages": continue
        value = change.get("value") or {}
```

Four things this must do that the reference gets wrong:

**Assert tenant identity.** `value["metadata"]["phone_number_id"]` must equal the
path `identifier`; drop and log on mismatch. Hermes extracts this field, passes
it down through two call layers, and never reads it. One Meta App can subscribe
several numbers, so for a multi-tenant service that is a tenant-isolation hole.

**Return `None` for non-message events.** `reaction`, `system`, `unsupported`,
`order`, `location`, `contacts`, and every `statuses[]` entry. Hermes maps
unknown types to `TEXT` with `body=""`, so an emoji thumbs-up starts a real agent
turn with an empty prompt. This is exactly what the protocol's loop-safety
contract at `registry.py:160-174` exists for.

**Log `statuses[]` and `value.errors[]`; do not emit channel observations.**
They carry asynchronous send failures the `POST /messages` 200 did not report, so
they must be visible — but `append_channel_observation`
(`channel_observations.py:25`) requires a redis handle and an `agent_id`, and
`parse` is called as `platform.parse(body, creds=…, identifier=…)`
(`dispatcher.py:355`) with neither. That queue is also the follow-mode memory
firehose, drained into the agent's long-term channel memory — delivery telemetry
does not belong there. Log at WARNING for `failed` and `errors[]`, DEBUG for
`sent`/`delivered`/`read`, with tenant + wamid + code, **inside a try/except** so
a logging failure cannot turn a status webhook into a 400 and a Meta retry loop
(`dispatcher.py:360-365` converts any `parse` exception to 400).

**Set `msg.ts = wamid`**, never the WhatsApp `timestamp`. The timestamp is
second-resolution and would collide across senders in the existing dedup cache.
The wamid is globally unique.

Types that **do** produce an `InboundMessage`: `text`, `image`, `video`, `audio`,
`document`, `sticker`. Body extraction is `text.body` for text and
`<type>.caption` for the five media kinds. Build a
`wa_id → contacts[].profile.name` index for display names.

**One `InboundMessage` per webhook.** `parse` returns a single message and the
dispatcher runs one pipeline pass per POST. When Meta coalesces several user
messages into one notification — bursts and retry backlogs; rare for reactive
DM traffic — v1 delivers the first and WARN-logs the dropped count. Framework
fan-out is an explicit non-goal (§10).

Read `audio.voice` (a boolean) to distinguish a voice note from an uploaded audio
file. Hermes has a dead `"voice"` type branch — the Cloud API never sends
`type: "voice"`.

**Carry the tenant id and wamid in `msg.source`**, the free-form adapter metadata
dict — `source["phone_number_id"]` and `source["wamid"]` — exactly as Telegram
carries `source["message_id"]` (`telegram.py:290`). `ack_received` is invoked as
`ack(msg, creds=…, config=…)` (`dispatcher.py:404`) with **no** `routing` and no
`identifier`, and `msg.identifier` is the conversation id (the sender's `wa_id`),
not the tenant. Anything `ack_received` needs must ride on `msg`. No change to
the `InboundMessage` dataclass is required.

**Set `visibility="dm"`** alongside `is_dm=True` — the Telegram private-chat
precedent (`telegram.py:206`). Only `boundary_token` reads it today, and an
unbranched platform lands in the isolated fall-through regardless (§11 #12),
but the dataclass default is `"private"`, which misdescribes a DM surface and
would miskey a future `wa:d:` boundary branch.

### 5.4 Group refusal

WhatsApp Cloud API is treated as DM-only. Refuse anything whose sender cannot be
resolved to a 1:1 `wa_id`, and log the full raw message type at WARNING.

**Do not copy hermes' group heuristic.** It checks a `chat` key that does not
exist in Meta's schema; its own comment says *"Defensive: if Meta ever delivers a
group-shaped payload"*, and its test feeds a hand-written dict rather than a real
envelope. Encoding a fabricated field shape is worse than refusing on the absence
of a resolvable sender, and the WARNING log captures the real shape the first
time one arrives.

### 5.5 Dedup — scope note

Setting `ts = wamid` gets the existing in-process `MessageDeduplicator`
(`dedup.py`, TTL 300 s, max 2000 entries) for free: the shared inbound pipeline
constructs it (`inbound.py:405`) and keys it on the bare `msg.ts` string
(`inbound.py:442`) — a single global cache, which is exactly why a
second-resolution timestamp would collide across senders and a wamid cannot.

Cross-replica duplicates remain possible: the cache is per-process and the
channels Deployment runs more than one replica. **This is a pre-existing
platform-wide gap that Slack and Telegram share today**, not something WhatsApp
introduces. Making dedup durable (Redis `SET NX EX`, keyed by tenant) is a
separate platform improvement and is out of scope here.

One reference bug worth *not* reproducing: hermes commits the dedup key before
the work succeeds, so an exception during event construction escapes → 500 →
Meta retries → the retry is dropped as a duplicate → the message is permanently
lost. Our pipeline enqueues durably before acking, so the ordering is already
correct; keep it that way.

---

## 6. Outbound

### 6.1 `send`

1. Transcode markdown via `whatsapp_format.render_whatsapp`.
2. Split with the existing `split_text(text, 4096)` (`text_split.py:23`) —
   WhatsApp's `text.body` cap. Telegram's platform cap is the same 4096, but do
   not copy its call site: `telegram.py:653` feeds `split_text` 3500
   (`_MAX_SOURCE_CHUNK`) to leave headroom for HTML-render inflation. WhatsApp
   markup does not inflate and §6.6 runs the transcoder *before* splitting, so
   the full 4096 is correct here.
3. `POST /{phone_number_id}/messages` per chunk.
4. Return `SendResult` carrying the wamid from `response["messages"][0]["id"]`.

**No reply quoting in v1.** WhatsApp is 1:1, so quoting adds little, and enabling
it would require three changes outside this design's scope: `reply_to_mode` added
to §7 `config_keys`; relaxing the `msg.is_dm` early-return in
`_record_reply_target` (`inbound.py:938-939`), which always bails for a DM-only
platform; and extending the Telegram-hardcoded fresh-reply re-read at
`store.py:1189-1190` to WhatsApp, without which every reply would quote the
session's *first* message.

Follow the existing partial-send convention (`slack.py:1044`,
`telegram.py:677`): on a mid-sequence failure report `success=True` with the last
id, so a retry does not duplicate already-delivered chunks.

Empty or whitespace-only content short-circuits with zero HTTP calls and returns
`SendResult(success=True, message_id=None)`. That is the correct terminal state,
not a wart: `_deliver_item` (`dispatcher.py:836-914`) has exactly two branches and
never reads `SendResult.retryable`, so `success=False` would requeue an unsendable
item every 30 s for 30 minutes before `mark_dead`. `dispatcher.py:887` already
guards the `mark_bot_message` follow-up on a non-null id. In practice this is
near-unreachable — `_build_channel_payload` (`store.py:112-115`) does not enqueue
empty content.

### 6.2 `ack_received` — read receipts and typing

One request sets blue double-checkmarks **and** the typing pip:

```json
{"messaging_product": "whatsapp",
 "status": "read",
 "message_id": "<wamid>",
 "typing_indicator": {"type": "text"}}
```

POSTed to `/{phone_number_id}/messages` — the same endpoint as a normal send, with
no `to`, no `recipient_type` and no `type`. Both values come from `msg.source`
(§5.3), because `ack_received` receives only `(msg, creds=…, config=…)`. The pip
auto-dismisses on the next outbound message or after 25 seconds, so there is no
cancel path to manage.

This matters more than it appears. WhatsApp cannot edit a sent message, so
`dispatcher.py:710-718` silently swallows **every** intermediate narration event
for platforms without `supports_edit`. Without read receipts the agent is
completely silent for the entire tool-calling phase.

The receipt must fire **after** the allow/deny gate, so filtered senders never
receive one. `dispatcher.py:401-404` already calls `ack_received` only when the
outcome is `PROCESSED`, so this holds for free.

Log `131009` ("message id older than 30 days", common after a long-quiet
conversation) at INFO rather than WARNING.

### 6.3 `send_files` — outbound media

Two steps: `POST /{phone_number_id}/media` (multipart) → `media_id` →
`POST /messages` with `{"type": "image", "image": {"id": media_id}}`.

Multipart shape, with scalars as `(None, value)`:

```python
files = {
    "file": (basename, fh, mime_type),
    "messaging_product": (None, "whatsapp"),
    "type": (None, mime_type),   # the MIME string, not the kind word
}
```

Enforce caps **client-side before the round trip**, with the cap value in the
error string: image 5 MB, video 16 MB, audio 16 MB, document 100 MB, sticker
100 KB. Caption cap 1024 chars; `caption` applies only to `image`/`video`/
`document`, `filename` only to `document`.

If media is sent by `link` rather than upload, require `https://` — Meta rejects
plain HTTP, and the reference's `startswith(("http://", "https://"))` sniff
forwards a doomed request instead of uploading locally.

Give uploads a longer HTTP timeout than sends; a single 30-second client does not
cover a 100 MB document.

### 6.4 The `MEDIA:` gate change

`dispatcher.py:760` reads:

```python
if platform.kind == "slack" and "MEDIA:" in content:
```

Change to `if send_files_fn is not None and "MEDIA:" in content:`.

The existing comment — *"Gated to Slack: never strip a marker on a platform that
cannot upload"* — describes a **capability**; the kind-check was the cheapest way
to express it when Slack was the only platform with `send_files`. `send_files_fn`
is already resolved two lines above at `dispatcher.py:758`. Telegram has no
`send_files`, so its behaviour is unchanged.

This change is **mandatory, not optional**: `harness/prompts/platforms/whatsapp.md`
already ships and tells the model *"The file will be sent as a native WhatsApp
attachment."* Without the gate change, users receive literal
`MEDIA:/root/media/images/foo.png` text.

WhatsApp is the first platform with `send_files` but **without** `supports_edit`,
a combination no existing platform has. The marker-only reply path
(`dispatcher.py:788-823`) uploads first (`:792`) and only after a successful
upload deletes the thinking placeholder via `delete_message` (`:798-806`), gated
on a recorded placeholder ts (`update_ts`) and a redis handle. WhatsApp posts no
placeholder (`post_thinking_placeholder` unimplemented), so `update_ts` stays
`None` and the delete leg must stay inert; add a regression test covering it.

### 6.5 `download_file` — inbound media

Inbound media is fetched by the generic `_attachments` hook
(`runner.py:237-299`), which calls
`platform.download_file(creds=…, url=…, max_bytes=…)` at `runner.py:275` under a
10-file / 20 MB budget (`runner.py:255-256`) and ingests the result via
`ingest_attachment_bytes` at `:281`.

Implement it with Telegram's exact signature —
`async def download_file(self, *, creds: dict, url: str, max_bytes: int) -> bytes | None`
(`telegram.py:770-772`) — carrying the `media_id` in the `url` argument the way
Telegram carries its `file_id`, and doing both Graph hops inside:

`GET /{ver}/{media_id}` → `{url, mime_type, file_size}` → `GET <lookaside url>`.

`max_bytes` is a **hard cap, not a streaming contract**: the framework buffers the
whole body. Enforce the cap against `file_size` from the step-one metadata
*before* fetching the bytes.

`file_fetch.py` (the Slack-only `fetch_channel_file` tool, which hardcodes
`kind="slack"`) and `channel_media.py` (the outbound `MEDIA:` resolver) are **not**
on this path and need no change.

The signed lookaside URL **still requires the `Authorization: Bearer` header** —
Meta documents this and it is the most common inbound-media mistake. The URL
expires in roughly five minutes, so the download must happen inline during the
webhook. On a 403/410 at step two, retry once by re-running step one. Sanitize
the Meta-supplied `media_id` before it reaches any path component.

Port the MIME→extension override table **including its comments**, which record
why each row exists:

```python
_WHATSAPP_MIME_EXTENSION_OVERRIDES = {
    "audio/ogg": ".ogg", "audio/x-opus+ogg": ".ogg", "audio/opus": ".ogg",  # not mimetypes' .oga
    "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",                             # iOS voice memos
    "image/jpeg": ".jpg",                                                    # not legacy .jpe
}
```

### 6.6 Markdown transcoding

`whatsapp_format.render_whatsapp`, mirroring `telegram_format.py`. WhatsApp
syntax is `*bold*`, `_italic_`, `~strike~`, ` ```mono``` `.

The prompt tells the model to emit no markdown, but models routinely disobey, so
the transcoder is not optional.

Port the **sentinel technique**: protect fenced blocks and inline code by
replacing them with `\x00FENCE{i}\x00` placeholders before transforming, then
restore. The **trailing** `\x00` is load-bearing — without it a sequential
`str.replace` restore of index 1 corrupts index 11. `\x00` never appears in LLM
output.

Known reference bugs to fix rather than inherit: `***bold italic***` degrades
to `**bold italic**` (a stray asterisk on *each* side); `![alt](url)` becomes
`!alt (url)`; lists, blockquotes and tables pass through untouched; nothing is
escaped, so a literal asterisk is unrepresentable. (The link regex's `[^)]+`
capture is *not* a bug despite appearances: the uncaptured tail of a
`)`-containing URL passes through literally and the output is byte-identical.)

Note the ordering trap: formatting runs **before** splitting, so a chunk boundary
can land inside a converted `*bold*` span and leave an unpaired marker. Only
backtick balance is checked. Decide whether to extend the balance check to
emphasis markers or accept the artifact.

### 6.7 Error classification

Format Graph errors as `graph error {code} (HTTP {status}): {message}` —
hermes' `_format_graph_error`, ported. Keep its fallback too: when Meta returns
no `error.code`, the string is `HTTP {status}: {message}` with no `graph error`
prefix, so code-less errors can never match a permanent prefix and stay
retryable by construction. Then add the **delimited prefix** — never the bare
code — to `_PERMANENT_DELIVERY_ERRORS` (`delivery.py:58-75`):

```python
"graph error 190 (", "graph error 100 (",
"graph error 131026 (", "graph error 131047 (",
```

`is_permanent_delivery_error` (`delivery.py:83-88`) is an **unanchored,
case-insensitive substring test** shared by every platform through the single
`_record_delivery_failure` (`dispatcher.py:659`). A bare `"100"` would mark any
Slack or Telegram error string containing those digits — a rate-limit message
mentioning "1000 requests", a size error mentioning "100 MB" — as permanently
dead. Every existing entry is a distinctive alphabetic token (`is_archived`,
`token_revoked`, `bot was blocked by the user`), never a bare number. **Add a
regression test asserting a Slack error containing `100` is still retryable.**

| Code | Meaning | Classification |
|---|---|---|
| `190` (subcodes 463 expired / 467 revoked) | token dead | permanent |
| `100` | bad object id — usually a phone number pasted into the Phone Number ID field | permanent |
| `131026` | recipient not on WhatsApp / undeliverable | permanent |
| `131047` | 24-hour window closed | permanent |
| `130429`, `4`, HTTP 429, HTTP 5xx | rate limited / transient | retryable — **no entry needed**, retryable is the default |

Surfacing a dead token as a Studio credential alarm is **v2, not v1**: the only
terminal action available at the failure site is
`mark_dead(item.id, error)` (`dispatcher.py:666`), which flips the row to
`status="failed"` and WARN-logs the error (`delivery.py:299-312`) —
`delivery_outbox` has no error column, so the reason survives only in logs. No
channel-health or credential-alarm surface exists in either repo today.

Meta's default throughput is 80 messages/second per business phone number.
`channels/rate_gate.py` (a `check_inbound_rate_limit` wrapper around the
runtime-layer `PerTenantRateLimiter` — the class itself lives in
`surogates/runtime`, not channels) is referenced only by
`tests/runtime/test_channel_adapter_rate_limit.py`; nothing in the channels
stack ever calls it, so no inbound rate limiting is active for any platform
today. **Decision: not wired in
v1.** Reactive-only traffic cannot approach 80/s, and wiring `rate_gate` is a
platform-wide change affecting Slack and Telegram equally. The limit is recorded
so a future high-volume feature does not rediscover it.

---

## 7. Credentials and configuration

Secrets resolve through `vault_ref_for_channel(kind, cred, identifier)` →
`vault://{kind}_{cred}_{identifier}` (`token_resolver.py:35`). The vault layer is
already proven kind-agnostic: `tests/runtime/test_channel_credentials.py:69`
asserts `vault_ref_for_channel("whatsapp", "bot_token", "W1")`.

```python
descriptor = ChannelDescriptor(
    vault_refs=lambda identifier: {
        "access_token": "access_token",
        "app_secret": "app_secret",
        "verify_token": "verify_token",
        # Non-secrets that must ride in creds: they are the only payload
        # every outbound surface receives (send, send_files, download_file,
        # send_private, post_input_nudge), and session config is a
        # creation-time snapshot that never carries routing config.
        "phone_number_id": "phone_number_id",
        "api_version": "api_version",
    },
    # No ``require_mention`` / ``allow_bots``: both are structurally
    # unreachable here (§7.1), so declaring them would put switches in
    # Studio that can never fire.
    config_keys=("identity_policy", "waba_id", "api_version"),
    webhook_registration="manual",
)
```

### 7.1 Two gating keys are deliberately absent

`require_mention` and `allow_bots` are **not** in `config_keys`, and the
provisioner does not write them. Both are unreachable on this platform, so
shipping them would surface controls in Studio that cannot change anything:

- `parse` always sets `is_dm=True` — WhatsApp Cloud API is 1:1 — and
  `_evaluate_mention_gate` (`inbound.py:964-966`) returns `True` immediately
  for a DM, before it ever reads `require_mention`. `free_response_channels`
  and `mention_patterns` are unreachable for the same reason.
- `parse` always sets `is_bot=False`, because Cloud API never marks a sender
  as a bot, so the `allow_bots` gate at `inbound.py:470` never runs.

`identity_policy` (shadow vs linked pairing) and `api_version` are live;
`waba_id` is operator-supplied data rather than a behavioural switch, and is
retained for the Meta-dashboard workflow and any future template work.

`webhook_registration="manual"`: Meta has no `setWebhook` equivalent for the
callback URL — it is set in the App Dashboard. `ChannelWebhookReconciler`
(`dispatcher.py:922-1116`) skips any platform whose
`descriptor.webhook_registration != "api"` (`:982-983`, `:1016-1017`,
`:1054-1057`) and will correctly skip WhatsApp.

| Value | Where it lives | Source |
|---|---|---|
| Access token | `vault://whatsapp_access_token_{pnid}` | Meta Business Settings → System users → Generate token, expiration **Never** |
| App secret | `vault://whatsapp_app_secret_{pnid}` | Meta App → Settings → Basic; 32 lowercase hex |
| Verify token | `vault://whatsapp_verify_token_{pnid}` + agent env | minted by the provisioner (reuse `_mint_webhook_secret`); returned via `extra_env` as `SUROGATES_WHATSAPP_VERIFY_TOKEN` so Studio's manage view can render it, and the env value wins on later saves |
| Phone number id | URL path + `vault://whatsapp_phone_number_id_{pnid}` | non-secret creds copy — outbound surfaces receive only `creds` |
| WABA id | `channel_routing.config` | Meta App → WhatsApp |
| API version | `channel_routing.config` + `vault://whatsapp_api_version_{pnid}` | default v23.0; the creds copy is what the outbound path reads |
| Display phone number | provisioner `extra_env` (agent env) | Graph-confirmed, phone-number-id fallback; the Studio `derivedKey` — see §11 frontend row 2 |

**A System User token is mandatory in production.** Dashboard tokens expire in 24
hours, so a channel configured with one silently dies on day two. Required
scopes: `whatsapp_business_messaging`, `whatsapp_business_management`,
`business_management`.

Agent env vars: `SUROGATES_WHATSAPP_ENABLED` (the `enabled_env` gate checked at
`channel_provisioning.py:359`), plus `_PHONE_NUMBER_ID`, `_ACCESS_TOKEN`,
`_APP_SECRET`, `_WABA_ID`, `_API_VERSION`,
`_IDENTITY_POLICY`, and two the provisioner re-emits via `extra_env` rather
than the forms: `_DISPLAY_PHONE` (the Studio `derivedKey`) and `_VERIFY_TOKEN`
(so the manage view can render the value the operator pastes into Meta).

**Process enablement: none required.** `WhatsAppChannelSettings.enabled`
defaults to `True`, unlike every other kind. The per-kind flag is a second gate
on top of `channel_routing`, and for a BYO-per-tenant channel the routing table
is already the only gate that matters: without a row the channel does nothing,
and an unknown `phone_number_id` fast-acks 200 with no side effects by design.
Requiring the flag bought no safety and cost a hand-applied ConfigMap edit per
environment — a step easy to forget whose only symptom is a webhook Meta reports
as unverifiable. Being on costs one mounted route and one 2-second outbox poll
that finds nothing. `SUROGATES_CHANNELS_WHATSAPP_ENABLED=false` (or
`channels.whatsapp.enabled: false`) still switches it off.

For reference, the runtime config in PROD is the **hand-applied** ConfigMap
`surogates-runtime-config`
(`k8s/surogates-runtime/production/30-runtime-configmap.yaml:189-195`), not a
chart template. The per-kind `channels.*.enabled` keys in
`k8s/surogates-runtime/values.yaml:187-190` are inert — no template consumes
them; only the top-level `channels.enabled` gates the Deployment
(`templates/channels-deployment.yaml:14`). That file needs no change: one
Deployment runs `surogates channels` for all platforms.

---

## 8. No database migration

Verified across both repos:

- `channel_identities.platform` and `.platform_user_id` — `Text`, with
  `UniqueConstraint("org_id", "platform", "platform_user_id")`
  (`db/models.py:204-226` — the constraint at `:211-213`, the `Text` columns at
  `:225-226`). No enum exists anywhere in the surogates schema.
- `sessions.channel`, `delivery_outbox.channel`, `ambient_schedules.platform`,
  `credentials.name` — all `Text`.
- ops `channel_routing.channel_kind` — `sa.String(64)`. Migration
  `a3f9c1d84e72` converted it from a PG enum with
  `postgresql_using='channel_kind::text'` and dropped the type. The `ChannelKind`
  docstring at `operate.py:40-46` states outright that new kinds need no schema
  migration (`ChannelKind` is a plain class of `str` constants, not an enum —
  members at `:48-50`). One caveat: that migration's *downgrade* recreates the
  enum with only slack/telegram/website and will fail once a `whatsapp` row
  exists — the schema is forward-only past that point.
- `channel_routing.channel_identifier` — `String(255)`; a `phone_number_id` is
  15–17 digits.

### 8.1 Identity

WhatsApp behaves like Slack and Telegram: shadow-user auto-provisioning by
default (`identity.py:120`), with pairing available when the agent config sets
`identity_policy=linked`.

`platform_user_id` is the `wa_id`: **E.164 digits with no leading `+`**. Normalize
at the adapter boundary. `identity.py:164` builds the shadow email with
`platform_user_id.lstrip("@")`, which does not strip a `+` — a value stored with
one would embed it in the email local part.

One stale comment to fix while in the area: `identity.py:162-163` says "there is
no unique constraint on email". There is — `uq_users_org_lower_email` on
`(org_id, lower(email))` at `db/models.py:190-195`.

**Known future risk, not addressed in v1:** Meta is rolling out Business-Scoped
User IDs. BSUIDs began appearing in webhooks in early April 2026, and for
username-adopting users `wa_id` may be absent entirely. Because the column is
`Text` and the constraint is exact-match, an opaque id is storable — but rows
written keyed on phone number will need migrating. Recorded as a risk; no v1
work.

---

## 9. Do not copy from the reference

| Pattern | Why it is wrong here |
|---|---|
| aiohttp server started in `connect()` with its own routes | We have a hardened FastAPI dispatcher. Implement `ChannelPlatform`, do not bring a second HTTP server. |
| `await self._dispatch_payload(payload)` inline before returning 200 | Runs two Graph round-trips and the full agent turn before acking. Meta's ack timeout fires, it retries, and dedup eats the retry — defeating the retry mechanism rather than using it. |
| Dedup committed before the work succeeds | A raise during event construction → 500 → Meta retries → retry dropped as duplicate → message permanently lost. |
| In-memory instance state (`_seen_wamids`, `_last_inbound_wamid_by_chat`, and the `_clarify_state` / `_exec_approval_state` / `_slash_confirm_state` interactive dicts) | No tenant key, no TTL, no cap (only `_seen_wamids` has one — 5000-entry FIFO); lost on restart, wrong across replicas. |
| Module-global media cache under the user's home dir | Multi-tenant data commingling and unbounded disk growth. |
| `hmac.compare_digest` on `str` for the verify token | `TypeError` on non-ASCII attacker input, converted to an opaque 401 with the real cause lost. |
| Group detection via a `chat` key | Invented field; do not encode a fabricated payload shape. |
| Unknown inbound type → `TEXT` with `body=""` | Starts a real agent turn on every reaction, system event and unsupported message. |
| `statuses[]` reduced to a DEBUG log | Discards delivery confirmation and asynchronous send failures. Log them properly (§5.3). |
| Ignoring `metadata.phone_number_id` | It is the tenant identity. |
| Env-var configuration and `~/.hermes/.env` | Our config is `ChannelDescriptor` + routing cache. Two documented env vars (`WHATSAPP_CLOUD_APP_ID`, `WHATSAPP_CLOUD_WABA_ID`) are read into fields nothing uses, and two more are read but undocumented. |
| `/health` endpoint exposing `phone_number_id` | Single-tenant observability leaking a tenant identifier. Emit per-tenant metrics instead. |
| Interactive resolvers invoked before the allow/deny gate | An approval tap from any `wa_id` resolves a dangerous-command approval — in hermes the resolver runs ~70 lines before `_should_process_message`. Not applicable in v1 (no native buttons), but a hard invariant if buttons land later: gate first, then resolve. |

---

## 10. Explicit non-goals

Out of scope for v1, each with the reason:

- **Message templates and any 24-hour-window machinery** — decision 2.
- **Native interactive buttons** — decision 3. `ask_user_question` still ships,
  rendered as a numbered text prompt (§3.3).
- **Voice-note transcription** — surogates has no STT. Inbound audio arrives as a
  file for the agent to handle.
- **ffmpeg / opus conversion for outbound voice notes** — the reference shells
  out, writes next to the source with no cleanup, and races on the output path
  under concurrency.
- **Reply quoting (`context`)** — §6.1; needs three changes outside this scope.
- **`statuses[]` → outbox reconciliation** — would require a
  `wamid → delivery_outbox row` mapping that does not exist;
  `delivery.py:272 mark_delivered(provider_message_id=…)` only DEBUG-logs the id.
  Statuses are logged (§5.3) so async failures remain visible.
- **Studio credential alarm on a dead token** — §6.7; no channel-health surface
  exists in either repo.
- **Durable cross-replica dedup** — a platform-wide gap shared with Slack and
  Telegram (§5.5).
- **Client-side send throttling** — §6.7.
- **Live Graph probes in Studio** — decision 4. The highest-value one, a
  `GET /{waba_id}/phone_numbers` number picker, would eliminate the most common
  setup error rather than diagnosing it; recorded for v2.
- **Embedded Signup / Meta Tech Provider onboarding** — decision 1.
- **Groups** — Cloud API is treated as DM-only (§5.4).
- **Multi-message batch fan-out** — the dispatcher runs one pipeline pass per
  webhook, so `parse` returns a single `InboundMessage`; when Meta coalesces
  several user messages into one notification, v1 delivers the first and
  WARN-logs the rest (§5.3). Fan-out needs a framework change shared by every
  platform.

---

## 11. Enumeration points

Adding a platform touches a fixed set of hardcoded lists. Silent-failure traps
are marked ⚠.

### surogates

| # | Location | Change | Failure if omitted |
|---|---|---|---|
| 1 | `channels/platforms/whatsapp.py` | new | — |
| 2 | `channels/platforms/whatsapp_api.py` | new | — |
| 3 | `channels/platforms/whatsapp_format.py` | new | — |
| 4 | `channels/platforms/__init__.py:12-13` | add the import (modules self-register at import time — cf. `telegram.py:1002-1005`) | never registered; this is the only discovery mechanism |
| 5 | `config.py:593-601` | `WhatsAppChannelSettings(ChannelKindSettings)`, env prefix `SUROGATES_CHANNELS_WHATSAPP_` (the per-kind `env_prefix` is load-bearing — `:582-587`) | — |
| 6 | `config.py:637-639` | `whatsapp` field on `ChannelsSettings` | permanently disabled — the comment at `:635-636` reads "Absent key → disabled"; no route mounted, no delivery loop |
| 7 | ⚠ `channels/constants.py:35` | `ADAPTER_CHANNELS` | `store.py:1148` drops every outbound event; outbox rows sit pending forever |
| 8 | ⚠ `channels/constants.py:41` | `INTERACTIVE_PROMPT_CHANNELS` **plus** a text-mode prompt renderer in `WhatsAppPlatform.send` | `ask_user_question` writes **no outbox row at all** (`store.py:133-144` + `:1170-1171`); the user sees nothing and the session parks. See §3.3 |
| 9 | ⚠ `channels/inbound.py:679` | add `whatsapp` to the pending-input platform tuple | a typed answer is treated as a new message and the question never resolves; the non-Slack fallthrough at `:896-917` (`resolve_text_answer`, `:910`) is the path a button-less platform needs, and it engages automatically once the kind is in the tuple |
| 10 | `channels/constants.py:51` | `END_USER_CHANNELS` | no `agent_users` enrollment; missing from the ops Users page |
| 11 | `session/store.py:155` + a branch beside `:1160` | `_THREAD_DEST_FIELDS` (consumed at `:1153`) and the per-channel destination builder | `destination` stays `{}` and every outbound item dies in `_deliver_item` with "missing channel_identifier" (`dispatcher.py:677-684`) |
| 12 | ⚠ `channels/memory_boundary.py:15` | `MANAGED_CHANNELS` (checked at `:76`) | **fail-open.** `session_memory_boundary` returns `None`, so memory falls back to the per-user / org-shared layout (`runtime/memory_protocol.py:83-87`, `orchestrator/worker.py:849-853`) and the workspace to the per-session layout (`storage/tenant.py:123-138`) — WhatsApp conversations would share memory with the user's web/Studio sessions. Omitting only the `boundary_token` branch (`:35-58`) is benign: the `:58` fall-through already yields per-conversation isolation (`fallback_id` is the deterministic per-conversation `session_key`, `inbound.py:620`). |
| 13 | `channels/dispatcher.py:146-163` | GET route mounting (§4) | webhook URL cannot be verified |
| 14 | `channels/dispatcher.py:760` | `MEDIA:` gate → capability check (§6.4) | literal `MEDIA:` text sent to users |
| 15 | `channels/delivery.py:58-75` | error classification, delimited prefixes (§6.7) | hard failures retried for 30 minutes then dropped silently |
| 16 | `api/routes/channel_files.py:97`, `:103`, `:154`, `:160` | Slack-hardcoded `fetch_channel_file` tool endpoints — widen or leave alone deliberately. The module already imports `effective_channel_platform` at `:30`. | the on-demand channel-file tool is unavailable on WhatsApp (inbound attachments still work via §6.5) |
| 17 | `docs/channels/whatsapp.md` + `docs/channels/index.md:7-13` | new doc + index row | — |

Files confirmed generic and requiring no change: `channels/resolve.py`,
`credentials.py`, `token_resolver.py`, `source.py`, `platform_resolve.py` (its
one special-case, `ambient` → `slack` at `:21-22`, is untouched), `dedup.py`,
`text_split.py`, `delivery.py` at the schema level. `inbound.py:632-633` already
writes session config keys generically as `f"{routing.platform}_channel_id"`.

### surogate-ops backend

| # | Location | Change | Failure if omitted |
|---|---|---|---|
| 1 | ⚠ `server/services/channel_provisioning.py:626` | `CHANNEL_PROVISIONERS["whatsapp"]` registered beside telegram's (dict defined at `:133`, slack registers at `:285`), `enabled_env="SUROGATES_WHATSAPP_ENABLED"` (gate at `:359`), plus an `extra_env` derived value (see frontend row 2) | **no `channel_routing` row is ever written**; every inbound webhook fast-acks 200. The single most load-bearing ops change. |
| 2 | ⚠ `core/commerce/features.py:66` | `CHANNEL_VOCAB` | **security-shaped**: `features_allow_channel:415-424` returns `True` for any channel outside the vocab, so a buyer on a Slack-only package chats over WhatsApp unmetered |
| 3 | `core/commerce/features.py:93` | `CHANNEL_LABELS` | package chips show a raw slug |
| 4 | `core/surogates_client.py:144-155` | `_format_channel_name` | renders "Whatsapp" via the `.title()` fallback at `:155` (Telegram already ships on that fallback) |
| 5 | `server/routes/agent_runtime.py:103` | `_MANAGED_CHANNELS` (consumed at `:1076`) | sender principal instead of agent principal for Composio |
| 6 | `server/routes/agent_runtime.py:256` | `_LINKABLE_CHANNEL_KINDS` (consumed by `_linkable_channels` at `:259`) | no `linkable_channels` projection; pairing not offered |
| 7 | `routes/commerce_public.py:230` | `channel_kind.in_([...])` — an inline duplicate of #6 | buy page will not advertise WhatsApp linking |
| 8 | `core/db/models/operate.py:48-50` | `ChannelKind` constant — a plain class of `str` constants, not an enum | cosmetic only, no migration |
| 9 | `tests/test_offer_features.py:65-67` | **currently asserts `{"channels": ["whatsapp"]}` raises "unknown channel"** | the test must be **inverted**, not extended — it uses `whatsapp` as its example of an unknown channel |

Confirmed generic: `routes/channels.py` (`/by-kind`, `/by-identifier`) has zero
platform vocabulary; `routes/admin_channels.py:73` takes a free-form
`channel_kind: str`; `sync_channel_routing_from_env`
(`channel_provisioning.py:319`) is kind-agnostic; `services/agents.py:690`
iterates `CHANNEL_PROVISIONERS`; `routes/mate.py:72` (`MateChannelUpsert.platform`)
is free-form with a `"slack"` default — acceptable under decision 2, which keeps
WhatsApp out of ambient routing.

### surogate-ops frontend

⚠ **`CHANNEL_ENV_KEYS` and the save paths must change together.** The invariant
is: *a key belongs in the set only if something re-emits it on save* — either a
form from its own state, or the backend provisioner's `extra_env`.
(`SUROGATES_SLACK_TEAM` and `SUROGATES_TELEGRAM_USERNAME` are in the set,
re-emitted by neither form, and re-minted server-side at
`channel_provisioning.py:249`, `:281`, `:622`, merged at `services/agents.py:705`.)
A key in the set that nothing re-emits is **deleted on the next save of either
form**, because `env_vars` is a wholesale column replace.

There are two independent save paths over the same set: `preserveExternalEnv`
(`channels-tab.tsx:93`, called only by the admin form at `:217`) and an inline
reimplementation in the work form (`work-agent-channels-tab.tsx:1449-1454`). They
differ — only the former also strips `RETIRED_CHANNEL_ENV_KEYS` — but both drop
set members. **Wiring only one form makes the other silently delete the
credentials on save.**

| # | Location | Change |
|---|---|---|
| 1 | ⚠ `features/agents/channels-tab.tsx:25-46` | `CHANNEL_ENV_KEYS` + WhatsApp fields in the admin form + the `env_vars` literal at `:219-239` (`RETIRED_CHANNEL_ENV_KEYS` sits at `:84-91`) |
| 2 | ⚠ `features/work/work-agent-channels-tab.tsx` | `ChannelView` union `:44-53`; `WhatsAppConnectView` / `WhatsAppManageView` modelled on `TelegramConnectView:1137` / `TelegramManageView:1220`; state + `env_vars` literal `:1456-1477`; the list-view cards (`:1996-2011`) and the view-routing chain (`:1797`, `:1814`, `:1884`, `:1900`) — without those the new views are unreachable. **`runConnect` (`:1500-1541`) is generic over key *names* but hard-requires a server-derived env value** — it treats an empty `derivedKey` as failed token validation and rolls the channel back to disabled (`:1522-1536`). WhatsApp must therefore either have its provisioner return an `extra_env` value to use as `derivedKey` (the Graph-confirmed display phone number is the natural choice) or use a connect path that skips the derived-value check. |
| 3 | `features/work/work-home-page.tsx:40`, `:46-54`, `:58-64` | `ChannelKey` type, env probes, label map |
| 4 | `features/work/work-agent-overview-page.tsx:67-82` | `activeChannelNames` |
| 5 | `features/work/work-agent-overview-state.ts:11-18`, `:24-46` | `VISIBLE_OVERVIEW_CHANNELS` — a channel outside this set is filtered out of the overview chart entirely — and the label if-chain |
| 6 | `features/work/work-agent-settings-page.tsx:1676` | `(["slack","telegram","website"] as const)`; the `SUROGATES_${c.toUpperCase()}_ENABLED` derivation is already generic |
| 7 | `features/agents/agent-commerce-panel.tsx:169-173` | `SELLABLE_CHANNELS` — its ids must match row 6's settings-page list (wired via `work-agent-settings-page.tsx:1900`), or the channel is silently unsellable |
| 8 | `features/onboarding/use-onboarding-progress.ts:19-24` | channel list |
| 9 | `features/public-agent/buy-page.tsx:344-345` | pre-existing bug: `linkable_channels?.map(c => c === "slack" ? "Slack" : "Telegram")` renders any third kind as "Telegram" |
| 10 | `features/work/mock-agent-detail-data.ts:10`, `:79`, `:114` | mock channel union / sample rows — already stale (no telegram or website); extend or skip deliberately |

`components/sessions/session-filters.ts` needs no change — the channel dropdown is
API-facet driven, though `displayLabel:99` will render "Whatsapp".

---

## 12. Studio setup flow

A form mirroring `TelegramConnectView`, with paste-time shape validation only.

| Field | Validation |
|---|---|
| Phone Number ID | numeric; **if 10–12 digits, reject with "that looks like a phone number"** and explain where the ID is found |
| Access Token | starts with `EAA`, length ≥ 100; diagnose known wrong prefixes (`sk-` → OpenAI, `xoxb-` → Slack, `ghp_` → GitHub) |
| App Secret | exactly 32 hex chars, **rejecting uppercase** with "Meta app secrets are lowercase hex — check your paste" |
| WABA ID | numeric, 10–25 |
| Verify Token | minted server-side on first save, surfaced back through agent env (`SUROGATES_WHATSAPP_VERIFY_TOKEN`) and rendered with a copy button; rotation defaults to No |

The phone-number-ID check is the highest-value idea in the reference: it is not a
length check but a **hypothesis about the user's mistake**, and the message names
what was pasted, what the field wants, and where to find it.

Do not reproduce the reference's app-secret validator, which lowercases before
matching hex — uppercase passes validation and then fails HMAC at runtime.

The form must render, with copy buttons and the collected values interpolated:

- Callback URL, built from `route_path(phone_number_id)` (§5.1) so it cannot
  drift from the mounted route
- Verify Token
- The instruction that **the form must be saved before pasting into Meta** (§4.3)
- A reminder to subscribe the **`messages`** webhook field — omitting it is the
  classic "verified but nothing arrives"
- A note that dev-mode numbers can only message 5 whitelisted recipients

Two allow-lists must be presented as distinct concepts, because operators
conflate them: Meta's **recipient whitelist** (who we can send to, max 5 in dev
mode) and **our** allow-list (who may talk to the agent).

---

## 13. Testing

Port the reference's fixture payloads verbatim — real Meta webhook envelopes are
the single most reusable artifact in the bundle. Available: inbound text (the
canonical full envelope), image, document, interactive button reply, reply
context, Graph media-metadata response, and Graph send responses.

Recompute signatures in tests, never hardcode them:

```python
def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
```

The reference has **no** fixtures for these, so they must be authored:

- `statuses[]` payloads (`sent`/`delivered`/`read`/`failed` with `recipient_id`,
  `conversation`, `pricing`, `errors[]`)
- multi-entry and multi-message batches — every reference fixture has exactly one
  of each, so fan-out logic has zero coverage to inherit
- unicode, emoji and RTL bodies — all reference fixtures are ASCII
- a 4096-boundary split that does not cut a codepoint
- the tenant-mismatch drop (`metadata.phone_number_id != identifier`)
- the GET handshake: success, wrong token, missing challenge, unconfigured token
  (assert **401 plus an ERROR log**, not 503 — §4.2), unknown identifier

Required regression tests specific to this design:

- `parse` returns `None` for `reaction`, `system`, `unsupported`, `order`,
  `location`, `contacts` and for `statuses[]`-only payloads, and never raises out
  of the status-logging path
- a Slack error string containing the digits `100` is still classified retryable
  (§6.7)
- the `MEDIA:` gate change does not alter Telegram behaviour, and the
  marker-only-reply branch is inert for a platform with `send_files` but without
  `supports_edit` (§6.4)
- `ask_user_question` on WhatsApp produces an outbox row, and a plain typed reply
  resolves it (§3.3)
- `ADAPTER_CHANNELS` contains every registered platform (guards trap #7)

---

## 14. Risks and open items

| Risk | Mitigation |
|---|---|
| Operator pastes the callback URL into Meta before saving the Studio form | UI copy + docs (§4.3); the failure is silent and misleading otherwise |
| Dashboard token used instead of a System User token | Dies after 24 h. Validation cannot distinguish them by shape; document prominently, and consider a `debug_token` expiry probe in v2 |
| Meta BSUID migration | `wa_id` may be absent for username-adopting users. Column is `Text` so opaque ids are storable; rows keyed on phone number will need migrating (§8.1) |
| Graph API version deprecation | Pin v23.0+, configurable per tenant (§3.4); v20.0 is removed 2026-09-24 |
| Cross-replica duplicate delivery | Pre-existing, shared with Slack and Telegram (§5.5) |
| PROD `runtime-channels` was deployed with `kubectl`, not helm | The `runtime` release has diverged from the on-disk chart. Dump live state before any deploy; do not assume a helm upgrade is safe |

Unverified facts carried from research, not to be treated as settled: Meta's
changelog page returned HTTP 500 on every fetch, so 2026-dated platform claims
come from feature pages rather than the changelog; the reference bundle's own
docs defer to Meta as authoritative for pricing, App Review and rate limits.
Confirm the Graph endpoint shapes in §6 against Meta's docs during
implementation.
