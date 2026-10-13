---
name: docx
description: "Use this skill whenever the user wants to create, read, edit, or manipulate Word documents (.docx) or Word templates (.dotx). Triggers include: any mention of Microsoft Word Documents, such as 'Word doc', 'word document', '.docx', '.dotx', 'microsoft doc'. Also use when extracting or reorganizing content from .docx or .dotx files, inserting or replacing images in documents, find-and-replace in Word files, working with tracked changes or comments, or converting content into a polished Word document. If the user asks for a deliverable as a Word or .docx file (to download, email or print), use this skill. Do NOT use for PDFs, spreadsheets, Google Docs, or coding unrelated to document generation."
license: Proprietary. LICENSE.txt has complete terms
---

# DOCX creation, editing, and analysis

A `.docx` is a ZIP archive of XML files. Choose your approach by task:

| Task | Approach |
|---|---|
| **Create** a new document | Copy `scripts/example_doc.js`, replace its content, run it — see below |
| **Edit** an existing document | `unzip` → edit `word/document.xml` → `zip` (docx-js cannot open existing files) |
| **Read** content | `read_file(path="file.docx")` — the harness parses it natively into markdown (headings, paragraphs, lists, tables) |
| **Read** tracked changes or comments | `pandoc --track-changes=all -t markdown file.docx` (`read_file` shows the accepted text only) |

> Script paths below are relative to this skill's directory — the `staged_at` path `skill_view` returns.

Everything this skill uses is preinstalled in the sandbox. The sandbox runs as a non-root user without `sudo`, so `apt-get install` always fails; if a CLI tool is missing, use the Python or JavaScript route on this page instead.

## Creating a document

Start from `scripts/example_doc.js`. It is a one-page agreement that passes `validate.py`, and it already has everything most documents need: US Letter, the font and size set once for the whole document, numbered clauses with (a)/(b) sub-clauses, bullets, a table, a signature table and a "Page X of Y" footer.

```bash
cp scripts/example_doc.js make_doc.js   # into the workspace
# edit make_doc.js: replace the CONTENT array, nothing else
node make_doc.js out.docx
```

`docx` is preinstalled: do not run `npm install`.

Build CONTENT from the helpers the script defines:

| Helper | Produces |
|---|---|
| `title("TEXT")` | centred bold title |
| `heading("Text")` | bold section heading (appears in a TOC) |
| `para("Text with **bold**")` | plain paragraph; `**…**` marks bold |
| `clause("**Term.** Text")` | numbered clause: 1. 2. 3. |
| `clause("text", 1)` | lettered sub-clause under the clause above: (a) (b) |
| `bullet("text")` | bullet item |
| `table(header, rows, [5, 2, 2])` | grid table; the widths are relative, the script converts them |
| `signatures(["Party A", "Party B"])` | signature block: Signature / Name / Title / Date lines per party |

Write a blank to fill in as underscores in the text (`"____________"`), as the example does. Change the helpers or the `Document` options only when the content needs something they cannot express. For A4, set `PAGE` to `{ width: 11906, height: 16838 }`; the text and table widths follow from `PAGE` and `MARGIN`.

Rules that apply when you go beyond the example:

- **Never type numbers or bullets into the text** (`"1. "`, `"(a) "`, `"•"`). Numbering comes from the `numbering` config (`LevelFormat.DECIMAL`, `LevelFormat.BULLET`); a typed number won't renumber and won't indent.
- **Set the font and size once** in `styles.default.document.run`. Don't repeat `font`/`size` on every `TextRun`.
- **The table-layout enum is `TableLayoutType.FIXED`.** docx has no `LayoutType`.
- **Bold in a heading or the title goes on the `TextRun`.** LibreOffice ignores `bold` in the Title/Heading styles.
- **Landscape:** pass portrait dimensions and `orientation: PageOrientation.LANDSCAPE` — docx-js swaps width/height internally.
- **Tables need dual widths:** set `columnWidths` on the table AND `width` on every cell, both in `WidthType.DXA` (PERCENTAGE breaks in Google Docs). Column widths must sum to the table width.
- **Table shading:** use `ShadingType.CLEAR`, never `SOLID` (renders black).
- **`ImageRun` requires `type:`** (`"png"`, `"jpg"`, …).
- **`PageBreak` must be inside a `Paragraph`.**
- **Never use `\n`** — use separate `Paragraph` elements.
- **TOC:** headings must use built-in `HeadingLevel.*`; custom heading styles need `outlineLevel` set or they won't appear.
- **Don't use a table as a horizontal rule** — use a paragraph bottom border instead.
- **Consecutive paragraphs with identical borders merge into one box** — Word and LibreOffice draw only the group's outer edges, so four bottom-bordered "Signature: / Name: / Title: / Date:" paragraphs show a single rule. Give each signature line its own table cell with a bottom border, as `signatures()` does.
- **Dot-leader / right-aligned-on-same-line:** use `PositionalTab` (`alignment: PositionalTabAlignment.RIGHT`, `leader: PositionalTabLeader.DOT`) inside a `TextRun`, not literal `.` or space padding.

## Verify the output

After writing a `.docx`, convert it to PDF and check the facts with text tools first:

