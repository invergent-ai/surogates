---
name: pdf
description: Use this skill whenever the user wants to do anything with PDF files. This includes reading or extracting text/tables from PDFs, combining or merging multiple PDFs into one, splitting PDFs apart, rotating pages, adding watermarks, creating new PDFs, filling PDF forms, encrypting/decrypting PDFs, extracting images, and OCR on scanned PDFs to make them searchable. If the user mentions a .pdf file or asks to produce one, use this skill.
license: Proprietary. LICENSE.txt has complete terms
---

# PDF Processing Guide

## Overview

This guide covers PDF **transformations** — merging, splitting, rotating, watermarking, encrypting, filling forms, extracting images, and OCRing scanned PDFs. **Reading text/tables is handled by `read_file` directly; do NOT use this skill for plain reading.** See REFERENCE.md for advanced library usage and FORMS.md for form filling.

**Dependencies are pre-installed in the sandbox image — do NOT `pip install` or `npm install` anything.** Just import or invoke directly.

- **Python libraries (always present):** `pypdf`, `pdfplumber`, `pypdfium2`, `reportlab`, `pytesseract`, `pdf2image`, `Pillow`, `pandas`, `numpy`.
- **JavaScript libraries (always present):** `pdf-lib`, `pdfjs-dist`.
- **CLI tools (present on current sandbox images):** `pdftotext` / `pdftoppm` / `pdfimages` (poppler-utils), `qpdf`, `pdftk`, `pandoc`, `tesseract`. Older sandbox images may be missing some of these — if a CLI tool returns `not found`, fall back to the equivalent Python library on this page rather than trying to install it.

## Reading PDF text — use `read_file`

For **any** request to read, summarise, analyse, extract data from, or aggregate the contents of a PDF, call `read_file` directly:

```
read_file(path="path/to/document.pdf")
```

