// A one-page agreement: US Letter, the font set once, numbered clauses with
// lettered sub-clauses, bullets, a fixed-width table, a signature table and
// a "Page X of Y" footer.  It passes scripts/office/validate.py.  Copy it,
// replace CONTENT, keep the helpers.   usage: node example_doc.js [out.docx]
const fs = require("fs");
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell, Footer,
  AlignmentType, BorderStyle, HeadingLevel, LevelFormat, PageNumber,
  ShadingType, TableBorders, TableLayoutType, WidthType,
} = require("docx");

const FONT = "Calibri"; // Arial, Calibri, Cambria, Times New Roman render true-to-width
const SIZE = 22; // half-points: 22 = 11 pt; never below 20
const PAGE = { width: 12240, height: 15840 }; // DXA, 1440 = 1 in.  US Letter; A4 is 11906 x 16838
const MARGIN = 1440; // 1 in; never below 1080 (0.75 in)
const TEXT_W = PAGE.width - 2 * MARGIN;

// "**Bold** plain" -> runs.  Font and size come from the document default.
const runs = (text) => text.split("**").map((t, i) => new TextRun({ text: t, bold: i % 2 === 1 }));
// LibreOffice ignores bold in the Title/Heading styles: set it on the run.
const title = (text) => new Paragraph({ heading: HeadingLevel.TITLE, children: [new TextRun({ text, bold: true })] });
const heading = (text) => new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun({ text, bold: true })] });
const para = (text) => new Paragraph({ children: runs(text) });
// Numbers come from the "clauses" numbering: 1. 2. 3., and (a) (b) at level 1.
// Never type "1." or "•" into the text.
const clause = (text, level = 0) => new Paragraph({ numbering: { reference: "clauses", level }, children: runs(text) });
const bullet = (text) => new Paragraph({ numbering: { reference: "bullets", level: 0 }, children: runs(text) });

// rel are relative column widths, e.g. [5, 2, 2]: they are scaled to DXA that
// sum to TEXT_W, set on the table AND on every cell (Word needs both).
function table(header, rows, rel) {
  const sum = rel.reduce((a, b) => a + b, 0);
  const widths = rel.map((x) => Math.floor((x * TEXT_W) / sum));
  widths[0] += TEXT_W - widths.reduce((a, b) => a + b, 0);
  const cell = (text, i, head) => new TableCell({
    width: { size: widths[i], type: WidthType.DXA },
    shading: head ? { type: ShadingType.CLEAR, fill: "E7E6E6" } : undefined,
    margins: { top: 60, bottom: 60, left: 100, right: 100 },
    children: [new Paragraph({ children: [new TextRun({ text, bold: head })] })],
  });
  return new Table({
    width: { size: TEXT_W, type: WidthType.DXA }, columnWidths: widths, layout: TableLayoutType.FIXED,
    rows: [header, ...rows].map((r, ri) => new TableRow({
      tableHeader: ri === 0, children: r.map((t, i) => cell(t, i, ri === 0)),
    })),
  });
}

// A label column, then one column per party with a gap between them.  Each
// blank is a cell with its own bottom border, so the lines never merge.
function signatures(parties, fields = ["Signature", "Name", "Title", "Date"]) {
  const GAP = 360, n = parties.length;
  const partyW = Math.floor((TEXT_W - 1400 - GAP * (n - 1)) / n);
  const widths = [TEXT_W - partyW * n - GAP * (n - 1), ...parties.flatMap((_, i) => (i ? [GAP, partyW] : [partyW]))];
  const none = { style: BorderStyle.NONE, size: 0, color: "FFFFFF" };
  const line = { style: BorderStyle.SINGLE, size: 6, color: "000000" };
  const cell = (i, text, underline) => new TableCell({
    width: { size: widths[i], type: WidthType.DXA },
    borders: { top: none, left: none, right: none, bottom: underline ? line : none },
    margins: { top: 280, left: 60 }, // room to write above the line
    children: [new Paragraph({ children: runs(text) })],
  });
  const row = (label, party) => new TableRow({
    children: widths.map((_, i) => (i === 0 ? cell(0, label) : i % 2 ? party(i) : cell(i, ""))),
  });
  return new Table({
    width: { size: TEXT_W, type: WidthType.DXA }, columnWidths: widths,
    layout: TableLayoutType.FIXED, borders: TableBorders.NONE,
    rows: [
      row("", (i) => cell(i, `**${parties[(i - 1) / 2]}**`)),
      ...fields.map((f) => row(`${f}:`, (i) => cell(i, "", true))),
    ],
  });
}

// ── CONTENT ────────────────────────────────────────────────────────────
const CONTENT = [
  title("CONSULTING AGREEMENT"),
  para("This Consulting Agreement is made on ____________ between **Acme SRL** (the \"Client\") and **Beta Ltd** (the \"Consultant\")."),
  clause("**Services.** The Consultant will provide the services described in Schedule A."),
  clause("**Term.** This Agreement runs for two (2) years from the date above."),
  clause("**Fees.** The Client will pay the fees below within thirty (30) days of each invoice:"),
  table(["Milestone", "Due", "Fee (EUR)"], [
    ["Discovery report", "Month 1", "4,000"],
    ["Pilot delivery", "Month 3", "9,500"],
  ], [5, 2, 2]),
  clause("**Confidentiality.** Each party will:"),
  clause("keep the other party's confidential information secret; and", 1),
  clause("use it only to perform this Agreement.", 1),
  clause("**Deliverables.** The Consultant will hand over:"),
  bullet("all source files and documentation;"),
  bullet("a final report summarising the results."),
  para("Signed by the authorised representatives of the parties:"),
  signatures(["Acme SRL", "Beta Ltd"]),
  heading("Schedule A: Services"),
  para("Market research, a pilot build and a final report."),
];

const doc = new Document({
  styles: {
    default: {
      document: { run: { font: FONT, size: SIZE }, paragraph: { spacing: { after: 120 } } },
      title: { run: { font: FONT, size: 32 }, paragraph: { alignment: AlignmentType.CENTER, spacing: { after: 240 } } },
      heading1: { run: { font: FONT, size: 26 }, paragraph: { spacing: { before: 240, after: 120 } } },
    },
  },
  numbering: {
    config: [
      { reference: "clauses", levels: [
        { level: 0, format: LevelFormat.DECIMAL, text: "%1.", style: { paragraph: { indent: { left: 360, hanging: 360 } } } },
        { level: 1, format: LevelFormat.LOWER_LETTER, text: "(%2)", style: { paragraph: { indent: { left: 900, hanging: 400 } } } },
      ] },
      { reference: "bullets", levels: [
        { level: 0, format: LevelFormat.BULLET, text: "•", style: { paragraph: { indent: { left: 720, hanging: 360 } } } },
      ] },
    ],
  },
  sections: [{
    properties: { page: { size: PAGE, margin: { top: MARGIN, right: MARGIN, bottom: MARGIN, left: MARGIN } } },
    footers: { default: new Footer({ children: [new Paragraph({ alignment: AlignmentType.CENTER, children: [
      new TextRun({ children: ["Page ", PageNumber.CURRENT, " of ", PageNumber.TOTAL_PAGES] }),
    ] })] }) },
    children: CONTENT,
  }],
});

const out = process.argv[2] || "example.docx";
Packer.toBuffer(doc).then((buf) => {
  fs.writeFileSync(out, buf);
  console.log(`written ${out}`);
});
