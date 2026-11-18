"""Which OneDrive/SharePoint changes in a skill-optimisation test chat may run for real.

Ops makes a scratch copy of the chosen folder and records its drive, root and
every item id.  A change runs for real only if its action is in FILE_ACTIONS
and every reference in its arguments points inside that copy.  Anything else,
including a key or id-shaped value this module does not know, is faked.

Harmless keys (never treated as references, whatever they hold): name, values,
address, worksheet_id_or_name, conflict_behavior, file_name, mimetype, content,
select, top, orderby, plus the upload source ``file.s3key`` / ``source_url``.
Any other key holding a non-empty string is a reason to fake (it may be an
id or path under a name we do not know); numbers and booleans pass.

STATE_TOOLS: builtin tools that write durable platform state (or reach people) outside the
chat.  In a test chat they return ``fake_result`` and record ``would_have``; their handler
never runs.  Unclear effects are included (fail closed).  One line per tool:

  memory                    saves/replaces/removes agent or user memory entries
  skill_manage              creates, edits, deletes skills and their files
  research_memory           writes the shared tenant evidence bank (.research/); unclear, included
  research_outline          writes the shared tenant outline (.research/); unclear, included
  cron_create/update/delete creates, changes, removes a schedule or routine
  cron_pause/resume         switches a schedule off or on
  cron_run_now              fires a schedule now (starts a real run)
  followup_enroll           enrols a person in a follow-up protocol
  followup_update           changes a person's follow-up record
  followup_withdraw_consent records a person's consent withdrawal
  escalate                  raises an alert to operators and the team
  team_message_person       sends a message to an enrolled person
  team_update_person        changes an enrolled person's record
  team_enroll_person        enrols a person
  team_escalation           writes/changes an escalation
  team_tell_member          sends a message to a team member
  send_form                 sends a WhatsApp Flow to the person
  browser_click/type/press_key  acts on a web page (may submit forms, may use a saved login)
  browser_upload_file/drag      uploads a file / drags on a web page
  browser_evaluate          runs script in a web page
  github                    GitHub REST proxy; can write (method is in the args), faked whole
  run_coding_agent          checks out repos and may open PRs
  start_case                starts a Keel case
  complete_item/return_item reports a Keel work item
  complete_step/raise_hand  reports or escalates a Keel step
  step_outcome              closes a journey step
  case_draft_form           drafts into a person's case form
  process_draft_form        drafts into a person's process form
  draft_save_file           saves a file in a Keel process draft
  draft_save_scenario       saves a scenario in a Keel process draft
  draft_process_save        saves a process draft
  draft_process_edit        edits a process draft
  draft_dry_run             runs a Keel draft (effect unclear); included
"""

from __future__ import annotations

from typing import Any

FILE_ACTIONS = frozenset({
    "ONE_DRIVE_MOVE_ITEM",  # rename only: the destination parent must be in the copy
    "ONE_DRIVE_ONEDRIVE_UPLOAD_FILE",
    "ONE_DRIVE_UPDATE_WORKBOOK_RANGE",
    "ONE_DRIVE_DELETE_ITEM",
    "ONE_DRIVE_ONEDRIVE_CREATE_FOLDER",
    "SHARE_POINT_UPDATE_DRIVE_ITEM",
})
STATE_TOOLS = frozenset({
    "memory", "skill_manage", "research_memory", "research_outline",
    "cron_create", "cron_update", "cron_delete", "cron_pause", "cron_resume", "cron_run_now",
    "followup_enroll", "followup_update", "followup_withdraw_consent", "escalate",
    "team_message_person", "team_update_person", "team_enroll_person", "team_escalation", "team_tell_member",
    "send_form", "github",
    "browser_click", "browser_type", "browser_press_key", "browser_upload_file", "browser_evaluate", "browser_drag", "run_coding_agent",
    "start_case", "complete_item", "return_item", "complete_step", "raise_hand", "step_outcome",
    "case_draft_form", "process_draft_form",
    "draft_save_file", "draft_save_scenario", "draft_process_save", "draft_process_edit", "draft_dry_run",
})
_DRIVE_KEYS = frozenset({"drive_id", "driveId", "parentReference.driveId"})
REFERENCE_KEYS = _DRIVE_KEYS | frozenset({
    "item_id", "itemId", "folder_item_id", "parent_folder", "folder", "folder_path",
    "parentReference.id", "server_relative_url", "site_name",
})
_HARMLESS = frozenset({
    "name", "values", "address", "worksheet_id_or_name", "conflict_behavior", "file_name",
    "mimetype", "content", "select", "top", "orderby", "file.s3key", "file.source_url",
    "s3key", "source_url",
})


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


def _reference_ok(key: str, value: Any, scratch: dict) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if key in _DRIVE_KEYS:
        return value == scratch.get("drive_id")
    if key == "site_name":
        return False  # no site is on the list; fail closed
    if value == scratch.get("root_id") or value in (scratch.get("item_ids") or ()):
        return True
    root = scratch.get("root_path")
    return "/" in value and bool(root) and _under(value, root)


def inside_scratch(slug: str, args: dict, scratch: dict) -> bool:
    """True only if *slug* is a file action and every reference in *args* is inside the copy."""
    if slug not in FILE_ACTIONS or not isinstance(args, dict) or not scratch:
        return False
    targets = 0
    by_path = has_drive = False
    for key, value in _leaves(args):
        if key in REFERENCE_KEYS:
            if not _reference_ok(key, value, scratch):
                return False
            if key in _DRIVE_KEYS:
                has_drive = True
            else:
                targets += 1
                by_path |= value not in (scratch.get("root_id"), *(scratch.get("item_ids") or ()))
        elif key in _HARMLESS:
            continue
        elif isinstance(value, str) and value:
            return False  # an unknown key holding text may be a reference ("targetFolderId": "ORIG")
    # A call that names nothing in the copy would act on a default location.
    # A path resolves in the default drive unless a drive (already checked) is named.
    return targets > 0 and (has_drive or not by_path)
