import { describe, expect, it } from "vitest";
import { compileContent, ContentError } from "../src/markdown.js";

describe("compileContent", () => {
  it("maps headings, paragraphs and inline styles", () => {
    expect(compileContent("# Title\n\nSome **bold** and *italic* with `code` and [a link](https://x.test).")).toEqual([
      { t: "h", level: 1, runs: [{ text: "Title" }] },
      {
        t: "p",
        runs: [
          { text: "Some " }, { text: "bold", b: true }, { text: " and " }, { text: "italic", i: true },
          { text: " with " }, { text: "code", code: true }, { text: " and " },
          { text: "a link", href: "https://x.test" }, { text: "." },
        ],
      },
    ]);
  });

  it("writes headings deeper than three as level three", () => {
    expect(compileContent("#### Deep")).toEqual([{ t: "h", level: 3, runs: [{ text: "Deep" }] }]);
  });

  it("gives each list its own numbering and nests levels", () => {
    expect(compileContent("- a\n- b\n  - c\n\n1. one\n2. two")).toEqual([
      { t: "li", list: 0, root: 0, ordered: false, level: 0, runs: [{ text: "a" }] },
      { t: "li", list: 0, root: 0, ordered: false, level: 0, runs: [{ text: "b" }] },
      { t: "li", list: 0, root: 0, ordered: false, level: 1, runs: [{ text: "c" }] },
      { t: "li", list: 1, root: 1, ordered: true, level: 0, runs: [{ text: "one" }] },
      { t: "li", list: 1, root: 1, ordered: true, level: 0, runs: [{ text: "two" }] },
    ]);
  });

  it("names the top-level list a nested list of the other kind is written under", () => {
    expect(compileContent("1. a\n   - b\n2. c\n\n- d")).toEqual([
      { t: "li", list: 0, root: 0, ordered: true, level: 0, runs: [{ text: "a" }] },
      { t: "li", list: 1, root: 0, ordered: false, level: 1, runs: [{ text: "b" }] },
      { t: "li", list: 0, root: 0, ordered: true, level: 0, runs: [{ text: "c" }] },
      { t: "li", list: 2, root: 2, ordered: false, level: 0, runs: [{ text: "d" }] },
    ]);
  });

  // The editor counts these levels below the list item an insert lands next to; a top-level list that joins none starts
  // at its shallowest level, its nested lists with it.
  it("reads a list item indented two or three spaces as one level down", () => {
    expect(compileContent("  1. sub\n1. main\n   1. nested")).toEqual([
      { t: "li", list: 0, root: 0, ordered: true, level: 1, runs: [{ text: "sub" }] },
      { t: "li", list: 0, root: 0, ordered: true, level: 0, runs: [{ text: "main" }] },
      { t: "li", list: 0, root: 0, ordered: true, level: 1, runs: [{ text: "nested" }] },
    ]);
    expect(compileContent("   - x\n     - y")).toEqual([
      { t: "li", list: 0, root: 0, ordered: false, level: 1, runs: [{ text: "x" }] },
      { t: "li", list: 0, root: 0, ordered: false, level: 2, runs: [{ text: "y" }] },
    ]);
    expect(compileContent(" - z")).toEqual([{ t: "li", list: 0, root: 0, ordered: false, level: 0, runs: [{ text: "z" }] }]);
  });

  it("keeps a later item indented two spaces at its list's level, as markdown reads a sibling", () => {
    expect(compileContent("1. a\n  2. b")).toEqual([
      { t: "li", list: 0, root: 0, ordered: true, level: 0, runs: [{ text: "a" }] },
      { t: "li", list: 0, root: 0, ordered: true, level: 0, runs: [{ text: "b" }] },
    ]);
  });

  it("keeps a table's header as its first row", () => {
    expect(compileContent("| A | B |\n|---|---|\n| 1 | **2** |")).toEqual([
      { t: "table", rows: [[[{ text: "A" }], [{ text: "B" }]], [[{ text: "1" }], [{ text: "2", b: true }]]] },
    ]);
  });

  it("keeps a code block's lines", () => {
    expect(compileContent("```\nx = 1\ny = 2\n```")).toEqual([{ t: "code", lines: ["x = 1", "y = 2"] }]);
  });

  it("places typed blocks between the markdown around them", () => {
    const picture = { type: "image", data: "AAAA", mime: "image/png", width: 10, height: 5 };
    expect(compileContent("## Risks\n<?block r?>\nAfter.", { r: picture })).toEqual([
      { t: "h", level: 2, runs: [{ text: "Risks" }] },
      { t: "image", src: "data:image/png;base64,AAAA", width: 10, height: 5 },
      { t: "p", runs: [{ text: "After." }] },
    ]);
  });

  it("refuses a placeholder no block answers, and a block of an unknown type", () => {
    expect(() => compileContent("<?block x?>")).toThrow(ContentError);
    expect(() => compileContent("<?block x?>", { x: { type: "video" } })).toThrow(ContentError);
  });

  it("turns what it does not map into plain text or nothing", () => {
    expect(compileContent("![a chart](https://x.test/c.png)\n\n<div>raw</div>\n\n> > quoted\n\n---")).toEqual([
      { t: "p", runs: [{ text: "a chart" }] },
      { t: "p", runs: [{ text: "quoted" }] },
    ]);
  });

  it("keeps the code blocks and tables inside a list item, after its line", () => {
    expect(compileContent("1. step\n\n   ```\n   cmd\n   ```\n\n   | A |\n   |---|\n   | 1 |\n2. next")).toEqual([
      { t: "li", list: 0, root: 0, ordered: true, level: 0, runs: [{ text: "step" }] },
      { t: "code", lines: ["cmd"] },
      { t: "table", rows: [[[{ text: "A" }]], [[{ text: "1" }]]] },
      { t: "li", list: 0, root: 0, ordered: true, level: 0, runs: [{ text: "next" }] },
    ]);
  });

  it("writes entities and tags the agent typed literally", () => {
    expect(compileContent('Use `&amp; <b>` and &amp; \\<b> "q" a < b')).toEqual([
      { t: "p", runs: [{ text: "Use " }, { text: "&amp; <b>", code: true }, { text: ' and &amp; <b> "q" a < b' }] },
    ]);
  });

  it("keeps only web and mail links", () => {
    expect(compileContent("[x](javascript:alert(1)) [y](https://x.test) [z](mailto:a@b.test) [w](data:text/html,hi)")).toEqual([
      {
        t: "p",
        runs: [{ text: "x " }, { text: "y", href: "https://x.test" }, { text: " " }, { text: "z", href: "mailto:a@b.test" }, { text: " w" }],
      },
    ]);
  });

  it("turns a soft line break into a space and keeps a hard one", () => {
    expect(compileContent("line one\nline two  \nthree\\\nfour")).toEqual([
      { t: "p", runs: [{ text: "line one line two\nthree\nfour" }] },
    ]);
  });

  it("refuses a placeholder that names an inherited property", () => {
    expect(() => compileContent("<?block constructor?>", {})).toThrow(/names no entry/);
  });

  it("refuses more than 400 blocks in one op", () => {
    expect(() => compileContent("p\n\n".repeat(401))).toThrow(ContentError);
  });
});

