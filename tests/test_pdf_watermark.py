"""The pdf skill's watermark.py stamps every page, on top of the content."""
import subprocess
import sys
from pathlib import Path

from pypdf import PdfReader
from reportlab.pdfgen import canvas

SCRIPT = Path(__file__).parents[1] / "skills/productivity/pdf/scripts/watermark.py"


def test_watermark_is_drawn_after_the_content(tmp_path):
    src, dst = tmp_path / "in.pdf", tmp_path / "out.pdf"
    c = canvas.Canvas(str(src))
    for n in range(2):
        c.rect(40, 40, 500, 700, fill=1)  # an opaque box that would hide a stamp drawn under it
        c.drawString(60, 760, f"page {n}")
        c.showPage()
    c.save()

    subprocess.run([sys.executable, str(SCRIPT), str(src), str(dst), "CONFIDENTIAL"], check=True)

    pages = PdfReader(dst).pages
    assert len(pages) == 2
    for page in pages:
        text = page.extract_text()
        assert text.index("CONFIDENTIAL") > text.index("page")  # stamp comes after the content
