"""Catch a turn that ends without the file the user asked for by name.

Two workspace-bench tasks failed this way with the work essentially done.
One produced ``tmp_stage1.docx`` through ``tmp_stage6.docx`` and never
assembled them into the report it was asked for; another read its five
source documents and never wrote anything. Both sessions reported success,
and both scored zero on every rubric.

The check is deliberately narrow. It fires only when the user named a
filename outright -- "write it to report.docx" -- and that exact name
appears nowhere in the turn's tool calls or their results. A name the user
never said is not a deliverable we can hold the model to, and a name that
shows up anywhere in the tool traffic is evidence enough that the file was
handled. Both defaults fail silent, because a wrong nudge costs a turn and
annoys someone whose work was already finished.

No model call: this is string matching over the transcript the loop
already holds.
"""

from __future__ import annotations

import json
import posixpath
import re

from surogates.harness.tool_schemas import CRON_WRITE_TOOLS

#: Extensions worth holding the model to. These are deliverable formats --
#: things a person asks to receive. Source-code extensions are excluded on
#: purpose: "fix the bug in parser.py" names an input, not an output, and
#: the file already exists.
_DELIVERABLE_EXTS = (
    "docx", "doc", "xlsx", "xls", "csv", "pptx", "ppt",
    "pdf", "md", "json", "html", "txt",
)

#: Bounded on both sides: the stem repetition is capped so a pathological
#: input cannot drive quadratic backtracking, and the class excludes the
#: separators that would let one match run across two filenames.
_FILENAME_RE = re.compile(
    r"[A-Za-z0-9_\-.]{1,200}\.(?:" + "|".join(_DELIVERABLE_EXTS) + r")\b",
    re.IGNORECASE,
)


def _tool_traffic(messages: list[dict]) -> str:
    """Every tool call argument and result in the turn, as one blob.

    A file can be produced by ``write_file``, by a script the model wrote,
    or by a shell heredoc -- the one thing common to all of them is that
    the name passes through tool arguments. Results are included too, so
    that a directory listing showing the file also counts as evidence.
    """
    parts: list[str] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "tool":
            content = m.get("content")
            if isinstance(content, str):
                parts.append(content)
        for call in m.get("tool_calls") or []:
            if isinstance(call, dict):
                args = (call.get("function") or {}).get("arguments")
                if isinstance(args, str):
                    parts.append(args)
    return "\n".join(parts).lower()


def missing_deliverables(
    user_text: str, messages: list[dict], limit: int = 5
) -> list[str]:
    """Filenames the user named that the turn never touched.

    Returns at most *limit* names, in the order the user gave them, so a
    request listing many outputs produces a nudge the model can act on
    rather than a wall of filenames.
    """
    if not user_text:
        return []
    traffic = _tool_traffic(messages)
    missing: list[str] = []
    seen: set[str] = set()
    for match in _FILENAME_RE.finditer(user_text):
        name = posixpath.basename(match.group(0))
        key = name.lower()
        # A bare extension ("...the .docx version") is not a filename.
        if key in seen or key.startswith("."):
            continue
        seen.add(key)
        if key not in traffic:
            missing.append(name)
        if len(missing) >= limit:
            break
    return missing


def deliverable_nudge(names: list[str]) -> str:
    """The nudge text naming what is still missing."""
    listed = ", ".join(names)
    return (
        "[System: The request asked for these files by name and they were "
        f"never written: {listed}. Produce them now, with exactly these "
        "names and extensions, then confirm. If one already exists under a "
        "different name, rename or re-save it to the name above.]"
    )


#: A reply telling the person something was saved to memory. Narrow on
#: purpose: "I saved the report" is about a file, and "noted" alone is an
#: acknowledgement, so only phrasings that claim remembering are matched.
_SAVE_CLAIM_RE = re.compile(
    r"\bnoted (?:it|this|that down)\b"
    r"|\bmade a note\b"
    r"|\b(?:i've|i have|i'll|i will)\s+(?:remember|memori[sz]ed?)\b"
    r"|\b(?:saved|stored|added|wrote|written)\b[^.\n]{0,40}\b(?:to|in|into)\s+(?:my\s+)?memory\b"
    r"|\bam\s+(?:notat|re[țţt]inut|memorat)\b"
    r"|\b(?:voi|o\s+s[ăa])\s+(?:re[țţt]ine|[țţt]ine?\s+minte)\b"
    r"|\b(?:salvat|ad[ăa]ugat|stocat|notat)\b[^.\n]{0,40}\b[îi]n\s+memori[ea]\b",
    re.IGNORECASE,
)

#: The harness's own nudges are user messages too; a turn starts at the
#: last message a person wrote.
_HARNESS_NUDGE_PREFIX = "[System:"


