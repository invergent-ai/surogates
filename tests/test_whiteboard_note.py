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

def _turn(n, note=True):
    """One replayed turn.

    ``note`` mirrors what really distinguishes the two kinds of image in
    a history: a canvas render always ships with the geometry note, an
    image the user dragged in never does.
    """
    from surogates.whiteboard.session import CANVAS_NOTE_HEADER

    text = f"{CANVAS_NOTE_HEADER}\nturn {n}" if note else f"turn {n}"
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": text},
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


def test_replay_leaves_images_that_are_not_canvas_renders_alone():
    # The board is a view mode, so a session that drew can also hold
    # images the user uploaded. Nothing supersedes those.
    messages = [_turn(1, note=False), _turn(2, note=False)]
    assert prune_superseded_canvas_images(messages) == messages


def test_pruning_preserves_the_surrounding_text():
    pruned = prune_superseded_canvas_images([_turn(1), _turn(2)])
    texts = [
        part["text"]
        for message in pruned
        for part in message["content"]
        if part.get("type") == "text"
    ]
    blob = "\n".join(texts)
    assert "turn 1" in blob and "turn 2" in blob


def test_pruning_does_not_mutate_the_input():
    messages = [_turn(1), _turn(2)]
    prune_superseded_canvas_images(messages)
    assert len(_images(messages)) == 2


# --- how big the user writes ------------------------------------------

def test_renders_the_handwriting_scale():
    """Without it the model sizes its answer blind.

    On a real board of ~250-unit digits it chose fontSize 90, and the
    answer landed a quarter the size of the sum it belonged to.
    """
    note = _whiteboard_note_from_metadata(_meta(inkHeight=252))
    assert "252 canvas units" in note
    assert "fontSize" in note


def test_omits_the_handwriting_scale_when_there_is_no_ink():
    # A board holding only agent objects has nothing to measure; an
    # invented number would be worse than silence.
    note = _whiteboard_note_from_metadata(_meta())
    assert "canvas units" not in note


def test_ignores_a_nonsense_handwriting_scale():
    for bad in (0, -5, "tall", None, True):
        note = _whiteboard_note_from_metadata(_meta(inkHeight=bad))
        assert "canvas units" not in (note or "")


# --- what is already on the board -------------------------------------

def test_renders_the_occupancy_grid():
    """The model cannot get this from the transcript.

    Its own draw calls record where it *asked* for things, and the user's
    drags, resizes and deletions are never recorded at all -- so history
    is not merely incomplete about the board, it is out of date.
    """
    note = _whiteboard_note_from_metadata(
        _meta(occupied=[[0, 3], [1, 3]], occupancyGrid=16),
    )
    assert "16x16 grid" in note
    assert "[[0, 3], [1, 3]]" in note
    # The grid is drawn on the image too, and must not be read as ink.
    assert "not something the user drew" in note


def test_the_grid_is_a_constraint_not_a_placement_rule():
    """Told to "place new objects in free cells", the model shopped.

    On a real board it wrote the answer to an integral below the working
    rather than after the "=" -- both cells were free, and it picked the
    one the grid mentioned over the one the content called for.
    """
    note = _whiteboard_note_from_metadata(
        _meta(occupied=[[0, 3]], occupancyGrid=16),
    )
    assert "where the content calls for it" in note
    assert "only to keep off" in note


def test_omits_occupancy_on_an_empty_board():
    assert "grid over sourceRect" not in _whiteboard_note_from_metadata(_meta())


def test_ignores_occupancy_without_a_grid_size():
    # The cells are meaningless without the divisor.
    note = _whiteboard_note_from_metadata(_meta(occupied=[[0, 3]]))
    assert "grid over sourceRect" not in note


def test_says_the_image_is_a_crop_of_a_larger_board():
    # Unsaid, the empty margin reads as free canvas and work lands on
    # objects sitting just outside the frame.
    note = _whiteboard_note_from_metadata(_meta(beyond=["right", "below"]))
    assert "continues right, below" in note
    assert "crop" in note


def test_omits_the_crop_note_when_the_frame_holds_everything():
    assert "continues" not in _whiteboard_note_from_metadata(_meta())


