"""The office skill loads as a built-in, gated on office_edit; the file skills point to it and hide where it is offered,
and the pdf skill leaves what office_edit does to a PDF to pdf-forms."""

import json
import re
from pathlib import Path

from surogates.tools.loader import ResourceLoader

SKILLS = Path(__file__).parents[1] / "skills"

# How each file skill's description starts: with the office skill, when the agent can edit live.
OFFICE_FIRST = {
    "docx": "When the office_edit tool is available and the document is in the workspace, use the office skill instead: ",
    "xlsx": "When the office_edit tool is available and the spreadsheet is a .xlsx in the workspace, use the office skill instead: ",
    "pptx": "When the office_edit tool is available and the deck is a .pptx in the workspace, use the office skill instead: ",
}
#: What office_edit does to a PDF, the pdf-forms skill does with scripts where office_edit is not offered.
PDF_FORMS_FIRST = (
    "Fill in a PDF form, annotate or redact a PDF, or add, remove or rotate its pages, when the office_edit tool is "
    "not available. "
)
#: pdf-forms' own files: the form-filling guide and the scripts it runs.
PDF_FORMS_FILES = ("FORMS.md", "LICENSE.txt") + tuple(
    f"scripts/{script}.py"
    for script in (
        "check_fillable_fields", "extract_form_field_info", "extract_form_structure", "fill_fillable_fields",
        "fill_pdf_form_with_annotations", "check_bounding_boxes", "create_validation_image", "convert_pdf_to_images",
    )
)


def test_the_office_skill_is_a_built_in_gated_on_office_edit():
    skills = {s.name: s for s in ResourceLoader()._load_skills_from_dir(str(SKILLS), "platform")}
    office = skills["office"]
    assert "office_edit" in office.requires_tools
    for reference in ("docx.md", "xlsx.md", "pptx.md", "pdf.md"):
        assert (SKILLS / "productivity/office/references" / reference).exists()


def test_the_office_skill_names_no_skill_its_chats_cannot_open():
    # Offered only where office_edit is, where the docx, xlsx and pptx skills are hidden: its index line and its rules
    # send the agent to none of them, and leave the pdf skill its work on a new file.
    office = {s.name: s for s in ResourceLoader()._load_skills_from_dir(str(SKILLS), "platform")}["office"]
    for text in (office.description, office.content):
        assert not re.search(r"\b(docx|xlsx|pptx)(,? (docx|xlsx|pptx|and|or))* skills?\b", text, re.IGNORECASE), text
    assert "sandbox skill" not in office.content
    assert "The pdf skill's work (merging, splitting, encrypting) writes a new file, never over an open one." in " ".join(
        office.content.split()
    )


def test_guides_stay_small():
    # The first office call on each type carries the skill's rules, that type's reference and its templates, whole.
    office = SKILLS / "productivity/office"
    rules = re.sub(r"\A---\n.*?\n---\n", "", (office / "SKILL.md").read_text(), count=1, flags=re.DOTALL).strip()
    assert rules.startswith("# Working on documents live with the user")
    for kind in ("docx", "xlsx", "pptx", "pdf"):
        reference = (office / "references" / f"{kind}.md").read_text().strip()
        templates = office / "templates" / f"{kind}.md"
        size = len(rules) + len(reference) + (len(templates.read_text().strip()) if templates.exists() else 0)
        assert size < 40_000, (kind, size)


def test_word_templates_state_lengths():
    # A new document starts from its template; a small model writes a section to the length its intent states.
    templates = (SKILLS / "productivity/office/templates/docx.md").read_text()
    assert len(templates) <= 12_000
    for heading in ("Report", "Letter", "Meeting minutes", "Proposal", "FAQ", "How-to guide"):
        assert re.search(rf"^## {heading}$", templates, re.MULTILINE), heading
    intents = re.findall(r"<\?pending [a-z0-9_-]+: ([^?]+)\?>", templates)
    assert intents and len(intents) == templates.count("<?pending"), "an intent reads as an instruction, not a question"
    for intent in intents:
        assert re.search(r"\d+ words", intent), intent
        # The editor keeps an intent's first 300 characters: its length must not be cut off.
        assert len(intent) <= 300, intent


