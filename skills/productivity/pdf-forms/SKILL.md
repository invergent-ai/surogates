---
name: pdf-forms
description: "Fill in a PDF form, annotate or redact a PDF, or add, remove or rotate its pages, when the office_edit tool is not available. Fills fillable and flat forms with the scripts FORMS.md walks through, puts notes and text on a page, blacks out words so they are gone from the file, and inserts, deletes and rotates pages with pypdf, qpdf or pdftk. Merging, splitting, creating, watermarking and encrypting PDFs are the pdf skill's."
fallback_for_tools: [office_edit]
license: Proprietary. LICENSE.txt has complete terms
---

# PDF Forms, Notes, Redaction and Pages

Changes to an existing PDF, made with scripts. Merging, splitting, creating, watermarking and encrypting are in the `pdf` skill; reading a PDF is `read_file`.

**Dependencies are pre-installed in the sandbox image — do NOT `pip install` or `npm install` anything.**

- **Python libraries (always present):** `pypdf`, `pdfplumber`, `pypdfium2`, `Pillow`.
- **CLI tools (present on current sandbox images):** `pdftoppm` (poppler-utils), `qpdf`, `pdftk`. If one returns `not found`, use the pypdf way on this page rather than trying to install it.

## Fill in a form

Follow FORMS.md, step by step and in order. It checks whether the PDF has fillable fields, then fills them, or writes the answers onto a flat form as annotations, with the scripts in `scripts/`.

## Add a note or text to a page

```python
from pypdf import PdfWriter
from pypdf.annotations import FreeText, Text

writer = PdfWriter(clone_from="input.pdf")
# A rect is (left, bottom, right, top) in PDF points, from the page's bottom-left corner.
writer.add_annotation(page_number=0, annotation=Text(rect=(50, 742, 70, 762), text="Check this total"))  # a comment icon
writer.add_annotation(page_number=0, annotation=FreeText(
    text="Approved", rect=(50, 700, 200, 720), font_size="12pt", border_color=None, background_color=None,
))  # text shown on the page
writer.write("annotated.pdf")
```

To put a note beside some words, find them with `pdfplumber` as the redaction below does: its `top` and `bottom` count from the page's top, so a rect's `bottom` is `page.height - bottom`.

## Redact

A black box drawn over text leaves the text in the file, and anyone can still copy it out. Render the page, black out the words on the image, and put the image in the page's place:

```python
import io
import subprocess

import pdfplumber
from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter

SRC, PAGE, DPI = "input.pdf", 1, 200  # PAGE counts from 1
WORDS = ["Jane Doe", "555-0142"]

with pdfplumber.open(SRC) as pdf:
    found = [hit for word in WORDS for hit in pdf.pages[PAGE - 1].search(word, regex=False)]
print(f"{len(found)} matches")  # 0 means the words are not in the text layer: a scanned page needs OCR first

subprocess.run(["pdftoppm", "-png", "-r", str(DPI), "-f", str(PAGE), "-l", str(PAGE), "-singlefile", SRC, "page"], check=True)
image = Image.open("page.png").convert("RGB")
draw, scale = ImageDraw.Draw(image), DPI / 72
for hit in found:
    draw.rectangle([hit["x0"] * scale - 2, hit["top"] * scale - 2, hit["x1"] * scale + 2, hit["bottom"] * scale + 2], fill="black")
flat = io.BytesIO()
image.save(flat, format="PDF", resolution=DPI)

writer = PdfWriter()
for number, page in enumerate(PdfReader(SRC).pages, start=1):
    writer.add_page(PdfReader(flat).pages[0] if number == PAGE else page)
writer.write("redacted.pdf")
```

The redacted page becomes a picture: its text can no longer be selected or searched. `pdftotext -f 1 -l 1 redacted.pdf -` (with your page number) must not print the words. The same words may also be on other pages, in the metadata (`PdfReader(SRC).metadata`), in form fields or in annotations.

## Add, remove and rotate pages

### pypdf
```python
from pypdf import PdfReader, PdfWriter

writer = PdfWriter(clone_from="input.pdf")
writer.pages[0].rotate(90)  # Rotate page 1 by 90 degrees clockwise
del writer.pages[2]  # Remove page 3
writer.insert_page(PdfReader("cover.pdf").pages[0], index=0)  # cover.pdf's first page becomes page 1
writer.insert_blank_page(index=2)  # A blank page at position 3, the size of the last page
writer.write("output.pdf")
```

Page indexes count from 0, and each insert or removal moves the pages after it.

### qpdf
```bash
qpdf input.pdf output.pdf --rotate=+90:1  # Rotate page 1 by 90 degrees
qpdf input.pdf --pages . 1-2,4-z -- output.pdf  # Remove page 3
qpdf input.pdf --pages . 1 cover.pdf 1 . 2-z -- output.pdf  # cover.pdf's first page after page 1
```

### pdftk
```bash
pdftk input.pdf rotate 1east output rotated.pdf  # Rotate page 1; the other pages stay as they are
pdftk input.pdf cat 1-2 4-end output output.pdf  # Remove page 3
pdftk A=input.pdf B=cover.pdf cat A1 B1 A2-end output output.pdf  # cover.pdf's first page after page 1
```

## Check what you changed

```bash
pdftoppm -png -r 80 output.pdf page && ls -1 "$PWD"/page-*.png
```

Ask `vision_analyze` about each page image once: a filled field, a note or a black box where it belongs, pages the right way up and in order.
