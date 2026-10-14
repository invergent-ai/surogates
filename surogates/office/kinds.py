"""Which files the document server opens, and whether they can be edited."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True, slots=True)
class OfficeKind:
    file_type: str  # the extension, as the document server names it
    document_type: str  # word, cell, slide or pdf
    editable: bool


# Older formats open read-only: the editor converts them to view them.
_KINDS: dict[str, tuple[str, bool]] = {
    "docx": ("word", True),
    "xlsx": ("cell", True),
    "pptx": ("slide", True),
    "pdf": ("pdf", True),
    "doc": ("word", False),
    "odt": ("word", False),
    "rtf": ("word", False),
    "xls": ("cell", False),
    "ods": ("cell", False),
    "ppt": ("slide", False),
    "odp": ("slide", False),
}


def office_kind(path: str) -> OfficeKind | None:
    extension = PurePosixPath(path).suffix.lower().lstrip(".")
    found = _KINDS.get(extension)
    return OfficeKind(extension, found[0], found[1]) if found else None