def test_sheet_templates_total_by_formula():
    # A small model types a number where a total belongs unless the template's totals are formulas over their rows.
    templates = (SKILLS / "productivity/office/templates/xlsx.md").read_text()
    assert len(templates) <= 8_000
    sections = dict(re.findall(r"^## ([^\n]+)\n(.*?)(?=^## |\Z)", templates, re.MULTILINE | re.DOTALL))
    assert list(sections) == ["Budget", "Invoice", "Tracker"]
    calls = {}
    for name, section in sections.items():
        (calls[name],) = [json.loads(block) for block in re.findall(r"```json\n(.*?)```", section, re.DOTALL)]
    assert any(op["op"] == "chart" for op in calls["Budget"]["ops"])
    for name, call in calls.items():
        cells = {}
        for op in call["ops"]:
            if op["op"] == "set":
                column, row = re.fullmatch(r"\w+!([A-Z])(\d+)", op["range"]).groups()
                for i, values in enumerate(op["values"]):
                    cells.update({(chr(ord(column) + j), int(row) + i): value for j, value in enumerate(values)})
        totals = [v for v in cells.values() if isinstance(v, str) and v.startswith("=SUM(")]
        assert totals, name
        # Each SUM runs over a whole list in one column, from the row under its header to its last row; the row under
        # the list is free, so a longer list moves no total, only the SUM's end.
        for column, first, last in re.findall(r"SUM\(([A-Z])(\d+):\1(\d+)\)", " ".join(totals)):
            first, last = int(first), int(last)
            assert not str(cells[(column, first - 1)]).startswith("="), (name, column, first)
            for row in range(first, last + 1):
                value = cells[(column, row)]
                assert isinstance(value, (int, float)) or value.startswith("="), (name, column, row)
            assert cells.get((column, last + 1), "") == "", (name, column, last)


def test_slide_templates_share_one_look():
    # Eight kinds of slide a deck is built from, in one palette and type scale, so slides from them sit in one deck;
    # then decks as orders of them. Small, as the slide guide they join is already the largest.
    templates = (SKILLS / "productivity/office/templates/pptx.md").read_text()
    assert len(templates) <= 14_000
    sections = dict(re.findall(r"^## ([^\n]+)\n(.*?)(?=^## |\Z)", templates, re.MULTILINE | re.DOTALL))
    kinds = ["Title", "Statement", "Big number", "Cards", "Table", "Chart", "Timeline", "Closing"]
    assert list(sections) == [*kinds, "Decks"]
    for kind in kinds:
        (call,) = [json.loads(block) for block in re.findall(r"```json\n(.*?)```", sections[kind], re.DOTALL)]
        assert [op["op"] for op in call["ops"]] == ["slides"], kind
    assert "```" not in sections["Decks"]
    for deck in ("pitch", "onboarding", "review"):
        assert deck in sections["Decks"].lower(), deck
    assert len(set(re.findall(r"#[0-9A-Fa-f]{6}\b", templates))) <= 5, "one palette"
    sizes = {int(size) for size in re.findall(r"font-size:\s*(\d+)px", templates)}
    assert sizes and min(sizes) >= 24 and len(sizes) <= 5, sizes
    for unwanted in ("border-radius", "opacity", "rgba("):
        assert unwanted not in templates, "square corners, and text at full strength"
    # A heading is at most 80 characters, two lines on either canvas; only the Statement's sentence runs longer.
    for kind in kinds:
        for heading in re.findall(r"<h2>(.*?)</h2>", sections[kind]):
            assert kind == "Statement" or len(heading) <= 80, (kind, heading)
    # The decks are orders for a user who named no slides: one who did gets exactly those.
    assert "A user who gives a number of slides or a list of them gets exactly those" in " ".join(
        templates.split("\n## ", 1)[0].split()
    )


OFFICE = SKILLS / "productivity/office"
#: The spreadsheet templates' sample rows: the one exception to specifics coming from the user, said in both places.
SAMPLES = ("The one exception: a spreadsheet template's sample rows, for a user who gave no rows, said in the chat to be "
           "samples.")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _writing_rules() -> str:
    return _flat((OFFICE / "SKILL.md").read_text().split("## Writing rules", 1)[1])


