---
name: workspace_rules
description: Workspace rules.
applies_when: session.config.workspace_path is set
---
## Workspace
Only `$HOME` (your working directory) is writable. Give the file tools relative paths (`report.md`, `data/x.csv`): they do not expand `$HOME` or `~`. In terminal commands stay inside `$HOME`; clone with `git clone <url>` (no target path). Never read credential files (`~/.ssh`, `~/.aws`, `~/.kube`).