describe("pending blocks, written in the markdown", () => {
  it("writes a line <?pending key: intent?> as a pending block", () => {
    expect(compileContent("# Q3\n\n## Summary\n<?pending summary: Three sentences on the quarter?>")).toEqual([
      { t: "h", level: 1, runs: [{ text: "Q3" }] },
      { t: "h", level: 2, runs: [{ text: "Summary" }] },
      { t: "pending", key: "summary", intent: "Three sentences on the quarter" },
    ]);
  });

  it("places it between the markdown around it, its intent trimmed", () => {
    expect(compileContent("## Risks\n  <?pending risks:   What could slip  ?>\nAfter.")).toEqual([
      { t: "h", level: 2, runs: [{ text: "Risks" }] },
      { t: "pending", key: "risks", intent: "What could slip" },
      { t: "p", runs: [{ text: "After." }] },
    ]);
  });

  it("cuts an intent at 300 characters", () => {
    const [block] = compileContent(`<?pending long: ${"x".repeat(400)}?>`);
    expect(block).toEqual({ t: "pending", key: "long", intent: "x".repeat(300) });
  });

  it("refuses a key used twice in one op", () => {
    expect(() => compileContent("<?pending summary: One?>\n\n<?pending summary: Two?>")).toThrow(
      new ContentError("<?pending summary: …?> is used twice in one op."),
    );
  });

  it("refuses a pending block in blocks, naming the inline form", () => {
    expect(() => compileContent("## Risks\n<?block risks?>", { risks: { type: "pending", intent: "What could slip" } })).toThrow(
      new ContentError("Pending blocks are written in the markdown now: <?pending risks: what it will hold?>."),
    );
  });

  it("refuses a key of two words, naming it as written and the key it can be", () => {
    const line = "<?pending leave types: Table of annual, sick, parental and unpaid leave allowances?>";
    const fixed = "<?pending leave_types: Table of annual, sick, parental and unpaid leave allowances?>";
    expect(() => compileContent(`## Leave\n${line}`)).toThrow(
      new ContentError(`A pending key is one word of letters, digits, - or _, and "leave types" is not: write ${fixed} instead.`),
    );
    expect(compileContent(fixed)).toEqual([
      { t: "pending", key: "leave_types", intent: "Table of annual, sick, parental and unpaid leave allowances" },
    ]);
  });

  it("refuses a key with other characters, its words joined by _ and its accents dropped", () => {
    for (const [key, fixed] of [["Q3 résumé (draft)", "Q3_resume_draft"], ["leave - types", "leave_types"], ["sales.north", "sales_north"], ["概要", "name"]]) {
      expect(() => compileContent(`<?pending ${key}: Three sentences?>`), key).toThrow(
        new ContentError(`A pending key is one word of letters, digits, - or _, and ${JSON.stringify(key)} is not: write <?pending ${fixed}: Three sentences?> instead.`),
      );
    }
  });

  it("refuses a pending line with no colon, saying the form rather than guessing which word is the key", () => {
    for (const line of ["<?pending leave types?>", "<?pending summary Three sentences on the quarter?>", "<?pending summary?>"]) {
      expect(() => compileContent(line), line).toThrow(
        new ContentError(`A pending line is <?pending key: what the section will hold?>, its key one word of letters, digits, - or _; ${JSON.stringify(line)} has no colon after its key.`),
      );
    }
  });

  it("refuses a pending line that does not end in ?>", () => {
    for (const line of ["<?pending summary: Three sentences", "<?pending summary: Three sentences?", "<?pending summary: Three sentences>"]) {
      expect(() => compileContent(line), line).toThrow(new ContentError(`A pending line ends in ?>, and ${JSON.stringify(line)} does not.`));
    }
  });

  it("refuses a pending line with text beside it", () => {
    for (const line of ["## Summary <?pending summary: Three sentences?>", "<?pending summary: Three sentences?> and more", "<?pending summary: Three sentences?>."]) {
      expect(() => compileContent(line), line).toThrow(new ContentError("A pending block is a line of its own, with nothing before or after it: <?pending name: what it will hold?>"));
    }
  });

  it("refuses a pending line with no key", () => {
    expect(() => compileContent("<?pending: Three sentences?>")).toThrow(
      new ContentError('A pending line is <?pending key: what the section will hold?>, its key one word of letters, digits, - or _; "<?pending: Three sentences?>" has no key before its colon.'),
    );
  });

  it("refuses a pending line with nothing after the colon", () => {
    expect(() => compileContent("<?pending summary:   ?>")).toThrow(
      new ContentError('A pending line says after the colon what its section will hold, and "<?pending summary:   ?>" says nothing there.'),
    );
  });

  it("refuses a pending line with no space before its key", () => {
    expect(() => compileContent("<?pendingsummary: Three sentences?>")).toThrow(
      new ContentError('A pending line has a space between <?pending and its key, and "<?pendingsummary: Three sentences?>" has none: write <?pending summary: Three sentences?> instead.'),
    );
  });

  it("reads a \\r, U+2028 or U+2029 inside an intent as any other character, in the line and in its suggested fix", () => {
    for (const intent of ["One\rtwo", "One\u2028two", "One\u2029two"]) {
      expect(compileContent(`<?pending summary: ${intent}?>`), intent).toEqual([{ t: "pending", key: "summary", intent }]);
      const fixed = `<?pending leave_types: ${intent}?>`;
      expect(() => compileContent(`<?pending leave types: ${intent}?>`), intent).toThrow(
        new ContentError(`A pending key is one word of letters, digits, - or _, and "leave types" is not: write ${fixed} instead.`),
      );
      expect(compileContent(fixed), intent).toEqual([{ t: "pending", key: "leave_types", intent }]);
    }
  });

  it("refuses a pending line in other letters, which markdown would drop unsaid, giving it back in lowercase", () => {
    for (const line of ["<?PENDING summary: Three sentences?>", "<?Pending summary:Three sentences  ?>"]) {
      expect(() => compileContent(line), line).toThrow(
        new ContentError(`A pending line starts with <?pending in lowercase, exactly <?pending key: intent?>, and ${JSON.stringify(line)} does not: write <?pending summary: Three sentences?> instead.`),
      );
    }
  });

  it("refuses a pending line in other letters and wrong otherwise, saying both", () => {
    expect(() => compileContent("<?Pending leave types: Leave allowances?>")).toThrow(
      new ContentError(
        'A pending line starts with <?pending in lowercase, exactly <?pending key: intent?>, and "<?Pending leave types: Leave allowances?>" does not. '
        + 'A pending key is one word of letters, digits, - or _, and "leave types" is not: write <?pending leave_types: Leave allowances?> instead.',
      ),
    );
    expect(() => compileContent("<?PENDING summary?>")).toThrow(
      new ContentError(
        'A pending line starts with <?pending in lowercase, exactly <?pending key: intent?>, and "<?PENDING summary?>" does not. '
        + 'A pending line is <?pending key: what the section will hold?>, its key one word of letters, digits, - or _; "<?pending summary?>" has no colon after its key.',
      ),
    );
    expect(() => compileContent("<?PENDING summary: Three sentences?> and more")).toThrow(
      new ContentError(
        'A pending line starts with <?pending in lowercase, exactly <?pending key: intent?>, and "<?PENDING summary: Three sentences?> and more" does not. '
        + "A pending block is a line of its own, with nothing before or after it: <?pending name: what it will hold?>",
      ),
    );
  });

  it("refuses a line that is nearly a placeholder, which markdown would drop unsaid, naming both forms", () => {
    const chart = { type: "chart", rows: [["", "a"], ["x", 1]] };
    for (const line of ["<? pending k: x?>", "<?BLOCK k?>", "<?Block k?>", "  <? block k ?>", "<?block k?> and more"]) {
      expect(() => compileContent(`## Risks\n${line}`, { k: chart }), line).toThrow(
        new ContentError(`A placeholder is a line of its own, written exactly <?pending key: intent?> or <?block key?>: ${JSON.stringify(line.trim())} is neither.`),
      );
    }
  });

  it("writes <?php and <?xml lines as before", () => {
    expect(compileContent('<?php echo 1; ?>\n\n<?xml version="1.0"?>')).toEqual([]);
    expect(compileContent('```\n<?php echo 1; ?>\n<?xml version="1.0"?>\n```')).toEqual([{ t: "code", lines: ["<?php echo 1; ?>", '<?xml version="1.0"?>'] }]);
  });

  it("refuses a long run of spaces promptly", () => {
    const started = performance.now();
    expect(() => compileContent(`<?pending k:${" ".repeat(5_000)}x`)).toThrow(/ends in \?>/);
    expect(performance.now() - started).toBeLessThan(1_000);
  });

  it("says what is wrong with a long or crafted pending line promptly", () => {
    const run = 100_000;
    const lines: [string, RegExp][] = [
      [`<?pending ${"a ".repeat(run)}: x?>`, /pending key is one word/],
      [`<?pending ${"a-".repeat(run)} ${"_".repeat(run)}: x?>`, /pending key is one word/],
      [`<?pending ${"é".repeat(run)} x: y?>`, /pending key is one word/],
      [`<?pending ${"a ".repeat(run)}?>`, /has no colon after its key/],
      [`<?pending${" ".repeat(run)}?>`, /has no colon after its key/],
      [`<?PENDING ${"a ".repeat(run)}: x?>`, /in lowercase.*pending key is one word/],
      [`<?Pending k:${" ".repeat(run)}x${" ".repeat(run)}?>`, /in lowercase.*write <\?pending k: x\?> instead/],
      [`<?pending ${"?".repeat(run)}`, /ends in \?>/],
      [`<?pending k:${" ".repeat(run)}?>`, /says nothing there/],
      [`<?pending k: ${"?>".repeat(run)}x`, /line of its own/],
    ];
    for (const [line, message] of lines) {
      const started = performance.now();
      expect(() => compileContent(line)).toThrow(message);
      expect(performance.now() - started, line.slice(0, 20)).toBeLessThan(1_000);
    }
  });

  it("reads a long intent of any characters promptly", () => {
    const run = 100_000;
    for (const intent of ["x\r".repeat(run), "?>".repeat(run), "x\u2028".repeat(run)]) {
      const started = performance.now();
      expect(compileContent(`<?pending k: ${intent}?>`)).toEqual([{ t: "pending", key: "k", intent: intent.slice(0, 300) }]);
      expect(performance.now() - started).toBeLessThan(1_000);
    }
  });
});

