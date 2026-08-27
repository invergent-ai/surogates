"""The canvas geometry note injected for a whiteboard turn, and the
pruning of superseded canvas snapshots from the replay."""
from surogates.harness.loop_context_replay import (
    build_user_message_dict,
    prune_superseded_canvas_images,
)
from surogates.harness.loop_messages import _whiteboard_note_from_metadata


def _meta(**over):
    payload = {
        "sourceRect": {"x": 1000, "y": 2000, "w": 800, "h": 600},
        "imageScale": 0.5,
        "latestInput": {"x": 1200, "y": 2100, "w": 300, "h": 200},
        "hotspots": [[3, 4], [3, 5]],
        "canvasSize": 20000,
        "mode": "sketch",
    }
    payload.update(over)
    return {"whiteboard": payload}


# --- the note ---------------------------------------------------------

def test_returns_none_without_whiteboard_metadata():
    assert _whiteboard_note_from_metadata(None) is None
    assert _whiteboard_note_from_metadata({"view_context": {}}) is None
    assert _whiteboard_note_from_metadata("nonsense") is None


def test_renders_the_source_rect_and_scale():
    note = _whiteboard_note_from_metadata(_meta())
    assert "1000" in note and "2000" in note
    assert "0.5" in note


def test_renders_the_latest_input_rect():
    note = _whiteboard_note_from_metadata(_meta())
    assert "latestInput" in note
    assert "1200" in note


def test_renders_hotspots_when_present():
    assert "hotspot" in _whiteboard_note_from_metadata(_meta()).lower()


def test_omits_hotspots_when_empty():
    note = _whiteboard_note_from_metadata(_meta(hotspots=[]))
    assert "hotspot" not in note.lower()


def test_renders_a_selection_when_present():
    note = _whiteboard_note_from_metadata(
        _meta(selection={"x": 5, "y": 6, "w": 7, "h": 8}),
    )
    assert "selection" in note.lower()


def test_omits_selection_when_absent():
    assert "selection" not in _whiteboard_note_from_metadata(_meta()).lower()


def test_renders_typed_input_as_transcription_ground_truth():
    note = _whiteboard_note_from_metadata(_meta(typedInput="integral of x^2"))
    assert "integral of x^2" in note


def test_tolerates_a_malformed_rect():
    # Client-supplied data: a malformed block must degrade to a shorter
    # note, never raise into the turn.
    note = _whiteboard_note_from_metadata(_meta(sourceRect="nope"))
    assert note is None or isinstance(note, str)


def test_survives_every_field_being_absent():
    note = _whiteboard_note_from_metadata({"whiteboard": {}})
    assert note is None or isinstance(note, str)


def test_the_note_reaches_the_replayed_user_message():
    # The note is only useful if build_user_message_dict folds it in.
    msg = build_user_message_dict({
        "content": "what is this",
        "metadata": _meta(),
    })
    content = msg["content"]
    text = content if isinstance(content, str) else "".join(
        part.get("text", "") for part in content if isinstance(part, dict)
    )
    assert "sourceRect" in text
    assert "what is this" in text


# --- replay pruning ---------------------------------------------------

def _turn(n):
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": f"turn {n}"},
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,AAA{n}"}},
        ],
    }


def _images(messages):
    return [
        part
        for message in messages
        for part in (
            message["content"] if isinstance(message["content"], list) else []
        )
        if isinstance(part, dict) and part.get("type") == "image_url"
    ]


def test_replay_keeps_only_the_newest_canvas_image():
    """Canvas snapshots are cumulative: snapshot N contains everything
    N-1 did, so replaying all of them is pure waste and would dominate
    context within a dozen turns."""
    pruned = prune_superseded_canvas_images([_turn(1), _turn(2), _turn(3)])
    images = _images(pruned)
    assert len(images) == 1
    assert "AAA3" in images[0]["image_url"]["url"]


def test_replay_leaves_a_single_canvas_image_alone():
    messages = [_turn(1)]
    assert prune_superseded_canvas_images(messages) == messages


def test_replay_leaves_an_image_free_history_alone():
    messages = [{"role": "user", "content": "plain text"}]
    assert prune_superseded_canvas_images(messages) == messages


def test_pruning_preserves_the_surrounding_text():
    pruned = prune_superseded_canvas_images([_turn(1), _turn(2)])
    texts = [
        part["text"]
        for message in pruned
        for part in message["content"]
        if part.get("type") == "text"
    ]
    assert "turn 1" in texts and "turn 2" in texts


def test_pruning_does_not_mutate_the_input():
    messages = [_turn(1), _turn(2)]
    prune_superseded_canvas_images(messages)
    assert len(_images(messages)) == 2
