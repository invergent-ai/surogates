---
name: workspace_rules
description: Surogate profile - workspace rules.
---
## Workspace
Only `$HOME` (your working directory) is writable. Give the file tools relative paths (`report.md`, `data/x.csv`): they do not expand `$HOME` or `~`. In terminal commands stay inside `$HOME`; clone with `git clone <url>` (no target path). Never read credential files (`~/.ssh`, `~/.aws`, `~/.kube`).
