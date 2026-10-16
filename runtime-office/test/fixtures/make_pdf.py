"""Builds sample.pdf, the PDF the PDF tests open: run it with a Python that has reportlab."""

from pathlib import Path

from reportlab.lib.colors import black
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

pdfmetrics.registerFont(TTFont("DejaVuSerif", "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"))
pdf = canvas.Canvas(str(Path(__file__).with_name("sample.pdf")))
pdf.setFont("DejaVuSerif", 20)
pdf.drawString(72, 760, "Service agreement")
pdf.setFont("DejaVuSerif", 12)
pdf.drawString(72, 730, "The supplier delivers the service from 1 March 2026.")
pdf.drawString(72, 712, "Fees are due within thirty days of each invoice.")
pdf.drawString(72, 660, "Client name:")
pdf.acroForm.textfield(name="client", x=170, y=652, width=250, height=20, borderColor=black, fontName="Helvetica")
pdf.drawString(72, 620, "Signed:")
pdf.acroForm.checkbox(name="signed", x=170, y=615, size=16)
pdf.showPage()
pdf.setFont("DejaVuSerif", 12)
pdf.drawString(72, 760, "Page two: the schedule of services.")
pdf.save()
