"""The editor config the document server opens a document with."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Literal
from uuid import UUID

from surogates.config import OfficeSettings
from surogates.office.kinds import OfficeKind
from surogates.office.tokens import sign, url_token

# What each access lets its person do: edit, review tracked changes, and comment. The editors take comments only in
# edit mode, so a reader who may comment opens in edit mode without editing.
_PERMISSIONS: dict[str, dict[str, bool]] = {
    "edit": {"edit": True, "review": True, "comment": True},
    "comment": {"edit": False, "review": False, "comment": True},
    "view": {"edit": False, "review": False, "comment": False},
    "read": {"edit": False, "review": False, "comment": True},
}
# The accesses whose editor opens in view mode.
_VIEW_MODE = ("view", "read")


def build_editor_config(
    *,
    settings: OfficeSettings,
    row_id: UUID,
    doc_key: str,
    path: str,
    kind: OfficeKind,
    user_id: str,
    user_name: str,
    access: Literal["edit", "comment", "view", "read"],
    theme: Literal["light", "dark"],
    plugins: bool = True,
) -> dict[str, Any]:
    """The signed config of one person's editor on the document, with what *access* lets them do.

    ``edit`` edits, reviews and comments; ``comment`` only comments; ``view`` only reads.
    ``read`` only reads, keeping the comment permission a view-only open has always sent.
    """
    base = settings.callback_base_url.rstrip("/")
    config: dict[str, Any] = {
        "width": "100%",
        "height": "100%",
        "document": {
            "fileType": kind.file_type,
            "key": doc_key,
            "title": PurePosixPath(path).name,
            "url": f"{base}/v1/office/files/{url_token(settings.token_secret, row_id, 'file')}",
            "permissions": {**_PERMISSIONS[access], "download": True, "print": True},
        },
        "documentType": kind.document_type,
        "editorConfig": {
            "callbackUrl": f"{base}/v1/office/callback/{url_token(settings.token_secret, row_id, 'callback')}",
            "mode": "view" if access in _VIEW_MODE else "edit",
            "lang": "en",
            "user": {"id": user_id, "name": user_name},
            # Strict mode would stop everyone's edits showing live.
            "coEditing": {"mode": "fast", "change": False},
            "customization": {
                "forcesave": True,
                "uiTheme": "theme-dark" if theme == "dark" else "theme-light",
                **({} if plugins else {"plugins": False}),
            },
        },
    }
    config["token"] = sign(settings.jwt_secret, config)
    return config
