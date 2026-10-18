"""office_read's and office_edit's descriptions: what every chat with the office tools has before its first call.

They lead with what a model most often gets wrong, in this order: documents are made and changed only with these tools;
one complete call per document type to copy; how many words fill a page; and checking with office_read before
answering. The ops' summaries come after.
"""

import json
import re
from pathlib import Path

from surogates.office.ops_input import OP_NAMES, OP_SHAPES
from surogates.tools.builtin.office import EDIT_DESCRIPTION, READ_DESCRIPTION

_EXAMPLE = re.compile(r"```json\n(.*?)\n```", re.DOTALL)
# runtime-office's suite compiles this copy of the Word call through the Word checks.
_WORD_CALL = Path(__file__).parents[1] / "runtime-office" / "test" / "edit-description-word.json"


def _first_sentence(text: str) -> str:
    return re.match(r"(.*?\.)(?:\s|$)", text, re.DOTALL).group(1)


def _examples() -> list[dict]:
    return [json.loads(block) for block in _EXAMPLE.findall(EDIT_DESCRIPTION)]


def test_office_edit_opens_with_documents_made_and_changed_only_with_it_new_ones_too():
    first = _first_sentence(EDIT_DESCRIPTION)
    assert "only with office_edit" in first, first
    assert "new ones too" in first, first
    for named in ("script", "python-docx", "openpyxl", "python-pptx", "pypdf"):
        assert named in first, named


def test_office_edit_shows_one_complete_call_per_document_type():
    calls = _examples()
    assert [call["path"].rsplit(".", 1)[-1] for call in calls] == list(OP_NAMES)
    for kind, call in zip(OP_NAMES, calls):
        assert isinstance(call["ops"], list) and call["ops"], kind
        for op in call["ops"]:
            assert op["op"] in OP_NAMES[kind], (kind, op)
            # Only keys the op takes: a model copies the example as it is.
            assert set(op) - {"op"} <= set(json.loads(OP_SHAPES[kind][op["op"]])), (kind, op)


def test_office_edit_writes_a_word_outlines_pending_sections_in_the_markdown():
    # A pending section is a line of the markdown: the outline needs no nested "blocks" map to close.
    word = _examples()[0]["ops"][0]["content"]
    assert word["markdown"].count("<?pending ") >= 2, word
    assert "blocks" not in word, word
    assert '"type": "pending"' not in EDIT_DESCRIPTION
    # Each section says its length, as the templates' do.
    intents = re.findall(r"<\?pending [a-z0-9_-]+: ([^?]+)\?>", word["markdown"])
    assert len(intents) == word["markdown"].count("<?pending ")
    for intent in intents:
        assert re.search(r" — \d+ words$", intent), intent
        # A model fills what an intent asks for: a date the user may not have given is asked for only if given.
        assert "date" not in intent or "if given" in intent, intent


def test_office_edit_sends_a_new_document_to_its_template():
    # For the chats that never open the office skill.
    assert "a new document starts from the office skill's templates/<type>.md" in EDIT_DESCRIPTION


def test_office_edit_shows_a_slide_in_the_templates_look():
    # A model copies the example's colours and sizes into the deck: they are the templates' palette and type scale.
    templates = (Path(__file__).parents[1] / "skills/productivity/office/templates/pptx.md").read_text()
    (deck,) = [call for call in _examples() if call["path"].endswith(".pptx")]
    html = deck["ops"][0]["slides"][0]["html"]
    colours = set(re.findall(r"#[0-9A-Fa-f]{6}\b", html))
    assert colours and colours <= set(re.findall(r"#[0-9A-Fa-f]{6}\b", templates)), colours
    assert {int(px) for px in re.findall(r"font-size:\s*(\d+)px", html)} <= {128, 64, 44, 32, 24}


def test_office_edit_shows_the_word_call_runtime_office_compiles():
    assert _EXAMPLE.findall(EDIT_DESCRIPTION)[0] == _WORD_CALL.read_text().strip()


def test_office_edit_says_how_many_words_fill_a_page_and_to_check_before_answering():
    assert "450 words" in EDIT_DESCRIPTION
    assert "office_read before you answer" in EDIT_DESCRIPTION


def test_office_edit_says_it_in_order_before_the_ops():
    made_only = EDIT_DESCRIPTION.index("only with office_edit")
    first_example = EDIT_DESCRIPTION.index("```json")
    page = EDIT_DESCRIPTION.index("450 words")
    check = EDIT_DESCRIPTION.index("office_read before you answer")
    ops = EDIT_DESCRIPTION.index("Word: insert")
    assert made_only < first_example < page < check < ops


def test_office_edit_leaves_the_slide_reference_to_the_first_call():
    # The first office call on a deck carries the slide reference: no call of the model's own fetches it.
    assert "skill_view" not in EDIT_DESCRIPTION


def test_office_read_is_how_you_check_your_edits():
    assert "check your edits" in READ_DESCRIPTION


def test_office_read_says_what_the_first_call_brings():
    assert "rules, reference and templates, as guide" in READ_DESCRIPTION


def test_the_descriptions_stay_short():
    # Both go with every request of every chat that has the office tools.
    assert len(READ_DESCRIPTION) + len(EDIT_DESCRIPTION) < 7_000
