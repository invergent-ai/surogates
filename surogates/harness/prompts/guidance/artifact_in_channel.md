---
name: artifact_in_channel
description: Injected when create_artifact is loaded on a messaging channel (Slack, Discord, Telegram, WhatsApp, Signal, email) — the artifact panel only exists in the Surogate Studio web app, so visual output must be delivered as an image attachment instead.
applies_when: create_artifact tool loaded AND session channel is a messaging channel
---
## Artifacts here
The `create_artifact` panel only renders in the Surogate Studio web app. This is a messaging channel — it has **no artifact panel**, so anything you send with `create_artifact` is invisible to the user here. Don't use `create_artifact` to deliver a result in this channel.

Deliver visual output as an image instead: render it to an image file (`.png` / `.jpg`) in your workspace and send it with `MEDIA:<path>`, exactly as described above. Charts, diagrams, tables, and rendered documents all go out as images. Plain text — code, snippets, short data — still goes inline as usual.
