"""Human-facing chat text for the /code command (pure string builders)."""

from __future__ import annotations

from surogates.coding_agents.command import PROVIDER_TO_AGENT

_SETUP = {
    "claude": (
        "On your machine, run `claude setup-token` (needs a Claude Pro/Max "
        "plan), then paste the token in **Settings → Coding Agents**."
    ),
    "codex": (
        "On your machine, run `codex login` (needs a ChatGPT plan), then paste "
        "the contents of `~/.codex/auth.json` in **Settings → Coding Agents**."
    ),
}


def render_help() -> str:
    return (
        "**Coding agents** — run Claude Code or Codex on your workspace using "
        "your own plan.\n\n"
        "- `/code claude \"<task>\"` — run Claude Code\n"
        "- `/code codex \"<task>\"` — run Codex\n"
        "- `/code login <claude|codex>` — connect your plan\n"
        "- `/code logout <claude|codex>` — disconnect\n"
        "- `/code status` — show what's connected\n\n"
        "Flags: `--model`, `--effort low|medium|high|xhigh`, `--allow read-only`."
    )


def render_login_instructions(agent: str) -> str:
    return _SETUP.get(agent, _SETUP["claude"])


def render_status(statuses: list[dict]) -> str:
    lines = ["**Coding agent connections**", ""]
    for status in statuses:
        agent = PROVIDER_TO_AGENT.get(status["provider"], status["provider"])
        if status.get("connected"):
            mode = status.get("auth_mode") or "?"
            lines.append(f"- **{agent}** — connected ({mode})")
        else:
            lines.append(f"- **{agent}** — not connected (`/code login {agent}`)")
    return "\n".join(lines)


def render_connect_first(agent: str) -> str:
    return (
        f"You haven't connected {agent} yet. Run `/code login {agent}` to "
        "connect your plan, then try again."
    )
