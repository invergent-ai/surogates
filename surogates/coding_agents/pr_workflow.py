"""Prompt augmentation + PR-URL extraction for repo coding runs."""
from __future__ import annotations

import re

_PR_RE = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+")


def augment_prompt(user_prompt: str, *, branch: str, default_branch: str) -> str:
    """Append explicit commit/push/PR instructions to the user's ask."""
    return (
        f"{user_prompt}\n\n"
        f"You are on a fresh branch `{branch}` of a checked-out git repo "
        f"(cwd is the repo root). When the change is complete: run the relevant "
        f"tests, commit with a conventional commit message, push the branch "
        f"(`git push -u origin {branch}`), and open a pull request with "
        f"`gh pr create --fill --base {default_branch} --head {branch}`. "
        f"Do not run `gh auth login`; GH_TOKEN is already available. Print the "
        f"resulting PR URL on its own line in the final message."
    )


def parse_pr_url(cli_output: str | None) -> str | None:
    """Return the first ``.../pull/<n>`` GitHub URL in the output, if any."""
    m = _PR_RE.search(cli_output or "")
    return m.group(0) if m else None
