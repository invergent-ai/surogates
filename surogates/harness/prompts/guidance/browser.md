---
name: browser
description: Injected when browser tools are available; gives browser interaction hygiene.
applies_when: any browser_* tool loaded
---
# Browser Interaction

Use `browser_get_state` before interacting with a page whenever you need a
target ref, and refresh refs after navigation, scrolling, modal dismissal, or
any large page change.

## Cookie and consent banners

Before clicking any user-requested button or link, check whether a cookie,
privacy, consent, newsletter, location, age-gate, or similar banner/dialog is
blocking the UI. If it is blocking interaction, accept or dismiss it first using
the clearest available safe action such as "Accept", "Accept all", "OK",
"Agree", "Continue", or a close button. Then refresh page state and continue
with the user's requested click.

Consent actions may be marked with `intent: accept_consent` in
`browser_get_state`; click those before other page controls when a banner is
blocking the page. `@eN` refs are action targets, not CSS selectors; do not pass
them as `selector` values.

Do not open consent settings or customize preferences unless the user asks for
that. Do not claim a user-requested click succeeded until the blocking banner is
gone or you have verified that the intended page action happened.
