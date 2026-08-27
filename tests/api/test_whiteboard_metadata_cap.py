"""metadata.whiteboard is client-supplied and lands in the event log, so
it carries a hard size cap."""
import pytest
from pydantic import ValidationError

from surogates.api.routes.sessions import (
    _MAX_WHITEBOARD_METADATA_BYTES,
    SendMessageRequest,
)


def test_accepts_a_small_whiteboard_block():
    req = SendMessageRequest(
        content="what is this",
        metadata={"whiteboard": {"imageScale": 0.5}},
    )
    assert req.metadata["whiteboard"]["imageScale"] == 0.5


def test_rejects_an_oversized_whiteboard_block():
    huge = {"whiteboard": {"pad": "x" * (_MAX_WHITEBOARD_METADATA_BYTES + 1)}}
    with pytest.raises(ValidationError) as exc:
        SendMessageRequest(content="hi", metadata=huge)
    assert "whiteboard" in str(exc.value)


def test_leaves_other_metadata_keys_alone():
    # The cap is scoped to the whiteboard block; view_context and other
    # keys keep their existing (uncapped) behaviour.
    req = SendMessageRequest(
        content="hi",
        metadata={"view_context": {"page": "agents"}},
    )
    assert req.metadata["view_context"]["page"] == "agents"


def test_accepts_metadata_without_a_whiteboard_block():
    assert SendMessageRequest(content="hi", metadata={}).metadata == {}


def test_accepts_no_metadata_at_all():
    assert SendMessageRequest(content="hi").metadata is None


def test_rejects_a_non_dict_whiteboard_block():
    with pytest.raises(ValidationError):
        SendMessageRequest(content="hi", metadata={"whiteboard": "nope"})


def test_accepts_a_realistic_atlas_payload():
    # A real turn carries geometry plus a 64-cell hotspot grid; it must
    # sit comfortably under the cap.
    payload = {
        "sourceRect": {"x": 1000, "y": 2000, "w": 1600, "h": 1200},
        "imageScale": 0.5,
        "latestInput": {"x": 1200, "y": 2100, "w": 300, "h": 200},
        "hotspots": [[i % 8, i // 8] for i in range(64)],
        "viewport": {"w": 1440, "h": 900},
        "canvasSize": 20000,
        "mode": "sketch",
    }
    req = SendMessageRequest(content="", metadata={"whiteboard": payload})
    assert req.metadata["whiteboard"]["mode"] == "sketch"
