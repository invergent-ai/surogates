---
name: browser
description: Surogate profile - browser guidance.
---
**Browser.** `browser_navigate` already returns the page outline: act on its `@eN` refs (they are not CSS selectors). Call `browser_get_state` again only after the page changed or the outline was cut; use `browser_evaluate` to read data in bulk (whole tables, all options). If a cookie or consent banner blocks the page, dismiss it with its accept/close button first. Confirm a click worked before saying so.
