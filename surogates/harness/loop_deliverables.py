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

A name the user gives for something that already exists is an input, not
a deliverable, and is never asked for:

- a name every mention of which marks it as an input, by the words just
  before it in its clause -- "using data.csv", "from the attached
  sales.xlsx", "based on notes.md";
- a file already in the session's workspace (an upload, an attachment, a
  file an earlier turn wrote; the caller passes their names as *present*),
  unless a mention asks for it to be written -- "post the numbers to
  status.md", "update the existing tracker.xlsx";
- a pattern or a template rather than a name -- "one file per issue named
  bulletin_YYYY_MM.txt", "{TICKER}_10K.txt".

Counting only names the user asks to receive by phrasing ("save ... as X")
would miss the cases this check exists for: the prompts that motivated it
name the deliverable without such a verb ("the task requires these files by
name: X").

No model call: this is string matching over the transcript the loop
already holds.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Collection

from surogates.harness.tool_schemas import CRON_WRITE_TOOLS

#: Extensions worth holding the model to. These are deliverable formats --
#: things a person asks to receive. Source-code extensions are excluded on
#: purpose: "fix the bug in parser.py" names an input, not an output, and
#: the file already exists.
_DELIVERABLE_EXTS = (
    "docx", "doc", "xlsx", "xls", "csv", "pptx", "ppt",
    "pdf", "md", "json", "html", "txt",
)

#: Bounded on both sides: the stem repetition is capped and a match starts
#: only where a name can (not inside a run of name characters), so a
#: pathological input cannot drive quadratic backtracking; the class
#: excludes the separators that would let one match run across two
#: filenames.
_FILENAME_RE = re.compile(
    r"(?<![A-Za-z0-9_\-.])[A-Za-z0-9_\-.]{1,200}\.(?:" + "|".join(_DELIVERABLE_EXTS) + r")\b",
    re.IGNORECASE,
)
#: A request naming more files than this is a pasted listing; names past it
#: are not weighed (those already seen still are, at every mention).
_MAX_NAMES = 1000


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


#: How far back from a filename its clause is read, and where a clause
#: ends: a sentence's end, a colon, a semicolon, a comma or a line break.
_CLAUSE_WINDOW = 120
_CLAUSE_END_RE = re.compile(r"[.!?](?=\s)|[;:,\n]")
_ABBREVIATIONS_RE = re.compile(r"\b(e\.g|i\.e)\.", re.IGNORECASE)
_WORD_RE = re.compile(r"[A-Za-z]+")

#: The word just before a filename, in its clause, that makes it an input:
#: "using data.csv", "from the attached sales.xlsx", "read notes.md".
_INPUT_WORDS = frozenset({
    "from", "read", "reading", "open", "opening", "load", "loading", "using",
    "analyse", "analyze", "analysing", "analyzing", "reviewing", "parse", "parsing",
    "given", "check", "checking", "consult", "eg", "ie",
})
#: Two words that do: "based on plan.md", "according to notes.md".
_INPUT_PAIRS = frozenset({("based", "on"), ("according", "to"), ("look", "at")})
#: The word that makes it a target, even when a file of that name exists:
#: "post the numbers to status.md", "update the existing tracker.xlsx".
_TARGET_WORDS = frozenset({
    "to", "into", "as", "save", "saving", "write", "writing", "create", "creating",
    "produce", "producing", "generate", "generating", "update", "updating", "edit",
    "editing", "overwrite", "export", "exporting", "append", "output", "deliver",
})
_TARGET_PAIRS = frozenset({("fill", "in"), ("fill", "out")})
#: Words passed over on the way back to the word that tells.
_FILLER_WORDS = frozenset({
    "the", "a", "an", "this", "that", "these", "those", "our", "my", "your", "its", "their",
    "file", "files", "document", "documents", "doc", "spreadsheet", "sheet", "workbook",
    "template", "named", "called", "existing", "new", "final",
    "attached", "uploaded", "provided",
})
#: Passed over, these still mark the file as one the user gave.
_GIVEN_WORDS = frozenset({"attached", "uploaded", "provided"})

_INPUT, _TARGET = "input", "target"

#: A filename pattern, not a name.  A year placeholder in any case
#: ("bulletin_YYYY.txt", "app_yyyymmdd.json"); two upper-case placeholder
#: segments in a row ("log_MM_DD.txt"); or one that is never a real name's
#: ("chapter_N.md", "report_DATE.pdf").  One segment such as ``MM`` alone
#: ("Q3_MM_final.xlsx") may be a real name, and counts.
_PLACEHOLDER = r"(?:YYYY|YY|MM|DD|HH|X{2,}|N{2,})"
_PLACEHOLDER_RE = re.compile(
    r"(?:^|[_\-.])(?:(?i:yyyy(?:mm(?:dd)?)?|yymmdd)|YY(?=[_\-.]" + _PLACEHOLDER + r"))(?=[_\-.]|$)"
    r"|(?:^|[_\-.])" + _PLACEHOLDER + r"[_\-.]" + _PLACEHOLDER + r"(?=[_\-.]|$)"
    r"|(?:^|[_\-.])(?:N|X|DATE|TIMESTAMP)(?=[_\-.]|$)"
)
#: Just before a match, these make it a piece of a template ("{TICKER}_10K.txt",
#: "<id>-notes.md", "report_*.md"), not a name.
_TEMPLATE_MARKS = frozenset("{}<>[]*$%")


def _role(user_text: str, start: int) -> str | None:
    """Whether the words just before a filename, in its clause, make it an
    input or a target; None when they say neither."""
    window = _ABBREVIATIONS_RE.sub(lambda m: m.group(1).replace(".", ""), user_text[max(0, start - _CLAUSE_WINDOW):start])
    words = [w.lower() for w in _WORD_RE.findall(_CLAUSE_END_RE.split(window)[-1])]
    given = False
    while words and words[-1] in _FILLER_WORDS:
        given = given or words[-1] in _GIVEN_WORDS
        words.pop()
    last = words[-1] if words else ""
    pair = (words[-2], last) if len(words) > 1 else ("", last)
    if pair in _INPUT_PAIRS:
        return _INPUT
    if last in _TARGET_WORDS or pair in _TARGET_PAIRS:
        return _TARGET
    if last in _INPUT_WORDS or given:
        return _INPUT
    return None


def missing_deliverables(
    user_text: str, messages: list[dict], limit: int = 5, present: Collection[str] = (),
) -> list[str]:
    """Filenames the user named that the turn never touched.

    Names that are inputs are left out (module docstring).  A name counts as
    an input when every mention of it reads as one, or when *present* (the
    lower-cased names of the files already in the session's workspace) holds
    it and no mention asks for it to be written.  Returns at most *limit*
    names, in the order the user gave them, so a request listing many outputs
    produces a nudge the model can act on rather than a wall of filenames.
    """
    if not user_text:
        return []
    roles: dict[str, set[str | None]] = {}
    names: dict[str, str] = {}
    for match in _FILENAME_RE.finditer(user_text):
        name = posixpath.basename(match.group(0))
        key = name.lower()
        start = match.start()
        # A bare extension ("...the .docx version") is not a filename, and
        # a piece of a template ("{TICKER}_10K.txt") is not one either.
        if key.startswith(".") or (start and user_text[start - 1] in _TEMPLATE_MARKS):
            continue
        if _PLACEHOLDER_RE.search(name.rsplit(".", 1)[0]):
            continue
        if key not in names:
            if len(names) >= _MAX_NAMES:
                continue
            names[key] = name
        said = roles.setdefault(key, set())
        if len(said) < 3:  # input, target and neither: nothing more to learn
            said.add(_role(user_text, start))
    if not names:
        return []
    traffic = _tool_traffic(messages)
    touched = {posixpath.basename(m.group(0)) for m in _FILENAME_RE.finditer(traffic)}
    missing: list[str] = []
    for key, name in names.items():
        said = roles[key]
        if said == {_INPUT} or (key in present and _TARGET not in said):
            continue
        if key in touched or key in traffic:
            continue
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
    # "am notat" alone, or of "this", claims remembering, as "noted it" does;
    # "am notat că…" / "am notat comanda" reports what the agent recorded.
    r"|\bam\s+notat(?=\s*[,.!]|\s*$|\s+(?:asta|acest\s+lucru)\b)"
    r"|\bam\s+(?:re[țţt]inut|memorat)\b"
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
