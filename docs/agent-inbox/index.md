# Agent Inbox

The agent inbox is the user's queue of raised-hand moments from active and
completed sessions. It turns selected session events into durable inbox rows so
the web UI and shared React SDK can show items that need user attention without
requiring the user to watch every session live.

Inbox items are user-scoped. Service-account/API-channel sessions do not have a
user inbox.

## When Inbox Items Are Created

`SessionStore.emit_event` mirrors recognized inbox events into the
`inbox_items` table in the same database transaction as the source event. The
source event remains the durable audit record; the inbox row is a denormalized
view for user workflow.

Recognized event types:

| Event type | Inbox kind | Meaning |
|---|---|---|
| `inbox.input_required` | `input_required` | The agent needs a text answer through the `clarify` flow. |
| `inbox.action_required` | `action_required` | The user must perform an external action, usually in the session or browser. |
| `inbox.task_complete` | `task_complete` | A session completed and has a summary/outcome. |
| `inbox.governance_gate` | `governance_gate` | A user-overridable governance decision needs approval or rejection. |
| `inbox.progress_checkin` | `progress_checkin` | A long-running session produced a progress update. |

After commit, the store publishes a Redis nudge on
`surogates:inbox:{user_id}`. The nudge is only a live-update hint; clients
should fetch the row from the API.

## Item Kinds

### `input_required`

Use this when the user can unblock the agent by answering a text question. The
canonical path is the `clarify` tool.

User actions:

| Button | Effect |
|---|---|
| Open session | Navigates to the related session. |
| Submit | Sends clarify answers to `/v1/sessions/{session_id}/clarify/{tool_call_id}/respond`, then refreshes the inbox item. |
| Delete | Hides the item by expiring it. It does not answer the agent. |

### `action_required`

Use this when the user must do something outside a text reply: sign in, pass
MFA, approve OAuth, handle CAPTCHA, use a file picker, grant consent, or perform
another browser/session action.

The harness has a user-action judge for final assistant drafts. Text questions
route to `clarify`; browser/session/manual action requests route to
`action_required`. The local fallback also classifies obvious login, browser,
approval, and manual-action language as `action_required` if the structured
judge fails.

User actions:

| Button | Effect |
|---|---|
| Open session | Navigates to the related session so the user can perform the action. |
| I completed this | Posts `{ "completed": true }` to `/v1/inbox/{item_id}/respond`, marks the item `responded`, emits a `USER_MESSAGE` event with source `inbox_action_completed`, and wakes the session. |
| Delete | Hides the item by expiring it. It does not tell the agent that the action was completed. |

### `task_complete`

Use this for informational completion notifications. The payload carries the
outcome, summary, duration, and optional error.

User actions:

| Button | Effect |
|---|---|
| Open session | Navigates to the completed session. |
| Acknowledge | Marks the item `acknowledged`. It does not wake the session. |
| Delete | Hides the item by expiring it. |

### `progress_checkin`

Use this for informational updates from long-running sessions. The payload may
include iteration count, elapsed seconds, last tool, and a progress summary.

User actions:

| Button | Effect |
|---|---|
| Open session | Navigates to the running session. |
| Acknowledge | Marks the item `acknowledged`. It does not wake the session. |
| Delete | Hides the item by expiring it. |

### `governance_gate`

Use this when a governance policy denies a tool call but the denial is
configured as user-overridable. The payload includes the tool name, tool call
ID, policy reason, and argument excerpt.

User actions:

| Button | Effect |
|---|---|
| Open session | Navigates to the session where the gated tool call occurred. |
| Approve / Reject | Posts a decision to `/v1/inbox/{item_id}/respond`, emits a `USER_MESSAGE` event with source `inbox_governance_decision`, marks the item `responded`, and wakes the session. |
| Delete | Hides the item by expiring it. It does not approve or reject the gated action. |

## Statuses

| Status | Meaning |
|---|---|
| `pending` | The item still needs attention or has not been dismissed. |
| `acknowledged` | The user acknowledged an informational item. |
| `responded` | The user sent a response or completion signal back to the session. |
| `expired` | The item is hidden from default inbox views. Deleting an item sets this status. |

`acknowledged`, `responded`, and `expired` are terminal states. Current default
list queries hide `expired` items and include the other statuses unless a
specific `status` filter is provided.

## API