describe("typed pictures and charts", () => {
  it("compiles a picture the worker read", () => {
    expect(compileContent("<?block p?>", { p: { type: "image", data: "AAAA", mime: "image/png", width: 10, height: 5 } })).toEqual([
      { t: "image", src: "data:image/png;base64,AAAA", width: 10, height: 5 },
    ]);
    expect(() => compileContent("<?block p?>", { p: { type: "image", path: "a.png" } })).toThrow(/PNG or JPEG/);
  });

  it("compiles a chart's rows into series", () => {
    expect(compileContent("<?block c?>", {
      c: { type: "chart", chart: "line", title: "Growth", rows: [["", "2025", "2026"], ["North", 10, 12], ["East", 8, "9"]] },
    })).toEqual([{
      t: "chart", chart: "line", title: "Growth", categories: ["North", "East"],
      series: [{ name: "2025", values: [10, 8] }, { name: "2026", values: [12, 9] }],
    }]);
  });

  it("refuses a chart it cannot draw", () => {
    expect(() => compileContent("<?block c?>", { c: { type: "chart", chart: "radar", rows: [["", "a"], ["x", 1]] } })).toThrow(/one of/);
    expect(() => compileContent("<?block c?>", { c: { type: "chart", rows: [["", "a"]] } })).toThrow(/rows/);
    expect(() => compileContent("<?block c?>", { c: { type: "chart", rows: [["", "a"], ["x", "lots"]] } })).toThrow(/number/);
  });
});
