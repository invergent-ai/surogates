---
name: workspace_rules
description: Mandatory workspace/sandbox rules injected into the Context section whenever the session has a workspace_path. Describes $HOME-relative paths, blocked directories, and credential read-avoidance.
applies_when: session.config.workspace_path is set
---
## Workspace Rules (MANDATORY)
Your working directory is `$HOME`. The filesystem is sandboxed — ALL writes outside `$HOME` are blocked and will fail with `Read-only file system`.

**You MUST follow these rules for every command and file operation:**
1. ALWAYS work in `$HOME`. Never `cd` to `/tmp`, `/home`, or any absolute path.
2. Clone repos with `git clone <url>` (clones into `$HOME/<repo>`) — NEVER specify an absolute target path.
3. Use relative paths for all file tools (`read_file`, `write_file`, `list_files`, `search_files`, `patch`). Path arguments are taken **literally** — no shell expansion. `$HOME/foo.py`, `${HOME}/foo.py`, and `~user/foo.py` are NOT expanded; they create or look up directories with those exact names. Use plain relative paths like `foo.py` or `subdir/foo.py`.
4. In terminal commands, use relative paths or `$HOME`: `cd surogate && ls` NOT `cd /home/user/surogate`. Inside a shell command `$HOME` is fine because the shell expands it; the file tools above do not.
5. `/tmp`, `/etc`, `/home`, `/var` are all read-only. Do not try to write there.
6. Never read `~/.ssh`, `~/.aws`, `~/.kube`, or credential files.

Commands that violate these rules will fail. Do not retry with a different absolute path — use a relative path instead.
