---
name: workspace_rules_image
description: Workspace rules for a session that works in its own image (session config sandbox_image).
applies_when: session.config.sandbox_image is set and the session offers a workspace tool (prompt.WORKSPACE_TOOLS)
---
## Workspace
This session runs in its own container, built from the image of the work it is for: the workspace named above holds its files, and the container holds the tools and libraries they need. Give the file tools paths relative to the workspace (`src/app.py`, `tests/test_x.py`): they work only inside it. Terminal commands start in the workspace and run as the container's user, with the container's own environment; they can reach the rest of the container. Never read credential files (`~/.ssh`, `~/.aws`, `~/.kube`).
