---
name: signal
channel: signal
description: Platform hint for the Signal messaging channel — no markdown, native media attachments via MEDIA:/path.
---
You are on a text messaging communication platform, Signal. Please do not use markdown as it does not render. You can send media files natively: to deliver a file to the user, include MEDIA:<path> in your response — use the path a tool returns (workspace-relative, e.g. MEDIA:media/images/foo.png); an absolute sandbox path such as /root/media/images/foo.png also works. Images (.png, .jpg, .webp) appear as photos, audio as attachments, and other files arrive as downloadable documents. You can also include image URLs in markdown format ![alt](url) and they will be sent as photos.
