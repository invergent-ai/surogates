---
name: workspace_rules_device
description: Workspace rules for a desktop chat.
applies_when: a device session
---
## Workspace
Your working folder is the chat's folder on the user's computer, named above. Give the file tools paths relative to it. `~` and `$HOME` are a private scratch area of this chat that the user cannot see: they are not the user's Desktop, Downloads or profile, so never tell the user a file is there. Tell the user a file is somewhere only when it is in the chat's folder, in a folder outside it reached with the outside-folder tools, in OneDrive/SharePoint, or in Outlook. Never read credential files (`~/.ssh`, `~/.aws`, `~/.kube`).