def test_gives_the_cell_to_canvas_mapping():
    """Inverting the image formula by hand went wrong once per axis.

    Asked for the slot after an "x =", the model converted the column
    and left the row in image coordinates, landing its answer below the
    frame it had been shown -- row 17 of a 16-row grid.
    """
    note = _whiteboard_note_from_metadata(
        _meta(occupied=[[0, 3]], occupancyGrid=16,
              cellSize={"w": 82.0, "h": 53.18}),
    )
    assert "col*82.0" in note
    assert "row*53.18" in note
    assert "never a position measured off the image" in note


def test_omits_the_mapping_without_a_cell_size():
    note = _whiteboard_note_from_metadata(
        _meta(occupied=[[0, 3]], occupancyGrid=16),
    )
    assert "sourceRect.x + col" not in note


def test_ignores_a_malformed_cell_size():
    for bad in ({"w": "wide", "h": 10}, {"w": True, "h": 10}, {"w": 10}, 7):
        note = _whiteboard_note_from_metadata(
            _meta(occupied=[[0, 3]], occupancyGrid=16, cellSize=bad),
        )
        assert "sourceRect.x + col" not in (note or "")


# --- what became of the agent's own work ------------------------------

def test_reports_where_its_objects_sit_now():
    note = _whiteboard_note_from_metadata(_meta(agentObjects=[
        {"origin": "c1", "label": "e^x + C", "x": 480, "y": 390,
         "w": 300, "h": 88},
    ]))
    # The call id rides beside the label: it is the handle for
    # anchor/replaces, and without it in the note the model cannot
    # reference its own objects.
    assert '"e^x + C" (call c1) is at (480, 390), 300x88' in note


def test_reports_a_deleted_object():
    note = _whiteboard_note_from_metadata(_meta(agentObjects=[
        {"origin": "c1", "label": "gone", "removed": True},
    ]))
    assert "no longer on the board" in note


def test_reports_ink_drawn_around_one_of_its_objects():
    """The real case: an answer wrapped in brackets and squared.

    The object never moved -- what it means changed -- so a report of
    only what moved or vanished would have said nothing at all.
    """
    note = _whiteboard_note_from_metadata(_meta(agentObjects=[
        {"origin": "c1", "label": "e^x + C", "x": 480, "y": 390,
         "w": 300, "h": 88, "touched": True},
    ]))
    assert "drawn on or around it" in note
    assert "not as separate work beside it" in note


def test_omits_the_inventory_when_it_has_drawn_nothing():
    assert "as the board holds it now" not in _whiteboard_note_from_metadata(
        _meta(),
    )


def test_skips_a_malformed_inventory_entry():
    note = _whiteboard_note_from_metadata(_meta(agentObjects=[
        {"origin": "c1", "label": "bad", "x": "left", "y": 1, "w": 2, "h": 3},
        {"origin": "c2", "label": "good", "x": 1, "y": 2, "w": 3, "h": 4},
    ]))
    assert "bad" not in note
    assert "good" in note


def test_tolerates_a_nonsense_inventory():
    for bad in ("nope", [1, 2], [{"origin": "c1"}]):
        note = _whiteboard_note_from_metadata(_meta(agentObjects=bad))
        assert note is None or isinstance(note, str)


# --- labelled marks -----------------------------------------------------

def _marks():
    return [
        {"id": "A1", "kind": "ink", "x": 0, "y": 0, "w": 300, "h": 60},
        {"id": "A2", "kind": "ink", "x": 0, "y": 200, "w": 120, "h": 60,
         "fresh": True},
        {"id": "B1", "kind": "agent", "origin": "toolu_01A",
         "label": "e^x + C", "x": 340, "y": 0, "w": 200, "h": 70,
         "touched": True},
        {"id": "B2", "kind": "agent", "origin": "toolu_01B",
         "label": "gone", "removed": True},
    ]


def test_renders_every_mark_with_its_label():
    note = _whiteboard_note_from_metadata(_meta(marks=_marks()))
    assert "- A1: the user's ink at (0, 0), 300x60" in note
    assert '- B1 (call toolu_01A): "e^x + C" at (340, 0), 200x70' in note
    assert '- B2 (call toolu_01B): "gone" -- no longer on the board' in note


def test_says_which_marks_are_new_this_turn():
    # The newest ink is the question; naming it saves the model reading
    # the hotspot trail to find it.
    note = _whiteboard_note_from_metadata(_meta(marks=_marks()))
    assert "What the user just wrote is A2." in note
    assert "A2: the user's ink at (0, 200), 120x60 -- NEW" in note


