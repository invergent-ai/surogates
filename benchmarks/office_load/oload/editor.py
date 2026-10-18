"""The editor config a load client's page opens a document with, shaped as the harness builds the agent's."""

from __future__ import annotations

from typing import Any

import jwt

from oload.settings import Settings

ALGORITHM = "HS256"

# The kinds the tool opens: the extension, as the document server names it -> its documentType.
KINDS: dict[str, str] = {"docx": "word", "xlsx": "cell", "pptx": "slide", "pdf": "pdf"}


def sign(secret: str, claims: dict[str, Any]) -> str:
    return jwt.encode(claims, secret, algorithm=ALGORITHM)


def editor_config(
    settings: Settings,
    *,
    doc_key: str,
    kind: str,
    user_id: str,
    user_name: str,
    file_url: str,
    callback_url: str,
    plugins: bool = False,
) -> dict[str, Any]:
    """An editing config for *doc_key*, signed into its ``token``.

    *plugins* is off, as on the agent's page; a person's editor has them on.
    """
    try:
        document_type = KINDS[kind]
    except KeyError:
        raise ValueError(f"the tool opens {', '.join(KINDS)}, not {kind!r}") from None
    config: dict[str, Any] = {
        "width": "100%",
        "height": "100%",
        "document": {
            "fileType": kind,
            "key": doc_key,
            "title": f"{doc_key}.{kind}",
            "url": file_url,
            "permissions": {"edit": True, "review": True, "comment": True, "download": True, "print": True},
        },
        "documentType": document_type,
        "editorConfig": {
            "callbackUrl": callback_url,
            "mode": "edit",
            "lang": "en",
            "user": {"id": user_id, "name": user_name},
            "coEditing": {"mode": "fast", "change": False},
            "customization": {
                "forcesave": True,
                "uiTheme": "theme-light",
                **({} if plugins else {"plugins": False}),
            },
        },
    }
    config["token"] = sign(settings.jwt_secret, config)
    return config
