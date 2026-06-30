"""Synthetic prompt for an ambient review tick.

Injected as a synthetic user message (not from a real teammate).  Frames the
agent's job: review the channel's recalled context, decide whether anything
warrants a proactive post, and act ONLY through mate_ambient_post.
"""

from __future__ import annotations


def build_ambient_prompt(*, channel_label: str, task_changes: list[str]) -> str:
    lines = [
        f"[Ambient review of {channel_label}] This is an automated review, "
        "not a message from a teammate. No one is waiting on you.",
        "",
        "Using your recalled channel memory, recent activity, and your connected "
        "tools, decide whether anything genuinely warrants a proactive message "
        "right now — for example:",
        "- a thread you're involved in went quiet with an open question,",
        "- a delegated task changed state (done / blocked) worth reporting,",
        "- something relevant surfaced that the channel should know.",
        "",
        "Default to doing NOTHING. Only post when it clearly adds value and is "
        "worth interrupting the channel for.",
        "",
        "To post, call the mate_ambient_post tool with a clear message, the "
        "target_thread (or '' for the channel), and an honest 0-1 confidence. "
        "Low-confidence or over-budget posts are automatically suppressed, so "
        "do not pad — if nothing is worth saying, end your turn without posting.",
    ]
    if task_changes:
        lines += ["", "Recent delegated-task changes:"]
        lines += [f"- {c}" for c in task_changes]
    return "\n".join(lines)