def test_keeps_the_touched_warning_on_marks():
    note = _whiteboard_note_from_metadata(_meta(marks=_marks()))
    assert "drawn on or around it" in note


def test_tells_the_model_to_anchor_by_label():
    note = _whiteboard_note_from_metadata(_meta(marks=_marks()))
    assert "anchor:'A2'" in note
    assert "replaces:'B1'" in note


def test_marks_supersede_the_older_inventory():
    # Both present (never in practice): render marks once, not the
    # inventory again underneath.
    note = _whiteboard_note_from_metadata(_meta(
        marks=_marks(),
        agentObjects=[{"origin": "c9", "label": "dup", "x": 1, "y": 1,
                       "w": 1, "h": 1}],
    ))
    assert "dup" not in note


def test_falls_back_to_the_inventory_for_old_events():
    # Sessions recorded before marks existed still replay their notes.
    note = _whiteboard_note_from_metadata(_meta(
        agentObjects=[{"origin": "c1", "label": "old", "x": 1, "y": 2,
                       "w": 3, "h": 4}],
    ))
    assert '"old" (call c1) is at (1, 2)' in note


def test_skips_a_malformed_mark():
    note = _whiteboard_note_from_metadata(_meta(marks=[
        {"id": "A1", "kind": "ink", "x": "left"},
        "nonsense",
        {"kind": "ink", "x": 0, "y": 0, "w": 1, "h": 1},
        {"id": "A9", "kind": "ink", "x": 5, "y": 5, "w": 5, "h": 5},
    ]))
    assert "A9" in note
    assert "A1" not in note


# --- readings on marks --------------------------------------------------

def test_renders_a_stored_reading_on_its_mark():
    note = _whiteboard_note_from_metadata(_meta(marks=[
        {"id": "A1", "kind": "ink", "x": 0, "y": 0, "w": 300, "h": 60,
         "reading": "2x + 1 = 7", "readBy": "agent"},
    ]))
    assert 'A1: the user\'s ink at (0, 0), 300x60 -- reads: "2x + 1 = 7" (your earlier reading)' in note


def test_marks_a_user_confirmed_reading_as_such():
    note = _whiteboard_note_from_metadata(_meta(marks=[
        {"id": "A1", "kind": "ink", "x": 0, "y": 0, "w": 300, "h": 60,
         "reading": "x = 3", "readBy": "user"},
    ]))
    assert "(confirmed by the user)" in note


def test_flags_ink_without_a_reading_as_unread():
    note = _whiteboard_note_from_metadata(_meta(marks=[
        {"id": "A1", "kind": "ink", "x": 0, "y": 0, "w": 300, "h": 60},
    ]))
    assert "A1: the user's ink at (0, 0), 300x60 -- unread" in note


def test_tells_the_model_to_trust_readings_and_transcribe_the_rest():
    note = _whiteboard_note_from_metadata(_meta(marks=[
        {"id": "A1", "kind": "ink", "x": 0, "y": 0, "w": 300, "h": 60},
    ]))
    assert "do not re-read its pixels" in note
    assert "readings array" in note


# --- close-ups ----------------------------------------------------------

def test_points_the_model_at_the_close_up_for_reading():
    note = _whiteboard_note_from_metadata(_meta(
        crops=[{"mark": "A2", "imageIndex": 1, "scale": 1.83}],
    ))
    assert "image 2 is A2 close up at 1.83x" in note
    assert "not from the overview" in note


def test_omits_the_close_up_line_without_crops():
    assert "close up" not in _whiteboard_note_from_metadata(_meta())


def test_replay_keeps_every_image_of_the_newest_canvas_turn():
    """A canvas turn is the overview plus close-ups of the new ink.

    Pruning by image part kept only the last part -- the crop -- and
    dropped the overview of the very turn being answered.
    """
    from surogates.whiteboard.session import CANVAS_NOTE_HEADER

    def two_image_turn(n):
        return {
            "role": "user",
            "content": [
                {"type": "text", "text": f"{CANVAS_NOTE_HEADER}\\nturn {n}"},
                {"type": "image_url", "image_url": {"url": f"data:over{n}"}},
                {"type": "image_url", "image_url": {"url": f"data:crop{n}"}},
            ],
        }

    pruned = prune_superseded_canvas_images([two_image_turn(1), two_image_turn(2)])
    urls = [img["image_url"]["url"] for img in _images(pruned)]
    assert urls == ["data:over2", "data:crop2"]
