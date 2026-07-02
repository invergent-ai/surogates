"""Pure helpers to check out a repo into the coding sandbox workspace.

No side effects: callers run the returned script through the sandbox executor.
The PAT is NEVER inlined — the credential helper reads ``$GH_TOKEN`` at runtime.
"""
from __future__ import annotations

import re
import shlex
import time


def repo_dir_name(repo_url: str) -> str:
    tail = repo_url.rstrip("/").rsplit("/", 1)[-1]
    return tail[:-4] if tail.endswith(".git") else tail


def clone_url(repo_url: str) -> str:
    if repo_url.startswith("https://github.com/"):
        return repo_url
    if repo_url.startswith("git@github.com:"):
        return "https://github.com/" + repo_url[len("git@github.com:"):]
    if repo_url.startswith("ssh://git@github.com/"):
        return "https://github.com/" + repo_url[len("ssh://git@github.com/"):]
    raise ValueError("repo_url must be a GitHub URL")


def fix_branch_name(prompt: str, *, now: float) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower()).strip("-")[:40] or "change"
    ts = time.strftime("%Y%m%d%H%M%S", time.gmtime(now))
    return f"fix/{slug}-{ts}"


def git_auth_env(git_pat: str) -> dict[str, str]:
    return {
        "GH_TOKEN": git_pat,
        "GIT_TERMINAL_PROMPT": "0",
        "GH_PROMPT_DISABLED": "1",
    }


def clone_script(
    *, repo_url: str, default_branch: str, dest: str, branch: str,
) -> str:
    q_url = shlex.quote(clone_url(repo_url))
    q_branch = shlex.quote(default_branch)
    q_dest = shlex.quote(dest)
    q_fix_branch = shlex.quote(branch)
    # credential.helper echoes the token from $GH_TOKEN (never in argv/logs).
    # Use ``-c`` for the initial clone because the terminal sandbox forces
    # GIT_CONFIG_GLOBAL=/dev/null under SRT; write the helper repo-locally
    # after clone so later ``git push`` commands also use $GH_TOKEN.
    helper = "'!f(){ echo username=x-access-token; echo password=$GH_TOKEN; };f'"
    return (
        f"set -e; "
        f"rm -rf -- {q_dest}; "
        f"git -c credential.helper={helper} "
        f"clone --depth 1 --branch {q_branch} {q_url} {q_dest}; "
        f"cd {q_dest} && git config credential.helper {helper} && "
        f"git checkout -b {q_fix_branch} && "
        f"git config user.name 'surogate-agent' && "
        f"git config user.email 'agent@surogate.ai'"
    )