def _check_before_answering() -> str:
    return _flat((OFFICE / "SKILL.md").read_text().split("## Before you answer", 1)[1].split("\n## ", 1)[0])


def _template_headers() -> dict[str, str]:
    return {kind: _flat((OFFICE / "templates" / f"{kind}.md").read_text().split("\n## ", 1)[0])
            for kind in ("docx", "xlsx", "pptx")}


def test_specifics_come_from_the_user_and_are_never_made_up():
    # Templates ask for a document's usual facts; a small model fills each slot unless told what to do without one.
    assert ("**Specifics** (names, dates, times, figures, places, sources, contacts, and what happened: a meeting's "
            "decisions, a study's results) come from the user or their files: one they did not give is left out, never "
            f"made up. {SAMPLES}") in _writing_rules()
    assert ("No specific is made up: no name, date, time, figure, place, source or contact they did not give, and "
            f"nothing said to have happened that their material does not say. {SAMPLES}") in _check_before_answering()
    headers = _template_headers()
    assert ("A specific they did not give (a name, date, time, figure, place, source or contact) is left out, never "
            "made up") in headers["docx"]
    assert ("Any other specific the user did not give (a name, an address, an invoice number, a date, payment terms) "
            "stays empty, never made up.") in headers["xlsx"]
    assert ("a slide short of the user's figures, names, dates or sources comes out shorter, never with made-up "
            "ones.") in headers["pptx"]
    for kind, header in headers.items():
        assert "no kind fits" in header, kind
    # An invoice's payment terms are the user's to give, like its names and numbers.
    invoice = _flat((OFFICE / "templates/xlsx.md").read_text().split("## Invoice", 1)[1].split("\n## ", 1)[0])
    invoice_fill = invoice.split("```", 2)[2]
    assert ("The due date in B7 and the payment line in A12 are filled only from the terms the user gave" in invoice_fill
            and "else both stay empty" in invoice_fill), invoice_fill
    # A deck drops a kind of slide made of specifics the user did not give.
    decks = _flat((OFFICE / "templates/pptx.md").read_text().split("## Decks", 1)[1])
    assert ("A kind made of specifics needs the user's: with no figures, no Big number, no Chart and no Table of "
            "figures; with no names, no Table of who to ask.") in decks


def test_content_asked_for_but_not_detailed_is_drafted_as_a_proposal():
    # Told only never to make anything up, a small model asked the user what a first week looks like instead of drafting
    # it: content is drafted, and only specifics are held back.
    assert ("**Content** the user asked for but did not detail (a first-week plan, the points of a section, the steps "
            "of a process) is drafted from common practice as a proposal, with no specific they did not give; say in "
            "the chat what you drafted, for them to check.") in _writing_rules()
    check = _check_before_answering()
    assert ("Each part they asked for is there: one they did not detail is drafted as a proposal, never left out or "
            "asked about.") in check
    assert "Then say in one line what was done, what you drafted for the user to check, and what is missing." in check
    for kind, header in _template_headers().items():
        assert "asked for but did not detail" in header and "drafted from common practice" in header, kind
        assert "say in the chat what you drafted, for them to check, and what was missing." in header, kind
    assert ("A slide they asked for but did not detail (what the first week looks like, the steps of a plan) is drafted "
            "from common practice as a proposal, with no specific they did not give.") in _template_headers()["pptx"]
    # No section or slide is dropped for want of detail the model can draft.
    word = (OFFICE / "templates/docx.md").read_text()
    for intent in re.findall(r"<\?pending [a-z0-9_-]+: ([^?]+)\?>", word):
        assert "material names" not in intent, intent
    decks = _flat((OFFICE / "templates/pptx.md").read_text().split("## Decks", 1)[1])
    assert "Any other kind is drafted as a proposal when the user gave nothing for it." in decks