The harness parses PDFs natively (mupdf; a scan without a text layer is
OCR'd over its first pages only, and the output says so) and returns
markdown. Pagination via `offset`/`limit` is free (cached). Do
**NOT** write extraction scripts for plain reading — that bootstrap is
exactly what `read_file` eliminates.

When `read_file` is not enough — it fails or times out, the text comes
back garbled, or the question is about figures, charts, layout, embedded
images, attachments or form-field values — load the **`pdf-reading`**
skill. It covers the content inventory, page rasterization for
`vision_analyze`, OCR and table extraction.

The Python libraries on this page are for **transformations** (the next
sections), not for reading.

## Python Libraries

### pypdf - Basic Operations

#### Merge PDFs
```python
from pypdf import PdfWriter, PdfReader

writer = PdfWriter()
for pdf_file in ["doc1.pdf", "doc2.pdf", "doc3.pdf"]:
    reader = PdfReader(pdf_file)
    for page in reader.pages:
        writer.add_page(page)

with open("merged.pdf", "wb") as output:
    writer.write(output)
```

#### Split PDF
```python
reader = PdfReader("input.pdf")
for i, page in enumerate(reader.pages):
    writer = PdfWriter()
    writer.add_page(page)
    with open(f"page_{i+1}.pdf", "wb") as output:
        writer.write(output)
```

#### Extract Metadata
```python
reader = PdfReader("document.pdf")
meta = reader.metadata
print(f"Title: {meta.title}")
print(f"Author: {meta.author}")
print(f"Subject: {meta.subject}")
print(f"Creator: {meta.creator}")
```

#### Rotate Pages
```python
reader = PdfReader("input.pdf")
writer = PdfWriter()

page = reader.pages[0]
page.rotate(90)  # Rotate 90 degrees clockwise
writer.add_page(page)

with open("rotated.pdf", "wb") as output:
    writer.write(output)
```

### pdfplumber - Layout-aware text + tables (fallback only)

For plain text/markdown, `read_file(path)` is the right call (see top of
this skill).  Use `pdfplumber` only when you need precise layout
preservation or per-table structured extraction that `read_file` does
not give you — e.g., multi-line cells, merged headers, or extracting a
specific table on a specific page.

#### Extract Text with Layout
```python
import pdfplumber

with pdfplumber.open("document.pdf") as pdf:
    for page in pdf.pages:
        text = page.extract_text()
        print(text)
```

#### Extract Tables
```python
with pdfplumber.open("document.pdf") as pdf:
    for i, page in enumerate(pdf.pages):
        tables = page.extract_tables()
        for j, table in enumerate(tables):
            print(f"Table {j+1} on page {i+1}:")
            for row in table:
                print(row)
```

#### Advanced Table Extraction
```python
import pandas as pd

with pdfplumber.open("document.pdf") as pdf:
    all_tables = []
    for page in pdf.pages:
        tables = page.extract_tables()
        for table in tables:
            if table:  # Check if table is not empty
                df = pd.DataFrame(table[1:], columns=table[0])
                all_tables.append(df)

# Combine all tables
if all_tables:
    combined_df = pd.concat(all_tables, ignore_index=True)
    combined_df.to_excel("extracted_tables.xlsx", index=False)
```

### reportlab - Create PDFs

#### Basic PDF Creation
```python
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

c = canvas.Canvas("hello.pdf", pagesize=letter)
width, height = letter

# Add text
c.drawString(100, height - 100, "Hello World!")
c.drawString(100, height - 120, "This is a PDF created with reportlab")

# Add a line
c.line(100, height - 140, 400, height - 140)

# Save
c.save()
```

#### Create PDF with Multiple Pages
```python
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
from reportlab.lib.styles import getSampleStyleSheet

doc = SimpleDocTemplate("report.pdf", pagesize=letter)
styles = getSampleStyleSheet()
story = []

# Add content
title = Paragraph("Report Title", styles['Title'])
story.append(title)
story.append(Spacer(1, 12))

body = Paragraph("This is the body of the report. " * 20, styles['Normal'])
story.append(body)
story.append(PageBreak())

# Page 2
story.append(Paragraph("Page 2", styles['Heading1']))
story.append(Paragraph("Content for page 2", styles['Normal']))

# Build PDF
doc.build(story)
```

#### Fonts: any text beyond plain English

reportlab's built-in fonts (Helvetica, Times-Roman, Courier) only cover Western European letters. Romanian ș ț ă, Polish, Czech, Turkish, Greek and Cyrillic come out as **black boxes**. Register a TrueType font before building — Liberation Sans is installed (and is metric-compatible with Arial):

```python
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

FONTS = "/usr/share/fonts/truetype/liberation2/LiberationSans-"
for name, file in [("Sans", "Regular"), ("Sans-Bold", "Bold"),
                   ("Sans-Italic", "Italic"), ("Sans-BoldItalic", "BoldItalic")]:
    pdfmetrics.registerFont(TTFont(name, f"{FONTS}{file}.ttf"))
pdfmetrics.registerFontFamily("Sans", normal="Sans", bold="Sans-Bold",
                              italic="Sans-Italic", boldItalic="Sans-BoldItalic")

styles = getSampleStyleSheet()
for s in styles.byName.values():
    if hasattr(s, "fontName"):  # the list styles have none
        s.fontName = "Sans-Bold" if "Bold" in s.fontName else "Sans"
```

Then use `"Sans"` / `"Sans-Bold"` everywhere a font is named: table styles (`("FONTNAME", (0, 0), (-1, -1), "Sans")`) and canvas text (`c.setFont("Sans", 12)`). `LiberationSerif-*` is the Times equivalent. Use it for every document whose text isn't plain English: names and places carry these letters even in English text.

#### Subscripts and Superscripts

**IMPORTANT**: Never use Unicode subscript/superscript characters (₀₁₂₃₄₅₆₇₈₉, ⁰¹²³⁴⁵⁶⁷⁸⁹) in ReportLab PDFs. The built-in fonts do not include these glyphs, causing them to render as solid black boxes.

Instead, use ReportLab's XML markup tags in Paragraph objects:
```python
from reportlab.platypus import Paragraph
from reportlab.lib.styles import getSampleStyleSheet

styles = getSampleStyleSheet()

# Subscripts: use <sub> tag
chemical = Paragraph("H<sub>2</sub>O", styles['Normal'])

# Superscripts: use <super> tag
squared = Paragraph("x<super>2</super> + y<super>2</super>", styles['Normal'])
```

For canvas-drawn text (not Paragraph objects), manually adjust font the size and position rather than using Unicode subscripts/superscripts.

#### Check what you created

```bash
pdftotext output.pdf - | grep -c "■"          # must print 0
pdftoppm -png -r 80 output.pdf page && ls -1 "$PWD"/page-*.png
```

Each `■` is a character the font couldn't draw: switch to the TrueType font above. Then ask `vision_analyze` about each page image once, for the look: layout, tables, the watermark. Render with `pdftoppm`, not `pypdfium2`.

## Command-Line Tools

### pdftotext (poppler-utils)
```bash
# Extract text
pdftotext input.pdf output.txt

# Extract text preserving layout
pdftotext -layout input.pdf output.txt

# Extract specific pages
pdftotext -f 1 -l 5 input.pdf output.txt  # Pages 1-5
```

### qpdf
```bash
# Merge PDFs
qpdf --empty --pages file1.pdf file2.pdf -- merged.pdf

# Split pages
qpdf input.pdf --pages . 1-5 -- pages1-5.pdf
qpdf input.pdf --pages . 6-10 -- pages6-10.pdf

# Rotate pages
qpdf input.pdf output.pdf --rotate=+90:1  # Rotate page 1 by 90 degrees

# Remove password
qpdf --password=mypassword --decrypt encrypted.pdf decrypted.pdf
```

### pdftk (if available)
```bash
# Merge
pdftk file1.pdf file2.pdf cat output merged.pdf

# Split
pdftk input.pdf burst

# Rotate
pdftk input.pdf rotate 1east output rotated.pdf
```

## Common Tasks

### Extract Text from Scanned PDFs
```python
# pytesseract + pdf2image are pre-installed; tesseract-ocr is in the
# sandbox image's apt layer.  Just import and run.
import pytesseract
from pdf2image import convert_from_path

# Convert PDF to images
images = convert_from_path('scanned.pdf')

# OCR each page
text = ""
for i, image in enumerate(images):
    text += f"Page {i+1}:\n"
    text += pytesseract.image_to_string(image)
    text += "\n\n"

print(text)
```

### Add Watermark

Use the script. It stamps diagonal text **on top of** every page, tables and images included, in translucent grey, so the content stays readable under it:

```bash
python scripts/watermark.py document.pdf watermarked.pdf "CONFIDENTIAL"                 # opacity 0.15
python scripts/watermark.py document.pdf watermarked.pdf "DRAFT" --opacity 0.25
```

For a PDF you create, build it first without a watermark, then stamp it. Don't draw the watermark in reportlab's `onFirstPage`/`onLaterPages`: those run before the page content, so tables and filled shapes cover it.

### Extract Images
```bash
# Using pdfimages (poppler-utils)
pdfimages -j input.pdf output_prefix

# This extracts all images as output_prefix-000.jpg, output_prefix-001.jpg, etc.
```

### Password Protection
```python
from pypdf import PdfReader, PdfWriter

reader = PdfReader("input.pdf")
writer = PdfWriter()

for page in reader.pages:
    writer.add_page(page)

# Add password
writer.encrypt("userpassword", "ownerpassword")

with open("encrypted.pdf", "wb") as output:
    writer.write(output)
```

## Quick Reference

| Task | Best Tool | Command/Code |
|------|-----------|--------------|
| **Read text / tables / aggregate data** | **`read_file`** | `read_file(path="document.pdf")` |
| Merge PDFs | pypdf | `writer.add_page(page)` |
| Split PDFs | pypdf | One page per file |
| Layout-precise text / structured tables (fallback) | pdfplumber | `page.extract_text()` / `page.extract_tables()` |
| Create PDFs | reportlab | Canvas or Platypus; a TrueType font for non-English text |
| Watermark | `scripts/watermark.py` | `python scripts/watermark.py in.pdf out.pdf "CONFIDENTIAL"` |
| Command line merge | qpdf | `qpdf --empty --pages ...` |
| OCR scanned PDFs | `pdf-reading` skill or pytesseract | Convert to image first |
| Fill PDF forms | pdf-lib or pypdf (see FORMS.md) | See FORMS.md |

## Next Steps

- For advanced pypdfium2 usage, see REFERENCE.md
- For JavaScript libraries (pdf-lib), see REFERENCE.md
- If you need to fill out a PDF form, follow the instructions in FORMS.md
- For troubleshooting guides, see REFERENCE.md
