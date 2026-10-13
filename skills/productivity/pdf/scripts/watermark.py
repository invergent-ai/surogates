"""Stamp a diagonal text watermark ON TOP of every page of a PDF.

    usage: python watermark.py in.pdf out.pdf "CONFIDENTIAL" [--opacity 0.15]

The stamp is drawn over the page content (tables and images included) in
translucent grey, so it stays visible and the content under it readable.
Works on any PDF; for one you generate, build it first, then stamp it.
"""
import argparse
import io

from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import Color
from reportlab.pdfgen import canvas


def stamp_page(text, width, height, opacity):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(width, height))
    c.setFillColor(Color(0.4, 0.4, 0.4, alpha=opacity))
    size = min(width, height) / max(len(text), 1) * 1.6  # spans ~80% of the diagonal
    c.setFont("Helvetica-Bold", size)
    c.translate(width / 2, height / 2)
    c.rotate(45)
    c.drawCentredString(0, -size / 3, text)
    c.save()  # without save() the stamp PDF has no pages
    buf.seek(0)
    return PdfReader(buf).pages[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("text")
    ap.add_argument("--opacity", type=float, default=0.15, help="0-1; 0.1-0.2 keeps content readable")
    args = ap.parse_args()

    writer = PdfWriter()
    stamps = {}  # one stamp per page size
    for page in PdfReader(args.src).pages:
        # ponytail: uses the mediabox and ignores /Rotate; a rotated page gets a stamp rotated with it
        w, h = float(page.mediabox.width), float(page.mediabox.height)
        if (w, h) not in stamps:
            stamps[(w, h)] = stamp_page(args.text, w, h, args.opacity)
        page.merge_page(stamps[(w, h)], over=True)  # over=True: on top of the content
        writer.add_page(page)
    with open(args.dst, "wb") as f:
        writer.write(f)
    print(f"written {args.dst}")


if __name__ == "__main__":
    main()
