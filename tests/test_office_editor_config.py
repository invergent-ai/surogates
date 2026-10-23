"""Which files the editor opens, the signed URLs, and the editor config."""

from __future__ import annotations

import time
from uuid import uuid4

import pytest
from jose import jwt

from surogates.config import OfficeSettings
from surogates.office.editor_config import build_editor_config
from surogates.office.kinds import office_kind
from surogates.office.tokens import (
    InvalidOfficeToken,
    read_document_server_body,
    read_url_token,
    sign,
    url_token,
)

SECRET = "j" * 32
TOKEN_SECRET = "t" * 32


def _settings() -> OfficeSettings:
    return OfficeSettings(
        public_url="https://office.example",
        internal_url="http://docs.internal",
        callback_base_url="https://api.example/",
        jwt_secret=SECRET,
        token_secret=TOKEN_SECRET,
    )


@pytest.mark.parametrize(
    ("path", "file_type", "document_type", "editable"),
    [
        ("a/Report.DOCX", "docx", "word", True),
        ("sheet.xlsx", "xlsx", "cell", True),
        ("deck.pptx", "pptx", "slide", True),
        ("scan.pdf", "pdf", "pdf", True),
        ("old.doc", "doc", "word", False),
        ("old.ods", "ods", "cell", False),
    ],
)
def test_office_kind(path, file_type, document_type, editable):
    kind = office_kind(path)
    assert kind is not None
    assert (kind.file_type, kind.document_type, kind.editable) == (file_type, document_type, editable)


def test_not_office():
    assert office_kind("notes.md") is None
    assert office_kind("Makefile") is None


def test_url_token_round_trip():
    row = uuid4()
    assert read_url_token(TOKEN_SECRET, url_token(TOKEN_SECRET, row, "file"), "file") == row


def test_url_token_purpose_is_checked():
    token = url_token(TOKEN_SECRET, uuid4(), "file")
    with pytest.raises(InvalidOfficeToken):
        read_url_token(TOKEN_SECRET, token, "callback")


def test_file_token_expires():
    stale = jwt.encode({"sub": str(uuid4()), "pur": "file", "iat": 0, "exp": int(time.time()) - 1}, TOKEN_SECRET, algorithm="HS256")
    with pytest.raises(InvalidOfficeToken):
        read_url_token(TOKEN_SECRET, stale, "file")


def test_forged_token_is_refused():
    with pytest.raises(InvalidOfficeToken):
        read_url_token(TOKEN_SECRET, url_token("x" * 32, uuid4(), "callback"), "callback")


def test_document_server_body_in_body_and_in_header():
    body = {"key": "k", "status": 1}
    assert read_document_server_body(SECRET, {"token": sign(SECRET, body)}, None)["status"] == 1
    header = "Bearer " + sign(SECRET, {"payload": body})
    assert read_document_server_body(SECRET, {}, header)["key"] == "k"
    with pytest.raises(InvalidOfficeToken):
        read_document_server_body(SECRET, body, None)


def test_editor_config():
    row = uuid4()
    config = build_editor_config(
        settings=_settings(), row_id=row, doc_key="abc", path="docs/Plan.docx",
        kind=office_kind("docs/Plan.docx"), user_id="user:1", user_name="Flavius", access="edit", theme="dark",
    )
    assert config["document"]["key"] == "abc"
    assert config["document"]["title"] == "Plan.docx"
    assert config["document"]["fileType"] == "docx"
    assert config["documentType"] == "word"
    assert config["document"]["url"].startswith("https://api.example/v1/office/files/")
    assert config["editorConfig"]["callbackUrl"].startswith("https://api.example/v1/office/callback/")
    assert config["editorConfig"]["mode"] == "edit"
    assert config["editorConfig"]["user"] == {"id": "user:1", "name": "Flavius"}
    assert config["editorConfig"]["coEditing"] == {"mode": "fast", "change": False}
    assert config["editorConfig"]["customization"]["uiTheme"] == "theme-dark"
    signed = jwt.decode(config["token"], SECRET, algorithms=["HS256"])
    assert signed["document"] == config["document"]
    file_token = config["document"]["url"].rsplit("/", 1)[1]
    assert read_url_token(TOKEN_SECRET, file_token, "file") == row


def test_view_config_cannot_edit():
    config = build_editor_config(
        settings=_settings(), row_id=uuid4(), doc_key="abc", path="old.doc",
        kind=office_kind("old.doc"), user_id="user:1", user_name="F", access="view", theme="light",
    )
    assert config["editorConfig"]["mode"] == "view"
    assert config["document"]["permissions"]["edit"] is False
    assert config["editorConfig"]["customization"]["uiTheme"] == "theme-light"


def test_comment_access_comments_without_editing():
    def config(access):
        return build_editor_config(
            settings=_settings(), row_id=uuid4(), doc_key="abc", path="pack.docx",
            kind=office_kind("pack.docx"), user_id="user:1", user_name="F", access=access, theme="light",
        )

    commenting = config("comment")
    assert commenting["editorConfig"]["mode"] == "edit"
    assert {key: commenting["document"]["permissions"][key] for key in ("edit", "review", "comment")} == {
        "edit": False, "review": False, "comment": True,
    }
    viewing = config("view")
    assert viewing["editorConfig"]["mode"] == "view"
    assert viewing["document"]["permissions"]["comment"] is False
