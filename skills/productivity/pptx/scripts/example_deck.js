// A minimal structured deck: theme, two layouts with named placeholders,
// sections, scheme colours, a native chart and speaker notes.  It passes
// scripts/office/validate.py.  Copy it, change THEME and the slides, keep
// the shape.   usage: node example_deck.js [out.pptx]
const path = require("path");
const pptxgen = require("pptxgenjs");
const { applyTheme } = require(path.join(__dirname, "apply_theme.js"));

const THEME = {
  name: "Example",
  headFontFace: "Cambria",
  bodyFontFace: "Calibri",
  // All twelve slots, six hex digits, no "#" -- applyTheme refuses anything else.
  colors: {
    dk1: "1F2933", lt1: "FFFFFF", dk2: "3E4C59", lt2: "F5F7FA",
    accent1: "0B6E4F", accent2: "08A045", accent3: "6BBF59",
    accent4: "C9A227", accent5: "E36414", accent6: "5F0F40",
    hlink: "0B6E4F", folHlink: "5F0F40",
  },
};

(async () => {
  const out = process.argv[2] || "example.pptx";
  const pres = new pptxgen();
  pres.layout = "LAYOUT_WIDE"; // 13.33 x 7.5 in
  pres.theme = { headFontFace: THEME.headFontFace, bodyFontFace: THEME.bodyFontFace };
  const C = pres.SchemeColor;

  // Title placeholders render centred unless they set align.
  pres.defineSlideMaster({
    title: "TITLE",
    background: { color: THEME.colors.dk1 },
    objects: [
      { placeholder: { options: { name: "title", type: "title", x: 0.8, y: 2.5, w: 11.7, h: 1.4,
        fontSize: 44, bold: true, color: C.background1, align: "left", valign: "middle", margin: 0 }, text: "" } },
      { placeholder: { options: { name: "subtitle", type: "body", x: 0.8, y: 4.0, w: 11.7, h: 0.8,
        fontSize: 20, color: C.background2, align: "left", valign: "top", margin: 0 }, text: "" } },
    ],
  });
  pres.defineSlideMaster({
    title: "CONTENT",
    background: { color: THEME.colors.lt1 },
    slideNumber: { x: 12.5, y: 7.0, w: 0.5, h: 0.3, fontSize: 10, color: THEME.colors.dk2 },
    objects: [
      { placeholder: { options: { name: "title", type: "title", x: 0.6, y: 0.4, w: 12.1, h: 0.9,
        fontSize: 32, bold: true, color: C.text1, align: "left", valign: "middle", margin: 0 }, text: "" } },
      { placeholder: { options: { name: "body", type: "body", x: 0.6, y: 1.6, w: 5.6, h: 5.0,
        fontSize: 16, color: C.text1, align: "left", valign: "top", margin: 0 }, text: "" } },
    ],
  });

  pres.addSection({ title: "Opening" });
  const cover = pres.addSlide({ masterName: "TITLE", sectionTitle: "Opening" });
  cover.addText("Fresh bread, every neighbourhood", { placeholder: "title" });
  cover.addText("Seed round  |  October 2026", { placeholder: "subtitle" });
  cover.addNotes("Open with the one-line thesis.");

  pres.addSection({ title: "Market" });
  const s = pres.addSlide({ masterName: "CONTENT", sectionTitle: "Market" });
  s.addText("Demand is growing faster than supply", { placeholder: "title" });
  s.addText([
    { text: "Metro population up 6% since 2021", options: { bullet: true, breakLine: true } },
    { text: "Three in four loaves are industrial", options: { bullet: true, breakLine: true } },
    { text: "No branded fresh-bake chain yet", options: { bullet: true } },
  ], { placeholder: "body", paraSpaceAfter: 8 });
  // Plot-area styling lives under plotArea; top-level fill/border objects crash writeFile().
  s.addChart(pres.ChartType.bar, [{ name: "Revenue", labels: ["2024", "2025", "2026"], values: [1.2, 2.4, 4.1] }], {
    x: 6.7, y: 1.6, w: 6.0, h: 5.0, barDir: "col",
    showTitle: true, title: "Revenue (EUR M)", titleFontFace: "+mn-lt", titleColor: THEME.colors.dk1,
    showValue: true, dataLabelPosition: "outEnd", dataLabelFontFace: "+mn-lt",
    dataLabelFormatCode: "0.0", // the default "#,##0" labels 1.2 as 1
    chartColors: [THEME.colors.accent1], showLegend: false,
    catAxisLabelFontFace: "+mn-lt", valAxisLabelFontFace: "+mn-lt",
    catAxisLabelColor: THEME.colors.dk2, valAxisLabelColor: THEME.colors.dk2,
    valGridLine: { color: "E4E7EB", size: 0.75 }, catGridLine: { style: "none" },
    plotArea: { fill: { color: THEME.colors.lt1 } },
  });
  s.addNotes("Walk the chart left to right.");

  await pres.writeFile({ fileName: out });
  await applyTheme(out, THEME);
  console.log(`written ${out}`);
})();
