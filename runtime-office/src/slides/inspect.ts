import type { Canvas, Color, Fill, Para, Run, ShapeSpec, SlideSpec, Stroke } from "./measure.js";

export interface InspectArgs { phase: "check" | "shapes"; canvas: Canvas; minPx: number; icons: string[] }
export type Inspected =
  | { error: string }
  | { stacks: string[]; faces: { family: string; bold: boolean; italic: boolean }[] }
  | { spec: SlideSpec; icons: { name: string; color: string; px: number }[]; embeds: { html: string; w: number; h: number }[] };

// Runs in the measuring page. "check" holds the slide to Claude Slides' format and says which font stacks and faces
// its text uses; "shapes" measures it into a SlideSpec. An svg, an icon or an embed becomes a placeholder picture
// ("svg:n", "icon:n", "embed:n", in order of appearance) that Node draws afterwards.
export function inspect(args: InspectArgs): Inspected {
  const TAGS = new Set(["SECTION", "H1", "H2", "H3", "P", "UL", "OL", "LI", "BR", "B", "I", "U", "A", "SPAN", "DIV", "IMG", "TABLE", "THEAD", "TBODY", "TR", "TH", "TD", "SVG", "HR", "X-SHAPE", "X-ICON", "X-CONNECTOR", "X-EMBED", "ASIDE"]);
  const TEXT = new Set(["H1", "H2", "H3", "P"]);
  const INLINE = new Set(["B", "I", "U", "A", "SPAN", "BR"]);
  const ATTRIBUTES: Record<string, string[]> = {
    SECTION: ["data-section", "data-transition", "hidden"], DIV: ["data-text", "data-fit"], IMG: ["src", "alt", "data-crop"], A: ["href"],
    "X-SHAPE": ["kind"], "X-ICON": ["name"], "X-CONNECTOR": ["x1", "y1", "x2", "y2", "head", "head-start", "head-end", "route", "from"],
  };
  const SHAPES = ["rect", "rounded", "ellipse", "diamond", "arrow-right", "arrow-left", "arrow-up", "arrow-down", "line"];
  // Claude Slides' style properties, as the longhands Chromium expands them into.
  const STYLE = /^(position|left|top|right|bottom|width|height|min-width|min-height|max-width|max-height|display|flex-direction|flex-wrap|row-gap|column-gap|align-items|justify-content|justify-items|align-self|justify-self|flex-grow|flex-shrink|flex-basis|grid-template-columns|grid-template-rows|grid-template-areas|grid-column-start|grid-column-end|grid-row-start|grid-row-end|aspect-ratio|padding-(top|right|bottom|left)|overflow-x|overflow-y|font-family|font-size|font-weight|font-style|font-variant-.+|font-stretch|font-size-adjust|font-kerning|font-optical-sizing|font-feature-settings|font-variation-settings|line-height|letter-spacing|text-align|text-transform|white-space|white-space-collapse|text-wrap|text-wrap-mode|text-wrap-style|text-decoration-.+|-webkit-text-stroke-(width|color)|color|background-.+|-webkit-text-fill-color|border-(top|right|bottom|left)-(width|style|color)|border-(top-left|top-right|bottom-right|bottom-left)-radius|border-image-.+|box-shadow|text-shadow|opacity|transform|filter|backdrop-filter|mix-blend-mode|object-fit|margin-(top|right|bottom|left)|box-sizing)$/;
  const UNITS = /\d(?:\.\d+)?(?:rem|vw|vh|vmin|vmax|ch|ex|cm|mm|in|pc|q)\b/i;
  const { canvas } = args;
  const ptPerPx = canvas.emuPerPx / 12700;

  class Refused extends Error {}
  const fail = (message: string): never => {
    throw new Refused(message);
  };
  const tagOf = (el: Element): string => el.tagName.toUpperCase();
  const describe = (el: Element): string => {
    const parts: string[] = [];
    for (let at: Element | null = el; at && at !== document.body; at = at.parentElement) {
      const parent: Element | null = at.parentElement;
      const tag = at.tagName;
      const same = parent ? Array.from(parent.children).filter((child) => child.tagName === tag) : [];
      parts.unshift(at.tagName.toLowerCase() + (same.length > 1 ? `:nth-of-type(${same.indexOf(at) + 1})` : ""));
    }
    const words = (el.textContent ?? "").trim().split(/\s+/).slice(0, 6).join(" ");
    return `<${parts.join(" > ")}>${words ? ` ("${words}")` : ""}`;
  };
  const round = (value: number): number => Math.round(value * 100) / 100;
  const emu = (px: number): number => Math.round(px * canvas.emuPerPx);
  const twips = (px: number): number => Math.round(px * ptPerPx * 20);
  const color = (css: string): Color | null => {
    const m = /rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\)/.exec(css);
    if (!m) return null;
    const alpha = m[4] === undefined ? 1 : Number(m[4]);
    if (alpha === 0) return null;
    return { hex: `#${[m[1], m[2], m[3]].map((v) => Math.round(Number(v)).toString(16).padStart(2, "0")).join("")}`, alpha };
  };
  const family = (cs: CSSStyleDeclaration): string => cs.fontFamily.split(",")[0]!.trim().replace(/^["']|["']$/g, "");
  // Inside an svg or an embed is no slide content: the one is drawn whole, the other runs on a page of its own.
  const inside = (el: Element): boolean => !!el.parentElement?.closest("svg, x-embed");
  const pinned = (el: Element): boolean => getComputedStyle(el).position === "absolute";
  const isHost = (el: Element): boolean => tagOf(el) === "SECTION" || ["relative", "absolute"].includes(getComputedStyle(el).position);

  try {
    const root = document.body.firstElementChild as HTMLElement | null;
    const bodyText = Array.from(document.body.childNodes).some((n) => n.nodeType === Node.TEXT_NODE && (n.textContent ?? "").trim());
    if (!root || tagOf(root) !== "SECTION" || document.body.children.length !== 1 || bodyText) fail("A slide is one <section> and nothing beside it.");
    const section = root!;
    const elements = [section, ...Array.from(section.querySelectorAll("*"))].filter((el) => !inside(el));

    // --- the format: tags, attributes, styles and limits (Claude Slides' format.md)
    if (elements.length > 200) fail("A slide has at most 200 elements: split it.");
    for (const el of Array.from(section.querySelectorAll("svg *"))) {
      if (["foreignobject", "script"].includes(el.tagName.toLowerCase())) fail(`${describe(el)}: an svg holds no script or foreignObject.`);
    }
    let embeds = 0;
    for (const el of elements) {
      const tag = tagOf(el);
      if (!TAGS.has(tag)) fail(`${describe(el)}: a slide holds h1-h3, p, ul, ol, li, br, b, i, u, a, span, div, img, table, tr, th, td, svg, hr, x-shape, x-icon, x-connector, x-embed and one aside, not <${tag.toLowerCase()}>.`);
      if (tag !== "SVG") {
        for (const attribute of Array.from(el.attributes)) {
          const name = attribute.name;
          const own = ATTRIBUTES[tag] ?? [];
          // The format ignores class, and data-* it does not name.
          if (["style", "id", "class", "data-build-in", "data-build-out"].includes(name) || own.includes(name) || name.startsWith("data-")) continue;
          fail(`${describe(el)} has the attribute ${name}, which the slide format does not take there.`);
        }
      }
      if (tag === "ASIDE" && (el.parentElement !== section || el !== section.lastElementChild)) fail("The <aside> of speaker notes is the section's last child.");
      if (tag === "A" && !/^(https?:|mailto:)/i.test(el.getAttribute("href") ?? "")) fail(`${describe(el)}: a link goes to an http(s) or mailto address.`);
      if (tag === "IMG" && !/^data:image\/(png|jpeg|svg\+xml);base64,/i.test(el.getAttribute("src") ?? "")) fail(`${describe(el)}: a picture from the workspace (a PNG, JPEG or SVG).`);
      if (tag === "X-SHAPE" && !SHAPES.includes(el.getAttribute("kind") ?? "")) fail(`${describe(el)}: kind is one of ${SHAPES.join(", ")}.`);
      if (tag === "X-ICON" && !args.icons.includes(el.getAttribute("name") ?? "")) fail(`${describe(el)}: name is one of ${args.icons.join(", ")}.`);
      if (tag === "X-EMBED") {
        if (el.parentElement !== section || !pinned(el)) fail(`${describe(el)}: an x-embed is pinned (position:absolute) and a child of the section.`);
        if (el.innerHTML.length > 16_384) fail(`${describe(el)}: an x-embed is at most 16 KB.`);
        if (++embeds > 8) fail("A slide has at most 8 x-embeds.");
      }
      if (tag === "SVG" && el.outerHTML.length > 53_248) fail(`${describe(el)}: an svg is at most 52 KB.`);
      if (tag === "UL" || tag === "OL") {
        for (const li of Array.from(el.children)) {
          if (tagOf(li) !== "LI" || Array.from(li.children).some((c) => !INLINE.has(tagOf(c)))) fail(`${describe(li)}: a list holds plain <li> items, one level.`);
        }
      }
      if (tag === "TR" && Array.from((el as HTMLTableRowElement).cells).some((c) => c.colSpan > 1 || c.rowSpan > 1)) fail(`${describe(el)}: no colspan or rowspan.`);
      const style = (el as HTMLElement).style;
      for (let i = 0; i < (style?.length ?? 0); i++) {
        const property = style[i]!;
        const value = style.getPropertyValue(property);
        // A shorthand sets its other longhands to their initial value.
        if (value === "initial") continue;
        if (!STYLE.test(property)) fail(`${describe(el)} uses ${property}, which is not in the slide format.`);
        if (/^margin-/.test(property) && parseFloat(value) !== 0) fail(`${describe(el)}: no margin (use gap or padding).`);
        if (/var\(|currentcolor/i.test(value)) fail(`${describe(el)}: no var() or currentcolor.`);
        if (UNITS.test(value) || (property !== "letter-spacing" && /\d(?:\.\d+)?em\b/i.test(value))) fail(`${describe(el)} sets ${property} to ${value}: lengths are px (no em, rem, vw or vh).`);
        if (tag === "SPAN" && /^font-(size|family)$/.test(property)) fail(`${describe(el)}: no sizes or fonts on a span (use two blocks).`);
      }
      let depth = 0;
      for (let at = el.parentElement; at && at !== section; at = at.parentElement) if (tagOf(at) === "DIV") depth++;
      if (tag === "DIV" && depth >= 15) fail(`${describe(el)}: at most 15 nested divs.`);
    }
    if ((section.textContent ?? "").length > 20_000) fail("A slide holds at most 20,000 characters of text.");

    // --- the text's font stacks and faces, and its size
    const stacks = new Set<string>();
    const faces = new Map<string, { family: string; bold: boolean; italic: boolean }>();
    const walker = document.createTreeWalker(section, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const parent = node.parentElement!;
      if (!(node.textContent ?? "").trim() || inside(parent) || parent.closest("aside")) continue;
      const cs = getComputedStyle(parent);
      if (parseFloat(cs.fontSize) < args.minPx - 0.01) fail(`Text in ${describe(parent)} is ${round(parseFloat(cs.fontSize))} px; the least on a slide is ${args.minPx} px.`);
      stacks.add(cs.fontFamily);
      const face = { family: family(cs), bold: parseInt(cs.fontWeight, 10) >= 600, italic: cs.fontStyle === "italic" };
      faces.set(`${face.family}|${face.bold}|${face.italic}`, face);
    }
    if (args.phase === "check") return { stacks: [...stacks], faces: [...faces.values()] };

    // --- with the resolved fonts loaded: nothing past the slide's edge, nothing overflowing its box
    const origin = section.getBoundingClientRect();
    const shown = elements.filter((el) => tagOf(el) !== "ASIDE" && getComputedStyle(el).display !== "none");
    // The edges first: a box past them also makes the section overflow, which says less.
    for (const el of shown) {
      if (el === section) continue;
      const r = el.getBoundingClientRect();
      const past = Math.max(origin.left - r.left, r.right - origin.right, origin.top - r.top, r.bottom - origin.bottom);
      if (past > 0.5) fail(`${describe(el)} reaches ${Math.ceil(past)} px past the slide's edge: move it in, or split the slide.`);
    }
    // The editor lays a text box's lines by their pitch, but Chromium's scroll overflow also counts the glyphs'
    // content area reaching past a line height tighter than it: that much is no overflow. An inline box is as tall as
    // its font's content area whatever the line height, so a hidden span in the element's font measures it.
    const areas = new Map<string, number>();
    const overhang = (el: Element): number => {
      const cs = getComputedStyle(el);
      const lineHeight = parseFloat(cs.lineHeight);
      if (!Number.isFinite(lineHeight)) return 0;
      const key = `${cs.fontStyle}|${cs.fontWeight}|${cs.fontSize}|${cs.fontFamily}`;
      let area = areas.get(key);
      if (area === undefined) {
        const holder = document.createElement("div");
        holder.style.cssText = "position:absolute;left:0;top:0;display:block;visibility:hidden";
        const probe = holder.appendChild(document.createElement("span"));
        Object.assign(probe.style, { fontFamily: cs.fontFamily, fontSize: cs.fontSize, fontWeight: cs.fontWeight, fontStyle: cs.fontStyle });
        probe.textContent = "Hg";
        section.appendChild(holder);
        area = probe.getBoundingClientRect().height;
        holder.remove();
        areas.set(key, area);
      }
      return Math.max(0, area - lineHeight);
    };
    for (const el of shown) {
      const tag = tagOf(el);
      const cs = getComputedStyle(el);
      let over: number;
      if (tag === "DIV" && !el.hasAttribute("data-text")) {
        // A clipping div clips what spills; the text boxes inside it are checked themselves.
        if (cs.overflowX === "hidden") continue;
        // Its children's border boxes, laid by line height, stay in its content box; a pinned child it places, in its
        // padding box.
        const r = el.getBoundingClientRect();
        const padding = { left: r.left + parseFloat(cs.borderLeftWidth), right: r.right - parseFloat(cs.borderRightWidth), top: r.top + parseFloat(cs.borderTopWidth), bottom: r.bottom - parseFloat(cs.borderBottomWidth) };
        const content = { left: padding.left + parseFloat(cs.paddingLeft), right: padding.right - parseFloat(cs.paddingRight), top: padding.top + parseFloat(cs.paddingTop), bottom: padding.bottom - parseFloat(cs.paddingBottom) };
        over = 0;
        for (const child of Array.from(el.children)) {
          if (getComputedStyle(child).display === "none") continue;
          if (pinned(child) && !isHost(el)) continue;
          const inner = pinned(child) ? padding : content;
          const c = child.getBoundingClientRect();
          over = Math.max(over, inner.left - c.left, c.right - inner.right, inner.top - c.top, c.bottom - inner.bottom);
        }
      } else if (TEXT.has(tag) || ["UL", "OL", "LI", "TD", "TH", "DIV"].includes(tag)) {
        // An item or paragraph inside spills its own glyphs into this box's scroll overflow too.
        const allowance = Math.max(...[el, ...Array.from(el.querySelectorAll("*"))].map(overhang));
        over = Math.max(el.scrollWidth - el.clientWidth, el.scrollHeight - el.clientHeight - allowance);
      } else continue;
      if (over > 1) fail(`The content of ${describe(el)} overflows its box by ${Math.ceil(over)} px: give it room, or split the slide; nothing shrinks.`);
    }

    // --- the shapes
    let svgs = 0;
    const icons: { name: string; color: string; px: number }[] = [];
    const embedded: { html: string; w: number; h: number }[] = [];
    const box = (el: Element) => {
      const r = el.getBoundingClientRect();
      return { x: r.left - origin.left, y: r.top - origin.top, w: r.width, h: r.height };
    };
    const place = (el: Element) => {
      const b = box(el);
      return { x: emu(b.x), y: emu(b.y), w: emu(b.w), h: emu(b.h), rot: 0 };
    };
    const solid = (css: string): Fill | null => {
      const c = color(css);
      return c ? { type: "solid", color: c } : null;
    };
    const stroke = (cs: CSSStyleDeclaration): Stroke | null => {
      const widths = [cs.borderTopWidth, cs.borderRightWidth, cs.borderBottomWidth, cs.borderLeftWidth].map(parseFloat);
      if (cs.borderTopStyle === "none" || !(widths[0]! > 0) || widths.some((w) => w !== widths[0])) return null;
      const c = color(cs.borderTopColor);
      return c ? { w: emu(widths[0]!), color: c, dash: "solid" } : null;
    };
    const runs = (container: Element): Run[] => {
      const out: Run[] = [];
      const add = (text: string, from: Element) => {
        const cs = getComputedStyle(from);
        let underline = false;
        for (let at: Element | null = from; at && at !== container.parentElement; at = at.parentElement) {
          if (getComputedStyle(at).textDecorationLine.includes("underline")) underline = true;
        }
        const run: Run = { text, size: round(parseFloat(cs.fontSize) * ptPerPx), font: family(cs), color: color(cs.color) ?? { hex: "#000000", alpha: 1 } };
        if (parseInt(cs.fontWeight, 10) >= 600) run.b = true;
        if (cs.fontStyle === "italic") run.i = true;
        if (underline) run.u = true;
        const link = from.closest("a");
        if (link && container.contains(link)) run.href = link.getAttribute("href")!;
        out.push(run);
      };
      const walk = (node: Node) => {
        for (const child of Array.from(node.childNodes)) {
          if (child.nodeType === Node.TEXT_NODE) {
            const text = (child.textContent ?? "").replace(/[ \t\n\r\f]+/g, " ");
            if (text) add(text, child.parentElement!);
          } else if (child.nodeType === Node.ELEMENT_NODE) {
            if (tagOf(child as Element) === "BR") add("\n", child as Element);
            else walk(child);
          }
        }
      };
      walk(container);
      // As layout collapses them: no space at a paragraph's start or end, nor beside a line break.
      for (let i = 0; i < out.length; i++) {
        if (i === 0 || out[i - 1]!.text.endsWith("\n")) out[i]!.text = out[i]!.text.replace(/^ /, "");
        if (i === out.length - 1 || out[i + 1]!.text.startsWith("\n")) out[i]!.text = out[i]!.text.replace(/ $/, "");
      }
      const merged: Run[] = [];
      for (const run of out.filter((r) => r.text)) {
        const last = merged[merged.length - 1];
        if (last && JSON.stringify({ ...last, text: "" }) === JSON.stringify({ ...run, text: "" })) last.text += run.text;
        else merged.push(run);
      }
      return merged;
    };
    const paragraph = (el: Element): Para => {
      const cs = getComputedStyle(el);
      const size = parseFloat(cs.fontSize);
      const lineHeight = parseFloat(cs.lineHeight);
      const align = ({ left: "left", start: "left", center: "center", right: "right", end: "right", justify: "justify" } as Record<string, string>)[cs.textAlign] ?? "left";
      // The editor's single spacing is 1.2 times the font size. No margins in this format: no spacing before or after.
      return { align, line: Number.isFinite(lineHeight) ? Math.round((lineHeight / (1.2 * size)) * 1000) / 1000 : 1, before: 0, after: 0, runs: runs(el) };
    };
    const listParas = (list: Element, left: number, indent?: number): Para[] => {
      const cs = getComputedStyle(list);
      const ordered = tagOf(list) === "OL";
      return Array.from(list.children).map((li) => {
        const para = paragraph(li);
        // The item's text starts where its box does; its marker hangs in the list's left padding.
        para.indent = twips(indent ?? box(li).x - left);
        para.hanging = twips(indent ?? parseFloat(cs.paddingLeft));
        if (ordered) {
          para.numbered = true;
          para.start = 1;
        } else {
          para.bullet = ({ none: "", square: "▪", circle: "◦" } as Record<string, string>)[cs.listStyleType] ?? "•";
        }
        return para;
      });
    };
    // A text element is one shape: its box, its fill and border, and its padding as the text's insets.
    const textShape = (el: Element): ShapeSpec => {
      const cs = getComputedStyle(el);
      const b = box(el);
      const line = stroke(cs);
      const side = (padding: string, border: string) => emu(parseFloat(padding) + (line ? 0 : parseFloat(border) || 0));
      const tag = tagOf(el);
      let paras: Para[];
      if (tag === "UL" || tag === "OL") paras = listParas(el, b.x);
      else if (tag === "DIV") {
        const indent = Number(el.getAttribute("data-text")) || undefined;
        paras = Array.from(el.children).flatMap((child) => (["UL", "OL"].includes(tagOf(child)) ? listParas(child, b.x, indent) : [paragraph(child)]));
      } else paras = [paragraph(el)];
      return {
        kind: "shape", ...place(el), geometry: { preset: "rect" }, fill: solid(cs.backgroundColor), stroke: line,
        text: {
          insets: [side(cs.paddingLeft, cs.borderLeftWidth), side(cs.paddingTop, cs.borderTopWidth), side(cs.paddingRight, cs.borderRightWidth), side(cs.paddingBottom, cs.borderBottomWidth)],
          anchor: "t", wrap: cs.whiteSpace !== "nowrap", paras,
        },
      };
    };
    const picture = (el: HTMLImageElement): ShapeSpec => {
      const b = box(el);
      const fit = getComputedStyle(el).objectFit;
      const nw = el.naturalWidth || b.w, nh = el.naturalHeight || b.h;
      if (fit === "contain") {
        const scale = Math.min(b.w / nw, b.h / nh), w = nw * scale, h = nh * scale;
        return { kind: "picture", x: emu(b.x + (b.w - w) / 2), y: emu(b.y + (b.h - h) / 2), w: emu(w), h: emu(h), rot: 0, src: el.src };
      }
      if (fit === "cover") {
        const scale = Math.max(b.w / nw, b.h / nh);
        const cx = (100 * (1 - b.w / (nw * scale))) / 2, cy = (100 * (1 - b.h / (nh * scale))) / 2;
        return { kind: "picture", ...place(el), src: el.src, crop: [round(cx), round(cy), round(cx), round(cy)] };
      }
      return { kind: "picture", ...place(el), src: el.src };
    };
    const table = (el: HTMLTableElement): ShapeSpec => {
      const rows = Array.from(el.rows);
      if (!rows.length) fail(`${describe(el)} has no rows.`);
      const first = Array.from(rows[0]!.cells);
      const pad = getComputedStyle(first[0]!);
      return {
        kind: "table", ...place(el), cols: first.map((c) => emu(box(c).w)),
        pad: [pad.paddingTop, pad.paddingRight, pad.paddingBottom, pad.paddingLeft].map((v) => twips(parseFloat(v))),
        rows: rows.map((row) => {
          if (row.cells.length !== first.length) fail(`${describe(row)}: every row has as many cells as the first.`);
          return {
            h: emu(box(row).h), fill: color(getComputedStyle(row).backgroundColor),
            cells: Array.from(row.cells).map((c) => ({ paras: [paragraph(c)], fill: color(getComputedStyle(c).backgroundColor), border: stroke(getComputedStyle(c)) })),
          };
        }),
      };
    };
    // What one element draws by itself; a container's children are laid out by layer().
    const own = (el: Element): ShapeSpec[] => {
      const tag = tagOf(el);
      const cs = getComputedStyle(el);
      if (tag === "SVG") return [{ kind: "picture", ...place(el), src: `svg:${svgs++}` }];
      if (tag === "X-ICON") {
        icons.push({ name: el.getAttribute("name")!, color: color(cs.color)?.hex ?? "#000000", px: box(el).w });
        return [{ kind: "picture", ...place(el), src: `icon:${icons.length - 1}` }];
      }
      if (tag === "X-EMBED") {
        const b = box(el);
        embedded.push({ html: el.innerHTML, w: b.w, h: b.h });
        return [{ kind: "picture", ...place(el), src: `embed:${embedded.length - 1}` }];
      }
      if (tag === "IMG") return [picture(el as HTMLImageElement)];
      if (tag === "TABLE") return [table(el as HTMLTableElement)];
      if (TEXT.has(tag) || tag === "UL" || tag === "OL" || (tag === "DIV" && el.hasAttribute("data-text"))) return [textShape(el)];
      if (tag === "DIV") {
        const fill = solid(cs.backgroundColor), line = stroke(cs);
        return fill || line ? [{ kind: "shape", ...place(el), geometry: { preset: "rect" }, fill, stroke: line }] : [];
      }
      // hr, x-shape and x-connector are not drawn here.
      return [];
    };
    const isContainer = (el: Element): boolean => (tagOf(el) === "DIV" && !el.hasAttribute("data-text")) || tagOf(el) === "SECTION";
    // Claude Slides' paint order: in a host, its flow content is one layer where its first flow child sits. In the
    // section, pinned children listed before that layer paint below it and the rest above; in any other host (a
    // position:relative or pinned div) pinned children always paint over its flow.
    const layer = (host: Element): ShapeSpec[] => {
      const below: ShapeSpec[] = [], flow: ShapeSpec[] = [], above: ShapeSpec[] = [];
      let seen = false;
      const walk = (parent: Element) => {
        for (const child of Array.from(parent.children)) {
          if (tagOf(child) === "ASIDE" || getComputedStyle(child).display === "none") continue;
          if (pinned(child)) {
            const shapes = [...own(child), ...(isContainer(child) ? layer(child) : [])];
            (seen || host !== section ? above : below).push(...shapes);
            continue;
          }
          seen = true;
          flow.push(...own(child));
          if (isContainer(child)) flow.push(...(isHost(child) ? layer(child) : (walk(child), [])));
        }
      };
      walk(host);
      return [...below, ...flow, ...above];
    };
    const spec: SlideSpec = {
      background: solid(getComputedStyle(section).backgroundColor), shapes: layer(section),
      notes: (section.querySelector(":scope > aside")?.textContent ?? "").trim(), hidden: section.hasAttribute("hidden"), transition: null,
    };
    return { spec, icons, embeds: embedded };
  } catch (error) {
    if (error instanceof Refused) return { error: error.message };
    throw error;
  }
}