def test_the_user_is_asked_only_when_the_work_cannot_be_done_without_the_answer():
    # In a chat nobody answers at once, a needless question stalls the work.
    assert ("**Ask the user** only when the work cannot be done without the answer (which of two documents to change); "
            "otherwise do the work, and say in the chat what is missing or proposed.") in _writing_rules()
    for kind, header in _template_headers().items():
        assert re.search(r"Ask only when the (document|sheet|deck) cannot be (written|made) without the answer", header), kind


def test_the_word_references_examples_say_when_they_are_shorter_than_asked():
    # A model copies an example's length: one far under its section's words says it is shortened.
    reference = (SKILLS / "productivity/office/references/docx.md").read_text()
    asked = {key: int(words) for key, words in re.findall(r"<\?pending ([a-z0-9_-]+): [^?]*?(\d+) words\?>", reference)}
    fills = 0
    for heading, section in re.findall(r"^## ([^\n]+)\n(.*?)(?=^## |\Z)", reference, re.MULTILINE | re.DOTALL):
        for block in re.findall(r"```json\n(.*?)```", section, re.DOTALL):
            # One call per block, or one call or op per line.
            try:
                examples = [json.loads(block)]
            except json.JSONDecodeError:
                examples = [json.loads(line) for line in block.strip().splitlines()]
            for op in (op for example in examples for op in example.get("ops", [example] if "op" in example else [])):
                key = (op.get("target") or {}).get("pending")
                if op["op"] != "replace" or key not in asked:
                    continue
                fills += 1
                words = len(re.findall(r"[A-Za-z0-9%.]+", re.sub(r"<\?block [^?]+\?>", "", op["content"]["markdown"])))
                if words < asked[key] / 2:
                    assert re.search(r"^Shortened here\b", section, re.MULTILINE), (heading, words, asked[key])
    assert fills, "the reference fills a section its outline planted"


def test_each_file_skill_loads_and_sends_the_agent_to_the_office_skill_first():
    # A frontmatter the loader cannot parse drops its skill silently: each one must be here.
    skills = {s.name: s for s in ResourceLoader()._load_skills_from_dir(str(SKILLS), "platform")}
    for name, sentence in OFFICE_FIRST.items():
        assert skills[name].description.startswith(sentence), name


def test_the_docx_xlsx_pptx_and_pdf_forms_skills_hide_where_the_office_tools_are_offered():
    # The model picks a file skill over the office skill despite the description; hiding it is what holds.
    # The pdf skill stays: it merges, splits, creates, watermarks and encrypts, which the office tools do not.
    skills = {s.name: s for s in ResourceLoader()._load_skills_from_dir(str(SKILLS), "platform")}
    for name in ("docx", "xlsx", "pptx", "pdf-forms"):
        assert skills[name].fallback_for_tools == ["office_edit"], name
    assert not skills["pdf"].fallback_for_tools


def test_the_pdf_skill_leaves_what_office_edit_does_to_pdf_forms():
    skills = {s.name: s for s in ResourceLoader()._load_skills_from_dir(str(SKILLS), "platform")}
    assert skills["pdf-forms"].description.startswith(PDF_FORMS_FIRST)
    # Shown in every chat: its description names nothing office_edit does, so it never draws the model from it.
    description = skills["pdf"].description
    assert not re.search(r"office_edit|\bforms?\b|rotat|annotat|redact", description, re.IGNORECASE), description
    # Its body teaches none of it, and sends that work on by where the chat is.
    body = skills["pdf"].content
    assert "fill, annotate, redact or rotate: office_edit where the chat has it, else the pdf-forms skill" in body
    for teaching in ("FORMS.md", ".rotate(", "--rotate", "1east"):
        assert teaching not in body, teaching


def test_the_form_guide_and_its_scripts_live_in_pdf_forms():
    pdf, forms = SKILLS / "productivity/pdf", SKILLS / "productivity/pdf-forms"
    for path in PDF_FORMS_FILES:
        assert (forms / path).is_file(), path
        assert path == "LICENSE.txt" or not (pdf / path).exists(), path
    # What stays: the reference, the license and the watermark script.
    assert sorted(p.relative_to(pdf).as_posix() for p in pdf.rglob("*") if p.is_file()) == [
        "LICENSE.txt", "REFERENCE.md", "SKILL.md", "scripts/watermark.py",
    ]
