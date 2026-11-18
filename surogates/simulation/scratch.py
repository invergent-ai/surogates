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
    # Dot segments, percent-encoding and backslashes can climb out of the root: refuse them.
    if "%" in path or "\\" in path or {".", ".."} & set(path.split("/")):
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