def _current_turn(messages: list[dict]) -> list[dict]:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.startswith(_HARNESS_NUDGE_PREFIX):
            continue
        return messages[index + 1:]
    return messages


def _called_successfully(turn: list[dict], names: frozenset[str]) -> bool:
    """Whether the turn made a call of one of *names* that did not report
    ``success: false``."""
    calls = {
        call.get("id")
        for m in turn if isinstance(m, dict)
        for call in m.get("tool_calls") or []
        if isinstance(call, dict)
        and (call.get("function") or {}).get("name") in names
    }
    for m in turn:
        if not (isinstance(m, dict) and m.get("role") == "tool"):
            continue
        if m.get("tool_call_id") not in calls:
            continue
        try:
            result = json.loads(m.get("content") or "")
        except (TypeError, ValueError):
            return True
        if not (isinstance(result, dict) and result.get("success") is False):
            return True
    return False


def unbacked_save_claim(final_text: str, messages: list[dict]) -> bool:
    """The reply says something was saved to memory and this turn made no
    successful memory call. The caller checks the tool is available."""
    if not final_text or not _SAVE_CLAIM_RE.search(final_text.replace("’", "'")):
        return False
    return not _called_successfully(_current_turn(messages), frozenset({"memory"}))


SAVE_CLAIM_NUDGE = (
    "[System: You said you saved this but made no memory call. Call memory "
    "now, or correct your reply.]"
)


#: A reply telling the person a schedule exists or runs.  Narrow as the
#: memory claim is: "I can schedule it" offers, it does not claim.
_SCHEDULE_CLAIM_RE = re.compile(
    r"\b(?:i've|i have|i)\s+(?:now\s+)?(?:scheduled|set up (?:a|the|your) (?:schedule|loop|cron|routine|recurring))\b"
    r"|\b(?:schedule|loop|cron(?: job)?|routine)s?\s+(?:is|are|has been|have been)\s+(?:now\s+)?"
    r"(?:set up|created|running|active|scheduled|live|in place)\b"
    r"|\bscheduled (?:it|this|that|them) (?:to|for)\b"
    r"|\bwill (?:now )?run (?:every|daily|each|hourly)\b"
    r"|\bam programat\b"
    r"|\b(?:bucla|programarea|rutina)\s+(?:este|e)\s+(?:acum\s+)?(?:activ[ăa]|pornit[ăa]|creat[ăa]|setat[ăa])\b"
    r"|\b(?:bucla|programarea|rutina)\s+ruleaz[ăa]\b"
    r"|\b(?:este|e|sunt) (?:acum )?programat[ăae]?\b"
    r"|\bam (?:creat|setat|pornit) (?:o|un|bucla|rutina) ?(?:programare|rutin[ăa]|bucl[ăa]|loop)?\b",
    re.IGNORECASE,
)

#: What backs a claim that a schedule exists: one made or changed, or one
#: looked up, in the same turn.
SCHEDULE_TOOLS = frozenset({
    "cron_create", "cron_update", "cron_resume", "cron_run_now", "cron_list", "cron_get",
})
#: Every tool that makes, changes or reads a schedule.
CRON_TOOLS = SCHEDULE_TOOLS | CRON_WRITE_TOOLS


def unbacked_schedule_claim(final_text: str, messages: list[dict]) -> bool:
    """The reply says a schedule exists or runs and this turn neither made
    one nor looked one up successfully.  A turn whose schedule call was
    refused outright already told the model why; a nudge would only repeat
    it.  A preview awaiting the user's yes (``would``) is not a refusal."""
    if not final_text or not _SCHEDULE_CLAIM_RE.search(final_text.replace("’", "'")):
        return False
    turn = _current_turn(messages)
    return not (_called_successfully(turn, SCHEDULE_TOOLS) or _refused(turn, CRON_TOOLS))


def _refused(turn: list[dict], names: frozenset[str]) -> bool:
    """Whether a call of one of *names* this turn was refused, not previewed."""
    calls = {
        call.get("id")
        for m in turn if isinstance(m, dict)
        for call in m.get("tool_calls") or []
        if isinstance(call, dict) and (call.get("function") or {}).get("name") in names
    }
    for m in turn:
        if isinstance(m, dict) and m.get("role") == "tool" and m.get("tool_call_id") in calls:
            try:
                result = json.loads(m.get("content") or "")
            except (TypeError, ValueError):
                continue
            if isinstance(result, dict) and result.get("success") is False and "would" not in result:
                return True
    return False


def schedule_claim_nudge(can_schedule: bool) -> str:
    if can_schedule:
        return (
            "[System: You said a schedule is set up, but no tool made or showed one this "
            "turn. Make it with cron_create (or check cron_list), or correct your reply.]"
        )
    return (
        "[System: You said a schedule is set up, but you have no tool that makes "
        "schedules here and none was made. Correct your reply: say it is not scheduled.]"
    )