All inbox API routes require an interactive user JWT. Service-account tokens are
rejected because service-account sessions have no user inbox.

Base path: `/v1/inbox`

### List Items

```http
GET /v1/inbox?status=pending&kind=action_required&session_id=<uuid>&limit=50&cursor=<cursor>
```

Query parameters:

| Parameter | Description |
|---|---|
| `status` | Optional exact status filter. Without it, expired items are hidden. |
| `kind` | Optional exact kind filter. |
| `session_id` | Optional session UUID filter. |
| `cursor` | Optional cursor returned by the previous page. |
| `limit` | Page size, 1-200. Defaults to 50. |

Response:

```json
{
  "items": [
    {
      "id": 123,
      "org_id": "0d6d...",
      "user_id": "db61...",
      "session_id": "7f52...",
      "source_event_id": 456,
      "kind": "action_required",
      "status": "pending",
      "title": "Browser action required",
      "body": "Open the browser session and complete sign-in.",
      "payload": {
        "action_type": "browser",
        "target": "browser",
        "instructions": "Open the browser session and complete sign-in.",
        "context": "The browser is showing a login page.",
        "reason": "browser_login"
      },
      "action_ref": {
        "type": "open_session",
        "session_id": "7f52...",
        "target": "browser",
        "completion_endpoint": "/v1/inbox/{item_id}/respond"
      },
      "created_at": "2026-05-11T12:00:00+00:00",
      "updated_at": "2026-05-11T12:00:00+00:00",
      "read_at": null,
      "responded_at": null
    }
  ],
  "next_cursor": null
}
```

### Get One Item

```http
GET /v1/inbox/{item_id}
```

Returns `404` if the item does not belong to the authenticated user.

### Mark Read

```http
POST /v1/inbox/{item_id}/read
```

Sets `read_at` if it is not already set. The operation is idempotent.

### Acknowledge

```http
POST /v1/inbox/{item_id}/ack
```

Only `task_complete` and `progress_checkin` items are acknowledgeable. The
route returns `409` for other kinds or invalid terminal-state transitions.

### Respond

```http
POST /v1/inbox/{item_id}/respond
Content-Type: application/json
```

For governance decisions:

```json
{ "decision": "approve" }
```

or:

```json
{ "decision": "reject" }
```

For action-required completion:

```json
{ "completed": true }
```

Responding emits a `USER_MESSAGE` event, transitions the inbox item to
`responded`, and wakes the session.

### Delete

```http
DELETE /v1/inbox/{item_id}
```

Returns `204`. This is a soft delete: the row remains for audit/event linkage,
but its status becomes `expired`, so it disappears from default list views.

### Live Stream

```http
GET /v1/inbox/stream
```

The stream uses Server-Sent Events. It first sends a `snapshot` event:

```json
{ "unread_ids": [123, 124] }
```

Then it sends `item` events for new inbox rows:

```json
{ "item_id": 125, "kind": "input_required" }
```

Clients should fetch `/v1/inbox/{item_id}` after an `item` event.

## Shared React SDK

The shared inbox UI lives in `sdk/agent-chat-react`.

Main component and hook:

- `InboxPanel`
- `useInboxUnreadCount`

Adapter methods used by the inbox UI:

| Method | Required by `InboxPanel` | Purpose |
|---|---:|---|
| `listInbox` | yes | Load paginated inbox items. |
| `getInboxItem` | yes | Refresh or fetch one item. |
| `markInboxItemRead` | yes | Mark selected items read. |
| `acknowledgeInboxItem` | yes | Acknowledge informational items. |
| `deleteInboxItem` | no | Show the Delete button when present. |
| `respondGovernanceInboxItem` | yes | Approve or reject governance gates. |
| `respondActionRequiredInboxItem` | no | Enable `I completed this` for action-required items. |
| `openInboxStream` | yes | Subscribe to live inbox nudges. |

Every inbox detail view shows `Open session` and, when supported by the
adapter, `Delete`. Kind-specific actions are rendered in the same row.

## Operational Notes

- Inbox rows are per-user and per-org; API lookups always include the current
  user ID.
- The inbox is a workflow view, not the source of truth. The source event log
  remains authoritative.
- Redis streaming is best-effort. Clients should tolerate missed nudges by
  refreshing the list.
- Deleting an item does not answer the agent. For blocking work, use `Submit`,
  `I completed this`, or `Approve` / `Reject` as appropriate.
