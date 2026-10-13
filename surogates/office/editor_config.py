"""The editor config the document server opens a document with."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Literal
from uuid import UUID

from surogates.config import OfficeSettings
from surogates.office.kinds import OfficeKind
from surogates.office.tokens import sign, url_token


def build_editor_config(
    *,
    settings: OfficeSettings,
    row_id: UUID,
    doc_key: str,
    path: str,
    kind: OfficeKind,
    user_id: str,
    user_name: str,
    edit: bool,
    theme: Literal["light", "dark"],
) -> dict[str, Any]:
    base = settings.callback_base_url.rstrip("/")
    config: dict[str, Any] = {
        "width": "100%",
        "height": "100%",
        "document": {
            "fileType": kind.file_type,
            "key": doc_key,
            "title": PurePosixPath(path).name,
            "url": f"{base}/v1/office/files/{url_token(settings.token_secret, row_id, 'file')}",
            "permissions": {"edit": edit, "review": edit, "comment": True, "download": True, "print": True},
        },
        "documentType": kind.document_type,
        "editorConfig": {
            "callbackUrl": f"{base}/v1/office/callback/{url_token(settings.token_secret, row_id, 'callback')}",
            "mode": "edit" if edit else "view",
            "lang": "en",
            "user": {"id": user_id, "name": user_name},
            # Strict mode would stop everyone's edits showing live.
            "coEditing": {"mode": "fast", "change": False},
            "customization": {
                "forcesave": True,
                "uiTheme": "theme-dark" if theme == "dark" else "theme-light",
            },
        },
    }
    config["token"] = sign(settings.jwt_secret, config)
    return config
