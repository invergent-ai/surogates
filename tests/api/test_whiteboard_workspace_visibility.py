"""The canvas document is writable by its client but hidden from the tree.

Blocked and hidden used to be the same flag. The whiteboard needs them
apart: the browser client is the canvas document's sole writer, so
blocking it would break the feature -- but showing a canvas.json in the
file browser invites a delete that destroys the user's ink, which the
event-log tail cannot rebuild (it only carries the agent's objects).
"""
import pytest
from fastapi import HTTPException

from surogates.api.routes.workspace import (
    _is_hidden,
    _is_reserved,
    _validate_path,
)


def test_artifacts_stay_blocked():
    assert _is_reserved("_artifacts/abc/v1.json") is True
    with pytest.raises(HTTPException):
        _validate_path("_artifacts/abc/v1.json")


def test_artifacts_stay_hidden():
    assert _is_hidden("_artifacts/abc/v1.json") is True


def test_the_canvas_document_is_writable():
    # No exception: the client must be able to PUT its own canvas.
    _validate_path("_whiteboard/canvas.json")


def test_the_canvas_document_is_hidden_from_the_tree():
    assert _is_hidden("_whiteboard/canvas.json") is True


def test_the_canvas_document_is_not_reserved():
    assert _is_reserved("_whiteboard/canvas.json") is False


def test_ordinary_files_are_visible_and_writable():
    assert _is_hidden("notes.md") is False
    assert _is_reserved("notes.md") is False
    _validate_path("notes.md")


def test_path_traversal_is_still_rejected():
    with pytest.raises(HTTPException):
        _validate_path("../etc/passwd")


def test_absolute_paths_are_still_rejected():
    with pytest.raises(HTTPException):
        _validate_path("/etc/passwd")