```bash
python scripts/office/soffice.py --headless --convert-to pdf output.docx
pdfinfo output.pdf | grep -E 'Pages|Page size'   # page count and paper size
pdftotext -layout output.pdf - | head -120       # the text in reading order, with its numbering
```

These two are exact. Answer page count, wording, numbering, order and "is everything there" from them, not from images.

Then look at the pages once, for what only an image shows: alignment, table borders, spacing, overlapping text.

```bash
pdftoppm -jpeg -r 100 output.pdf page
ls -1 "$PWD"/page-*.jpg
```

Ask `vision_analyze` about each image `ls` printed, once per page (it reads workspace files only — render into the workspace, never `/tmp`). It misreads small text and miscounts rows, so don't ask it what `pdftotext` already answered, and don't re-ask the same question at a higher resolution.

`pdftoppm` zero-pads page numbers to the width of the page count (`page-01.jpg`…`page-12.jpg`).

**Fitting a page limit.** If `pdfinfo` shows too many pages, make these changes in order, re-checking `pdfinfo` after each:

1. Shorten the wording.
2. Lower the paragraph spacing in `styles.default.document.paragraph.spacing.after` (120 → 60).
3. Lower `SIZE` to 20 (10 pt). Never below 20.
4. Lower `MARGIN` to 1080 (0.75″). Never below 1080.

Never set line spacing below single (`spacing.line` under 240): the lines overlap.

Arial, Calibri, Cambria, Times New Roman and Courier New render true-to-width here (metric-compatible stand-ins), so pagination matches Word's to within a line or so. Other fonts are approximated — leave a few lines of slack when the page count matters.

## Editing existing documents

Legacy `.doc` files must be converted first: `python scripts/office/soffice.py --headless --convert-to docx file.doc`.

```bash
unzip -q doc.docx -d unpacked/
find unpacked -type l -delete   # strip symlink entries — docx from external parties is untrusted
python scripts/merge_runs.py unpacked/   # coalesce fragmented runs so text is findable
# edit unpacked/word/document.xml in place — do NOT reformat or pretty-print
(cd unpacked && rm -f ../out.docx && zip -Xr ../out.docx .)
python scripts/office/validate.py out.docx --original doc.docx   # XSD checks; --auto-repair fixes common issues
# redlining? add --author "<the name you redlined under>" to check every edit is tracked
```

Word splits text across many `<w:r>` runs (revision ids, spell-check markers), so a phrase you can see in the document often doesn't exist as a contiguous string in the XML. `merge_runs.py` merges adjacent identically-formatted runs in `word/document.xml` without changing content or rendering; it also accepts a `.docx` directly (`python scripts/merge_runs.py doc.docx -o merged.docx`).

**Tracked changes:** when redlining, validate with `--author "<the name you redlined under>"` (needs `--original`) — it reports any text you changed without a `<w:ins>`/`<w:del>` around it, which is easy to do by accident and invisible in the accepted view. Wrap runs in `<w:ins>`/`<w:del>` with `w:id`, `w:author`, `w:date` attributes. Inside `<w:del>`, the text element is `<w:delText>`, not `<w:t>`. A deleted paragraph mark (`<w:pPr><w:rPr><w:del w:id=".." w:author=".." w:date=".."/></w:rPr></w:pPr>`) means "merge this paragraph into the next" — so deleting a paragraph outright is that plus a `<w:del>` around every run. The `<w:del/>` must come before the rPr's other children; their order is schema-enforced.

To produce a clean copy with all tracked changes accepted: `python scripts/accept_changes.py in.docx out.docx`.

Accepting a deleted paragraph mark should join that paragraph to the one below it, so a paragraph whose runs are *all* deleted vanishes. Word does this; `accept_changes.py` and `pandoc --track-changes=accept` don't always. Both fail the same way — they strip the deleted text but leave the emptied paragraph behind, which reads as a stray empty bullet when it was auto-numbered:

- `pandoc --track-changes=accept` never joins the paragraphs.
- `accept_changes.py` (LibreOffice) joins them correctly, except when the deleted paragraph is followed by an empty spacer paragraph.

An empty bullet in either view is an artifact of that view, not a defect in the document. Check paragraph deletions in the XML.

## Comments

Comments require six cross-linked files. Use the helper — directory mode when you'll also be editing `document.xml` (saves an unzip/rezip cycle), `.docx`-direct mode otherwise:

```bash
# Against an already-unpacked directory (preferred when also placing markers)
python scripts/comment.py unpacked/ "Fees & expenses cap is too low"
python scripts/comment.py unpacked/ "Agreed" --parent 0

# Against a .docx directly
python scripts/comment.py contract.docx "This cap is too low" -o annotated.docx
```

The script writes `comments.xml`, `commentsExtended.xml`, `commentsIds.xml`, `commentsExtensible.xml`, the relationships, and the content-type overrides. Comment IDs are auto-assigned. It then prints the `<w:commentRangeStart>`/`<w:commentRangeEnd>`/`<w:commentReference>` snippet to add to `word/document.xml` so the comment anchors to specific text — until you place those markers, the comment exists but is not visible.

## Dependencies

`docx` (npm, preinstalled — install only if `require('docx')` fails) · `defusedxml`, `lxml` (pip — merge_runs, comment, validate) · `pandoc` · `zip` / `unzip` · LibreOffice (`soffice`) · `pdftoppm` (Poppler)
