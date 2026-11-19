"""Which OneDrive/SharePoint changes in a skill-optimisation test chat may run for real.

Ops makes a scratch copy of the chosen folder and records its drive, root and
every item id.  A change runs for real only if its action is in FILE_ACTIONS
and every reference in its arguments points inside that copy.  Anything else,
including a key or id-shaped value this module does not know, is faked.

Name keys (``name``, ``file_name``, ``file.name``) must be one plain path segment:
no ``/``, ``\\`` or ``%``, not empty, not dots only.  Harmless keys (never treated as
references, whatever they hold): values, address, worksheet_id_or_name,
conflict_behavior, mimetype, ``file.mimetype``, content, select, top, orderby, plus
the upload source ``file.s3key`` / ``source_url``.
Any other key holding a non-empty string is a reason to fake (it may be an
id or path under a name we do not know); numbers and booleans pass.

STATE_TOOLS: builtin tools that write durable platform state (or reach people) outside the
chat.  In a test chat they return ``fake_result`` and record ``would_have``; their handler
never runs.  Unclear effects are included (fail closed).  ``STATE_TOOLS_WHY`` says, per
tool, what state it writes.  Every other builtin is in CHAT_LOCAL_OR_READ_TOOLS with why it
is left real; a test fails while a registered builtin is in neither list.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

FILE_ACTIONS = frozenset({
    "ONE_DRIVE_MOVE_ITEM",  # rename only: the destination parent must be in the copy
    "ONE_DRIVE_ONEDRIVE_UPLOAD_FILE",
    "ONE_DRIVE_UPDATE_WORKBOOK_RANGE",
    "ONE_DRIVE_DELETE_ITEM",
    "ONE_DRIVE_ONEDRIVE_CREATE_FOLDER",
    "SHARE_POINT_UPDATE_DRIVE_ITEM",
})
STATE_TOOLS_WHY: dict[str, str] = {
    "memory": "saves/replaces/removes agent or user memory entries",
    "skill_manage": "creates, edits, deletes skills and their files",
    "research_memory": "writes the shared tenant evidence bank (.research/); unclear, included",
    "research_outline": "writes the shared tenant outline (.research/); unclear, included",
    "cron_create": "creates a schedule or routine",
    "cron_update": "changes a schedule or routine",
    "cron_delete": "removes a schedule or routine",
    "cron_pause": "switches a schedule off",
    "cron_resume": "switches a schedule on",
    "cron_run_now": "fires a schedule now (starts a real run)",
    "followup_enroll": "enrols a person in a follow-up protocol",
    "followup_update": "changes a person's follow-up record",
    "followup_withdraw_consent": "records a person's consent withdrawal",
    "escalate": "raises an alert to operators and the team",
    "team_message_person": "sends a message to an enrolled person",
    "team_update_person": "changes an enrolled person's record",
    "team_enroll_person": "enrols a person",
    "team_escalation": "writes/changes an escalation",
    "team_tell_member": "sends a message to a team member",
    "send_form": "sends a WhatsApp Flow to the person",
    "github": "GitHub REST proxy; can write (method is in the args), faked whole",
    "browser_click": "acts on a web page (may submit forms, may use a saved login)",
    "browser_type": "acts on a web page (may submit forms, may use a saved login)",
    "browser_press_key": "acts on a web page (may submit forms, may use a saved login)",
    "browser_upload_file": "uploads a file on a web page",
    "browser_evaluate": "runs script in a web page",
    "browser_drag": "drags on a web page",
    "run_coding_agent": "checks out repos and may open PRs",
    "start_case": "starts a Keel case",
    "complete_item": "reports a Keel work item",
    "return_item": "reports a Keel work item",
    "complete_step": "reports a Keel step",
    "raise_hand": "escalates a Keel step",
    "step_outcome": "closes a journey step",
    "case_draft_form": "drafts into a person's case form",
    "process_draft_form": "drafts into a person's process form",
    "draft_save_file": "saves a file in a Keel process draft",
    "draft_save_scenario": "saves a scenario in a Keel process draft",
    "draft_process_save": "saves a process draft",
    "draft_process_edit": "edits a process draft",
    "draft_dry_run": "runs a Keel draft (effect unclear); included",
}
STATE_TOOLS = frozenset(STATE_TOOLS_WHY)
# Every other builtin, reviewed as a read or as acting only inside the chat, and left real.
# A test builds the full builtin registry and fails until a new tool is put in one list.
CHAT_LOCAL_OR_READ_WHY: dict[str, str] = {
    "agent": "starts a sub-agent of this chat (it inherits test mode)",
    "send_message": "messages this chat's own sub-agent",
    "stop_agent": "stops this chat's own sub-agent or monitor",
    "list_agents": "lists this chat's sub-agents",
    "monitor": "watches a command in the chat's own workspace",
    "consult_expert": "asks an expert; its loop withholds STATE_TOOLS in a test chat",
    "ask_user_question": "asks the person in this chat",
    "todo": "the chat's own task list",
    "load_tools": "offers held-back tools to this chat",
    "skill_step": "records a procedure step as a chat event",
    "skill_view": "reads a skill",
    "skills_list": "reads the skill list",
    "loop_wait": "pauses this chat's own loop",
    "loop_complete": "ends this chat's own loop",
    "terminal": "runs in the chat's own sandbox workspace",
    "process": "manages the chat's own sandbox processes",
    "read_file": "reads the chat's workspace",
    "write_file": "writes the chat's own workspace",
    "patch": "edits the chat's own workspace",
    "list_files": "reads the chat's workspace",
    "search_files": "reads the chat's workspace",
    "office_read": "reads a document in the chat's workspace",
    "office_edit": "edits a document in the chat's own workspace",
    "create_artifact": "creates a file in the chat's own workspace",
    "whiteboard_draw": "draws on this chat's canvas",
    "generate_image": "creates media in the chat's workspace",
    "generate_video": "creates media in the chat's workspace",
    "vision_analyze": "reads an image",
    "web_search": "reads the web",
    "web_extract": "reads a web page",
    "browser_navigate": "opens a page (a GET; reviewed and left real)",
    "browser_get_state": "reads the open page",
    "browser_screenshot": "reads the open page",
    "browser_scroll": "scrolls the open page",
    "browser_wait": "waits on the open page",
    "browser_close": "closes this chat's browser",
    "end_call": "ends this chat's own phone call",
    "share_note": "posts to this chat's context-group board (reviewed and left real)",
    "read_board": "reads the coordination board",
    "expand_note": "reads a board note",
    "mate_ambient_post": "posts only from an ambient turn, gated by the session's channel (reviewed and left real)",
    "kb_list_pages": "reads the knowledge base",
    "kb_read_page": "reads the knowledge base",
    "kb_search_pages": "reads the knowledge base",
    "session_search": "reads past sessions",
    "user_reports": "reads operator reports",
    "fetch_channel_file": "reads a shared channel file",
    "fetch_channel_messages": "reads channel messages",
    "cron_get": "reads a schedule",
    "cron_list": "reads schedules",
    "followup_contacts": "reads follow-up contacts",
    "followup_status": "reads a follow-up record",
    "team_overview": "reads the team",
    "case_read": "reads a Keel case",
    "case_history": "reads a Keel case",
    "case_files": "reads a Keel case",
    "case_file_read": "reads a Keel case file",
    "process_read": "reads a Keel process",
    "process_history": "reads a Keel process",
    "process_waits": "reads a Keel process",
    "process_move_check": "checks a Keel move without making it",
    "draft_files": "reads a Keel draft",
    "draft_read": "reads a Keel draft",
    "draft_main": "reads a Keel draft",
    "draft_process_read": "reads a Keel draft process",
    "draft_process_check": "checks a Keel draft process",
    "draft_process_reference": "reads Keel's reference",
}
CHAT_LOCAL_OR_READ_TOOLS = frozenset(CHAT_LOCAL_OR_READ_WHY)
_DRIVE_KEYS = frozenset({"drive_id", "driveId", "parentReference.driveId"})
REFERENCE_KEYS = _DRIVE_KEYS | frozenset({
    "item_id", "itemId", "folder_item_id", "parent_folder", "folder", "folder_path",
    "parentReference.id", "server_relative_url", "site_name",
})
# A name lands in a path ("<folder>/<name>"), so it must be exactly one segment.
_NAME_KEYS = frozenset({"name", "file_name", "file.name"})
_HARMLESS = frozenset({
    "values", "address", "worksheet_id_or_name", "conflict_behavior",
    "mimetype", "content", "select", "top", "orderby", "file.s3key", "file.source_url",
    "file.mimetype", "s3key", "source_url",
})


def _one_segment(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    # NFKC folds lookalikes (U+FF0F to "/", U+2024 to ".", NBSP to space);
    # U+2215/U+2044 have no folding, so they are refused by name.
    v = unicodedata.normalize("NFKC", value)
    return bool(re.sub(r"[\s.]", "", v)) and not any(c in v for c in "/\\%\u2215\u2044")


def _norm(path: str) -> str:
    return "/" + "/".join(p for p in path.split("/") if p)


def _under(path: str, root: str) -> bool:
    # Dot segments (and near-dot ones like ".. " or "..." a server may trim to ".."),
    # percent-encoding and backslashes can climb out of the root: refuse them.
    if "%" in path or "\\" in path or any(seg and not seg.strip(" .") for seg in path.split("/")):
        return False
    path, root = _norm(path), _norm(root)
    return path == root or path.startswith(root + "/")


def _leaves(value: Any, prefix: str = ""):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _leaves(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(value, list):
        for v in value:
            yield from _leaves(v, prefix)
    else:
        yield prefix, value


def _reference_ok(key: str, value: Any, scratch: dict, known: set) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if key in _DRIVE_KEYS:
        return value == scratch.get("drive_id")
    if key == "site_name":
        return False  # no site is on the list; fail closed
    if value in known:
        return True
    root = scratch.get("root_path")
    return "/" in value and bool(root) and _under(value, root)


def inside_scratch(slug: str, args: dict, scratch: dict) -> bool:
    """True only if *slug* is a file action and every reference in *args* is inside the copy."""
    if slug not in FILE_ACTIONS or not isinstance(args, dict) or not scratch:
        return False
    known = {scratch.get("root_id"), *(scratch.get("item_ids") or ())}
    refs: list[tuple[str, str]] = []
    for key, value in _leaves(args):
        if key in REFERENCE_KEYS:
            if not _reference_ok(key, value, scratch, known):
                return False
            refs.append((key, value))
        elif key in _NAME_KEYS:
            if not _one_segment(value):
                return False
        elif key in _HARMLESS:
            continue
        elif isinstance(value, str) and value:
            return False  # an unknown key holding text may be a reference ("targetFolderId": "ORIG")
    targets = [value for key, value in refs if key not in _DRIVE_KEYS]
    # A call that names nothing in the copy would act on a default location.
    # A path resolves in the default drive unless a drive (already checked) is named.
    return bool(targets) and (len(targets) < len(refs) or all(value in known for value in targets))
